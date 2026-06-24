"""
pronunciation_fix.py v2 ── 中文多音字纠正(按引擎分流)
============================================================
v1 → v2 关键变化:
  - 按 TTS 引擎选不同的修正策略,因为不同引擎认识不同的标注语法
  - edge_tts:  完全不支持自定义 SSML/拼音标注 → 走"同义词改写"
  - gpt_sovits: 支持 G2P,可走"拼音夹注"(具体语法见下)
  - 老接口 fix_pronunciation(text) 保留,默认按 sovits 走,向后兼容

为什么要分流:
  edge-tts Python 库从 5.0.0 起 Microsoft 主动封了自定义 SSML,
  只剩 <voice> + <prosody>。<phoneme> 标签不能用。
  实测加 "血(xuè)泊" 这种标注,edge-tts 会逐字念成"血 / xuè / 泊",
  不仅没纠正,反而多读了一个拼音 token。这是用户实际遇到的 bug。

  GPT-SoVITS 的 G2P 模块对夹注的处理也不统一。常见可用语法:
    a) "血{xue4}泊"     大括号 + 数字声调 (部分 fork 支持)
    b) "血[xue4]泊"     方括号 + 数字声调 (另一些 fork)
    c) "血xue4泊"       不加括号直接夹拼音 (有些版本能识别)
    d) "血泊"           直接靠 G2P 自己猜 (赌命)

  本模块用 (a),最常见。如果你的 SoVITS 版本是别的,改 _format_sovits_inline。

设计原则:
  1. 同义词改写是兜底,优先保留语义和字数
  2. 不能改写的就只能用拼音标注 + 赌引擎认识
  3. 如果引擎不认识,至少不应该比原文更糟糕(关键纪律)

用法:
  from pronunciation_fix import fix_pronunciation
  fixed = fix_pronunciation("此事已了断", engine="edge_tts")
  # → "此事已结束"
  fixed = fix_pronunciation("此事已了断", engine="gpt_sovits")
  # → "此事已了{liao3}断"
"""

import re
from typing import Optional, Literal


# ════════════════════════════════════════════════════════════════
# 修正词典
# ════════════════════════════════════════════════════════════════
#
# 每条 fix 三个字段:
#   src       原文(精确匹配的字符串)
#   sovits    GPT-SoVITS 走的形式(拼音夹注)
#   edge      Edge TTS 走的形式(同义词改写,因为它不支持拼音)
#
# 写新条目时的纪律:
#   - edge 字段一定是合法中文,不能含括号/拼音/标点
#   - edge 改写后字数尽量和 src 一致(否则会影响时长估算)
#   - sovits 字段用 "字{pinyin数字声调}字" 格式
#     声调: 1阴平 2阳平 3上声 4去声 5轻声
#
# 字典维护建议:
#   遇到一个新读错的词,跑两次手动测试:
#     1. python pronunciation_fix.py "原文" sovits
#     2. python pronunciation_fix.py "原文" edge_tts
#   听对了再加进来。

_FIXES = [
    # ── 「了」liǎo vs le ─────────────────────────────────────
    {"src": "了断",       "sovits": "了{liao3}断",      "edge": "结束"},
    {"src": "了结",       "sovits": "了{liao3}结",      "edge": "终结"},
    {"src": "了却",       "sovits": "了{liao3}却",      "edge": "了断"},  # 慎用,可能仍读 le
    {"src": "了然",       "sovits": "了{liao3}然",      "edge": "明白"},
    {"src": "了悟",       "sovits": "了{liao3}悟",      "edge": "顿悟"},
    {"src": "了如指掌",   "sovits": "了{liao3}如指掌",  "edge": "一清二楚"},
    {"src": "了不起",     "sovits": "了{liao3}不起",    "edge": "厉害"},
    {"src": "未了",       "sovits": "未了{liao3}",      "edge": "未完"},

    # ── 「还」huán vs hái ────────────────────────────────────
    {"src": "还魂",       "sovits": "还{huan2}魂",      "edge": "回魂"},
    {"src": "还阳",       "sovits": "还{huan2}阳",      "edge": "回阳"},
    {"src": "还愿",       "sovits": "还{huan2}愿",      "edge": "酬愿"},
    {"src": "还债",       "sovits": "还{huan2}债",      "edge": "偿债"},
    {"src": "归还",       "sovits": "归还{huan2}",      "edge": "归回"},
    {"src": "偿还",       "sovits": "偿还{huan2}",      "edge": "偿付"},

    # ── 「行」háng vs xíng ───────────────────────────────────
    {"src": "道行",       "sovits": "道行{heng2}",      "edge": "道法"},
    {"src": "行家",       "sovits": "行{hang2}家",      "edge": "内行人"},
    {"src": "行当",       "sovits": "行{hang2}当",      "edge": "营生"},
    {"src": "银行",       "sovits": "银行{hang2}",      "edge": "钱庄"},
    {"src": "内行",       "sovits": "内行{hang2}",      "edge": "内行人"},
    {"src": "外行",       "sovits": "外行{hang2}",      "edge": "门外汉"},

    # ── 「乐」yuè vs lè ─────────────────────────────────────
    {"src": "乐器",       "sovits": "乐{yue4}器",       "edge": "琴瑟"},
    {"src": "音乐",       "sovits": "音乐{yue4}",       "edge": "曲调"},
    {"src": "乐曲",       "sovits": "乐{yue4}曲",       "edge": "曲子"},
    {"src": "乐师",       "sovits": "乐{yue4}师",       "edge": "琴师"},

    # ── 「传」zhuàn vs chuán ──────────────────────────────────
    {"src": "传记",       "sovits": "传{zhuan4}记",     "edge": "生平"},
    {"src": "自传",       "sovits": "自传{zhuan4}",     "edge": "自述"},
    {"src": "列传",       "sovits": "列传{zhuan4}",     "edge": "列志"},
    {"src": "外传",       "sovits": "外传{zhuan4}",     "edge": "野史"},
    {"src": "水浒传",     "sovits": "水浒传{zhuan4}",   "edge": "水浒"},

    # ── 「薄」bó / báo / bò ──────────────────────────────────
    {"src": "薄荷",       "sovits": "薄{bo4}荷",        "edge": "薄荷叶"},  # bò
    {"src": "薄饼",       "sovits": "薄{bao2}饼",       "edge": "煎饼"},
    {"src": "单薄",       "sovits": "单薄{bo2}",        "edge": "瘦弱"},

    # ── 「差」chā / chà / chāi / cī ──────────────────────────
    {"src": "差遣",       "sovits": "差{chai1}遣",      "edge": "派遣"},
    {"src": "出差",       "sovits": "出差{chai1}",      "edge": "公干"},
    {"src": "差事",       "sovits": "差{chai1}事",      "edge": "差使"},
    {"src": "参差",       "sovits": "参差{ci1}",        "edge": "高低不齐"},

    # ── 「血」xuè vs xiě ────────────────────────────────────
    # 关键:用户反馈"血泊"在 edge_tts 上被念成"血xuè泊(bo)"
    # 改写策略:edge 走同义词,完全不带"血"+"泊"组合
    {"src": "血泊",       "sovits": "血{xue4}泊",       "edge": "血滩"},
    {"src": "血淋淋",     "sovits": "血{xie3}淋淋",     "edge": "鲜血淋漓"},
    {"src": "血流成河",   "sovits": "血{xue4}流成河",   "edge": "尸横遍野"},
    {"src": "血脉",       "sovits": "血{xue4}脉",       "edge": "血缘"},
    {"src": "血色",       "sovits": "血{xue4}色",       "edge": "血红"},

    # ── 「泊」bó vs pō ──────────────────────────────────────
    # 独立条:有些场景"泊"独立出现
    {"src": "湖泊",       "sovits": "湖泊{po1}",        "edge": "湖泽"},
    {"src": "停泊",       "sovits": "停泊{bo2}",        "edge": "停靠"},
    {"src": "漂泊",       "sovits": "漂泊{bo2}",        "edge": "漂流"},
    {"src": "梁山泊",     "sovits": "梁山泊{po1}",      "edge": "梁山水寨"},

    # ── 鬼故事常用 ──────────────────────────────────────────
    {"src": "招魂",       "sovits": "招魂{hun2}",       "edge": "唤魂"},
    {"src": "亡魂",       "sovits": "亡魂{hun2}",       "edge": "亡灵"},
    {"src": "冤魂",       "sovits": "冤魂{hun2}",       "edge": "怨灵"},
    {"src": "含冤",       "sovits": "含冤{yuan1}",      "edge": "蒙冤"},
    {"src": "降妖",       "sovits": "降{xiang2}妖",     "edge": "伏妖"},
    {"src": "降魔",       "sovits": "降{xiang2}魔",     "edge": "伏魔"},
    {"src": "降服",       "sovits": "降{xiang2}服",     "edge": "制服"},
    {"src": "投降",       "sovits": "投降{xiang2}",     "edge": "归降"},

    # ── 「重」zhòng vs chóng ─────────────────────────────────
    {"src": "重蹈覆辙",   "sovits": "重{chong2}蹈覆辙", "edge": "再犯老错"},
    {"src": "重逢",       "sovits": "重{chong2}逢",     "edge": "再遇"},
    {"src": "重现",       "sovits": "重{chong2}现",     "edge": "再现"},
    {"src": "重生",       "sovits": "重{chong2}生",     "edge": "再生"},

    # ── 「冠」guān vs guàn ───────────────────────────────────
    {"src": "弹冠相庆",   "sovits": "弹冠{guan1}相庆",  "edge": "举杯相庆"},
    {"src": "冠军",       "sovits": "冠{guan4}军",      "edge": "魁首"},

    # ── 「卷」juǎn vs juàn ───────────────────────────────────
    {"src": "卷宗",       "sovits": "卷{juan4}宗",      "edge": "案宗"},
    {"src": "试卷",       "sovits": "试卷{juan4}",      "edge": "考卷"},

    # ── 鬼魂场景古字 ─────────────────────────────────────────
    {"src": "处刑",       "sovits": "处{chu3}刑",       "edge": "行刑"},
    {"src": "处死",       "sovits": "处{chu3}死",       "edge": "赐死"},
    {"src": "处决",       "sovits": "处{chu3}决",       "edge": "判决"},
]


# ════════════════════════════════════════════════════════════════
# 引擎类型
# ════════════════════════════════════════════════════════════════

Engine = Literal["gpt_sovits", "edge_tts", "raw"]
#   gpt_sovits   走 sovits 字段,拼音夹注
#   edge_tts     走 edge 字段,同义词改写
#   raw          原文返回,不做任何处理(调试用)


# ════════════════════════════════════════════════════════════════
# 内部:正则编译与匹配
# ════════════════════════════════════════════════════════════════

# 按 src 长度降序,长词优先匹配(防止"血"先匹配吃掉"血泊"的机会)
_FIXES_SORTED = sorted(_FIXES, key=lambda x: len(x["src"]), reverse=True)
_FIX_PATTERN = re.compile(
    "|".join(re.escape(f["src"]) for f in _FIXES_SORTED)
)

# 引擎 → {src: 替换文本} 的映射,初始化时一次性算好
_FIX_MAP_BY_ENGINE = {
    "gpt_sovits": {f["src"]: f["sovits"] for f in _FIXES_SORTED},
    "edge_tts":   {f["src"]: f["edge"]   for f in _FIXES_SORTED},
}


# ════════════════════════════════════════════════════════════════
# 公开 API
# ════════════════════════════════════════════════════════════════

def fix_pronunciation(text: str, engine: Engine = "gpt_sovits") -> str:
    """
    对输入文本做多音字修正,按引擎选不同策略。

    Args:
        text:    原文
        engine:  "gpt_sovits" / "edge_tts" / "raw"
                 默认 gpt_sovits 保持向后兼容(v1 没有这个参数,默认相当于此)

    Returns:
        修正后的文本。如果引擎不需要修正(raw)或没匹配,返回原文。
    """
    if not text:
        return text
    if engine == "raw":
        return text
    if engine not in _FIX_MAP_BY_ENGINE:
        # 未知引擎,降级 raw,不破坏原文
        return text

    fix_map = _FIX_MAP_BY_ENGINE[engine]

    def _replace(match):
        return fix_map[match.group()]

    return _FIX_PATTERN.sub(_replace, text)


def add_fix(src: str, sovits: str, edge: str):
    """
    运行时动态添加修正规则。

    Args:
        src:    原文
        sovits: SoVITS 走的拼音夹注形式
        edge:   Edge TTS 走的同义词改写
    """
    global _FIXES_SORTED, _FIX_PATTERN, _FIX_MAP_BY_ENGINE

    _FIXES.append({"src": src, "sovits": sovits, "edge": edge})
    _FIXES_SORTED[:] = sorted(_FIXES, key=lambda x: len(x["src"]), reverse=True)
    _FIX_PATTERN = re.compile(
        "|".join(re.escape(f["src"]) for f in _FIXES_SORTED)
    )
    for eng in _FIX_MAP_BY_ENGINE:
        key = "sovits" if eng == "gpt_sovits" else "edge"
        _FIX_MAP_BY_ENGINE[eng] = {f["src"]: f[key] for f in _FIXES_SORTED}


def list_fixes(engine: Engine = "gpt_sovits") -> list:
    """
    返回当前所有 fix 在指定引擎下的 (src, replacement) 对。调试用。
    """
    if engine == "raw":
        return []
    fix_map = _FIX_MAP_BY_ENGINE.get(engine, {})
    return list(fix_map.items())


# ════════════════════════════════════════════════════════════════
# 命令行测试
# ════════════════════════════════════════════════════════════════
#
# 用法:
#   python pronunciation_fix.py                            ← 跑预设测试集
#   python pronunciation_fix.py "此事已了断" sovits         ← 单句测试
#   python pronunciation_fix.py "此事已了断" edge_tts
#   python pronunciation_fix.py --list edge_tts            ← 列出所有规则
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        eng = sys.argv[2] if len(sys.argv) >= 3 else "gpt_sovits"
        print(f"== Fixes for engine: {eng} ==")
        for src, rep in list_fixes(eng):
            print(f"  {src!r:20} → {rep!r}")
        sys.exit(0)

    if len(sys.argv) >= 2:
        text = sys.argv[1]
        eng = sys.argv[2] if len(sys.argv) >= 3 else "gpt_sovits"
        print(f"原文 [{eng}]: {text}")
        print(f"修正    : {fix_pronunciation(text, eng)}")
        sys.exit(0)

    # 默认:跑预设测试集,对比两个引擎的输出
    tests = [
        "此事已了断,不必多言。",
        "她的冤魂在此徘徊了三百年。",
        "道士精通降妖之术,道行极深。",
        "书生翻开那本列传,不禁血泊一般的往事涌上心头。",
        "客栈女主人还魂归来,只为还愿。",
        "血泊中横陈着数具尸首,血色未干。",
        "重生之后,他终于重逢了昔日故人。",
    ]
    for t in tests:
        sov = fix_pronunciation(t, "gpt_sovits")
        edg = fix_pronunciation(t, "edge_tts")
        print(f"原文      : {t}")
        if sov != t:
            print(f"  sovits  : {sov}")
        if edg != t:
            print(f"  edge_tts: {edg}")
        if sov == t and edg == t:
            print(f"  (无需修正)")
        print()
