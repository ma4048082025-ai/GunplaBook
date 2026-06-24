"""
cli.py ── 长故事生产全流程入口
====================================
封装 outline → chapter → storyboard → to_pipeline 的完整流程。
也可以分步骤单独跑。

用法：
  # 一键生成（不接入 pipeline）
  python -m long_writer.cli new \\
      --concept "民国上海法租界的旗袍鬼" \\
      --words 4000 --chapters 10 \\
      --theme chinese_horror_tales

  # 单步执行（用于人工审核中间产物）
  python -m long_writer.cli outline   --concept "..." --words 4000
  python -m long_writer.cli chapters  scripts/long_xxx_outline.yaml
  python -m long_writer.cli storyboard scripts/long_xxx_segments.yaml
  python -m long_writer.cli convert   scripts/long_xxx_storyboard.yaml

  # 人工编辑 .md 后同步
  python -m long_writer.cli sync scripts/long_xxx_outline.yaml

  # 完整流程（包括接入 pipeline，全自动，慎用）
  python -m long_writer.cli auto \\
      --concept "..." --theme chinese_horror_tales \\
      --then-twophase --then-produce
"""

import argparse
import subprocess
import sys
from pathlib import Path


# ════════════════════════════════════════════════════════════════
# v2.4.6: 启发式 seed 问询(选 theme + 抽 archetype + LLM 出反常种子)
# ════════════════════════════════════════════════════════════════

def _scrub(s):
    """清除字符串里的 surrogate code point。

    macOS/Linux 在某些终端环境下,input() 接受非 ASCII 输入时会用
    surrogateescape 把字节假装成字符串塞进来,导致后续 encode 'utf-8' 报错。
    这里把它们还原成原始字节再用 utf-8 解码。
    """
    if not isinstance(s, str):
        return s
    try:
        s.encode('utf-8')
        return s  # 干净的字符串直接返回
    except UnicodeEncodeError:
        return s.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')


def _ask(prompt: str = "  > ") -> str:
    """安全 input:自动 strip + scrub surrogate。所有 cli 问询都走这个。"""
    raw = input(prompt)
    return _scrub(raw).strip()


# ════════════════════════════════════════════════════════════════

def _themes_dir() -> Path:
    """定位 themes/ 目录(优先工程根)。"""
    # cli.py 在 tools/long_writer/ 下,工程根是 parents[2]
    here = Path(__file__).resolve()
    for parent in [here.parents[2], Path.cwd(), here.parents[1]]:
        cand = parent / "themes"
        if cand.exists() and cand.is_dir():
            return cand
    return Path.cwd() / "themes"   # 兜底


def _load_theme_yaml(theme_id: str) -> dict:
    """读单个 theme yaml,失败返回空 dict。"""
    import yaml
    path = _themes_dir() / f"{theme_id}.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"  ⚠ 读取 theme {theme_id} 失败: {e}")
        return {}


def _list_themes_with_summary() -> list:
    """扫描 themes/ 下非 .bak 的 yaml,返回 [{id, name, desc}, ...]"""
    out = []
    for path in sorted(_themes_dir().glob("*.yaml")):
        if path.name.endswith(".bak"):
            continue
        theme = _load_theme_yaml(path.stem)
        out.append({
            "id":   path.stem,
            "name": theme.get("name", path.stem),
            "desc": theme.get("description", ""),
        })
    return out


def _pick_theme() -> str:
    """让用户从可用 theme 列表里选一个,返回 theme_id。"""
    print()
    print("  " + "═" * 56)
    print("  第 1 步: 选个主题")
    print("  " + "═" * 56)
    print()
    themes = _list_themes_with_summary()
    if not themes:
        print("  ⚠ 未找到 themes/ 目录或无可用 theme,用默认 chinese_horror_tales")
        return "chinese_horror_tales"

    for i, t in enumerate(themes, 1):
        print(f"  {i}) {t['name']}  ({t['id']})")
        if t['desc']:
            print(f"      {t['desc']}")
    print()
    while True:
        choice = _ask(f"  输入 1-{len(themes)} > ")
        if choice.isdigit() and 1 <= int(choice) <= len(themes):
            return themes[int(choice) - 1]["id"]
        print("  请输入有效编号")


def _pick_archetypes(theme: dict) -> list:
    """从 theme 的 storyboard.character_archetypes 里勾选角色类型。

    返回选中的 [{key, en_desc}] 列表;用户也可以选 0 = 自己想。
    """
    archetypes = (theme.get("storyboard", {}) or {}).get("character_archetypes", {}) or {}
    if not archetypes:
        return []

    print()
    print("  " + "═" * 56)
    print("  第 2 步: 主角类型 (可多选)")
    print("  " + "═" * 56)
    print()
    print("  这个主题里常见的角色原型 —— 用逗号分隔多选,或 0 跳过自己想:")
    print()
    keys = list(archetypes.keys())
    for i, k in enumerate(keys, 1):
        en = archetypes[k]
        # 截短英文描述显示
        en_short = en if len(en) <= 80 else en[:77] + "..."
        print(f"  {i}) {k}")
        print(f"      {en_short}")
    print(f"  0) 跳过 —— 我自己想角色,LLM 别预设")
    print()

    while True:
        raw = _ask(f"  选 (例: 1,3 / 1 / 0) > ")
        if raw == "0":
            return []
        try:
            idx_list = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if all(1 <= i <= len(keys) for i in idx_list):
                return [{"key": keys[i-1], "en_desc": archetypes[keys[i-1]]}
                        for i in idx_list]
        except ValueError:
            pass
        print("  请输入有效编号(如 1 / 1,3 / 0)")


def _pick_era(theme: dict) -> str:
    """从 theme 的 storyboard.natural_era_words 里选时代。"""
    era_words = (theme.get("storyboard", {}) or {}).get("natural_era_words", []) or []
    if not era_words:
        # 没有 era 配置就跳过
        return ""

    print()
    print("  " + "═" * 56)
    print("  第 3 步: 时代背景")
    print("  " + "═" * 56)
    print()
    for i, w in enumerate(era_words, 1):
        print(f"  {i}) {w}")
    print(f"  0) 不限/自填")
    print()
    while True:
        raw = _ask(f"  输入 0-{len(era_words)} > ")
        if raw == "0":
            custom = _ask("  自己写时代背景(回车=不限): ")
            return custom
        if raw.isdigit() and 1 <= int(raw) <= len(era_words):
            return era_words[int(raw)-1]
        print("  请输入有效编号")


def _ask_seed_idea() -> str:
    """问用户的一句话故事种子(强制必填)。"""
    print()
    print("  " + "═" * 56)
    print("  第 4 步: 用一两句话描述你想写的故事")
    print("  " + "═" * 56)
    print()
    print("  (模糊也行,后面 LLM 会帮你具体化)")
    while True:
        raw = _ask("  > ")
        if raw:
            return raw
        print("  (这一项必填)")


def _generate_weirdness_candidates(theme_id: str,
                                    archetypes: list,
                                    era: str,
                                    seed: str,
                                    n: int = 5) -> list:
    """让 LLM 根据 theme + 用户已选信息,生成 n 个"反常种子"候选。

    返回 list[str],每条是一个具体的反常元素描述。
    失败时返回 [] (走兜底)。
    """
    try:
        from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except Exception as e:
        print(f"  ⚠ LLM 模块加载失败,跳过反常种子生成: {e}")
        return []

    arch_desc = ""
    if archetypes:
        arch_desc = "用户已经选了这些角色类型: " + ", ".join(
            f"{a['key']}" for a in archetypes)

    prompt = f"""你是中文恐怖/悬疑/奇幻短视频的资深编剧。
用户想写一个故事,核心信息如下:

  主题风格: {theme_id}
  时代背景: {era or '不限'}
  {arch_desc}
  用户原始想法: {seed}

请你为这个故事生成 {n} 个"反常种子" —— 一个具体的、奇怪的、能成为整个故事钩子的物件/事件/场景。
要求:
  1. 每个种子必须具体到能想象出画面 (不是"诡异的氛围"这种抽象描述)
  2. 互不相同,代表不同的钩子方向 (物件 / 怪事 / 场景 / 人物特征)
  3. 跟用户的原始想法、时代、角色类型有关联,但要带反常元素
  4. 长度: 每个 15-30 字
  5. 不要套路化 (不要"老宅闹鬼""井底白衣女鬼")

输出严格 JSON 数组(无 markdown 围栏):
[
  "种子1",
  "种子2",
  "种子3",
  "种子4",
  "种子5"
]
只返回 JSON 数组,不要其他文字。"""

    try:
        llm = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY,
                         base_url=LLM_BASE_URL, temperature=0.9)
        raw = llm.invoke([HumanMessage(content=prompt)]).content
        import json, re
        # 剥围栏
        text = raw.strip()
        if text.startswith("```"):
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if m:
                text = m.group(0)
        data = json.loads(text)
        if isinstance(data, list) and all(isinstance(x, str) for x in data):
            return data
    except Exception as e:
        print(f"  ⚠ 反常种子生成失败: {e}")
    return []


def _pick_weirdness(theme_id: str,
                    archetypes: list,
                    era: str,
                    seed: str) -> str:
    """生成反常种子候选,让用户选一个或自己填。"""
    print()
    print("  " + "═" * 56)
    print("  第 5 步: 反常种子 (整个故事的钩子)")
    print("  " + "═" * 56)
    print()
    print("  这一步决定故事是不是套路。我让 LLM 根据你已选的信息生成几个候选 ...")

    candidates = _generate_weirdness_candidates(
        theme_id, archetypes, era, seed)
    if not candidates:
        print("  (候选生成失败,直接让你自己填)")
        return _ask("\n  反常种子(具体的物件/怪事/场景): ")

    print()
    for i, c in enumerate(candidates, 1):
        print(f"  {i}) {c}")
    print(f"  0) 都不满意,我自己想")
    print()
    while True:
        raw = _ask(f"  选 0-{len(candidates)} > ")
        if raw == "0":
            custom = _ask("  自己写: ")
            return custom
        if raw.isdigit() and 1 <= int(raw) <= len(candidates):
            return candidates[int(raw)-1]
        print("  请输入有效编号")


# 时代感姓氏池(打破"陈远正"均值,可根据 era 调整)
_NAME_HINT_POOLS = {
    "republic": "沈/谢/陆/姜/苏/林/江/裴/宋/顾",   # 民国
    "ancient":  "司马/欧阳/慕容/百里/独孤/上官/沈/谢/陆/苏",
    "modern":   "韩/秦/夏/方/江/陆/沈",
    "default":  "沈/谢/陆/姜/苏/林/江/裴/宋/顾",
}


def _name_hint_for_era(era: str) -> str:
    if not era:
        return _NAME_HINT_POOLS["default"]
    e = era.lower()
    if "republic" in e or "1920" in e or "1930" in e or "民国" in era:
        return _NAME_HINT_POOLS["republic"]
    if "ancient" in e or "dynasty" in e or "tang" in e or "ming" in e or "qing" in e:
        return _NAME_HINT_POOLS["ancient"]
    if "1980" in e or "1990" in e or "modern" in e:
        return _NAME_HINT_POOLS["modern"]
    return _NAME_HINT_POOLS["default"]


def _build_rich_concept(theme_id: str,
                        seed: str,
                        archetypes: list,
                        era: str,
                        weirdness: str) -> str:
    """把所有启发式收集的信息拼成一个信息密度高的 concept 字符串。

    这个字符串会作为 generate_outline(concept=...) 的输入,
    LLM 看到具体细节后会跳出"陈远正/林月"这种均值化命名。
    """
    parts = [seed]

    if era:
        parts.append(f"时代背景: {era}")

    if archetypes:
        arch_strs = []
        for a in archetypes:
            arch_strs.append(f"{a['key']}({a['en_desc']})")
        parts.append("核心角色类型应包含: " + "; ".join(arch_strs))

    if weirdness:
        parts.append(f"故事的关键反常元素(必须在前 1-2 章引入): {weirdness}")

    name_hint = _name_hint_for_era(era)
    parts.append(
        f"主角姓氏应从以下时代感姓氏中选: {name_hint}。"
        f"不要用陈/李/王/张这种过于普遍的姓,主角名要有个性,避免'陈远正''林月'这类均值化命名。"
    )

    return "。".join(parts) + "。"


def _interactive_seed(default_words: int, default_chapters: int) -> dict:
    """完整启发式 seed 流程,返回喂给 generate_outline 的字典。"""
    print()
    print("  " + "═" * 60)
    print("  欢迎进入对话式大纲生成 (v2.4.6 启发式 seed)")
    print("  " + "═" * 60)
    print()
    print("  接下来我会问你 5 个问题来锁定故事的骨架,")
    print("  然后调 LLM 生成初稿大纲,再进入对话精炼。")

    # 1. theme
    theme_id = _pick_theme()
    theme = _load_theme_yaml(theme_id)

    # 2. archetypes
    archetypes = _pick_archetypes(theme)

    # 3. era
    era = _pick_era(theme)

    # 4. seed idea
    seed = _ask_seed_idea()

    # 5. weirdness (LLM 出候选)
    weirdness = _pick_weirdness(theme_id, archetypes, era, seed)

    # 6. 字数/章数
    print()
    print("  " + "═" * 56)
    print("  最后 2 个粗粒度参数")
    print("  " + "═" * 56)
    print()
    raw_w = _ask(f"  目标字数 [回车={default_words}]: ")
    raw_c = _ask(f"  章节数   [回车={default_chapters}]: ")
    try:
        words = int(raw_w) if raw_w else default_words
    except ValueError:
        words = default_words
    try:
        chapters = int(raw_c) if raw_c else default_chapters
    except ValueError:
        chapters = default_chapters

    concept = _build_rich_concept(theme_id, seed, archetypes, era, weirdness)

    # 7. 总结确认
    print()
    print("  " + "═" * 60)
    print("  确认信息")
    print("  " + "═" * 60)
    print(f"  主题:     {theme_id}")
    print(f"  时代:     {era or '(不限)'}")
    print(f"  角色原型: {', '.join(a['key'] for a in archetypes) or '(LLM 自决)'}")
    print(f"  原始想法: {seed}")
    print(f"  反常种子: {weirdness}")
    print(f"  字数/章数: {words} / {chapters}")
    print()
    print("  拼好的富 concept (LLM 实际收到的):")
    print(f"  ┌{'─' * 58}")
    for line in concept.split("。"):
        line = line.strip()
        if line:
            print(f"  │ {line}。")
    print(f"  └{'─' * 58}")
    print()
    confirm = _ask("  开始生成大纲? [Y/n] > ").lower()
    if confirm in ("n", "no", "不", "q", "quit"):
        print("  取消")
        sys.exit(0)

    return {
        "concept":  concept,
        "theme":    theme_id,
        "words":    words,
        "chapters": chapters,
    }


# ════════════════════════════════════════════════════════════════
# v2.4.6 启发式 seed 模块结束
# ════════════════════════════════════════════════════════════════


def cmd_outline(args):
    # v2.4.5 (interactive): --interactive flag → 进入对话式精炼
    # v2.4.6 (interactive): 加 --from-outline / 启发式 seed
    # 默认行为完全跟旧版一致(一次性生成),零回归
    if getattr(args, "interactive", False):
        from pathlib import Path as _P
        import yaml as _yaml
        from long_writer.creator_agent import CreatorAgent, SimpleLLMEngine
        from long_writer.creator_agent.facets import OutlineFacet

        facet = OutlineFacet()
        from_path = getattr(args, "from_outline", None)
        initial_source = None
        story_id = ""

        if from_path:
            # ── 模式 A:在已有 outline 上对话 ──
            src = _P(from_path)
            if not src.exists():
                print(f"  ⚠ 文件不存在: {src}")
                sys.exit(1)
            print(f"  [outline] 加载已有大纲: {src}")
            with open(src, "r", encoding="utf-8") as f:
                existing = _yaml.safe_load(f)
            story_id = existing.get("story_id", "")
            initial_source = src
            # facet 会通过 initial_source 直接 load_state,不需要 seed_*

        elif not args.concept:
            # ── 模式 B:没传 --concept → 启发式问询 ──
            seed_info = _interactive_seed(args.words, args.chapters)
            facet.seed_concept  = seed_info["concept"]
            facet.seed_theme    = seed_info["theme"]
            facet.seed_words    = seed_info["words"]
            facet.seed_chapters = seed_info["chapters"]
            facet.seed_series   = args.series

        else:
            # ── 模式 C:传了 --concept(老用法,直接用) ──
            facet.seed_concept  = args.concept
            facet.seed_theme    = args.theme
            facet.seed_words    = args.words
            facet.seed_chapters = args.chapters
            facet.seed_series   = args.series

        agent = CreatorAgent(facet=facet, engine=SimpleLLMEngine())
        agent.run(initial_source=initial_source, story_id=story_id)
        return

    # ── 非交互式路径(完全不动旧行为) ──
    from long_writer.outline import generate_outline
    if not args.concept:
        print("  ⚠ 必须传 --concept,或加 -i 进入对话式")
        sys.exit(1)
    generate_outline(
        concept        = args.concept,
        total_words    = args.words,
        chapters_count = args.chapters,
        theme_id       = args.theme,
        series         = args.series,
    )


def cmd_chapters(args):
    # v2.4.5 (interactive): --interactive flag → 进入对话式精炼
    # 精炼完成后,自动跑 chapter_writer(sync_from_md=True)
    # 把 .md 同步成 segments.yaml,再走 doctor 流水线。
    # doctor 完全无感(它收到的就是 segments.yaml)。
    if getattr(args, "interactive", False):
        from pathlib import Path as _P
        from long_writer.creator_agent import CreatorAgent, SimpleLLMEngine
        from long_writer.creator_agent.facets import ChapterFacet
        from long_writer.chapter_writer import write_all_chapters

        facet = ChapterFacet()
        facet.only_chapter = getattr(args, "chapter", None)

        agent = CreatorAgent(facet=facet, engine=SimpleLLMEngine())
        agent.run(initial_source=_P(args.outline))

        # 对话结束 → 用 chapter_writer 把 .md 同步成 segments.yaml + 跑 doctor
        # (doctor 默认开启,跟原 cmd_chapters 一致)
        print("\n  [interactive] 对话结束,同步 .md → segments.yaml + 跑 doctor 审稿...")
        enabled_doctors = None
        if getattr(args, "doctors", None):
            enabled_doctors = [r.strip() for r in args.doctors.split(",") if r.strip()]
        write_all_chapters(
            outline_path     = args.outline,
            sync_from_md     = True,
            enable_doctor    = not getattr(args, "no_doctor", False),
            enabled_doctors  = enabled_doctors,
            enable_structural= not getattr(args, "no_structural", False),
            doctor_only      = getattr(args, "doctor_only", None),
        )
        return

    from long_writer.chapter_writer import write_all_chapters
    enabled_doctors = None
    if getattr(args, "doctors", None):
        enabled_doctors = [r.strip() for r in args.doctors.split(",") if r.strip()]
    write_all_chapters(
        outline_path     = args.outline,
        only_chapter     = args.chapter,
        force            = args.force,
        sync_from_md     = False,
        enable_doctor    = not getattr(args, "no_doctor", False),
        enabled_doctors  = enabled_doctors,
        enable_structural= not getattr(args, "no_structural", False),
        doctor_only      = getattr(args, "doctor_only", None),
    )


def _parse_reviewers(reviewers_arg: str):
    """共用：把 --reviewers 字符串解析成列表。None 表示全开。"""
    if not reviewers_arg:
        return None
    return [r.strip() for r in reviewers_arg.split(",") if r.strip()]


def cmd_storyboard(args):
    from long_writer.long_storyboard import generate_storyboard
    generate_storyboard(
        args.segments,
        max_dynamic_total = args.max_dynamic,
        enable_review     = not args.no_review,
        enabled_reviewers = _parse_reviewers(args.reviewers),
        review_only       = args.review_only,
    )


def cmd_convert(args):
    from long_writer.to_pipeline import convert_to_pipeline
    convert_to_pipeline(args.storyboard, args.output)


# ════════════════════════════════════════════════════════════════
# v2.3.5：portraits 三个子命令（定妆照生成 + 挑选 + 固化）
# ════════════════════════════════════════════════════════════════

def cmd_portraits_generate(args):
    """为故事 lead 角色生成定妆照候选 (v2.3.5 同步)"""
    from long_writer.portraits import cmd_generate as _gen
    _gen(args)   # v2.3.5：portraits.cmd_generate 已改成同步函数


def cmd_portraits_list(args):
    from long_writer.portraits import cmd_list as _list
    _list(args)


def cmd_portraits_pick(args):
    from long_writer.portraits import cmd_pick as _pick
    _pick(args)


def cmd_sync(args):
    from long_writer.chapter_writer import write_all_chapters
    write_all_chapters(args.outline, sync_from_md=True)


def cmd_new(args):
    """outline → chapters → storyboard → convert，一气呵成（中间不停）"""
    from long_writer.outline           import generate_outline
    from long_writer.chapter_writer    import write_all_chapters
    from long_writer.long_storyboard   import generate_storyboard
    from long_writer.to_pipeline       import convert_to_pipeline

    print("\n" + "="*60)
    print("  长故事生产流程：四步全自动")
    print("="*60)

    outline = generate_outline(
        concept        = args.concept,
        total_words    = args.words,
        chapters_count = args.chapters,
        theme_id       = args.theme,
        series         = args.series,
    )
    story_id = outline["story_id"]
    outline_path = f"scripts/{story_id}_outline.yaml"

    if not args.skip_review_pause:
        print("\n  ⚠ 暂停：请审核大纲后按回车继续，或 Ctrl+C 中止")
        input()

    enabled_doctors = None
    if getattr(args, "doctors", None):
        enabled_doctors = [r.strip() for r in args.doctors.split(",") if r.strip()]
    write_all_chapters(
        outline_path     = outline_path,
        enable_doctor    = not getattr(args, "no_doctor", False),
        enabled_doctors  = enabled_doctors,
        enable_structural= not getattr(args, "no_structural", False),
        doctor_only      = getattr(args, "doctor_only", None),
    )
    seg_path = f"scripts/{story_id}_segments.yaml"

    if not args.skip_review_pause:
        print("\n  ⚠ 暂停：请审核 .md 主稿后按回车继续，或 Ctrl+C 中止")
        print(f"     可编辑 scripts/{story_id}.md，然后跑 sync 同步")
        input()

    generate_storyboard(
        seg_path,
        max_dynamic_total = args.max_dynamic,
        enable_review     = not getattr(args, "no_review", False),
        enabled_reviewers = _parse_reviewers(getattr(args, "reviewers", None)),
        review_only       = getattr(args, "review_only", None),
    )
    sb_path = f"scripts/{story_id}_storyboard.yaml"

    pipeline_yaml = convert_to_pipeline(sb_path)

    print("\n" + "="*60)
    print("  ✓ 长故事完成，已就绪进入主管线:")
    print(f"    python run.py twophase {pipeline_yaml}")
    print(f"    python run.py produce  {pipeline_yaml} --platform douyin "
          f"--sovits http://YOUR_SOVITS_HOST:9880")
    print("="*60)


def cmd_auto(args):
    """new + 自动接入 twophase + produce"""
    cmd_new(args)
    # 找到最新的 stories/long_*.yaml
    latest = max(Path("stories").glob("long_*.yaml"),
                 key=lambda p: p.stat().st_mtime)
    print(f"\n  → 自动进入 twophase: {latest}")
    if args.then_twophase:
        subprocess.run(["python", "run.py", "twophase", str(latest)])
    if args.then_produce:
        cmd = ["python", "run.py", "produce", str(latest),
               "--platform", args.platform]
        if args.sovits:
            cmd += ["--sovits", args.sovits]
        subprocess.run(cmd)


def _add_review_args(p):
    """共用：给 storyboard / new / auto 子命令加审稿参数"""
    p.add_argument("--no-review", action="store_true",
                   help="禁用审稿（默认开启 4 个审稿员）")
    p.add_argument("--reviewers", default=None,
                   help="审稿员白名单，逗号分隔。可选: "
                        "narrative,visual,flux,dialogue。默认全开")
    p.add_argument("--review-only", default=None,
                   help="只重审某章（如 'ch03'），其他章节读已有缓存")


def _add_doctor_args(p):
    """共用：给 chapters / new / auto 子命令加编剧大师参数"""
    p.add_argument("--no-doctor", action="store_true",
                   help="禁用编剧大师审稿（v2.3 默认开启）")
    p.add_argument("--doctors", default=None,
                   help="D 层启用列表，逗号分隔。可选: "
                        "continuity,logic,rhythm,dialogue。默认全开")
    p.add_argument("--no-structural", action="store_true",
                   help="禁用 A 层结构编辑")
    p.add_argument("--doctor-only", default=None,
                   help="只重审某章（如 'ch03'）")


def main():
    parser = argparse.ArgumentParser(
        description="长故事/唱故事生产线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── outline ──────────────────────────────────────
    p = sub.add_parser("outline", help="生成大纲")
    # v2.4.6: --concept 非必填(交互式可不传,走启发式问询)
    p.add_argument("--concept",  required=False, default=None,
                   help="一句话故事概念(交互式模式可不传,会走启发式问询)")
    p.add_argument("--words",    type=int, default=4000)
    p.add_argument("--chapters", type=int, default=10)
    p.add_argument("--theme",    default="chinese_horror_tales")
    p.add_argument("--series",   default="long_tales")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="(v2.4.5) 进入对话式精炼大纲(LLM 给建议+多轮迭代)")
    p.add_argument("--from-outline", default=None,
                   help="(v2.4.6) 基于已有 *_outline.yaml 继续对话,不重新生成")
    p.set_defaults(func=cmd_outline)

    # ── chapters ─────────────────────────────────────
    p = sub.add_parser("chapters", help="生成章节正文（v2.3 默认含编剧大师）")
    p.add_argument("outline")
    p.add_argument("--chapter", default=None, help="只重写指定章节")
    p.add_argument("--force",   action="store_true")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="(v2.4.5) 对话式章节精修(LLM 给建议+多轮迭代后接 doctor)")
    _add_doctor_args(p)
    p.set_defaults(func=cmd_chapters)

    # ── storyboard ───────────────────────────────────
    p = sub.add_parser("storyboard", help="生成分镜（v2.2 默认含审稿）")
    p.add_argument("segments")
    p.add_argument("--max-dynamic", type=int, default=5)
    _add_review_args(p)
    p.set_defaults(func=cmd_storyboard)

    # ── convert ──────────────────────────────────────
    p = sub.add_parser("convert", help="转换为 pipeline 格式")
    p.add_argument("storyboard")
    p.add_argument("--output", default=None)
    p.set_defaults(func=cmd_convert)

    # ── portraits（v2.3.5 新增）─────────────────────
    # convert 之后、twophase 之前的人工挑选定妆照工序
    p = sub.add_parser("portraits",
                       help="（v2.3.5）为 lead 角色生成定妆照候选（PuLid 用）")
    p.add_argument("story_yaml")
    p.add_argument("-n", "--n-candidates", type=int, default=4)
    p.add_argument("--character", default=None,
                   help="只为指定角色生成（默认全部 lead）")
    # v2.8: 默认生所有角色(含 extras)。要排除 extras 用 --no-extras
    p.add_argument("--no-extras", action="store_true",
                   help="不给 extra 配角生成(默认全部生成)")
    # 兼容旧参数: --include-extras 仍可用,无作用(默认就开)
    p.add_argument("--include-extras", action="store_true",
                   help="(已默认开启,保留向后兼容)")
    p.add_argument("--force", action="store_true",
                   help="即使已有 portrait_ref 也重新生成")
    p.set_defaults(func=cmd_portraits_generate)

    p = sub.add_parser("portraits_list",
                       help="（v2.3.5）看候选定妆照 + 已选状态")
    p.add_argument("story_yaml")
    p.set_defaults(func=cmd_portraits_list)

    p = sub.add_parser("portraits_pick",
                       help="（v2.3.5）固化某候选为正式定妆照 + 写回 outline")
    p.add_argument("story_yaml")
    p.add_argument("--character", required=True)
    p.add_argument("--pick", required=True, help="候选编号（如 v3）")
    p.set_defaults(func=cmd_portraits_pick)

    # ── sync ──────────────────────────────────────────
    p = sub.add_parser("sync", help="人工编辑 md 后同步到 segments.yaml")
    p.add_argument("outline")
    p.set_defaults(func=cmd_sync)

    # ── new ──────────────────────────────────────────
    p = sub.add_parser("new",  help="一键全流程（半自动，中间停顿审核）")
    p.add_argument("--concept", required=True)
    p.add_argument("--words",       type=int, default=4000)
    p.add_argument("--chapters",    type=int, default=10)
    p.add_argument("--theme",       default="chinese_horror_tales")
    p.add_argument("--series",      default="long_tales")
    p.add_argument("--max-dynamic", type=int, default=5)
    p.add_argument("--skip-review-pause", action="store_true",
                   help="跳过暂停审核（全自动）")
    _add_doctor_args(p)
    _add_review_args(p)
    p.set_defaults(func=cmd_new)

    # ── auto ─────────────────────────────────────────
    p = sub.add_parser("auto", help="full chain 含 twophase/produce")
    p.add_argument("--concept", required=True)
    p.add_argument("--words",       type=int, default=4000)
    p.add_argument("--chapters",    type=int, default=10)
    p.add_argument("--theme",       default="chinese_horror_tales")
    p.add_argument("--series",      default="long_tales")
    p.add_argument("--max-dynamic", type=int, default=5)
    p.add_argument("--platform",    default="douyin")
    p.add_argument("--sovits",      default="")
    p.add_argument("--then-twophase", action="store_true")
    p.add_argument("--then-produce",  action="store_true")
    p.add_argument("--skip-review-pause", action="store_true", default=True)
    _add_doctor_args(p)
    _add_review_args(p)
    p.set_defaults(func=cmd_auto)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
