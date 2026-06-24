"""
feedback.py - 反馈诊断系统
============================
职责：
  1. LLM 诊断分类（输出标签，不输出数字）
  2. Python 翻译表（标签 → 确定性参数调整）
  3. 关键词兜底（LLM 失败时中文匹配）

核心思想：
  LLM 负责"看懂问题" → 分类任务，7B 完全胜任
  Python 负责"执行操作" → 确定性规则，不依赖 LLM

主题解耦：
  DIAGNOSIS_PROMPT 和 TAG_TO_PARAMS 是通用默认值。
  主题包 themes/xxx.yaml 可以通过 feedback 字段覆盖：
    feedback.subject_description → 注入诊断提示词的主语描述
    feedback.tag_overrides       → 覆盖/补充任意 tag 的参数规则
  调用方将 ThemeConfig 实例传入 diagnose()/translate()/parse_feedback()
  即可自动应用主题覆盖，无需修改本文件。

单独测试：
  python feedback.py "图片太模糊，而且有水印"
  python feedback.py "背景不对，应该是太空"
  python feedback.py "角色特征不明显，细节也不够"
"""

import json
import random
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from config import OLLAMA_URL, TEXT_MODEL


# ════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════
def clean_json(text: str) -> str:
    text = text.encode("utf-8", errors="ignore").decode("utf-8")
    if "```" in text:
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)
    text = text.strip()

    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return text[start:]


# ════════════════════════════════════════
# 第一步：LLM 诊断提示词（通用模板）
# subject_description 由主题包注入，默认"AI生成图片"
# ════════════════════════════════════════
_DIAGNOSIS_TEMPLATE = """你是AI图像质量诊断师。分析用户对{subject}的反馈。
从以下标签中选出所有适用的，只返回JSON，不要包含```json```或任何其他文字：
{{
  "tags": [],
  "severity": "mild"
}}

可用标签：
  too_blurry      -> 模糊、不清晰、不锐利
  weak_character  -> 角色不像、LoRA效果弱、特征不明显
  bad_composition -> 构图不好、画面杂乱、需要换一张
  has_watermark   -> 有水印、文字、logo、UI元素
  wrong_bg        -> 背景不对
  too_dark        -> 太暗、灰、曝光不足
  over_saturated  -> 色彩过饱和、太艳
  has_unwanted    -> 出现了不该有的内容（人物/驾驶员/多余元素）
  lack_detail     -> 细节不够、纹理不精细
  bad_lighting    -> 光效不好
  too_bright      -> 过曝、太亮

severity 三级：mild / moderate / severe

只返回JSON，例如：{{"tags": ["too_blurry", "has_watermark"], "severity": "moderate"}}"""

# 默认 subject（无主题时使用）
_DEFAULT_SUBJECT = "AI生成图片"


# ════════════════════════════════════════
# 第二步：确定性翻译表（通用默认值）
# 主题包通过 feedback.tag_overrides 覆盖 add_pos / add_neg
# 数值型参数（steps_delta / cfg_delta 等）不允许主题覆盖，
# 保持确定性，只有提示词词汇随主题变化
# ════════════════════════════════════════
_BASE_TAG_TO_PARAMS: dict = {
    "too_blurry": {
        "steps_delta":   +8,
        "denoise_delta": +0.15,
        "add_pos": "sharp focus, highly detailed, ultra sharp",
    },
    "weak_character": {
        "lora_delta": +0.1,
        "cfg_delta":  +1.0,
        # add_pos: 主题包覆盖，默认为空（通用词不如不加）
        "add_pos": "",
    },
    "bad_composition": {
        "change_seed": True,
    },
    "has_watermark": {
        "add_neg": "watermark, text, logo, ui, hud, letters, numbers, signature",
    },
    "wrong_bg": {
        # add_pos / add_neg: 主题包覆盖（"太空"/"水墨山水"完全不同）
        "add_pos": "",
        "add_neg": "",
    },
    "too_dark": {
        "cfg_delta": +0.8,
        "add_pos": "vivid colors, bright lighting, high contrast",
    },
    "over_saturated": {
        "cfg_delta": -0.5,
    },
    "has_unwanted": {
        # add_neg: 主题包覆盖（高达排"pilot"，鬼故事排"modern clothes"）
        "add_neg": "",
    },
    "lack_detail": {
        "steps_delta":   +5,
        "denoise_delta": +0.10,
        "add_pos": "intricate details, fine texture, highly detailed",
    },
    "bad_lighting": {
        "add_pos": "dramatic lighting, cinematic lighting, rim light",
    },
    "too_bright": {
        "cfg_delta": -0.5,
        "add_pos": "balanced exposure",
    },
}

# 严重度乘数
SEVERITY_MULTIPLIER = {"mild": 1.0, "moderate": 1.5, "severe": 2.0}

# 参数上下限
LIMITS = {
    "cfg":             (4.0,  10.0),
    "steps":           (20,   60),
    "lora_strength":   (0.5,  1.5),
    "upscale_denoise": (0.30, 0.85),
}


# ════════════════════════════════════════
# 关键词兜底映射（通用中文，不含主题词汇）
# ════════════════════════════════════════
KEYWORD_TO_TAGS = {
    "模糊": "too_blurry",     "不清晰": "too_blurry",
    "不清楚": "too_blurry",   "清晰度": "too_blurry",
    "不像": "weak_character",  "角色": "weak_character",
    "LoRA": "weak_character",  "lora": "weak_character",
    "构图": "bad_composition", "换一张": "bad_composition",
    "换个": "bad_composition", "重新": "bad_composition",
    "水印": "has_watermark",   "文字": "has_watermark",
    "logo": "has_watermark",
    "背景": "wrong_bg",
    "暗": "too_dark",          "太黑": "too_dark",
    "灰": "too_dark",          "曝光不足": "too_dark",
    "饱和": "over_saturated",  "太艳": "over_saturated",
    "多余": "has_unwanted",    "不该有": "has_unwanted",
    "细节": "lack_detail",     "精细": "lack_detail",
    "光效": "bad_lighting",    "光影": "bad_lighting",
    "过曝": "too_bright",      "太亮": "too_bright",
    "刺眼": "too_bright",
}


# ════════════════════════════════════════
# 主题覆盖合并
# ════════════════════════════════════════
def _merge_theme(theme=None) -> tuple[str, dict]:
    """
    从 ThemeConfig 读取 feedback 配置，与通用默认值合并。
    返回 (subject_description, merged_tag_to_params)

    ThemeConfig 里预期结构（theme.py 的 _load_feedback() 读取）：
      feedback:
        subject_description: "机甲/机器人图片"
        tag_overrides:
          weak_character:
            add_pos: "white mecha, blue accents, v-fin"
          wrong_bg:
            add_pos: "deep space, starfield"
            add_neg: "indoor, ground, building"
          has_unwanted:
            add_neg: "human, pilot, person, driver"
    """
    import copy
    merged = copy.deepcopy(_BASE_TAG_TO_PARAMS)
    subject = _DEFAULT_SUBJECT

    if theme is None:
        return subject, merged

    # 读取 theme.feedback（FeedbackConfig dataclass 或 dict）
    fb = getattr(theme, "feedback", None)
    if fb is None:
        return subject, merged

    # subject_description
    sd = getattr(fb, "subject_description", None) or (
        fb.get("subject_description") if isinstance(fb, dict) else None)
    if sd:
        subject = sd

    # tag_overrides：只允许覆盖字符串型的 add_pos / add_neg
    overrides = getattr(fb, "tag_overrides", None) or (
        fb.get("tag_overrides") if isinstance(fb, dict) else {})
    for tag, patch in (overrides or {}).items():
        if tag not in merged:
            merged[tag] = {}
        patch_dict = patch if isinstance(patch, dict) else vars(patch)
        for key in ("add_pos", "add_neg"):
            if key in patch_dict and patch_dict[key]:
                merged[tag][key] = patch_dict[key]

    return subject, merged


# ════════════════════════════════════════
# 核心函数
# ════════════════════════════════════════
def diagnose(feedback: str, theme=None) -> tuple:
    """
    诊断反馈文本，返回 (tags_list, severity_str)
    先尝试 LLM，失败时降级关键词匹配。
    theme: ThemeConfig 实例，用于注入 subject_description
    """
    subject, tag_params = _merge_theme(theme)
    prompt = _DIAGNOSIS_TEMPLATE.format(subject=subject)

    try:
        llm = ChatOpenAI(base_url=OLLAMA_URL, api_key="ollama", model=TEXT_MODEL)
        response = llm.invoke([
            SystemMessage(content=prompt),
            HumanMessage(content=f"用户反馈：{feedback}\n请诊断，只返回JSON。"),
        ])
        print(f"  [诊断] LLM响应: {response.content[:120]}")
        t    = clean_json(response.content)
        data = json.loads(t[t.find("{"):t.rfind("}")+1])
        tags     = data.get("tags", [])
        severity = data.get("severity", "moderate")
        valid_tags = [tg for tg in tags if tg in tag_params]
        if valid_tags:
            print(f"  [诊断] 标签: {valid_tags}  严重度: {severity}")
            return valid_tags, severity
        print(f"  [诊断] LLM标签无效，降级关键词匹配")
    except Exception as e:
        print(f"  [诊断] LLM失败: {e}，降级关键词匹配")

    return _keyword_fallback(feedback)


def _keyword_fallback(feedback: str) -> tuple:
    """中文关键词匹配兜底"""
    tags = []
    for keyword, tag in KEYWORD_TO_TAGS.items():
        if keyword in feedback and tag not in tags:
            tags.append(tag)

    severity = "moderate"
    if any(w in feedback for w in ["大幅", "非常", "严重", "大大"]):
        severity = "severe"
    elif any(w in feedback for w in ["稍微", "略微", "一点"]):
        severity = "mild"

    if not tags:
        tags = ["bad_composition"]

    print(f"  [诊断] 关键词: {tags}  严重度: {severity}")
    return tags, severity


def translate(tags: list, severity: str, current_params: dict,
              theme=None) -> dict:
    """
    标签 → 参数调整（确定性计算，不依赖 LLM）
    theme: ThemeConfig 实例，用于合并主题词汇覆盖
    返回 new_params
    """
    _, tag_params = _merge_theme(theme)

    mult = SEVERITY_MULTIPLIER.get(severity, 1.0)
    new  = current_params.copy()

    all_add_pos = []
    all_add_neg = []
    change_seed = False

    for tag in tags:
        rule = tag_params.get(tag, {})

        if "steps_delta" in rule:
            new["steps"] += int(rule["steps_delta"] * mult)
        if "cfg_delta" in rule:
            new["cfg"] += rule["cfg_delta"] * mult
        if "denoise_delta" in rule:
            new["upscale_denoise"] = (
                new.get("upscale_denoise", 0.40) + rule["denoise_delta"] * mult)
        if "lora_delta" in rule:
            new["lora_strength"] = (
                new.get("lora_strength", 1.0) + rule["lora_delta"] * mult)
        if rule.get("change_seed"):
            change_seed = True
        # 只追加非空字符串
        if rule.get("add_pos"):
            all_add_pos.append(rule["add_pos"])
        if rule.get("add_neg"):
            all_add_neg.append(rule["add_neg"])

    # 限幅
    new["cfg"] = round(
        max(LIMITS["cfg"][0], min(LIMITS["cfg"][1], new["cfg"])), 1)
    new["steps"] = max(
        LIMITS["steps"][0], min(LIMITS["steps"][1], new["steps"]))
    new["lora_strength"] = round(
        max(LIMITS["lora_strength"][0],
            min(LIMITS["lora_strength"][1],
                new.get("lora_strength", 1.0))), 2)
    new["upscale_denoise"] = round(
        max(LIMITS["upscale_denoise"][0],
            min(LIMITS["upscale_denoise"][1],
                new.get("upscale_denoise", 0.40))), 2)

    if all_add_pos:
        new["positive"] = new.get("positive", "") + ", " + ", ".join(all_add_pos)
    if all_add_neg:
        new["negative"] = new.get("negative", "") + ", " + ", ".join(all_add_neg)
    if change_seed:
        new["seed"] = random.randint(10000, 99999)

    return new


def parse_feedback(feedback: str, current_params: dict,
                   theme=None) -> dict:
    """
    对外统一入口：诊断 → 翻译 → 报告
    theme: ThemeConfig 实例（可选），传入后自动应用主题覆盖
    """
    tags, severity = diagnose(feedback, theme=theme)
    new_params = translate(tags, severity, current_params, theme=theme)

    print(f"\n  -- 反馈诊断报告 --")
    print(f"  标签: {tags}")
    print(f"  严重度: {severity} (x{SEVERITY_MULTIPLIER.get(severity, 1.0)})")
    print(f"  CFG:     {current_params.get('cfg', '?')} -> {new_params['cfg']}")
    print(f"  Steps:   {current_params.get('steps', '?')} -> {new_params['steps']}")
    print(f"  LoRA:    {current_params.get('lora_strength', '?')} -> {new_params['lora_strength']}")
    print(f"  Denoise: {current_params.get('upscale_denoise', 0.40)} -> {new_params['upscale_denoise']}")
    if new_params.get("seed") != current_params.get("seed"):
        print(f"  新Seed:  {new_params.get('seed')}")
    print(f"  ----------------------")
    return new_params


# ════════════════════════════════════════
# 独立测试（不加载 ComfyUI，可在 Mac 本地跑）
# ════════════════════════════════════════
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("feedback.py - 反馈诊断系统独立测试\n")
        print("用法: python feedback.py <反馈文本>")
        print()
        print("示例:")
        print('  python feedback.py "图片太模糊，而且有水印"')
        print('  python feedback.py "背景不对，应该是太空"')
        print('  python feedback.py "角色特征不明显，细节也不够"')
        print('  python feedback.py "构图不好，换一张"')
        print('  python feedback.py "slightly too dark"')
        sys.exit(0)

    test_feedback = " ".join(sys.argv[1:])
    print(f"输入: '{test_feedback}'\n")

    mock_params = {
        "cfg": 6.5,
        "steps": 30,
        "seed": 11001,
        "lora_strength": 1.0,
        "upscale_denoise": 0.40,
        "positive": "score_9, masterpiece, best quality",
        "negative": "score_1, worst quality",
    }
    print(f"当前参数: CFG={mock_params['cfg']} Steps={mock_params['steps']} "
          f"LoRA={mock_params['lora_strength']}")

    print(f"\n--- 完整流程（无主题覆盖）---")
    new_params = parse_feedback(test_feedback, mock_params)
    print(f"\n最终参数: CFG={new_params['cfg']} Steps={new_params['steps']} "
          f"LoRA={new_params['lora_strength']} Denoise={new_params['upscale_denoise']}")

    print(f"\n--- 关键词兜底（不依赖LLM）---")
    tags_kw, sev_kw = _keyword_fallback(test_feedback)
    print(f"结果: tags={tags_kw} severity={sev_kw}")
