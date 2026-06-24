"""
outline.py ── 概念 → 大纲（Step 1）
======================================
输入：concept（中文短句，比如"民国时期上海法租界的旗袍鬼"）
输出：scripts/<story_id>_outline.yaml

大纲结构：
  title:        故事标题
  premise:      30-50字核心钩子（用于抖音首屏文字）
  total_words:  目标字数（默认4000）
  chapters:     8-12个章节
    - id:           ch01
      title:        章节标题
      arc_role:     hook/setup/rising/climax/twist/falling/resolution
      target_words: 字数（自动分配）
      summary:      一段话章节大纲（200字左右）
      tone:         tension/eerie/melancholy/peaceful
  characters:   主要人物
    - name, desc, voice_ref_id, key_features
  bgm_palette:  整体BGM色调推荐
  visual_palette: 整体视觉色调推荐

设计：
  - LLM 一次调用产出全片大纲，保证节奏感
  - 每章 arc_role 决定该章节的"叙事职能"
  - target_words 自动按 arc_role 分配（climax > rising > setup）
"""

import argparse
import json
from pathlib import Path
from datetime import datetime

import yaml


SCRIPTS_DIR = Path("scripts")
DEFAULT_TOTAL_WORDS = 4000
DEFAULT_CHAPTERS    = 10

# 各 arc_role 的字数权重（climax 最重，hook 最轻）
ARC_WORD_WEIGHTS = {
    "hook":       0.6,
    "setup":      1.0,
    "rising":     1.2,
    "climax":     1.5,
    "twist":      1.3,
    "falling":    1.0,
    "resolution": 0.8,
}


def _allocate_words(chapters: list, total_words: int) -> list:
    """按 arc_role 权重分配每章字数"""
    weights = [ARC_WORD_WEIGHTS.get(c.get("arc_role", "rising"), 1.0)
               for c in chapters]
    total_w = sum(weights) or 1.0
    for ch, w in zip(chapters, weights):
        ch["target_words"] = int(total_words * w / total_w)
    return chapters


# v2.3.3：角色 role 字段规范化
VALID_ROLES = ("lead", "extra", "group")
DEFAULT_VOICE_BY_DESC = {
    # 默认 fallback voice（按描述关键词启发式选）
    "old":     "old_male_wise",
    "老":      "old_male_wise",
    "young":   "young_male_scholar",
    "年轻":    "young_male_scholar",
    "middle":  "middle_male_calm",
    "中年":    "middle_male_calm",
    "female":  "middle_female_warm",
    "女":      "middle_female_warm",
}


def _guess_default_voice(desc: str) -> str:
    """根据角色描述启发式选择默认 voice"""
    if not desc:
        return "middle_male_calm"
    desc_l = desc.lower()
    for key, voice in DEFAULT_VOICE_BY_DESC.items():
        if key in desc_l:
            return voice
    return "middle_male_calm"


def _normalize_character_roles(characters: list) -> list:
    """
    规范化 character role 字段。
    - 缺 role 的旧大纲 → 默认 lead（向后兼容）
    - role 非法值 → 修正为 lead
    - extra/group 缺 voice_ref_id → 启发式补
    """
    if not isinstance(characters, list):
        return []
    normalized = []
    for c in characters:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        role = c.get("role", "lead")
        if role not in VALID_ROLES:
            print(f"  [outline] ⚠ 角色 {c['name']} role='{role}' 非法 → 默认 lead")
            role = "lead"
        c["role"] = role

        # extra/group 兜底补 voice（group 可无 voice，但补一个不会错）
        if role in ("extra", "group") and not c.get("voice_ref_id"):
            c["voice_ref_id"] = _guess_default_voice(c.get("desc", ""))
            print(f"  [outline] {c['name']} ({role}) 未指定 voice → "
                  f"启发式填 {c['voice_ref_id']}")
        normalized.append(c)
    return normalized


def _normalize_character_demographics(characters: list) -> list:
    """
    v2.4.2 升级(原 _normalize_character_en_name):规范化角色 demographics。

    管理字段:en_name / gender / age / face_features
    管理对象:lead 和 extra(group 跳过,group 不需要个人身份)

    核心改动 vs v2.4:
      - gender 缺失 → 启发式推断(从 desc 和 name 找性别线索),不再兜底 'unknown'
      - 'unknown' 也走启发式(LLM 偶尔会偷懒填 unknown)
      - 推断失败才默认 male + warning,提醒用户编辑 outline.yaml
      - age 缺失 → 启发式推断年龄段后给中位数(young=22, middle=40, old=65)
      - face_features 缺失 → 留空,由 portraits 阶段按 gender+age 给默认骨相

    为什么不沿用 'unknown' 兜底:
      'unknown' 这个值会让下游(portraits)走 fallback 路径,但 fallback 
      本身就是启发式推断 —— 既然必然要推,不如在 outline 阶段就推完写死,
      让下游有确定值可用。
    """
    if not isinstance(characters, list):
        return []
    for c in characters:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        role = c.get("role", "lead")
        # group 角色不必有个人身份字段,跳过
        if role == "group":
            continue
        cname = c["name"]
        desc  = c.get("desc", "") or ""

        # ── en_name ──────────────────────────────────────────
        if not c.get("en_name"):
            c["en_name"] = cname  # 兜底用中文名
            print(f"  [outline] ⚠ 角色 {cname} ({role}) 未指定 en_name → "
                  f"兜底用中文名(影响 coordinator 双 lead 扫描准确率)")

        # ── gender:启发式推断 ────────────────────────────────
        raw_gender = (c.get("gender") or "").strip().lower()
        if raw_gender in ("male", "female"):
            # 已经是合法值 —— 回写小写规范形式(防 LLM 写 MALE / Male 等大小写不一)
            c["gender"] = raw_gender
        else:
            inferred = _infer_gender_from_text(cname + " " + desc)
            if inferred:
                c["gender"] = inferred
                print(f"  [outline] {cname} ({role}) gender={raw_gender or '缺失'} → "
                      f"启发式推断为 {inferred}(based on desc/name)")
            else:
                c["gender"] = "male"   # 真没法推时默认 male
                print(f"  [outline] ⚠ 角色 {cname} ({role}) gender 完全推不出 → "
                      f"默认 male。强烈建议手动编辑 outline.yaml 修正。")

        # ── age:启发式推断 ──────────────────────────────────
        # outline 里 age 可能是 int 或 str 形式的整数,也可能没有
        raw_age = c.get("age")
        try:
            if raw_age is not None and raw_age != "":
                n = int(str(raw_age).strip())
                if 5 <= n <= 110:
                    c["age"] = n
                else:
                    raise ValueError("out of range")
            else:
                raise ValueError("missing")
        except (ValueError, TypeError):
            # 推年龄段(young/middle/old)→ 取中位数
            band = _infer_age_band_from_text(cname + " " + desc)
            band_to_age = {"young": 22, "middle": 40, "old": 65}
            c["age"] = band_to_age[band]
            print(f"  [outline] {cname} ({role}) age={raw_age or '缺失'} → "
                  f"启发式推断为 {c['age']}(段={band})")

        # ── face_features:不强行兜底,留空让 portraits 按 gender+age 给默认 ─
        # face_features 是给 prompt 的英文短语,LLM 没给就别瞎填中文,
        # 留空时 portraits.py 的 _build_facial_anchor 会按性别+年龄给一组合理默认骨相
        if not c.get("face_features"):
            # 不打印 warning,因为 portraits 会兜底,而且 LLM 可能根本想不出脸部描述
            pass

    return characters


# ── 启发式推断辅助函数(v2.4.2) ────────────────────────────
# 跟 portraits.py 里的相同逻辑保持一致:lazy import 那边的常量,避免重复定义。
# 但为了让 outline.py 可以独立 import 不依赖 portraits 模块,这里复制一份精简版。

_OUTLINE_GENDER_FEMALE = (
    "女人", "女孩", "姑娘", "少女", "妇人", "妻", "妾",
    "尼姑", "女道", "丫鬟", "婢女", "歌女", "舞女", "妓女",
    "母亲", "娘", "妈", "祖母", "奶奶", "姐姐", "妹妹",
    "皇后", "公主", "王妃", "格格", "侄女", "孙女", "女儿",
    "小妹", "嫂子", "婶子", "夫人", "太太", "小姐",
)
_OUTLINE_GENDER_MALE = (
    "退伍兵", "士兵", "军人", "战士", "男人", "男孩", "小伙", "汉子",
    "和尚", "道士", "书生", "举人", "秀才", "公子", "少爷", "老爷",
    "父亲", "爹", "爸", "祖父", "爷爷", "兄长", "哥哥", "弟弟",
    "守墓人", "更夫", "屠夫", "船夫", "农夫", "渔夫", "车夫",
    "管家", "门卫", "保安", "司机", "工人",
    "皇帝", "王爷", "将军", "县令", "知府", "捕头",
    "侄子", "孙子", "儿子",
)
_OUTLINE_AGE_YOUNG = (
    "少女", "少年", "小伙", "姑娘", "学生", "丫鬟", "书生", "公子",
    "小姐", "童", "幼", "年轻",
    "女儿", "儿子", "侄子", "侄女", "孙子", "孙女",
    "小哥", "小妹", "弟弟", "妹妹",
)
_OUTLINE_AGE_MIDDLE = (
    "退伍兵", "壮年", "中年", "父亲", "母亲", "干部", "工人",
    "记者", "医生", "法医", "警察", "队长", "教师",
)
_OUTLINE_AGE_OLD = (
    "老人", "老者", "老头", "老婆婆", "老妇", "祖父", "祖母",
    "爷爷", "奶奶", "守墓人", "白发", "老拐", "老头子", "老者",
)


def _infer_gender_from_text(text: str) -> str:
    """从中文文本推性别。返回 'male'/'female' 或 ''(推不出)。"""
    if not text:
        return ""
    for w in _OUTLINE_GENDER_FEMALE:
        if w in text:
            return "female"
    for w in _OUTLINE_GENDER_MALE:
        if w in text:
            return "male"
    return ""


def _infer_age_band_from_text(text: str) -> str:
    """推年龄段,默认 middle。"""
    if not text:
        return "middle"
    scores = {"young": 0, "middle": 0, "old": 0}
    for w in _OUTLINE_AGE_YOUNG:
        if w in text:
            scores["young"] += 1
    for w in _OUTLINE_AGE_MIDDLE:
        if w in text:
            scores["middle"] += 1
    for w in _OUTLINE_AGE_OLD:
        if w in text:
            scores["old"] += 1
    best = max(scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] > 0 else "middle"


# 旧函数名保留为别名,防止其他模块 import 它(向后兼容)
_normalize_character_en_name = _normalize_character_demographics

def generate_outline(concept: str,
                     total_words: int = DEFAULT_TOTAL_WORDS,
                     chapters_count: int = DEFAULT_CHAPTERS,
                     theme_id: str = "chinese_horror_tales",
                     series: str = "long_tales",
                     output_path: str = None) -> dict:
    """
    生成大纲并写入 scripts/<id>_outline.yaml。
    返回大纲 dict。
    """
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage
    from feedback import clean_json
    # v2.4.7: 从 narrative_profile 读题材引导,替代写死的恐怖模板
    try:
        from .narrative_profile import load_narrative
    except ImportError:
        from narrative_profile import load_narrative
    narrative = load_narrative(theme_id)

    print(f"\n  [outline] 概念: {concept}")
    print(f"  [outline] 目标: {total_words} 字 / {chapters_count} 章 / 主题={theme_id}")
    print(f"  [outline] 题材风格: {narrative.identity[:50]}...")
    print(f"  [outline] 调用 LLM 生成大纲（约 30-60 秒）...")

    # v2.4.7: identity / benchmark / pacing 都从 narrative_profile 注入
    # 之前写死"对标'老妖鬼故事'"导致所有题材都出恐怖故事
    tone_list = "/".join(narrative.tone_options) if narrative.tone_options else "neutral"
    forbidden_hint = ""
    if narrative.forbidden_tones:
        forbidden_hint = f"\n⚠ 严禁使用的 tone: {' / '.join(narrative.forbidden_tones)}"
    safety_hint = ""
    if narrative.story_safety:
        safety_hint = f"\n【内容安全约束】\n{narrative.story_safety}\n"

    prompt = f"""{narrative.identity}。为以下概念创作完整大纲。

概念：{concept}
目标字数：{total_words}（合计，分散到各章节）
章节数量：{chapters_count}（必须严格 {chapters_count} 章）
内容主题：{theme_id}

【创作准则】({narrative.benchmark})
叙事重点: {narrative.arc_emphasis}
节奏指引: {narrative.pacing_guide}
视觉钩子: {narrative.visual_hook_focus}
{safety_hint}
【章节 arc_role 必须包含的结构】
- hook（1章）：开篇钩子
- setup（1-2章）：交代背景、主角、初始情境
- rising（2-3章）：情节升级
- climax（1章）：最高潮
- twist（1章）：意外反转(若题材不适合反转可省)
- falling（1-2章）：余波处理
- resolution（1章）：余韵收尾

返回严格 JSON（不要 markdown 代码块）：
{{
  "title":   "故事标题（吸引人，10字内，带情绪关键词）",
  "premise": "30-50字核心钩子，可作为视频开头文字",
  "characters": [
    {{
      "name":         "角色名(中文)",
      "role":         "lead | extra | group",
      "en_name":      "英文名(lead/extra 必填,group 可省;格式: 'Chen Yuanzheng' 标准 Pinyin 首字母大写)",
      "gender":       "male | female (lead/extra 必填,二选一,不允许 unknown;group 可省)",
      "age":          "整数年龄(lead/extra 必填,从角色背景合理推断,如 25/40/65;group 可省)",
      "desc":         "一句话背景",
      "key_features": "外观特征(英文,服装/道具/发型为主,group 类可省略)",
      "face_features":"面部独特性(英文,2-4 个短语,如 'square face, thick eyebrows, mole on cheek' 或 'oval face, almond eyes, full lips';用于让定妆照有可辨识度,lead 必填,extra 可省)",
      "voice_ref_id": "声音库key(group 类可省略)"
    }}
  ],
  "chapters": [
    {{
      "id":       "ch01",
      "title":    "章节标题",
      "arc_role": "hook/setup/rising/climax/twist/falling/resolution 之一",
      "tone":     "{tone_list} 之一",
      "summary":  "200字左右的章节大纲，写明：发生什么事、谁出场、有什么转折/揭秘"
    }}
  ],
  "bgm_palette":    "整体BGM色调推荐,与题材风格匹配",
  "visual_palette": "整体视觉色调推荐,与题材风格匹配"
}}{forbidden_hint}

═══════════════════════════════════════════════════════════════
【v2.3.3 关键：角色分级（role 字段）】⚠️ 必须填写
═══════════════════════════════════════════════════════════════
所有可能在 narration 中被提及或可能说台词的实体都要列出，按以下分级：

**lead（主角，2-4 人）**：
  - 戏份重，画面里需要稳定一致的脸（后期会训 LoRA 锁定）
  - 必填：role / en_name / gender(male|female) / age / desc / key_features（英文）/ face_features（英文）/ voice_ref_id
  - face_features 是定妆照可辨识度的核心,不要写成跟 key_features 重复的服装内容,
    要写脸部本身:脸型/眉毛/眼睛/胡须/痣或疤等。不同角色的 face_features
    必须明显不同(防止全片角色一张通用脸)。
  - 例：周建军（拆迁队头）、画中女（关键超自然角色）

**extra（配角/有名字的次要角色）**：
  - 戏份不重但有台词/有名字
  - 必填：role / en_name / gender(male|female) / age / desc / voice_ref_id
  - 可选：key_features / face_features（即使没 LoRA 也建议写，FLUX 会按描述画）
  - 例：王主任（拆迁办）、老李（看门人）、小张（同事）

**group（群体/无名配角）**：
  - 没有个人身份，只是"工人们""路人""学生甲"这种泛指
  - 不会有专属台词（如果他们说话，文本里通常是"有人说""一个人喊"）
  - 必填：role / desc
  - 不必填：en_name / gender / age / key_features / face_features / voice_ref_id

⚠️ 严格要求：所有可能在章节 narration 里被提及的人都要列出来。
   宁可多列 extra/group，也不要让分镜大师/编剧大师遇到未建模的角色。

【常见 extra 角色应该建模的判断】
  - "拆迁办主任"、"档案管理员"、"门卫老李" → 有职务/身份 → extra
  - "工人们"、"路人甲乙"、"学生们" → 群体泛指 → group
  - "工人甲突然结巴说话" → 即使是工人甲一个人开口，
                            如果他没有名字、只用职业指代 → group
                            （他的台词会自动降级为 narrator 旁白代述）

【voice_ref_id 选择指南】
  lead 主角按个性精选；extra 按性别+年龄+性格选通用 voice：

可选 voice_ref_id（来自 refs/voice_library.yaml）：
  - young_male_scholar / middle_male_calm / old_male_wise / young_male_sinister
  - young_female_pure / ghost_female_sorrowful / ghost_female_seductive
  - middle_female_warm / old_female_kindly
  - narrator_male_storyteller / narrator_female_calm / narrator_male_grave
"""

    llm = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY,
                     base_url=LLM_BASE_URL, temperature=0.8)
    full_content = ""
    for chunk in llm.stream([HumanMessage(content=prompt)]):
        full_content += chunk.content

    t = clean_json(full_content)
    if "```" in t:
        import re
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', t, re.DOTALL)
        if m:
            t = m.group(1)
        else:
            t = t.replace("```json", "").replace("```", "")

    try:
        outline = json.loads(t.strip())
    except json.JSONDecodeError as e:
        print(f"  [outline] ❌ JSON 解析失败: {e}")
        print(f"  [outline] LLM 原始输出（前500字）:")
        print(full_content[:500])
        raise

    # 后处理：补充元数据 + 字数分配
    outline["concept"]      = concept
    outline["total_words"]  = total_words
    outline["theme_id"]     = theme_id
    outline["series"]       = series
    outline["created_at"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    outline["chapters"]     = _allocate_words(outline["chapters"], total_words)

    # v2.3.3：character role 字段规范化
    # 旧大纲（无 role 字段）→ 默认 lead；新大纲必填 role
    outline["characters"] = _normalize_character_roles(
        outline.get("characters", []))

    # v2.4.2:规范化角色 demographics(en_name / gender / age / face_features)
    # 升级自 v2.4 的 _normalize_character_en_name:
    #   - gender 缺失/'unknown' → 启发式推断而非 'unknown' 兜底
    #   - 新增 age 字段规范化(缺失时按 desc 推断年龄段)
    outline["characters"] = _normalize_character_demographics(
        outline["characters"])

    # 生成 story_id
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    story_id = f"long_{ts}_{outline['title'][:20]}"
    outline["story_id"] = story_id

    # 写入 scripts/<id>_outline.yaml
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = output_path or str(SCRIPTS_DIR / f"{story_id}_outline.yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(outline, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)

    # v2：版本保留
    try:
        history_dir = SCRIPTS_DIR / "_outline_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        ts_short = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = history_dir / f"{story_id}_{ts_short}.yaml"
        with open(history_path, "w", encoding="utf-8") as f:
            yaml.dump(outline, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        print(f"  [outline] 版本副本: {history_path.name}")
    except Exception as e:
        print(f"  [outline] 版本副本写入失败（不影响主流程）: {e}")

    # 打印摘要
    print(f"\n  [outline] ✓ 已生成: {out_path}\n")
    print(f"  ── 大纲摘要 ──")
    print(f"  标题: {outline['title']}")
    print(f"  钩子: {outline['premise']}")
    # v2.3.3：按 role 分组打印
    leads  = [c for c in outline['characters'] if c.get('role') == 'lead']
    extras = [c for c in outline['characters'] if c.get('role') == 'extra']
    groups = [c for c in outline['characters'] if c.get('role') == 'group']
    print(f"  主角 ({len(leads)}): {', '.join(c['name'] for c in leads) or '无'}")
    if extras:
        print(f"  配角 ({len(extras)}): {', '.join(c['name'] for c in extras)}")
    if groups:
        print(f"  群体 ({len(groups)}): {', '.join(c['name'] for c in groups)}")
    print(f"  视觉: {outline.get('visual_palette', '')}")
    print(f"  BGM:  {outline.get('bgm_palette', '')}")
    print(f"\n  ── 章节列表 ──")
    for ch in outline["chapters"]:
        print(f"  {ch['id']} [{ch['arc_role']:10s}] "
              f"{ch.get('tone', '?'):10s} "
              f"{ch['target_words']:>4}字  {ch['title']}")

    print(f"\n  下一步:")
    print(f"    1. 审核 {out_path}（修改章节顺序/字数/tone 等）")
    print(f"    2. python -m tools.long_writer.chapter_writer {out_path}")

    return outline


def main():
    parser = argparse.ArgumentParser(description="长故事大纲生成")
    parser.add_argument("--concept",  required=True,
                        help="故事概念，例如：'民国时期上海法租界的旗袍鬼'")
    parser.add_argument("--words",    type=int, default=DEFAULT_TOTAL_WORDS,
                        help=f"目标总字数（默认 {DEFAULT_TOTAL_WORDS}）")
    parser.add_argument("--chapters", type=int, default=DEFAULT_CHAPTERS,
                        help=f"章节数（默认 {DEFAULT_CHAPTERS}）")
    parser.add_argument("--theme",    default="chinese_horror_tales",
                        help="主题 ID（决定生图风格）")
    parser.add_argument("--series",   default="long_tales",
                        help="所属系列名")
    parser.add_argument("--output",   default=None,
                        help="输出路径（默认自动生成 scripts/long_xxx_outline.yaml）")
    args = parser.parse_args()

    generate_outline(
        concept        = args.concept,
        total_words    = args.words,
        chapters_count = args.chapters,
        theme_id       = args.theme,
        series         = args.series,
        output_path    = args.output,
    )


if __name__ == "__main__":
    main()
