"""
run.py ── 命令行入口（精简版）
================================
保留命令：
  twophase  两阶段生图（主流程）
  video     从已生成资产出视频
  status    查看生图进度
  reset     清空注册表
  inventory 查看 ComfyUI 模型清单

用法：
  python run.py twophase stories/haunted_inn.yaml
  python run.py twophase stories/haunted_inn.yaml --resume
  python run.py twophase stories/haunted_inn.yaml --pages 7
  python run.py twophase stories/haunted_inn.yaml --pages 5,6,7
  python run.py produce    stories/haunted_inn.yaml
  python run.py status   stories/haunted_inn.yaml
  python run.py reset    stories/haunted_inn.yaml
  python run.py inventory
"""

# ─── 抑制 comfy-script _watch 的已知噪音（不影响功能）───────────
import sys as _sys2

class _StderrFilter:
    """过滤 comfy_script 的 _set_node_progress 噪音；其他 stderr 原样输出。"""
    _NOISE_KEYS = (
        "_set_node_progress",
        "Failed to watch, will retry",
        "comfy_script/runtime/__init__.py",
    )
    _IN_NOISE_BLOCK = False

    def __init__(self, real):
        self._real = real
        self._buf = ""

    def write(self, s):
        self._buf += s
        # 按行处理；不足一行的留在缓冲里
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._handle_line(line + "\n")

    def _handle_line(self, line):
        # 进入噪音块的标志
        if "Failed to watch, will retry" in line or "_set_node_progress" in line:
            self._IN_NOISE_BLOCK = True
            return
        # traceback 后续行（缩进开头 / Traceback 行 / AttributeError 行）
        if self._IN_NOISE_BLOCK:
            if (line.startswith(("Traceback", "  File", "    ", "AttributeError"))
                    or "comfy_script/runtime/__init__.py" in line):
                return
            # 空行或正常内容 → 噪音块结束
            self._IN_NOISE_BLOCK = False
        self._real.write(line)

    def flush(self):
        if self._buf:
            self._real.write(self._buf)
            self._buf = ""
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)

_sys2.stderr = _StderrFilter(_sys2.stderr)
# ────────────────────────────────────────────────────────────────
# ─── 三层架构 sys.path 注入（migrate.py 自动添加）────────────────
import sys as _sys
import os as _os
_root = _os.path.dirname(_os.path.abspath(__file__))
for _sub in ("infra", "core", "tools"):
    _p = _os.path.join(_root, _sub)
    if _os.path.isdir(_p) and _p not in _sys.path:
        _sys.path.insert(0, _p)
if _root not in _sys.path:
    _sys.path.insert(0, _root)
# ────────────────────────────────────────────────────────────────


import sys
import argparse


# ── 初始化（连接 ComfyUI）────────────────────────────────────

def _init(story_path: str, theme_path: str = None, no_ip: bool = False):
    from comfy_script.runtime import load
    from config import COMFY_SERVER, SOVITS_URL
    from pipeline import set_skip_ipadapter

    print(f"连接 ComfyUI...")
    load(COMFY_SERVER)
    print(f"OK")

    if no_ip:
        set_skip_ipadapter(True)
        print(f"  IP-Adapter 已禁用（--no-ip）")


def _load_configs(story_path: str, theme_path: str = None):
    from theme import ThemeConfig
    from story import StoryConfig

    story  = StoryConfig(story_path)
    tpath  = theme_path or story.default_theme_path
    theme  = ThemeConfig(tpath)

    print(f"  主题: {theme.name}  ({tpath})")
    print(f"  故事: {story.title}  ({story_path})")
    # v2.3 长篇副线程检测
    try:
        _is_long = getattr(story, "is_long_story", False)
        _hold_n = sum(1 for _p in story.pages if _p.get("_hold"))
        _skip_n = sum(1 for _p in story.pages if _p.get("_skip_llm_alignment"))
        if _is_long or _hold_n or _skip_n:
            print(f"\n  [v2.3] 检测到长篇副线程：{len(story.pages)} 页"
                  f"（{_skip_n} 走快速通道，{_hold_n} 个 hold）\n")
    except Exception:
        pass

    return theme, story


# ── 命令：twophase ───────────────────────────────────────────

def cmd_two_phase(story_path: str,
                  theme_path: str  = None,
                  no_ip:      bool = False,
                  resume:     bool = False,
                  pages             = None):
    """
    两阶段生产：
      Phase 1  ComfyUI 常驻，逐页各生 1 张，不评分
      Phase 2a LLaVA 加载，全员评分，失败页进重生队列
      Phase 2b agent+feedback 合并参数，生 4 张候选
      Phase 2c LLaVA 重加载，候选评分 + 同分比较选优

    用法：
      python run.py twophase stories/xxx.yaml
      python run.py twophase stories/xxx.yaml --resume
      python run.py twophase stories/xxx.yaml --pages 7
      python run.py twophase stories/xxx.yaml --pages 5,6,7
    """
    _init(story_path, theme_path, no_ip)
    theme, story = _load_configs(story_path, theme_path)

    # ── 页面过滤 ─────────────────────────────────────────────
    if pages is not None:
        if isinstance(pages, int):
            page_nums = [pages]
        else:
            page_nums = [int(x.strip()) for x in pages.split(",")]
        original_count = len(story.pages)
        story.pages = [pg for pg in story.pages if pg["page"] in page_nums]
        print(f"  [页面过滤] 指定页面: {page_nums} → 实际处理: {len(story.pages)}/{original_count} 页")
        if not story.pages:
            print(f"  !! 没有匹配的页面，退出")
            return

    from pipeline import Pipeline
    p = Pipeline(theme, story, mode="auto")
    p.run_two_phase(resume=resume)





# ── 命令：produce（v2 视频生产）──────────────────────────────

def cmd_produce(story_path: str,
                theme_path: str = None,
                platform: str = "youtube",
                sovits_host: str = "",
                bgm: str = "",
                wan_model: str = "1.3B",
                max_dynamic: int = 5,
                no_ip: bool = False,
                resume: bool = True):
    """
    完整视频生产：motion决策 → AI视频clip → 组装最终视频。
    需要先运行 twophase 生成图片。

    用法：
      python run.py produce stories/haunted_inn.yaml
      python run.py produce stories/haunted_inn.yaml --platform youtube
      python run.py produce stories/haunted_inn.yaml --sovits http://YOUR_SOVITS_HOST:9880
      python run.py produce stories/haunted_inn.yaml --wan-model 14B
      python run.py produce stories/haunted_inn.yaml --bgm assets/bgm/ghost_theme.mp3
    """
    _init(story_path, theme_path, no_ip)
    if not sovits_host:
        sovits_host = SOVITS_URL
        print(f"  [produce] 未指定 --sovits，使用默认地址: {sovits_host}")
    theme, story = _load_configs(story_path, theme_path)

    from pipeline import Pipeline
    p = Pipeline(theme, story, mode="auto")

    # 检查是否有 APPROVED 资产
    from registry import Registry
    reg = Registry(story.path)
    approved = reg.all_approved(story.story_id)
    if not approved:
        print("  !! 没有 APPROVED 资产，请先运行 twophase")
        return

    print(f"  已有 {len(approved)} 页 APPROVED 资产")

    video_path = p.make_video_v2(
        platform=platform,
        sovits_host=sovits_host,
        bgm_path=bgm,
        wan_model=wan_model,
        max_dynamic=max_dynamic,
        resume=resume,
    )

    if video_path:
        print(f"\n  最终视频: {video_path}")

# ════════════════════════════════════════
# cmd: write — 写故事（接 story_writer，支持单个/批量）
# ════════════════════════════════════════

def cmd_write(concept: str = "",
              concepts_file: str = "",
              batch: int = 1,
              theme_path: str = None,
              pages: int = 8,
              series: str = "",
              review_mode: str = "human",
              offline: bool = False,
              output: str = ""):
    """
    写故事入口。三种模式：
      1. 单个故事:  cmd_write(concept="...", batch=1)
      2. 批量变体:  cmd_write(concept="...", batch=3)
      3. 批量列表:  cmd_write(concepts_file="concepts.txt")

    输出路径自动生成 stories/auto_<时间戳>_<slug>.yaml
    """
    from story_writer import (create_story, create_story_batch,
                                DEFAULT_THEME)
    theme_path = theme_path or DEFAULT_THEME

    # 模式 1：concepts_file 批量
    if concepts_file:
        from pathlib import Path as _P
        cf = _P(concepts_file)
        if not cf.exists():
            print(f"  !! concepts_file 不存在: {cf}")
            return None
        concepts = [line.strip() for line in
                     cf.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.strip().startswith("#")]
        if not concepts:
            print(f"  !! concepts_file 为空")
            return None
        print(f"  从 {cf} 读取 {len(concepts)} 个 concept，批量生成")
        return create_story_batch(
            concepts=concepts,
            theme_path=theme_path,
            pages=pages,
            series=series,
            review_mode=review_mode,
            offline=offline,
        )

    # 模式 2：单 concept + batch>1（生成变体）
    if not concept:
        print(f"  !! write 需要 --concept 或 --concepts-file")
        return None

    if batch > 1:
        concepts = [f"{concept}（第{i+1}个变体）" for i in range(batch)]
        print(f"  --batch {batch}：基于 '{concept}' 生成 {batch} 个变体")
        return create_story_batch(
            concepts=concepts,
            theme_path=theme_path,
            pages=pages,
            series=series,
            review_mode=review_mode,
            offline=offline,
        )

    # 模式 3：单个故事
    return create_story(
        concept=concept,
        theme_path=theme_path,
        pages=pages,
        output_path=output or None,
        series=series,
        review_mode=review_mode,
        offline=offline,
    )


# ════════════════════════════════════════
# cmd: status
# ════════════════════════════════════════

def cmd_status(story_path: str, theme_path: str = None):
    """查看生图进度和资产状态。"""
    from story import StoryConfig
    from registry import Registry, AssetStatus

    story = StoryConfig(story_path)
    reg   = Registry(story.path)
    total = len(story.pages)

    print(f"\n{'='*55}")
    print(f"  {story.title}  ({total} 页)")
    print(f"{'='*55}")

    assets  = reg.all_assets(story.story_id)
    by_page = {a.page_num: a for a in assets}

    approved = generated = pending = 0
    for page in story.pages:
        pn    = page["page"]
        title = page["title"]
        asset = by_page.get(pn)
        if asset is None:
            print(f"  p{pn:02d}  ○ 待生图    {title}")
            pending += 1
        elif asset.status in (AssetStatus.APPROVED, AssetStatus.UPSCALED):
            score = f"{asset.score:.1f}" if asset.score >= 0 else "?"
            print(f"  p{pn:02d}  ✓ 已通过({score})  {title}")
            approved += 1
        else:
            print(f"  p{pn:02d}  ◑ 已生图   {title}")
            generated += 1

    print(f"\n  通过={approved}  已生图={generated}  待生图={pending}  共={total}")
    print(f"{'='*55}")


# ── 命令：reset ─────────────────────────────────────────────

def cmd_reset(story_path: str = None, all_stories: bool = False):
    """清空注册表（危险操作，需要确认）。"""
    from registry import Registry

    if all_stories:
        confirm = input("  !! 清空所有故事的注册表？(输入 yes 确认): ").strip()
        if confirm != "yes":
            print("  已取消")
            return
        reg = Registry(story_path or "stories/_dummy.yaml")
        reg.reset_all()
        print("  已清空所有注册表")
    elif story_path:
        from story import StoryConfig
        story   = StoryConfig(story_path)
        confirm = input(f"  !! 清空 [{story.title}] 的注册表？(回车确认 / q 取消): ").strip()
        if confirm.lower() in ("q", "quit", "n"):
            print("  已取消")
            return
        reg = Registry(story.path)
        reg.reset(story.story_id)
        print(f"  已清空: {story.title}")
    else:
        print("  请指定故事路径，或加 --all 清空所有")


# ── 命令：inventory ─────────────────────────────────────────

def cmd_inventory():
    """查看 ComfyUI 服务器上的模型清单。"""
    from comfy_script.runtime import load
    from config import COMFY_SERVER, SOVITS_URL
    import requests

    print(f"连接 ComfyUI...")
    load(COMFY_SERVER)

    try:
        r = requests.get(f"{COMFY_SERVER}/object_info", timeout=10).json()
    except Exception as e:
        print(f"  无法连接 ComfyUI: {e}")
        return

    def _list(node_name, key):
        try:
            items = (r.get(node_name, {})
                     .get("input", {}).get("required", {})
                     .get(key, [{}])[0] or [])
            return sorted(items)
        except Exception:
            return []

    print(f"\n{'='*60}")
    print(f"  ComfyUI 资产清单  ({COMFY_SERVER})")
    print(f"{'='*60}")

    ckpts = _list("CheckpointLoaderSimple", "ckpt_name")
    print(f"\n【Checkpoint】共 {len(ckpts)} 个")
    for c in ckpts: print(f"  {c}")

    # FLUX GGUF
    gguf = _list("UnetLoaderGGUF", "unet_name")
    if gguf:
        print(f"\n【FLUX GGUF UNet】共 {len(gguf)} 个")
        for c in gguf: print(f"  {c}")

    loras = _list("LoraLoader", "lora_name")
    print(f"\n【LoRA】共 {len(loras)} 个")
    for l in loras: print(f"  {l}")

    cns = _list("ControlNetLoader", "control_net_name")
    print(f"\n【ControlNet】共 {len(cns)} 个")
    for c in cns: print(f"  {c}")

    vaes = _list("VAELoader", "vae_name")
    print(f"\n【VAE】共 {len(vaes)} 个")
    for v in vaes: print(f"  {v}")

    clips = _list("DualCLIPLoader", "clip_name1")
    if clips:
        print(f"\n【CLIP (DualCLIPLoader)】共 {len(clips)} 个")
        for c in clips: print(f"  {c}")

    print(f"\n{'='*60}")


# ── full：一键全流程 ─────────────────────────────────────────

def cmd_full(story_path: str,
             theme_path: str = None,
             no_ip: bool = False,
             resume: bool = True,  # 修改这里，默认从 False 改为 True
             pages: str = None,
             platform: str = "youtube",
             sovits_host: str = "",
             bgm: str = "",
             wan_model: str = "1.3B",
             max_dynamic: int = 5):
    """
    一键全流程：twophase（生图）+ produce（生视频）
    sovits_host 留空时自动用默认地址。
    """
    # 默认 sovits 地址（不传时自动填）
    if not sovits_host:
        sovits_host = SOVITS_URL
        print(f"  [full] 未指定 --sovits，使用默认地址: {sovits_host}")

    print("\n" + "=" * 55)
    print("  Step 1/2: 生图 (twophase)")
    print("=" * 55)
    cmd_two_phase(
        story_path = story_path,
        theme_path = theme_path,
        no_ip      = no_ip,
        resume     = resume,
        pages      = pages,
    )

    print("\n" + "=" * 55)
    print("  Step 2/2: 生视频 (produce)")
    print("=" * 55)
    cmd_produce(
        story_path  = story_path,
        theme_path  = theme_path,
        platform    = platform,
        sovits_host = sovits_host,
        bgm         = bgm,
        wan_model   = wan_model,
        max_dynamic = max_dynamic,
        no_ip       = no_ip,
    )

    print("\n" + "=" * 55)
    print("  ✓ 全流程完成")
    print("=" * 55)


# ── main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="GunplaBook 全流程入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
命令示例（按主流程顺序）：

  写故事
  ────────────────────────────────────
  python run.py write --concept "月夜古宅中的狐仙传说" --pages 8
  python run.py write --concept "深夜古宅" --batch 3              # 一次生成 3 个变体
  python run.py write --concepts-file concepts.txt                # 批量从文件读

  生图
  ────────────────────────────────────
  python run.py twophase stories/auto_20260101_120000_xxx.yaml
  python run.py twophase stories/xxx.yaml --resume                # 断点续跑
  python run.py twophase stories/xxx.yaml --pages 5,6,7

  生视频
  ────────────────────────────────────
  python run.py produce stories/xxx.yaml --platform youtube
  python run.py produce stories/auto_20260429_150438_雨夜山路的红伞女子.yaml --platform youtube
  python run.py produce stories/xxx.yaml --sovits http://YOUR_SOVITS_HOST:9880

  一键全流程（生图 + 生视频）
  ────────────────────────────────────
  python run.py full stories/xxx.yaml --platform douyin
  python run.py full stories/xxx.yaml --platform youtube --resume
  # --sovits 省略时自动用 http://YOUR_SOVITS_HOST:9880

  其他
  ────────────────────────────────────
  python run.py status   stories/xxx.yaml
  python run.py reset    stories/xxx.yaml
  python run.py reset    --all
  python run.py inventory
        """
    )

    parser.add_argument("cmd",   help="命令: write / twophase / produce / full / status / reset / inventory")
    parser.add_argument("story", nargs="?", help="故事 YAML 路径（write 命令时不填）")

    parser.add_argument("--theme",    default=None,  help="主题包路径（覆盖故事YAML里的theme字段）")
    parser.add_argument("--no-ip",    action="store_true", help="跳过 IP-Adapter")
    parser.add_argument("--resume",   action="store_true", help="断点续跑（twophase/full：跳过已有 APPROVED 资产）")
    parser.add_argument("--no-resume", dest="no_resume", action="store_true",
                        help="full 命令专用：强制重生（默认 resume=True）")
    parser.add_argument("--pages",    default=None,  help="只跑指定页面，如 7 或 5,6,7")
    parser.add_argument("--platform", default=None,  choices=["douyin", "youtube"],
                        help="视频平台（douyin竖屏/youtube横屏）")
    parser.add_argument("--all",      action="store_true", help="reset: 清空所有故事数据")
    parser.add_argument("--sovits", default="", help="GPT-SoVITS API 地址，如 http://YOUR_SOVITS_HOST:9880")
    parser.add_argument("--bgm", default="", help="背景音乐文件路径")
    parser.add_argument("--wan-model", default="1.3B", choices=["1.3B", "14B"], help="Wan 2.1 模型尺寸")
    parser.add_argument("--max-dynamic", default=5, type=int, help="最多动态 clip 数量")

    # ── write 命令专用参数 ─────────────────────────────────
    parser.add_argument("--concept", default="",
                        help="write 命令: 故事概念（中文短语）")
    parser.add_argument("--concepts-file", default="",
                        help="write 命令: 批量从文件读 concept（每行一个）")
    parser.add_argument("--batch", type=int, default=1,
                        help="write 命令: 批量生成数量（>1 时基于 --concept 生成变体）")
    parser.add_argument("--story-pages", type=int, default=8,
                        help="write 命令: 每个故事多少页（默认 8）")
    parser.add_argument("--series", default="",
                        help="write 命令: 系列名")
    parser.add_argument("--review", default="human",
                        choices=["human", "auto", "ai"],
                        help="write 命令: 审核模式（human=人工/auto=不审核/ai=AI审核）")
    parser.add_argument("--offline", action="store_true",
                        help="write 命令: 离线模式（不查询 ComfyUI）")
    parser.add_argument("--output",  default="",
                        help="write 命令: 单个故事时指定输出路径")

    args = parser.parse_args()
    cmd  = args.cmd

    # ── write: 不需要 story（用 --concept / --concepts-file）──
    if cmd == "write":
        cmd_write(
            concept       = args.concept,
            concepts_file = args.concepts_file,
            batch         = args.batch,
            theme_path    = args.theme,
            pages         = args.story_pages,
            series        = args.series,
            review_mode   = args.review,
            offline       = args.offline,
            output        = args.output,
        )
        return

    # inventory 不需要 story
    if cmd == "inventory":
        cmd_inventory()
        return

    # reset 可以不带 story（配合 --all）
    if cmd == "reset":
        cmd_reset(
            story_path  = args.story,
            all_stories = getattr(args, "all", False),
        )
        return

    # 其余命令都需要 story
    if not args.story:
        print(f"  !! 命令 '{cmd}' 需要指定故事 YAML 路径")
        parser.print_help()
        sys.exit(1)

    if cmd == "twophase":
        cmd_two_phase(
            story_path = args.story,
            theme_path = args.theme,
            no_ip      = args.no_ip,
            resume     = args.resume,
            pages      = args.pages,
        )



    elif cmd == "produce":
        # produce 默认 resume=True；--no-resume 关闭
        produce_resume = not getattr(args, "no_resume", False)
        cmd_produce(
            story_path  = args.story,
            theme_path  = args.theme,
            platform    = getattr(args, "platform", None) or "youtube",
            sovits_host = getattr(args, "sovits", ""),
            bgm         = getattr(args, "bgm", ""),
            wan_model   = getattr(args, "wan_model", "1.3B"),
            max_dynamic = getattr(args, "max_dynamic", 5),
            no_ip       = args.no_ip,
            resume      = produce_resume,
        )

    elif cmd == "full":
        cmd_full(
            story_path  = args.story,
            theme_path  = args.theme,
            no_ip       = args.no_ip,
            resume      = not getattr(args, "no_resume", False),
            pages       = args.pages,
            platform    = getattr(args, "platform", None) or "youtube",
            sovits_host = getattr(args, "sovits", ""),
            bgm         = getattr(args, "bgm", ""),
            wan_model   = getattr(args, "wan_model", "1.3B"),
            max_dynamic = getattr(args, "max_dynamic", 5),
        )

    elif cmd == "status":
            cmd_status(args.story, args.theme)

    else:
        print(f"  未知命令: {cmd}")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
