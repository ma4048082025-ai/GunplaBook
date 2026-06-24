"""
core/bgm_mood_resolver.py ── BGM 情绪规则裁决
=================================================================
解决问题: 分镜大师产出的 yaml 里 bgm_mood 全是 tension,儿童剧也变恐怖。
原因: 分镜大师只看眼前镜头,缺少"整集情绪曲线"的全局视角,fallback 默认 tension。

方案: 不加 LLM agent,用纯规则函数,根据 3 个维度裁决:
  1. story 类型(题材) ── horror / children / lyrical / mystery / epic
  2. chapter.arc_role ── hook / rising / climax / twist / resolve
  3. shot.tone & narration 文本关键词

适用位置: coordinator.py 的 post_check 末尾,在 _purge_empty_shots 之后调用。

用法:
  from bgm_mood_resolver import resolve_bgm_moods
  fixes = resolve_bgm_moods(shots, story_meta, chapter)
  for fix in fixes:
      log.append(fix)

不破坏任何现有契约。如果某 shot 的 bgm_mood 已经被分镜大师明确指定
且合法,就保留不动。
"""

from __future__ import annotations
import re
from typing import Optional


# ════════════════════════════════════════════════════════════════
# 扩展 BGM 词表 (7 → 8 类)
# ════════════════════════════════════════════════════════════════
# 注意: 这个列表必须和 long_storyboard.py 的 VALID_BGM_MOODS 同步。
# 部署时,把 long_storyboard.py 第 51 行也改成这个列表。

VALID_BGM_MOODS = (
    "tension",        # 紧张悬疑   ← 恐怖故事主轴
    "climax",         # 高潮爆发   ← 鬼出现 / 真相揭晓
    "melancholy",     # 哀伤抒情   ← 告别 / 回忆 / 悲剧
    "playful",        # 欢快俏皮   ← 儿童剧 / 喜剧场面 (新增)
    "warm",           # 温暖治愈   ← 童话安全段 / 团圆 (新增)
    "mystery",        # 神秘探索   ← 探险 / 解谜 / 未知 (新增)
    "epic",           # 史诗宏大   ← 章节结尾 / 神祇出场 (新增)
    "serene",         # 平静日常   ← 开场介绍 / 安全过渡 (新增)
)


# ════════════════════════════════════════════════════════════════
# 1. 题材 → 默认 mood
# ════════════════════════════════════════════════════════════════
# story_meta["genre"] / theme name 映射到题材类别
GENRE_KEYWORDS = {
    "horror":   ("horror", "ghost", "鬼", "恐怖", "聊斋", "灵异", "亡魂", "悬疑"),
    "children": ("children", "child", "kids", "kid", "童话", "儿童", "亲子",
                 "古镇奇遇", "小学生"),
    "lyrical":  ("lyrical", "literary", "抒情", "文艺", "诗意", "唯美"),
    "mystery":  ("mystery", "detective", "案件", "侦探", "悬疑"),
    "epic":     ("epic", "fantasy", "史诗", "奇幻", "神话"),
}

# 题材级首选 mood (按出现频率)
GENRE_PREFERRED_MOODS = {
    "horror":   ["tension", "melancholy", "mystery", "climax"],
    "children": ["playful", "warm", "serene", "mystery"],
    "lyrical":  ["melancholy", "warm", "serene", "epic"],
    "mystery":  ["mystery", "tension", "climax", "melancholy"],
    "epic":     ["epic", "tension", "climax", "warm"],
}


def _detect_genre(story_meta: dict, theme_name: str = "") -> str:
    """从 story_meta / theme 推断题材。"""
    # 显式字段优先
    if "genre" in story_meta and story_meta["genre"] in GENRE_KEYWORDS:
        return story_meta["genre"]

    # 否则扫描多个字段做关键词匹配
    haystack = " ".join([
        str(story_meta.get("title", "")),
        str(story_meta.get("synopsis", "")),
        str(story_meta.get("visual_palette", "")),
        str(story_meta.get("bgm_palette", "")),
        theme_name,
    ]).lower()

    for genre, kws in GENRE_KEYWORDS.items():
        for kw in kws:
            if kw.lower() in haystack:
                return genre

    return "horror"  # 全无信号时默认 horror,因为这是主线题材


# ════════════════════════════════════════════════════════════════
# 2. arc_role → mood 倾向
# ════════════════════════════════════════════════════════════════
ARC_MOOD_BIAS = {
    # arc_role: [(mood, weight), ...] weight 越高越倾向
    "hook":     [("tension", 0.4), ("mystery", 0.3), ("serene", 0.3)],
    "rising":   [("tension", 0.5), ("mystery", 0.3), ("melancholy", 0.2)],
    "climax":   [("climax", 0.6), ("tension", 0.3), ("epic", 0.1)],
    "twist":    [("mystery", 0.4), ("tension", 0.3), ("climax", 0.3)],
    "resolve":  [("melancholy", 0.4), ("warm", 0.3), ("epic", 0.3)],
    # 兼容旧 arc 字段
    "setup":    [("serene", 0.5), ("mystery", 0.3), ("tension", 0.2)],
    "build":    [("tension", 0.5), ("mystery", 0.3), ("climax", 0.2)],
    "release":  [("warm", 0.4), ("melancholy", 0.4), ("serene", 0.2)],
}


# ════════════════════════════════════════════════════════════════
# 3. shot.tone + narration 关键词 → mood 信号
# ════════════════════════════════════════════════════════════════
TONE_TO_MOOD = {
    # tone 字段直接映射
    "eerie":     "tension",
    "tense":     "tension",
    "scary":     "tension",
    "peaceful":  "serene",
    "calm":      "serene",
    "happy":     "playful",
    "joyful":    "playful",
    "warm":      "warm",
    "sad":       "melancholy",
    "lonely":    "melancholy",
    "tragic":    "melancholy",
    "epic":      "epic",
    "grand":     "epic",
    "mysterious": "mystery",
    "curious":   "mystery",
    "climax":    "climax",
    "intense":   "climax",
}

# narration 文本关键词信号
NARRATION_KEYWORDS = {
    "playful": [
        "笑", "嘻", "哈哈", "蹦", "跳", "玩", "闹", "乐", "扮鬼脸",
        "小黄牛", "小狗", "蹭蹭", "撒娇", "顽皮", "调皮", "天真",
    ],
    "warm": [
        "团圆", "拥抱", "微笑", "温暖", "回家", "母亲", "孩子",
        "怀里", "依偎", "感动", "守护",
    ],
    "tension": [
        "黑影", "鬼", "诡异", "毛骨悚然", "心惊", "战栗", "寒意",
        "屏息", "脚步声", "诡谲", "阴森", "尖叫", "惨叫",
    ],
    "climax": [
        "猛然", "突然", "炸开", "轰然", "爆", "崩塌", "嘶吼",
        "破窗", "粉碎", "炸雷", "断头", "鲜血", "扑来",
    ],
    "melancholy": [
        "泪", "孤", "独", "思念", "故人", "亡", "凋零", "苍凉",
        "凄", "怅惘", "哀", "悲",
    ],
    "mystery": [
        "迷雾", "未知", "线索", "诡秘", "暗号", "符号", "玄机",
        "探查", "追踪", "蹊跷", "古怪",
    ],
    "epic": [
        "天地", "山河", "万物", "苍穹", "亿万", "千年", "神祇",
        "宿命", "大势", "壮阔",
    ],
    "serene": [
        "晨", "暮", "夕阳", "炊烟", "宁静", "平和", "日子",
        "村口", "镇上", "市集", "街市",
    ],
}


def _detect_mood_from_text(narration: str) -> Optional[str]:
    """根据 narration 文本关键词推断 mood。返回最强信号或 None。"""
    if not narration:
        return None
    text = narration.lower()
    scores = {mood: 0 for mood in VALID_BGM_MOODS}
    for mood, kws in NARRATION_KEYWORDS.items():
        for kw in kws:
            if kw in text or kw.lower() in text:
                scores[mood] += 1
    best = max(scores.items(), key=lambda x: x[1])
    return best[0] if best[1] > 0 else None


def _detect_mood_from_tone(tone: str) -> Optional[str]:
    """根据 shot.tone 字段映射 mood。"""
    if not tone:
        return None
    tone_norm = tone.lower().strip()
    if tone_norm in TONE_TO_MOOD:
        return TONE_TO_MOOD[tone_norm]
    return None


# ════════════════════════════════════════════════════════════════
# 主决策函数
# ════════════════════════════════════════════════════════════════

def _resolve_one_shot(shot: dict,
                       genre: str,
                       arc_role: str,
                       chapter_tone: str = "",
                       prev_mood: Optional[str] = None) -> tuple[str, str]:
    """
    决策单个 shot 的 bgm_mood。
    返回: (mood, reason)

    优先级:
      1. shot 自己已明确写了合法 mood → 保留
      2. shot.tone 字段命中 TONE_TO_MOOD → 用映射
      3. narration 文本关键词命中 → 用关键词信号
      4. arc_role bias + 题材首选 → 加权随机
      5. 兜底: 题材首选第一个
    """
    # 1. 已有合法 mood
    existing = shot.get("bgm_mood")
    if existing and existing in VALID_BGM_MOODS:
        # 但 tension 是 fallback 默认值,需要二次审查
        # 如果是儿童题材却写了 tension,大概率是 fallback,要重判
        if existing == "tension" and genre == "children":
            pass  # 落到下面重判
        else:
            return existing, "shot 已指定且合法"

    # 2. shot.tone 字段映射
    shot_tone = shot.get("tone") or chapter_tone
    by_tone = _detect_mood_from_tone(shot_tone)
    if by_tone and by_tone in VALID_BGM_MOODS:
        # 题材过滤: 儿童剧不能选 climax/tension(除非剧情真有惊吓段)
        if not (genre == "children" and by_tone in ("climax",)):
            return by_tone, f"shot.tone={shot_tone}"

    # 3. narration 文本关键词
    narration = shot.get("narration", "")
    by_text = _detect_mood_from_text(narration)
    if by_text:
        return by_text, f"narration 关键词命中"

    # 4. arc_role + 题材首选
    bias_list = ARC_MOOD_BIAS.get(arc_role, [])
    preferred = GENRE_PREFERRED_MOODS.get(genre, GENRE_PREFERRED_MOODS["horror"])

    # 取 arc bias 里在题材首选范围内的最高权重项
    for mood, _w in bias_list:
        if mood in preferred:
            return mood, f"arc_role={arc_role} + genre={genre}"

    # 5. 兜底: 题材首选第一个
    return preferred[0], f"fallback genre={genre}"


def resolve_bgm_moods(shots: list,
                      story_meta: dict,
                      chapter: dict,
                      theme_name: str = "") -> list[dict]:
    """
    对一章的所有 shots 做 bgm_mood 全局裁决。

    Args:
        shots:       这一章的 shot 列表(coordinator 已处理过的)
        story_meta:  故事级元数据(title/synopsis/genre/visual_palette ...)
        chapter:     当前章信息(arc_role/tone/title ...)
        theme_name:  主题文件名(如 children_storybook)

    Returns:
        fixes 列表,每条 {shot_idx, before, after, reason},
        用于 coordinator log 报告。

    Side effects:
        直接修改 shots[i]["bgm_mood"]。
    """
    fixes = []
    genre = _detect_genre(story_meta, theme_name)
    arc_role = chapter.get("arc_role", "rising")
    chapter_tone = chapter.get("tone", "")

    prev_mood: Optional[str] = None
    consecutive_same = 0

    for i, shot in enumerate(shots):
        # extend hold / silent_beat 跟随前镜,不单独决策
        if shot.get("_hold") and (shot.get("_hold_type") or "extend") == "extend":
            shot["bgm_mood"] = prev_mood or "tension"
            continue

        before = shot.get("bgm_mood", "")
        new_mood, reason = _resolve_one_shot(
            shot, genre, arc_role,
            chapter_tone=chapter_tone,
            prev_mood=prev_mood,
        )

        # 防止连续 5 镜同一 mood: 注入一个 arc_role 范围内的兄弟 mood
        if new_mood == prev_mood:
            consecutive_same += 1
            if consecutive_same >= 4:
                # 强制切换到 arc 内第二选择
                bias_list = ARC_MOOD_BIAS.get(arc_role, [])
                alternatives = [m for m, _ in bias_list if m != new_mood
                                and m in VALID_BGM_MOODS]
                if alternatives:
                    new_mood = alternatives[0]
                    reason += " | 强制切换避免单调"
                    consecutive_same = 0
        else:
            consecutive_same = 0

        if new_mood != before:
            fixes.append({
                "shot_idx": i,
                "before":   before,
                "after":    new_mood,
                "reason":   reason,
            })
            shot["bgm_mood"] = new_mood

        prev_mood = new_mood

    return fixes


# ════════════════════════════════════════════════════════════════
# 调试 CLI
# ════════════════════════════════════════════════════════════════
# 用法:
#   python core/bgm_mood_resolver.py <story_yaml_path>
#   不会改文件,只会模拟跑一遍输出 fixes
#
# ════════════════════════════════════════════════════════════════

def _cli_main():
    import sys
    import yaml

    if len(sys.argv) < 2:
        print("Usage: python bgm_mood_resolver.py <storyboard.yaml>")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    story_meta = {
        "title":          data.get("title", ""),
        "synopsis":       data.get("synopsis", ""),
        "genre":          data.get("genre", ""),
        "visual_palette": data.get("visual_palette", ""),
        "bgm_palette":    data.get("bgm_palette", ""),
    }
    theme_name = data.get("theme", "")
    pages = data.get("pages", [])

    # 简化: 把所有 pages 当成一章处理(实际部署时按章遍历)
    fake_chapter = {"arc_role": "rising", "tone": "tension"}

    fixes = resolve_bgm_moods(pages, story_meta, fake_chapter, theme_name)

    print(f"\n题材推断: {_detect_genre(story_meta, theme_name)}")
    print(f"修复次数: {len(fixes)}")
    print()
    for fix in fixes:
        print(f"  page {fix['shot_idx']+1}: "
              f"{fix['before']:12} → {fix['after']:12} "
              f"({fix['reason']})")

    # 统计修复后的分布
    moods = [p.get("bgm_mood", "?") for p in pages]
    print("\n修复后 mood 分布:")
    from collections import Counter
    for mood, count in Counter(moods).most_common():
        print(f"  {mood:15} {count} 镜")


if __name__ == "__main__":
    _cli_main()
