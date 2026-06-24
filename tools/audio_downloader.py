"""
tools/audio_downloader.py ── 音频素材下载助手
================================================================
把"去 freesound 下载音频"变成一个"跑脚本 → 看 TODO 列表 → 点链接"
的工程化流程。让代码工程师不用面对"挑素材"的焦虑。

用法:
  python -m tools.audio_downloader              # 列出所有缺失的 sfx
  python -m tools.audio_downloader --next 5     # 只列前 5 个最关键的
  python -m tools.audio_downloader --check      # 检查目录完整性,不打印链接
  python -m tools.audio_downloader --bgm        # 列 BGM 下载任务

输出形如:
  [TODO 1/10] rain_light
    搜索: https://freesound.org/search/?q=light+rain+ambient&f=license:%22Creative+Commons+0%22
    保存到: refs/sfx/ambient/rain_light/rain_light_01.wav
    建议: 选 30秒以上的,听感平和不戏剧化
  ...

[流程]
  1. 跑 python -m tools.audio_downloader --next 5
  2. 复制第一条的"搜索"链接,在浏览器打开
  3. 选第一个能用的 CC0 音频(听 10 秒不刺耳就行,别挑剔)
  4. 下载,改名为脚本提示的文件名
  5. 拖到脚本提示的文件夹
  6. 再跑 python -m tools.audio_downloader --check 验证
  7. 第一条变成 ✓,继续下一条
"""

import sys
import urllib.parse
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("需要 pip install pyyaml")
    sys.exit(1)


# ════════════════════════════════════════════════════════════════
# 配置
# ════════════════════════════════════════════════════════════════

# 项目根目录(本脚本在 tools/ 下,根目录是父级)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SFX_REGISTRY_PATH = PROJECT_ROOT / "refs" / "sfx" / "registry.yaml"
SFX_ROOT = PROJECT_ROOT / "refs" / "sfx"
BGM_ROOT = PROJECT_ROOT / "refs" / "bgm"

# 最关键的 10 个 sfx(优先级排序,先下这些就够跑通)
PRIORITY_SFX = [
    "rain_light", "rain_heavy", "thunder_distant", "wind_howl",
    "low_drone", "thunder_crack", "bell_toll", "door_creak",
    "heartbeat_loop", "ghost_whisper",
]

# 默认 BGM 7 类的搜索词(走 YouTube Audio Library,不去 freesound)
BGM_CATEGORIES = {
    "ambient_dark":    ("dark ambient drone",        "黑色底床,大部分恐怖叙事镜头默认用"),
    "tension_build":   ("cinematic tension build",   "推进/悬念/危机渐近"),
    "climax_impact":   ("epic horror hit",           "高潮爆发/鬼出现"),
    "melancholy_solo": ("sad piano solo",            "抒情/告别/悲剧"),
    "mystery_explore": ("mysterious exploration",    "探险/找线索/未知"),
    "serene_warm":     ("peaceful warm ambient",     "温暖治愈/儿童剧/合家欢"),
    "epic_majestic":   ("epic orchestral",           "章节结尾/史诗/宏大"),
    "playful_kids":    ("playful children music",    "欢快俏皮/儿童剧"),
}


# ════════════════════════════════════════════════════════════════
# freesound 搜索链接生成
# ════════════════════════════════════════════════════════════════

def freesound_search_url(query: str, cc0_only: bool = True,
                          min_duration: int = 0) -> str:
    """构造 freesound 搜索 URL,默认过滤 CC0。"""
    base = "https://freesound.org/search/"
    params = {"q": query, "s": "score desc"}
    filters = []
    if cc0_only:
        filters.append('license:"Creative Commons 0"')
    if min_duration > 0:
        filters.append(f"duration:[{min_duration} TO 600]")
    if filters:
        params["f"] = " ".join(filters)
    return base + "?" + urllib.parse.urlencode(params)


def youtube_audio_library_url() -> str:
    """YouTube Audio Library 入口。"""
    return "https://studio.youtube.com/channel/UC/music"


# ════════════════════════════════════════════════════════════════
# 检查目录状态
# ════════════════════════════════════════════════════════════════

def load_sfx_registry() -> dict:
    """读 registry.yaml。"""
    if not SFX_REGISTRY_PATH.exists():
        print(f"[error] 找不到 registry: {SFX_REGISTRY_PATH}")
        print(f"        请先把 registry.yaml 放到 refs/sfx/")
        sys.exit(1)
    with open(SFX_REGISTRY_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_sfx_dir(category: str, sfx_id: str) -> tuple[bool, list[str]]:
    """
    检查某 sfx 文件夹有没有文件。
    返回 (是否就绪, 现存文件名列表)。
    """
    target_dir = SFX_ROOT / category / sfx_id
    if not target_dir.exists():
        return False, []
    # 接受 wav/mp3/ogg/flac
    audio_files = []
    for ext in ("*.wav", "*.mp3", "*.ogg", "*.flac"):
        audio_files.extend(target_dir.glob(ext))
    return len(audio_files) > 0, [f.name for f in audio_files]


def check_bgm_dir(category: str) -> tuple[bool, int]:
    """检查 BGM 某类有几首。"""
    target_dir = BGM_ROOT / category
    if not target_dir.exists():
        return False, 0
    count = 0
    for ext in ("*.wav", "*.mp3", "*.ogg", "*.flac"):
        count += len(list(target_dir.glob(ext)))
    return count > 0, count


# ════════════════════════════════════════════════════════════════
# 主任务: 列出 SFX TODO
# ════════════════════════════════════════════════════════════════

def list_sfx_todos(reg: dict, limit: Optional[int] = None,
                    priority_only: bool = False) -> None:
    """打印缺失的 sfx 下载任务。"""
    todos = []
    done = []

    for category in ("ambient", "stinger", "transition"):
        entries = reg.get(category, {})
        if not entries:
            continue
        for sfx_id, info in entries.items():
            ready, existing = check_sfx_dir(category, sfx_id)
            if ready:
                done.append((category, sfx_id, existing))
            else:
                todos.append((category, sfx_id, info))

    # 排序: 优先级 sfx 排前面
    if priority_only:
        todos = [(c, s, i) for c, s, i in todos if s in PRIORITY_SFX]
    else:
        todos.sort(key=lambda x: (
            PRIORITY_SFX.index(x[1]) if x[1] in PRIORITY_SFX else 999,
            x[0], x[1]
        ))

    total_todos = len(todos)
    if limit:
        todos = todos[:limit]

    print()
    print("═" * 70)
    print(f"  音频素材下载任务 (剩余 {total_todos} 个,已完成 {len(done)} 个)")
    print("═" * 70)
    print()

    if not todos:
        print("  ✓ 所有 SFX 都已就绪!")
        if done:
            print(f"\n  已就绪文件夹: {len(done)} 个")
        return

    for i, (category, sfx_id, info) in enumerate(todos, 1):
        is_priority = sfx_id in PRIORITY_SFX
        marker = "🔥" if is_priority else "  "
        print(f"{marker} [TODO {i}/{len(todos)}] {sfx_id}")
        print(f"     用途: {info.get('desc', '?')}")
        query = info.get("freesound", sfx_id.replace("_", " "))
        url = freesound_search_url(query, min_duration=5 if category == "ambient" else 0)
        print(f"     搜索: {url}")
        target_file = SFX_ROOT / category / sfx_id / f"{sfx_id}_01.wav"
        print(f"     保存到: {target_file.relative_to(PROJECT_ROOT)}")
        if category == "ambient":
            print(f"     提示: ambient 选 30秒以上的循环音,选听感平和的")
        elif category == "stinger":
            print(f"     提示: stinger 选 0.5-3秒的短促音,不要太长")
        print()

    print("─" * 70)
    print("  操作流程:")
    print("    1. 点上面任一条的[搜索]链接")
    print("    2. 选第一个能听的(不要挑剔,30 秒搞定一个)")
    print("    3. 下载,改名为提示的文件名")
    print("    4. 放到提示的文件夹")
    print("    5. 跑 python -m tools.audio_downloader --check 验证")
    print("─" * 70)


# ════════════════════════════════════════════════════════════════
# 主任务: 列 BGM TODO
# ════════════════════════════════════════════════════════════════

def list_bgm_todos() -> None:
    """打印 BGM 各类的缺失状态。"""
    print()
    print("═" * 70)
    print("  BGM 下载任务 (推荐用 YouTube Audio Library)")
    print("═" * 70)
    print()
    print(f"  入口: {youtube_audio_library_url()}")
    print(f"  操作: 登录 YouTube → 左侧'音频库' → 搜索关键词 → 下载")
    print()
    print("─" * 70)

    todos = []
    for category, (query, desc) in BGM_CATEGORIES.items():
        ready, count = check_bgm_dir(category)
        status = f"✓ 已有 {count} 首" if ready else "❌ 缺失"
        print(f"  {status:15} {category:20} ({desc})")
        if not ready:
            todos.append((category, query, desc))

    print("─" * 70)
    print()

    if not todos:
        print("  ✓ 所有 BGM 类目都已就绪!")
        return

    print(f"  待下载: {len(todos)} 类,每类下 3-5 首")
    print()
    for category, query, desc in todos:
        target_dir = BGM_ROOT / category
        print(f"  [BGM TODO] {category}")
        print(f"    用途: {desc}")
        print(f"    YouTube 搜索词: {query}")
        print(f"    备选 Pixabay:    https://pixabay.com/music/search/{urllib.parse.quote(query)}/")
        print(f"    保存到: {target_dir.relative_to(PROJECT_ROOT)}/")
        print()


# ════════════════════════════════════════════════════════════════
# 只检查不打印链接
# ════════════════════════════════════════════════════════════════

def quick_check() -> None:
    """快速状态检查,不打印长链接。"""
    reg = load_sfx_registry()

    total_sfx = 0
    ready_sfx = 0
    for category in ("ambient", "stinger", "transition"):
        entries = reg.get(category, {})
        for sfx_id in entries:
            total_sfx += 1
            ready, _ = check_sfx_dir(category, sfx_id)
            if ready:
                ready_sfx += 1

    total_bgm = len(BGM_CATEGORIES)
    ready_bgm = sum(1 for c in BGM_CATEGORIES if check_bgm_dir(c)[0])

    priority_ready = sum(
        1 for sid in PRIORITY_SFX
        if any(check_sfx_dir(cat, sid)[0]
               for cat in ("ambient", "stinger", "transition"))
    )

    print()
    print("═" * 50)
    print("  音频素材状态")
    print("═" * 50)
    print(f"  SFX(全部):    {ready_sfx:3}/{total_sfx} ({100*ready_sfx//max(total_sfx,1)}%)")
    print(f"  SFX(优先 10): {priority_ready:3}/10")
    print(f"  BGM 类目:     {ready_bgm:3}/{total_bgm}")
    print("═" * 50)
    print()

    # 给个建议
    if priority_ready < 5:
        print("  建议: 先下载优先级 SFX,跑")
        print("        python -m tools.audio_downloader --priority")
    elif priority_ready < 10:
        print("  建议: 继续补齐优先级 SFX,跑")
        print("        python -m tools.audio_downloader --priority")
    elif ready_bgm < 4:
        print("  建议: 优先 SFX 已够用,开始下 BGM,跑")
        print("        python -m tools.audio_downloader --bgm")
    elif ready_sfx < total_sfx // 2:
        print("  建议: BGM 也有了,补完剩余 SFX,跑")
        print("        python -m tools.audio_downloader")
    else:
        print("  ✓ 状态良好,可以跑混音流水了!")
    print()


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════

def main():
    args = sys.argv[1:]

    if "--check" in args:
        quick_check()
        return

    if "--bgm" in args:
        list_bgm_todos()
        return

    if "--priority" in args:
        reg = load_sfx_registry()
        list_sfx_todos(reg, priority_only=True)
        return

    limit = None
    if "--next" in args:
        idx = args.index("--next")
        if idx + 1 < len(args):
            try:
                limit = int(args[idx + 1])
            except ValueError:
                pass

    reg = load_sfx_registry()
    list_sfx_todos(reg, limit=limit)


if __name__ == "__main__":
    main()
