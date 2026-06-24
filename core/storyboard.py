"""
storyboard.py ── 全片分镜规划（Phase 0.5）v3
=============================================
v3 改动（基于 v2）：

改动 A：visual_must_haves 从字符串列表升级为结构体列表
  旧：["red shoes", "mud"]
  新：[
        {
          "element":    "red embroidered wedding shoes",
          "attributes": "women's ancient chinese style, small size, phoenix embroidery",
          "state":      "mud on toe tip, placed inside wooden cabinet",
          "mood":       "eerie, sorrowful, horror reveal",
          "exclude":    "men's shoes, boots, modern footwear, clean shoes"
        }
      ]
  - attributes + state 注入 positive prompt（精确描述物体应该是什么样）
  - exclude 注入 negative prompt（排除语义错误的视觉）
  - mood 注入 positive prompt 末尾（叙事情绪锚点）

改动 B：_call_llm_storyboard 的 prompt 头部加"故事背景摘要"
  让分镜 LLM 带着全局叙事理解去规划每页视觉
  避免跨页叙事关联断裂（如 p4 的鞋是 p2 鬼女的嫁鞋这一关联）

输出结构（每页）：
  {
    "shot_type":         str,
    "focal_subject":     str,
    "composition_hint":  str,
    "transition_from":   str,
    "shot_prefix":       str,
    "visual_must_haves": list[dict],   # v3：结构体列表
  }

quality.py 的叙事检查兼容：
  narration_keywords 由 pipeline 从 visual_must_haves 提取 element 字段
  （见 pipeline_patch_v2.py 的 _make_context 改动）
"""

import json
from typing import Optional

SHOT_PREFIX_MAP = {
    "extreme_wide":    "Extreme wide shot,",
    "wide":            "Wide shot,",
    "medium":          "Medium shot,",
    "closeup":         "Close-up shot,",
    "extreme_closeup": "Extreme close-up,",
}

VALID_SHOT_TYPES = set(SHOT_PREFIX_MAP.keys())

_FALLBACK_CYCLE = [
    "extreme_wide", "medium", "closeup", "medium", "closeup", "wide"
]


# ════════════════════════════════════════════════════════════════
# 兜底分镜
# ════════════════════════════════════════════════════════════════

def _default_board(pages: list) -> dict:
    board = {}
    for i, p in enumerate(pages):
        pn   = p["page"]
        shot = _FALLBACK_CYCLE[i % len(_FALLBACK_CYCLE)]
        board[pn] = {
            "shot_type":         shot,
            "focal_subject":     "",
            "composition_hint":  "",
            "transition_from":   "",
            "shot_prefix":       SHOT_PREFIX_MAP[shot],
            "visual_must_haves": [],
            "bgm_mood":          "tension",   # v4 新增（默认 tension）
        }
    return board


# ════════════════════════════════════════════════════════════════
# 校验与填充
# ════════════════════════════════════════════════════════════════

def _validate_must_have_entry(entry) -> dict:
    """
    校验单个 must_have 条目，确保是合法结构体。
    兼容旧格式（字符串）和新格式（dict）。
    """
    if isinstance(entry, str):
        # 兼容旧格式：字符串升级为最小结构体
        return {
            "element":    entry.strip(),
            "attributes": "",
            "state":      "",
            "mood":       "",
            "exclude":    "",
        }
    if isinstance(entry, dict):
        return {
            "element":    str(entry.get("element",    "")).strip(),
            "attributes": str(entry.get("attributes", "")).strip(),
            "state":      str(entry.get("state",      "")).strip(),
            "mood":       str(entry.get("mood",       "")).strip(),
            "exclude":    str(entry.get("exclude",    "")).strip(),
        }
    return {"element": "", "attributes": "", "state": "", "mood": "", "exclude": ""}


def _validate_and_fill(raw: dict, pages: list) -> dict:
    board    = {}
    fallback = _default_board(pages)

    for p in pages:
        pn    = p["page"]
        entry = raw.get(str(pn)) or raw.get(pn) or fallback[pn]

        shot = entry.get("shot_type", "medium")
        if shot not in VALID_SHOT_TYPES:
            shot = _fuzzy_shot(shot)

        # visual_must_haves：校验每个条目，最多3项
        raw_must = entry.get("visual_must_haves", [])
        if not isinstance(raw_must, list):
            raw_must = []
        must_haves = [_validate_must_have_entry(x) for x in raw_must if x][:3]
        # 过滤掉 element 为空的条目
        must_haves = [m for m in must_haves if m["element"]]

        # bgm_mood 白名单兜底（v2：优先用 page 自带的）
        valid_moods = {"tension", "climax", "melancholy"}
        # 从 page 配置查找（如果 _validate_and_fill 调用方传入了 page_cfg）
        page_mood = ""
        # entry 是 LLM storyboard 输出的；page_mood 是 story.yaml 里手写的
        # 优先级：page 自带的 > LLM storyboard 输出
        page_cfg_mood = ""
        for p in pages:
            if p.get("page") == pn:
                page_cfg_mood = (p.get("bgm_mood") or "").lower().strip()
                break
        if page_cfg_mood and page_cfg_mood in valid_moods:
            bgm_mood = page_cfg_mood
        else:
            bgm_mood = (entry.get("bgm_mood") or "tension").lower().strip()
            if bgm_mood not in valid_moods:
                # 模糊匹配
                if "climax" in bgm_mood or "horror" in bgm_mood or "scared" in bgm_mood:
                    bgm_mood = "climax"
                elif "melan" in bgm_mood or "sad" in bgm_mood or "sorrow" in bgm_mood:
                    bgm_mood = "melancholy"
                else:
                    bgm_mood = "tension"

        board[pn] = {
            "shot_type":         shot,
            "focal_subject":     entry.get("focal_subject",    ""),
            "composition_hint":  entry.get("composition_hint", ""),
            "transition_from":   entry.get("transition_from",  ""),
            "shot_prefix":       SHOT_PREFIX_MAP[shot],
            "visual_must_haves": must_haves,
            "bgm_mood":          bgm_mood,    # v4 新增
        }

    return board


def _fuzzy_shot(raw: str) -> str:
    raw = raw.lower().replace(" ", "_").replace("-", "_")
    if "extreme_close" in raw or "extreme_c" in raw:
        return "extreme_closeup"
    if "close" in raw:
        return "closeup"
    if "extreme_wide" in raw or ("extreme" in raw and "wide" in raw):
        return "extreme_wide"
    if "wide" in raw:
        return "wide"
    if "medium" in raw or "mid" in raw:
        return "medium"
    return "medium"


# ════════════════════════════════════════════════════════════════
# 对外入口
# ════════════════════════════════════════════════════════════════

def build_storyboard(story, force: bool = False) -> dict:
    """
    生成全片分镜表，结果缓存到 stories/ 目录。
    force=True 时忽略缓存重新生成。
    """
    from pathlib import Path

    cache_path = Path(story.path).parent / f"_{story.story_id}_storyboard.json"

    if not force and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            board     = {int(k): v for k, v in cached.items()}
            page_nums = {p["page"] for p in story.pages}
            if page_nums.issubset(set(board.keys())):
                print(f"  [分镜] 读取缓存: {cache_path.name}")
                _print_board_summary(board, story.pages)
                return board
        except Exception:
            pass

    # v4：检测预制分镜（来自 long_writer.to_pipeline）
    # 全部页都有 _prebuilt_storyboard 字段时，跳过 LLM 调用直接用
    pages_with_prebuilt = [p for p in story.pages
                           if isinstance(p.get("_prebuilt_storyboard"), dict)
                           and p["_prebuilt_storyboard"].get("shot_type")]
    if len(pages_with_prebuilt) == len(story.pages) and len(story.pages) > 0:
        print(f"  [分镜] 检测到预制分镜（{len(story.pages)} 页全员有效），"
              f"跳过 LLM 调用直接复用")
        board = {}
        for p in story.pages:
            pn = p["page"]
            sb = p["_prebuilt_storyboard"]
            shot = sb.get("shot_type", "medium")
            if shot not in SHOT_PREFIX_MAP:
                shot = "medium"
            # 校验 must_haves（兼容字符串/dict）
            raw_haves = sb.get("visual_must_haves", []) or []
            must_haves = [_validate_must_have_entry(m) for m in raw_haves[:3]]
            board[pn] = {
                "shot_type":         shot,
                "focal_subject":     str(sb.get("focal_subject", "")).strip(),
                "composition_hint":  str(sb.get("composition_hint", "")).strip(),
                "transition_from":   str(sb.get("transition_from", "")).strip(),
                "shot_prefix":       SHOT_PREFIX_MAP[shot],
                "visual_must_haves": must_haves,
                "bgm_mood":          (p.get("bgm_mood") or "tension").lower().strip(),
            }
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(board, f, ensure_ascii=False, indent=2)
            print(f"  [分镜] 预制版缓存已写入: {cache_path.name}")
        except Exception as e:
            print(f"  [分镜] 缓存写入失败（非致命）: {e}")
        _print_board_summary(board, story.pages)
        return board

    print(f"  [分镜] 开始全片分镜规划（1次 LLM 调用，{len(story.pages)} 页）...")
    board = _call_llm_storyboard(story)

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(board, f, ensure_ascii=False, indent=2)
        print(f"  [分镜] 缓存已写入: {cache_path.name}")
    except Exception as e:
        print(f"  [分镜] 缓存写入失败（非致命）: {e}")

    return board


def _print_board_summary(board: dict, pages: list):
    print(f"  [分镜] 规划完成:")
    for p in pages:
        pn    = p["page"]
        entry = board.get(pn, {})
        must  = entry.get("visual_must_haves", [])
        # 打印每个 must_have 的 element
        elements = [m.get("element", "?") if isinstance(m, dict) else str(m)
                    for m in must]
        must_str = " | ".join(elements) if elements else "—"
        bgm = entry.get("bgm_mood", "tension")
        print(f"    p{pn} [{entry.get('shot_type','?'):17s}] "
              f"BGM:{bgm:11s} "
              f"焦点: {entry.get('focal_subject','')[:24]:24s}  "
              f"必须有: {must_str}")


# ════════════════════════════════════════════════════════════════
# LLM 调用
# ════════════════════════════════════════════════════════════════

def _build_story_synopsis(story) -> str:
    """
    改动 B：从 story 对象提取故事背景摘要，注入分镜 prompt 头部。
    让 LLM 带着全局叙事理解规划每页视觉，避免跨页叙事关联断裂。
    """
    # 收集角色描述
    char_lines = []
    for cname, cdata in story.characters.items():
        if cname == "narrator":
            continue
        desc = cdata.get("desc", "")
        if desc:
            char_lines.append(f"  - {cname}：{desc}")

    chars_str = "\n".join(char_lines) if char_lines else "  （无角色信息）"

    # 收集全片旁白（给 LLM 看故事全貌）
    all_narrations = []
    for p in story.pages:
        narr = p.get("narration", "")
        if narr:
            all_narrations.append(f"  第{p['page']}页：{narr}")
    narrations_str = "\n".join(all_narrations)

    synopsis = f"""【故事背景（全局叙事上下文，规划每页时必须参考）】
故事标题：{story.title}
主要角色：
{chars_str}

完整叙事脉络（按页顺序）：
{narrations_str}

重要提示：
- 规划每页的视觉元素时，必须理解该元素在整个故事中的叙事意义
- 例如：某页出现的鞋，要结合上下文判断是谁的鞋、什么状态、暗示什么
- 跨页有关联的道具（如第2页的红伞、第4页的嫁鞋），应在 visual_must_haves 里
  体现它们的叙事属性（归属人物、情感含义、外观细节），而非仅写物体名称
"""
    return synopsis


def _call_llm_storyboard(story) -> dict:
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage
    from feedback import clean_json

    pages = story.pages

    # 改动 B：故事背景摘要
    story_synopsis = _build_story_synopsis(story)

    # 各页旁白（带出场角色）
    page_lines = []
    for p in pages:
        chars     = p.get("characters", [])
        char_desc = [story.characters.get(c, {}).get("desc", c) for c in chars]
        char_str  = "、".join(char_desc) if char_desc else "无角色（纯背景）"
        page_lines.append(
            f"第{p['page']}页「{p['title']}」出场：{char_str}\n"
            f"  旁白：{p.get('narration', '')}"
        )
    pages_text = "\n\n".join(page_lines)

    prompt = f"""你是专业漫画分镜师，为以下短篇故事规划全片镜头表。

{story_synopsis}

各页旁白详情：
{pages_text}

景别选择规则：
- extreme_wide：建立场景地理关系，人物极小或不可见（开场、结尾）
- wide：人物全身可见，环境占主导（走路、场景介绍）
- medium：人物上半身，适合动作和对话（互动、展示道具）
- closeup：人物面部到肩部，强调情绪（关键情感时刻）
- extreme_closeup：局部特写（眼睛/手/关键道具），极度紧张或揭示细节

连贯性原则：避免连续相同景别；高潮前中景蓄力，高潮用特写释放；纯背景页用 extreme_wide 或 wide。

【任务】为每页完成：
1. 选择景别
2. 描述视觉焦点和构图
3. 提取"图中必须出现的视觉元素"结构体（1-3项）
4. 为每页选择 BGM 情绪标签（bgm_mood）：
   - tension     默认值，紧张铺垫/悬疑/不安（多数页用这个）
   - climax      恐怖高潮/惊悚揭示/极度紧张（关键揭示页）
   - melancholy  凄凉感伤/悲悯/收尾（结尾或反思页）
   全片建议节奏：开头铺垫=tension → 揭示页=climax → 结尾=melancholy
   一个 6 页故事典型分布：tension×3 + climax×2 + melancholy×1

visual_must_haves 每项结构：
- element：物体或人物的英文名称（具体名词，如 "red embroidered wedding shoes"）
- attributes：外观属性（材质/颜色/款式/大小，如 "women's ancient silk, phoenix pattern, small size"）
- state：当前状态（如 "mud on toe, placed in wooden cabinet, freshly dug"）
- mood：叙事情绪（如 "eerie reveal, sorrowful, horror"）
- exclude：明确排除的错误视觉（如 "men's shoes, boots, modern footwear"）

要求：
- attributes 要体现物体的归属（谁的）、文化背景（古风/中式）
- state 要体现旁白描述的具体状态，不要写抽象词
- exclude 要写出最容易被 AI 误画的错误版本
- 纯背景页（无角色）的 visual_must_haves 写环境关键元素

返回严格 JSON（key 为页码数字字符串）：
{{
  "1": {{
    "shot_type": "extreme_wide",
    "focal_subject": "lone scholar walking in heavy rain on mountain road",
    "composition_hint": "high angle, scholar tiny against vast dark mountains",
    "transition_from": "",
    "visual_must_haves": [
      {{
        "element": "scholar figure",
        "attributes": "young man in blue hanfu, scholar hat, carrying books, rain-soaked",
        "state": "walking alone on stone path, hunched against rain",
        "mood": "lonely, exhausted, lost",
        "exclude": "modern clothing, umbrella, companion"
      }},
      {{
        "element": "mountain road in heavy rain",
        "attributes": "ancient stone path, bamboo forest, misty dark mountains",
        "state": "torrential rain, puddles on stone, single red lantern barely visible",
        "mood": "ominous, isolated, stormy night",
        "exclude": "modern road, daylight, clear weather"
      }}
    ]
  }},
  "4": {{
    "shot_type": "extreme_closeup",
    "focal_subject": "pair of muddy red embroidered wedding shoes inside wooden cabinet",
    "composition_hint": "low angle, cabinet door frame visible, shoes centered",
    "transition_from": "reveals the ghost's tragic backstory from previous dialogue",
    "visual_must_haves": [
      {{
        "element": "red embroidered wedding shoes",
        "attributes": "women's ancient chinese style, small delicate size, red silk fabric, gold phoenix embroidery, traditional bridal footwear",
        "state": "mud caked on toe tips, fresh soil residue, placed neatly inside old wooden cabinet",
        "mood": "tragic, eerie reveal, horror, sorrowful bridal symbolism",
        "exclude": "men's shoes, boots, modern shoes, clean shoes, large shoes"
      }}
    ]
  }}
}}

只返回 JSON，不要任何解释或 markdown 代码块。"""

    try:
        llm = ChatOpenAI(
            model    = LLM_MODEL,
            api_key  = LLM_API_KEY,
            base_url = LLM_BASE_URL,
        )
        full_content = ""
        for chunk in llm.stream([HumanMessage(content=prompt)]):
            full_content += chunk.content

        t = clean_json(full_content)
        if "```" in t:
            import re
            m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', t, re.DOTALL)
            t = m.group(1) if m else t.replace("```json", "").replace("```", "")

        raw   = json.loads(t.strip())
        board = _validate_and_fill(raw, pages)
        _print_board_summary(board, pages)
        return board

    except json.JSONDecodeError as e:
        print(f"  [分镜] JSON 解析失败: {e}，使用兜底分镜")
        return _default_board(pages)
    except Exception as e:
        print(f"  [分镜] LLM 调用失败: {e}，使用兜底分镜")
        return _default_board(pages)


# ════════════════════════════════════════════════════════════════
# Prompt 注入（orchestrator 调用）
# ════════════════════════════════════════════════════════════════

def inject_shot_into_scene_prompt(scene_prompt: str,
                                   board_entry: Optional[dict]) -> tuple:
    """
    v3：把景别前缀 + 视觉焦点 + must_haves 注入 scene_prompt。

    返回 (new_positive: str, extra_negative: str)
      new_positive：注入了景别/焦点/物体属性/状态/情绪的完整正向描述
      extra_negative：从 must_haves[].exclude 汇总的负向词

    orchestrator._build_final_params 调用时：
      positive += new_positive（替换原 scene_prompt 部分）
      negative += extra_negative
    """
    if not board_entry:
        return scene_prompt, ""

    shot_prefix      = board_entry.get("shot_prefix",      "")
    focal_subject    = board_entry.get("focal_subject",    "")
    composition_hint = board_entry.get("composition_hint", "")
    must_haves       = board_entry.get("visual_must_haves", [])

    # ── 正向 prompt 组装 ──────────────────────────────────────
    pos_parts = []
    if shot_prefix:
        pos_parts.append(shot_prefix)
    if focal_subject:
        pos_parts.append(focal_subject + ",")
    if scene_prompt:
        pos_parts.append(scene_prompt)
    if composition_hint:
        pos_parts.append(composition_hint + ",")

    # 从结构体里提取 attributes / state / mood 注入正向
    element_details = []
    for m in must_haves:
        if not isinstance(m, dict):
            continue
        parts = []
        if m.get("attributes"):
            parts.append(m["attributes"])
        if m.get("state"):
            parts.append(m["state"])
        if m.get("mood"):
            parts.append(m["mood"])
        if parts:
            element_details.append(", ".join(parts))

    if element_details:
        pos_parts.append("with " + "; ".join(element_details) + ",")

    new_positive = " ".join(pos_parts).strip()

    # ── 负向 prompt 汇总 ─────────────────────────────────────
    excludes = []
    for m in must_haves:
        if isinstance(m, dict) and m.get("exclude"):
            excludes.append(m["exclude"])
    extra_negative = ", ".join(excludes) if excludes else ""

    return new_positive, extra_negative


def get_narration_keywords(board_entry: Optional[dict]) -> list:
    """
    从分镜条目提取 element 字段列表，供 quality.py 的叙事检查使用。
    替代旧版的字符串列表直接传递。
    """
    if not board_entry:
        return []
    must_haves = board_entry.get("visual_must_haves", [])
    keywords = []
    for m in must_haves:
        if isinstance(m, dict) and m.get("element"):
            keywords.append(m["element"])
        elif isinstance(m, str) and m:
            keywords.append(m)
    return keywords
