"""
story_writer.py ── 故事创作引擎（v3，FLUX 专版）
=================================================
v3 改动：
  - 新增 __main__ 入口，可直接独立运行（无需 run.py）
  - 精简为 FLUX 专版：只保留 flux/ 子目录下的 LoRA，其余底模过滤
  - lora_ref 填充逻辑对齐项目 yaml 格式：
      story_writer 生成 lora_ref → resolve_lora_refs 填充为 lora/lora_strength
      最终 yaml 格式与 haunted_inn.yaml 完全一致，pipeline 直接可用
  - FLUX 主题用 unet 字段，不依赖 checkpoint 做可用性判断
  - gather_context 新增 offline 快速路径（不查 ComfyUI，纯本地资产）
  - lora_status 字段：approved 的才出现在 LLM 可选列表里

用法（独立运行）：
  python story_writer.py --concept "月夜古宅中的狐仙传说" --pages 8
  python story_writer.py --concept "..." --pages 8 --offline
  python story_writer.py --concept "..." --pages 8 --review auto
  python story_writer.py --list_assets
  python story_writer.py --validate stories/xxx.yaml

用法（通过 run.py，向后兼容）：
  python run.py create_story --theme themes/chinese_ghost_flux.yaml \\
    --concept "月夜古宅中的狐仙传说" --pages 8
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
import yaml


# ════════════════════════════════════════
# 常量
# ════════════════════════════════════════

DEFAULT_THEME     = "themes/chinese_ghost_flux.yaml"
DEFAULT_LORAS_DIR = "loras"          # 只读 loras/flux/ 子目录
DEFAULT_STORIES_DIR = "stories"
DEFAULT_THEMES_DIR  = "themes"

# FLUX 的 CFG 范围（和 SD1.5/Pony 不同）
FLUX_CFG = {
    "opening":   (2.5, 3.0),
    "develop":   (3.0, 3.5),
    "climax":    (3.5, 4.5),
    "ending":    (2.5, 3.0),
}


# ════════════════════════════════════════
# 资产读取（FLUX 专版）
# ════════════════════════════════════════

def _get_server_loras(comfy_server: str, proxies: dict) -> set:
    """从 ComfyUI 获取服务器上的 LoRA 列表"""
    try:
        r = requests.get(f"{comfy_server}/object_info",
                         timeout=15, proxies=proxies).json()
        loras = (r.get("LoraLoader", {})
                  .get("input", {}).get("required", {})
                  .get("lora_name", [{}])[0] or [])
        return set(loras)
    except Exception:
        return set()


def _lora_on_server(lora_file: str, server_loras: set) -> bool:
    """路径尾部匹配，兼容 Windows/Mac 路径分隔符差异"""
    if not lora_file or not server_loras:
        return True   # 离线模式 server_loras 为空，默认通过
    norm = lora_file.replace("\\", "/")
    for s in server_loras:
        s_norm = s.replace("\\", "/")
        if s_norm == norm or s_norm.endswith("/" + norm) or norm.endswith("/" + s_norm):
            return True
    return False


def _read_flux_loras(loras_dir: str = DEFAULT_LORAS_DIR) -> list[dict]:
    """
    只读取 loras/flux/ 子目录下的 LoRA yaml。
    过滤条件：
      1. 必须在 flux/ 子目录
      2. lora_status 为 approved 或未设置（训练中/pending 的不出现在 LLM 列表）
    """
    result = []
    flux_dir = Path(loras_dir) / "flux"
    if not flux_dir.exists():
        # 也尝试根目录（向后兼容）
        flux_dir = Path(loras_dir)
    if not flux_dir.exists():
        return result

    for f in sorted(flux_dir.glob("*.yaml")):
        if f.stem.startswith("_") or f.stem == "README":
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                data = yaml.safe_load(fp)
            if not isinstance(data, dict):
                continue

            # 过滤未通过测试的 LoRA（pending/rejected 不出现在选项里）
            status = data.get("status", data.get("lora_status", "approved"))
            if status not in ("approved", ""):
                print(f"  [资产过滤] 跳过 {f.stem}: status={status}")
                continue

            result.append({
                "ref":           f.stem,
                "name":          data.get("name", f.stem),
                "file":          data.get("file", ""),
                "base_model":    "flux",
                "strength":      data.get("strength", 0.8),
                "trigger_solo":  data.get("trigger_solo", ""),
                "trigger_multi": data.get("trigger_multi", ""),
                "notes":         data.get("notes", ""),
                "_yaml_path":    str(f),
            })
        except Exception as e:
            print(f"  [资产读取] 读取 {f.name} 失败: {e}")
    return result


def _read_flux_themes(themes_dir: str = DEFAULT_THEMES_DIR) -> list[dict]:
    """只读取 FLUX 主题（通过 unet 字段判断，或文件名含 flux）"""
    result = []
    theme_path = Path(themes_dir)
    if not theme_path.exists():
        return result
    for f in sorted(theme_path.glob("*.yaml")):
        if f.stem.startswith("_"):
            continue
        try:
            with open(f, encoding="utf-8") as fp:
                data = yaml.safe_load(fp)
            model = data.get("model", {})
            # FLUX 主题特征：有 unet 字段，或 checkpoint 为空，或文件名含 flux
            is_flux = (
                bool(model.get("unet"))
                or "flux" in f.stem.lower()
                or "flux" in data.get("name", "").lower()
            )
            if not is_flux:
                continue
            result.append({
                "path":        str(f),
                "name":        data.get("name", f.stem),
                "description": data.get("description", ""),
                "unet":        model.get("unet", ""),
                "checkpoint":  model.get("checkpoint", ""),
                "style_prefix": data.get("prompts", {}).get("style_prefix", ""),
            })
        except Exception as e:
            print(f"  [资产读取] 读取 {f.name} 失败: {e}")
    return result


def _read_example_stories(stories_dir: str = DEFAULT_STORIES_DIR,
                           max_examples: int = 2) -> list[str]:
    """读取最近修改的故事 YAML 作为 few-shot 示例"""
    story_path = Path(stories_dir)
    if not story_path.exists():
        return []
    yamls = sorted(
        [f for f in story_path.glob("*.yaml") if not f.stem.startswith("_")],
        key=lambda f: f.stat().st_mtime, reverse=True
    )[:max_examples]
    result = []
    for f in yamls:
        try:
            result.append(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return result


def gather_context(theme_path: str = None,
                   offline: bool = False,
                   loras_dir: str = DEFAULT_LORAS_DIR,
                   themes_dir: str = DEFAULT_THEMES_DIR,
                   stories_dir: str = DEFAULT_STORIES_DIR) -> dict:
    """
    收集 FLUX 可用资产，返回结构化 context 供 LLM 使用。
    offline=True：跳过 ComfyUI 查询，直接用本地资产。
    """
    from config import COMFY_SERVER, PROXIES

    server_loras = set()
    if not offline:
        print(f"  [资产读取] 查询 ComfyUI 服务器 LoRA 列表...")
        server_loras = _get_server_loras(COMFY_SERVER, PROXIES)
        print(f"  [资产读取] 服务器: {len(server_loras)} 个 LoRA")
    else:
        print(f"  [资产读取] 离线模式，跳过服务器查询")

    all_themes = _read_flux_themes(themes_dir)
    all_loras  = _read_flux_loras(loras_dir)
    examples   = _read_example_stories(stories_dir)

    print(f"  [资产读取] 本地: {len(all_themes)} 个 FLUX 主题，{len(all_loras)} 个 FLUX LoRA")

    # 过滤指定主题
    available_themes = []
    for t in all_themes:
        if theme_path:
            if Path(t["path"]).name != Path(theme_path).name:
                continue
        available_themes.append(t)

    # 过滤服务器上存在的 LoRA
    available_loras = []
    skipped = []
    for lora in all_loras:
        if not offline and server_loras:
            if not _lora_on_server(lora["file"], server_loras):
                skipped.append(lora["ref"])
                continue
        available_loras.append(lora)

    if skipped:
        print(f"  [资产过滤] 服务器无文件，跳过: {skipped}")

    print(f"  [资产读取] 可用主题: {len(available_themes)} 个，"
          f"可用 LoRA: {len(available_loras)} 个")

    return {
        "available_themes": available_themes,
        "available_loras":  available_loras,
        "example_stories":  examples,
        "target_theme_path": theme_path,
        "_server_loras":    server_loras,
        "offline":          offline,
    }


def print_available_assets(context: dict):
    """打印当前可用资产清单"""
    print(f"\n{'='*60}")
    print(f"  当前可用 FLUX 资产清单")
    print(f"{'='*60}")

    themes = context["available_themes"]
    print(f"\n【FLUX 主题包】共 {len(themes)} 个")
    for t in themes:
        print(f"  [{t['name']}]  {t['path']}")
        if t["description"]:
            print(f"    说明: {t['description']}")

    loras = context["available_loras"]
    print(f"\n【FLUX LoRA 角色】共 {len(loras)} 个")
    for lo in loras:
        kind = "角色" if lo["trigger_solo"] else "风格增强"
        print(f"  [{lo['ref']}] {lo['name']} ({kind})  strength={lo['strength']}")
        if lo["trigger_solo"]:
            print(f"    trigger_solo: {lo['trigger_solo'][:80]}")
        if lo["notes"]:
            first_line = lo["notes"].strip().split("\n")[0]
            print(f"    备注: {first_line[:60]}")
    print(f"\n{'='*60}")


# ════════════════════════════════════════
# 故事生成（两阶段）
# ════════════════════════════════════════

def _build_asset_str(context: dict) -> tuple[str, str]:
    """把可用资产格式化成给 LLM 看的字符串"""
    themes = context["available_themes"]
    loras  = context["available_loras"]

    theme_lines = [f"  - {t['name']} ({t['path']})" for t in themes]
    theme_str = "\n".join(theme_lines) if theme_lines else "  （无可用主题）"

    lora_lines = []
    for lo in loras:
        line = f"  - lora_ref={lo['ref']}, 名称={lo['name']}, strength={lo['strength']}"
        if lo["trigger_solo"]:
            line += f"\n    trigger_solo: {lo['trigger_solo'].strip()}"
        if lo["trigger_multi"]:
            line += f"\n    trigger_multi: {lo['trigger_multi'].strip()}"
        lora_lines.append(line)
    lora_str = "\n".join(lora_lines) if lora_lines else "  （无可用 LoRA，用 prompt 描述角色）"

    return theme_str, lora_str


def _generate_outline(concept: str, pages: int,
                       context: dict, target_theme: dict, llm) -> str:
    """Stage 1：生成故事大纲"""
    from langchain_core.messages import HumanMessage, SystemMessage

    loras = context["available_loras"]
    char_loras = [lo for lo in loras if lo["trigger_solo"]]

    system = f"""你是漫画故事策划专家。根据概念和可用角色，制定故事大纲。

【可用角色 LoRA】
{chr(10).join(f"  - {lo['ref']}（{lo['name']}）" for lo in char_loras) if char_loras else "  无已训练角色，用文字描述"}
【主题风格】{target_theme.get('name', '')}

【叙事节奏要求】
  开场（1-2页）：环境建立，引入悬念
  发展（中间页）：矛盾升级，角色互动
  高潮（倒数2-3页）：决定性转折或对决
  结尾（最后1-2页）：余韵收场

每页格式（一行）：
第N页: [标题5字内] | [功能:开场/铺垫/冲突/高潮/转折/收场] | [旁白方向10字] | [角色:角色ref或无] | [动态:high/medium/low]

动态规则：high=戏剧性瞬间，每部最多3个；low=静态背景；medium=其余
只输出大纲，不要其他内容。"""

    response = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=f"故事概念：{concept}\n总页数：{pages}页"),
    ])
    outline = response.content.strip()
    print(f"  [大纲]\n{outline}")
    return outline


def generate_story(concept: str,
                   theme_path: str,
                   pages: int,
                   context: dict,
                   series: str = "") -> str:
    """
    两阶段故事生成，输出格式与方案 B（引用机制）对齐。

    生成的 yaml 格式（v2 简化版）：
      characters:
        innkeeper_ghost:
          lora_ref: innkeeper_ghost          ← 引用 loras/flux/<ref>.yaml
          desc: "..."
          key_features: "..."
          voice:
            engine: gpt_sovits
            ref_id: ghost_female_sorrowful   ← 引用 refs/voice_library.yaml
        narrator:
          voice:
            engine: edge_tts
            voice_id: zh-CN-YunjianNeural

      pages:
        - page: 1
          title: "..."
          characters: [...]
          scene_type: "..."
          seed: 1234567
          narration: "..."
          motion_hint: low
          bgm_mood: tension                  ← 新增字段，driver BGM 选择
          dialogue: [...]

    引用字段会被对应的 resolver 在运行时展开：
      lora_ref → resolve_lora_refs() → lora / lora_strength / trigger_solo
      ref_id   → voice_engine._resolve_ref_id() → ref_audio / ref_text
      bgm_mood → producer_v2 → refs/bgm/<mood>/*.mp3
    """
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage

    # 确定目标主题
    target_theme = {}
    for t in context["available_themes"]:
        if theme_path and Path(t["path"]).name == Path(theme_path).name:
            target_theme = t
            break
    if not target_theme and context["available_themes"]:
        target_theme = context["available_themes"][0]
    if not target_theme:
        target_theme = {"name": "chinese_ghost_flux", "path": theme_path or DEFAULT_THEME}

    llm = ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=0.75,
        max_tokens=6000,
    )

    # ── Stage 1：大纲 ─────────────────────────────────────
    print(f"  [故事生成] Stage 1: 规划大纲...")
    outline = _generate_outline(concept, pages, context, target_theme, llm)

    # ── Stage 2：展开完整 YAML ────────────────────────────
    print(f"  [故事生成] Stage 2: 展开完整 YAML...")

    theme_str, lora_str = _build_asset_str(context)
    lora_refs = [lo["ref"] for lo in context["available_loras"]]

    # v2：用内置模板示例，不再用 stories/ 下的旧 yaml（避免被老格式污染）
    example_str = """

=== 参考格式示例（v2 引用机制）===
title: 示例·雨夜古宅
series: ghost_tales_china
theme: themes/chinese_horror_tales.yaml

characters:
  scholar:
    lora_ref: ""
    desc: 赶考书生，途经此地夜宿古宅
    key_features: "young man in blue hanfu, scholar hat, carrying books, tired face"
    voice:
      engine: gpt_sovits
      ref_id: young_male_scholar

  innkeeper_ghost:
    lora_ref: ""
    desc: 古宅女主人，实为含冤而死的亡魂
    key_features: "white dress, red oil-paper umbrella, pale skin, long black hair"
    voice:
      engine: gpt_sovits
      ref_id: ghost_female_sorrowful

  narrator:
    voice:
      engine: edge_tts
      voice_id: zh-CN-YunjianNeural

scene_templates:
  rainy_courtyard:
    desc: 雨夜古宅外景
    image_type: background_only
    scene: "ancient chinese courtyard in heavy rain at midnight, traditional architecture"
    neg_add: "modern, daylight"
    cfg: 3.0
    steps: 20
    sampler: euler

pages:
- page: 1
  title: 夜叩古宅
  characters: [scholar]
  scene_type: rainy_courtyard
  seed: 4827163
  narration: 暴雨倾盆的夜里，书生敲响了一座古宅的大门。
  motion_hint: low
  bgm_mood: tension

- page: 2
  title: 红伞女子
  characters: [scholar, innkeeper_ghost]
  scene_type: rainy_courtyard
  seed: 9182736
  narration: 一名素衣女子撑着红伞开门，脸色苍白如纸。
  motion_hint: medium
  bgm_mood: tension
  dialogue:
    - speaker: innkeeper_ghost
      text: "公子风雨夜行，可愿借宿一晚？"
=== 示例结束 ==="""

    system2 = f"""你是漫画故事 YAML 生成专家。基于大纲，生成完整 story YAML。

【可用 FLUX 主题】
{theme_str}

【可用 FLUX LoRA 角色（lora_ref 只能用以下 ref 值）】
{lora_str}

【YAML 格式规范（必须严格遵守）】

1. 顶层结构：
title: 故事标题
series: {series or 'ghost_tales_china'}
theme: {target_theme.get('path', DEFAULT_THEME)}
characters:
  角色ref名:
    lora_ref: 角色ref名        ← 必填，从 LoRA 列表选，无对应 LoRA 则留空字符串
    desc: "角色简介"            ← 一句话背景
    key_features: "外观关键词"   ← 英文，用于生图（如 "white dress, red umbrella, pale skin"）
    voice:                    ← ★ 必填（除非角色是无声 NPC）
      engine: gpt_sovits      ← 主选 sovits（参考音频缺失时自动降级 edge_tts）
      ref_id: <从下方声音库选一个 key>   ← 引用 refs/voice_library.yaml，无需录音

  narrator:                   ← 旁白角色，必填，用 edge_tts 简洁稳定
    voice:
      engine: edge_tts
      voice_id: zh-CN-YunjianNeural  ← 推荐叙事男声（说书风）

★ 不需要再写 lora / lora_strength / lora_status / trigger_solo / trigger_multi 字段。
   有 lora_ref 时这些会被 resolve_lora_refs 自动从 loras/flux/<ref>.yaml 填充。
   无 lora_ref 时 key_features 会作为生图描述使用。

scene_templates:
  场景名:
    desc: "场景说明"
    image_type: solo_distant  ← solo_character/solo_distant/composite/background_only
    scene: "完整英文场景描述..."
    neg_add: "negative补充"
    cfg: 3.5                  ← FLUX 范围 1.0-5.0（开场2.5-3.0，高潮3.5-4.5）
    steps: 20
    sampler: euler
pages:
- page: 1
  title: "页面标题"
  characters: []             ← 出场角色的 ref 名列表
  scene_type: 场景名
  seed: 1234567
  narration: "旁白15-30字"
  motion_hint: low           ← high/medium/low
  bgm_mood: tension          ← ★ 必填：tension / climax / melancholy
  dialogue:                  ← ★ 仅在剧情转折/冲突页加，过渡页不加
    - speaker: 角色ref名
      text: "对白内容，10-25字"
    - speaker: 角色ref名
      text: "..."

2. image_type 选择规则：
   无人物纯背景 → background_only
   单人近景/特写/半身 → solo_character
   单人远景/全身/背影/小人 → solo_distant
   双人同框/对决 → composite（characters 必须有两个角色）

3. 旁白 narration：15-30字，口语化，适合TTS配音

4. 每页 seed 用不同6-8位随机数

5. 无 LoRA 的角色：lora_ref 留空字符串 ""，key_features 写详细外观（英文关键词）

★★★ dialogue 字段使用规则（重要，不要全加，也不要全不加）：
   - 关键剧情转折页（人物对话、冲突、表态）→ 加 dialogue
   - 过渡页（无人物、纯氛围、纯叙述）→ 不加 dialogue
   - 推荐比例：8 页里 4-5 页加 dialogue（约 50%-60%）
   - 每页对白 1-3 条，每条 10-25 字
   - 对白要符合角色性格和场景，避免"水台词"
   - speaker 必须是 characters 里定义的角色名（不能用 narrator）

★★★ voice.ref_id 选择参考（来自 refs/voice_library.yaml 已建好的 12 个声音）：
   男声：
     young_male_scholar       年轻书生（清朗斯文）
     middle_male_calm         中年沉稳（捕快/掌柜/夫子/道士）
     old_male_wise            老者智者（沙哑缓慢）
     young_male_sinister      年轻反派（阴柔阴险）
   女声：
     young_female_pure        清纯少女（温柔轻盈）
     ghost_female_sorrowful   鬼女哀怨（幽幽虚浮，含冤亡灵）
     ghost_female_seductive   女鬼魅惑（柔媚危险）
     middle_female_warm       中年妇人（温暖体贴）
     old_female_kindly        老妇（慈祥/有时反差诡异）
   旁白专用（仅 narrator 角色使用）：
     narrator_male_storyteller    男声说书人（评书风，最常用）
     narrator_female_calm         女声平静叙事（文学感）
     narrator_male_grave          男声沉重肃穆（悬疑高潮）

   选择规则：根据角色性别/年龄/性格匹配。匹配不准会让声音风格违和。
   例：「赶考书生」→ young_male_scholar；「含冤而死的女鬼」→ ghost_female_sorrowful
       「捕快」→ middle_male_calm；「妩媚迷人的女鬼」→ ghost_female_seductive

★★★ 每页必填 bgm_mood 字段（控制 BGM 选择）：
   tension     默认值，紧张铺垫/悬疑/不安（多数页用这个）
   climax      恐怖高潮/惊悚揭示/极度紧张（关键揭示页）
   melancholy  凄凉感伤/悲悯/收尾（结尾或反思页）
   8 页典型分布：tension×4-5 + climax×2 + melancholy×1，开头通常 tension，结尾通常 melancholy。

只输出 YAML 内容，从 title: 开始，不要代码块标记，不要说明文字。
{example_str}"""

    user2 = f"""故事概念：{concept}
主题：{target_theme.get('name', '')} ({target_theme.get('path', theme_path)})
可用 lora_ref 列表：{', '.join(lora_refs) if lora_refs else '无'}
系列：{series or 'ghost_tales_china'}
总页数：{pages}页

【已规划大纲】
{outline}

请严格按大纲和格式规范生成完整 YAML。"""

    start = time.time()
    response = llm.invoke([
        SystemMessage(content=system2),
        HumanMessage(content=user2),
    ])
    elapsed = time.time() - start
    print(f"  [故事生成] 完成（{elapsed:.1f}s）")

    text = response.content.strip()
    # 去除可能的代码块标记
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    return text


# ════════════════════════════════════════
# LoRA 引用填充（lora_ref → lora）
# ════════════════════════════════════════

def _build_lora_index(loras_dir: str = DEFAULT_LORAS_DIR) -> dict:
    """
    构建 lora_ref → 完整信息的索引（只处理 flux/ 子目录）。
    key: yaml stem（如 innkeeper_ghost）
    value: {file, full_path, strength, trigger_solo, trigger_multi}
    """
    index = {}
    # 优先 loras/flux/ 子目录
    flux_dir = Path(loras_dir) / "flux"
    search_dirs = [flux_dir, Path(loras_dir)]

    for search_dir in search_dirs:
        if not search_dir.exists():
            continue
        for f in search_dir.glob("*.yaml"):
            if f.stem.startswith("_") or f.stem == "README" or f.stem in index:
                continue
            try:
                with open(f, encoding="utf-8") as fp:
                    data = yaml.safe_load(fp)
                if not isinstance(data, dict):
                    continue
                fname = data.get("file", "")
                # FLUX LoRA 的 comfy_ref 格式：flux/filename.safetensors
                full_path = f"flux/{fname}" if fname and not fname.startswith("flux/") else fname
                index[f.stem] = {
                    "file":          fname,
                    "full_path":     full_path,
                    "strength":      data.get("strength", 0.8),
                    "trigger_solo":  data.get("trigger_solo", ""),
                    "trigger_multi": data.get("trigger_multi", ""),
                    "status":        data.get("status", data.get("lora_status", "approved")),
                }
            except Exception:
                pass
    return index


def resolve_lora_refs(yaml_data: dict,
                       lora_index: dict) -> tuple[dict, list[str]]:
    """
    把 lora_ref 字段解析为 lora/lora_strength 字段，
    对齐 haunted_inn.yaml 的格式，pipeline 直接可用。

    转换：
      lora_ref: innkeeper_ghost
        → lora: flux/innkeeper_ghost_flux_lora.safetensors
        → lora_strength: 0.8
        → trigger_solo: （从 lora yaml 补全，若生成时为空）
    """
    warnings = []
    characters = yaml_data.get("characters", {})

    for char_name, char_cfg in characters.items():
        if not isinstance(char_cfg, dict):
            continue
        lora_ref = char_cfg.get("lora_ref", "")
        if not lora_ref:
            continue
        if lora_ref not in lora_index:
            warnings.append(f"角色 {char_name}: lora_ref={lora_ref} 在 loras/ 找不到")
            continue

        info = lora_index[lora_ref]

        # 只填充，不覆盖用户手动填的
        if not char_cfg.get("lora"):
            char_cfg["lora"] = info["full_path"]
            print(f"  [LoRA填充] {char_name}: {lora_ref} → {info['full_path']}")

        if not char_cfg.get("lora_strength"):
            char_cfg["lora_strength"] = info["strength"]

        if not char_cfg.get("trigger_solo") and info["trigger_solo"]:
            char_cfg["trigger_solo"] = info["trigger_solo"]
            print(f"  [触发词填充] {char_name}: trigger_solo 已补全")

        if not char_cfg.get("trigger_multi") and info["trigger_multi"]:
            char_cfg["trigger_multi"] = info["trigger_multi"]

    return yaml_data, warnings


# ════════════════════════════════════════
# 验证（三层）
# ════════════════════════════════════════

def validate_story(yaml_text: str,
                   theme_path: str = None,
                   loras_dir: str = DEFAULT_LORAS_DIR) -> tuple[bool, list[str]]:
    """第零层：基础 YAML 语法 + 字段完整性检查"""
    errors, warnings = [], []

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return False, [f"YAML 语法错误: {e}"]

    if not isinstance(data, dict):
        return False, ["YAML 根节点不是字典"]

    for field in ("title", "characters", "scene_templates", "pages"):
        if field not in data:
            errors.append(f"缺少必填字段: {field}")
    if errors:
        return False, errors

    characters  = data.get("characters", {})
    scene_tmpls = data.get("scene_templates", {})
    pages       = data.get("pages", [])

    # 构建本地 lora 索引
    lora_index = _build_lora_index(loras_dir)

    # 检查 characters
    for char_name, char_cfg in characters.items():
        if not isinstance(char_cfg, dict):
            errors.append(f"角色 {char_name} 配置格式错误")
            continue
        lora_ref = char_cfg.get("lora_ref", "")
        if lora_ref and lora_ref not in lora_index:
            errors.append(f"角色 {char_name}: lora_ref={lora_ref} 在 loras/ 不存在")
        if not char_cfg.get("key_features"):
            warnings.append(f"角色 {char_name} 缺少 key_features")

    # 检查 scene_templates
    valid_image_types = {"solo_character", "solo_distant", "composite", "background_only"}
    for sname, scfg in scene_tmpls.items():
        if not isinstance(scfg, dict):
            errors.append(f"场景模板 {sname} 格式错误")
            continue
        it  = scfg.get("image_type", "")
        cfg = scfg.get("cfg", 0)
        if it and it not in valid_image_types:
            errors.append(f"场景 {sname}: image_type={it} 非法")
        if cfg and cfg > 5.0:
            warnings.append(f"场景 {sname}: FLUX cfg={cfg} 建议不超过 5.0")

    # 检查 pages
    page_nums = set()
    for page in pages:
        if not isinstance(page, dict):
            errors.append("pages 里有非字典元素")
            continue
        pn = page.get("page")
        if pn in page_nums:
            errors.append(f"page {pn} 重复")
        page_nums.add(pn)
        for char in page.get("characters", []):
            if char not in characters:
                errors.append(f"第{pn}页引用了未定义角色: {char}")
        st = page.get("scene_type", "")
        if st and st not in scene_tmpls:
            errors.append(f"第{pn}页引用了未定义场景类型: {st}")

    is_valid = len(errors) == 0
    if errors:
        print(f"\n  [验证] ✗ {len(errors)} 个错误：")
        for e in errors:
            print(f"    !! {e}")
    if warnings:
        print(f"  [验证] ⚠ {len(warnings)} 个警告：")
        for w in warnings:
            print(f"    ?? {w}")
    if is_valid and not warnings:
        print(f"  [验证] ✓ 通过")

    return is_valid, errors + [f"[警告] {w}" for w in warnings]


def validate_pipeline_compat(yaml_data: dict,
                              server_loras: set,
                              lora_index: dict) -> tuple[bool, list[str], list[str]]:
    """第一层：pipeline 兼容性检查（不调用外部服务，纯逻辑）"""
    errors, warnings = [], []
    valid_image_types = {"solo_character", "solo_distant", "composite", "background_only"}

    characters  = yaml_data.get("characters", {})
    scene_tmpls = yaml_data.get("scene_templates", {})
    pages       = yaml_data.get("pages", [])

    for char_name, char_cfg in characters.items():
        if not isinstance(char_cfg, dict):
            continue
        lora = char_cfg.get("lora", "")
        if lora and server_loras:
            if not _lora_on_server(lora, server_loras):
                errors.append(f"角色 {char_name}: lora={lora} 服务器上不存在")

    for page in pages:
        if not isinstance(page, dict):
            continue
        pn         = page.get("page", "?")
        chars      = page.get("characters", [])
        scene_type = page.get("scene_type", "")
        scene_cfg  = scene_tmpls.get(scene_type, {})
        image_type = scene_cfg.get("image_type", "")

        if image_type and image_type not in valid_image_types:
            errors.append(f"第{pn}页: image_type={image_type} 未知")
        if image_type == "composite" and len(chars) < 2:
            errors.append(f"第{pn}页: composite 场景需要 2 个角色，当前 {len(chars)} 个")
        if image_type == "background_only" and chars:
            warnings.append(f"第{pn}页: background_only 场景有角色 {chars}，建议清空")

        for char in chars:
            if char not in characters:
                errors.append(f"第{pn}页: 引用未定义角色 {char}")
            elif not characters[char].get("lora") and not characters[char].get("trigger_solo"):
                warnings.append(f"第{pn}页: 角色 {char} 无 lora 也无 trigger_solo")

    return len(errors) == 0, errors, warnings


def quality_warnings(yaml_data: dict) -> list[str]:
    """第二层：叙事质量预警（不阻断流程）"""
    warnings = []
    pages       = yaml_data.get("pages", [])
    scene_tmpls = yaml_data.get("scene_templates", {})

    if not pages:
        return warnings

    # 场景重复
    scene_type_counts: dict = {}
    for p in pages:
        st = p.get("scene_type", "")
        scene_type_counts[st] = scene_type_counts.get(st, 0) + 1
    for st, cnt in scene_type_counts.items():
        if cnt / len(pages) > 0.5 and len(pages) >= 4:
            warnings.append(f"场景 '{st}' 占 {cnt}/{len(pages)} 页，建议增加多样性")

    # 旁白长度
    for p in pages:
        narration = p.get("narration", "")
        pn = p.get("page", "?")
        if len(narration) < 8:
            warnings.append(f"第{pn}页旁白太短（{len(narration)}字）")
        if len(narration) > 50:
            warnings.append(f"第{pn}页旁白太长（{len(narration)}字），建议30字内")

    # composite 场景存在性
    total_chars = yaml_data.get("characters", {})
    has_composite = any(
        scene_tmpls.get(p.get("scene_type", "") or "", {}).get("image_type") == "composite"
        for p in pages
    )
    if len(total_chars) >= 2 and not has_composite and len(pages) >= 6:
        warnings.append("有多角色但无 composite 对决场景，可以增加视觉张力")

    return warnings


def run_all_validations(yaml_text: str,
                        theme_path: str = None,
                        loras_dir: str = DEFAULT_LORAS_DIR,
                        server_loras: set = None) -> tuple[bool, dict, str]:
    """统一三层校验 + lora_ref 填充入口"""
    print(f"\n  [校验] 开始三层校验...")

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return False, {"parse_error": str(e)}, yaml_text

    lora_index = _build_lora_index(loras_dir)

    # Layer 3：lora_ref 填充
    print(f"  [校验] Layer 3: lora_ref 自动填充...")
    data, resolve_warnings = resolve_lora_refs(data, lora_index)
    if resolve_warnings:
        for w in resolve_warnings:
            print(f"    ?? {w}")

    updated_yaml = yaml.dump(data, allow_unicode=True, default_flow_style=False,
                              sort_keys=False, width=120)

    # Layer 1：pipeline 兼容
    print(f"  [校验] Layer 1: pipeline 兼容性...")
    ok, compat_errors, compat_warnings = validate_pipeline_compat(
        data, server_loras or set(), lora_index)
    if compat_errors:
        print(f"  [校验] Layer 1 !! {len(compat_errors)} 个错误：")
        for e in compat_errors:
            print(f"    !! {e}")
    if compat_warnings:
        for w in compat_warnings:
            print(f"    ?? {w}")
    if ok:
        print(f"  [校验] Layer 1 ✓")

    # Layer 2：叙事质量
    print(f"  [校验] Layer 2: 叙事质量...")
    q_warnings = quality_warnings(data)
    if q_warnings:
        for w in q_warnings:
            print(f"    ~~ {w}")
    else:
        print(f"  [校验] Layer 2 ✓")

    can_run = ok
    report  = {
        "resolve_warnings": resolve_warnings,
        "compat_errors":    compat_errors,
        "compat_warnings":  compat_warnings,
        "quality_warnings": q_warnings,
    }
    print(f"  [校验] {'✓ 可以运行 twophase' if can_run else '✗ 有错误需要修复'}")
    return can_run, report, updated_yaml


# ════════════════════════════════════════
# 审核
# ════════════════════════════════════════

def _ai_review(yaml_text: str) -> tuple[bool, str]:
    from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage

    system = """你是漫画故事编辑，评审故事 YAML 的叙事质量。
只返回JSON：{"approved": true/false, "score": 1-10, "feedback": "简短评价", "suggestions": ["建议"]}
score >= 7 时 approved=true。评审维度：叙事弧完整、旁白适合配音、场景多样、角色安排合理。"""

    try:
        llm = ChatOpenAI(model=LLM_MODEL, api_key=LLM_API_KEY,
                         base_url=LLM_BASE_URL, temperature=0.3, max_tokens=500)
        r = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content=f"审核：\n\n{yaml_text[:3000]}"),
        ])
        text = r.content
        result = json.loads(text[text.find("{"):text.rfind("}")+1])
        approved = result.get("approved", False)
        score    = result.get("score", 0)
        feedback = result.get("feedback", "")
        print(f"\n  [AI审核] {score}/10  {'✓ 通过' if approved else '✗ 未通过'}  {feedback}")
        for s in result.get("suggestions", []):
            print(f"    → {s}")
        return approved, feedback
    except Exception as e:
        print(f"  [AI审核] 失败: {e}，默认通过")
        return True, ""


def review_story(yaml_text: str,
                 mode: str = "human",
                 output_path: str = None) -> Optional[str]:
    if mode == "auto":
        return yaml_text
    if mode == "ai":
        approved, _ = _ai_review(yaml_text)
        if not approved:
            print(f"  AI 审核未通过，仍要保存？(y/n): ", end="")
            if input().strip().lower() != "y":
                return None
        return yaml_text

    # human 模式
    print(f"\n{'─'*60}")
    print(yaml_text)
    print(f"{'─'*60}")
    print(f"  回车/y=保存  e=编辑  r=重新生成  n=放弃")
    choice = input(f"\n  > ").strip().lower()

    if choice in ("n", "q"):
        return None
    if choice == "r":
        return "REGENERATE"
    if choice == "e" and output_path:
        tmp = output_path + ".tmp"
        Path(tmp).write_text(yaml_text, encoding="utf-8")
        import subprocess, platform as _plat
        editor = "open" if _plat.system() == "Darwin" else "notepad"
        try:
            subprocess.run([editor, tmp])
        except Exception:
            pass
        input("  编辑完成后按回车继续...")
        edited = Path(tmp).read_text(encoding="utf-8")
        Path(tmp).unlink(missing_ok=True)
        return edited
    return yaml_text


# ════════════════════════════════════════
# 主入口函数
# ════════════════════════════════════════

def create_story(concept: str,
                 theme_path: str = DEFAULT_THEME,
                 pages: int = 8,
                 output_path: str = None,
                 series: str = "",
                 review_mode: str = "human",
                 offline: bool = False,
                 max_retries: int = 2) -> Optional[str]:
    """
    创建故事 YAML 的主函数。可直接调用，也可通过 CLI 触发。
    """
    print(f"\n{'='*58}")
    print(f"  故事创作引擎  v3 (FLUX 专版)")
    print(f"{'='*58}")
    print(f"  概念: {concept}")
    print(f"  主题: {theme_path}  页数: {pages}  离线: {offline}")

    context = gather_context(theme_path=theme_path, offline=offline)

    if not context["available_themes"] and not offline:
        print(f"  !! 没有可用 FLUX 主题，检查 themes/ 目录")
        # 警告但不退出，offline 模式继续
        if not offline:
            return None

    if not output_path:
        slug = re.sub(r'[^\w\u4e00-\u9fff]', '_', concept[:20]).strip("_")
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"stories/auto_{ts}_{slug}.yaml"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(max_retries + 1):
        if attempt > 0:
            print(f"\n  [重试] 第 {attempt+1} 次...")

        yaml_text = generate_story(concept, theme_path, pages, context, series)

        # 基础语法验证
        is_valid, errors = validate_story(yaml_text, theme_path)
        if not is_valid and attempt < max_retries:
            concept = concept + f"\n\n注意修正：" + "\n".join(errors[:5])
            continue

        # 三层校验 + lora_ref 填充
        server_loras = context.get("_server_loras", set())
        can_run, report, yaml_text = run_all_validations(
            yaml_text, theme_path, server_loras=server_loras)

        if not can_run and attempt < max_retries:
            errors = report.get("compat_errors", [])
            concept = concept + f"\n\n必须修正：" + "\n".join(errors[:5])
            continue

        # 人工/AI 审核
        result = review_story(yaml_text, mode=review_mode, output_path=output_path)
        if result is None:
            print(f"  已放弃")
            return None
        if result == "REGENERATE" and attempt < max_retries:
            continue
        yaml_text = result
        break

    Path(output_path).write_text(yaml_text, encoding="utf-8")
    print(f"\n  ✓ 故事已保存 → {output_path}")
    print(f"  下一步：python run.py twophase {output_path}")
    return output_path


# ════════════════════════════════════════
# 批量生成接口（为未来"内容工厂"留接口）
# ════════════════════════════════════════

def create_story_batch(concepts: list[str],
                        theme_path: str = DEFAULT_THEME,
                        pages: int = 8,
                        series: str = "",
                        review_mode: str = "auto",
                        offline: bool = False,
                        max_retries: int = 2,
                        delay_between: float = 3.0) -> list[Optional[str]]:
    """
    批量创建多个故事。

    参数:
      concepts:       概念列表（每个概念 → 一个独立故事）
      theme_path:     共用主题
      pages:          每个故事的页数
      review_mode:    批量场景默认 auto（不要弹人工审核）
      delay_between:  两次 API 调用之间的间隔，避免限速

    返回:
      [output_path, ...]  对应每个 concept 的输出路径，失败的为 None

    用例（CLI）：
      python story_writer.py --concept "古宅狐仙" --batch 3

    用例（代码）：
      from story_writer import create_story_batch
      paths = create_story_batch(
          concepts=["古宅狐仙", "深夜画馆", "镜中影"],
          pages=8,
          review_mode="auto",
      )
    """
    print(f"\n{'='*58}")
    print(f"  批量生成模式: {len(concepts)} 个故事")
    print(f"{'='*58}\n")

    results: list[Optional[str]] = []
    for idx, concept in enumerate(concepts, 1):
        print(f"\n────── [批量 {idx}/{len(concepts)}] {concept} ──────\n")
        try:
            path = create_story(
                concept=concept,
                theme_path=theme_path,
                pages=pages,
                output_path=None,    # 让 create_story 自动生成 auto_<ts>_<slug>.yaml
                series=series,
                review_mode=review_mode,
                offline=offline,
                max_retries=max_retries,
            )
            results.append(path)
        except Exception as e:
            print(f"  !! 故事 {idx} 生成失败: {e}")
            results.append(None)

        # 避免 API 限速（最后一个不等）
        if idx < len(concepts):
            import time as _time
            _time.sleep(delay_between)

    # 总结
    success = sum(1 for r in results if r)
    print(f"\n{'='*58}")
    print(f"  批量生成完成: {success}/{len(concepts)} 成功")
    print(f"{'='*58}")
    for idx, (concept, path) in enumerate(zip(concepts, results), 1):
        status = "✓" if path else "✗"
        print(f"  {status} {idx}. {concept[:30]:<30s} → {path or '失败'}")

    return results


# ════════════════════════════════════════
# CLI 独立入口
# ════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="故事创作引擎 v3 (FLUX 专版) - 可独立运行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 生成新故事（人工审核）
  python story_writer.py --concept "月夜古宅中的狐仙传说" --pages 8

  # 离线生成（不查询 ComfyUI）
  python story_writer.py --concept "月夜古宅中的狐仙传说" --pages 8 --offline

  # 全自动，不人工审核
  python story_writer.py --concept "月夜古宅中的狐仙传说" --pages 8 --review auto

  # 查看可用资产
  python story_writer.py --list_assets

  # 只验证不生成
  python story_writer.py --validate stories/haunted_inn.yaml
        """
    )
    parser.add_argument("--concept",     type=str, default=None,
                        help="故事概念，如'月夜古宅中的狐仙传说'")
    parser.add_argument("--batch",       type=int, default=1,
                        help="批量生成数量（默认 1）。>1 时基于 --concept 派生 N 个变体")
    parser.add_argument("--concepts_file", type=str, default=None,
                        help="批量模式：从文本文件读 concept 列表（每行一个）")
    parser.add_argument("--theme",       type=str, default=DEFAULT_THEME,
                        help=f"主题 YAML 路径（默认: {DEFAULT_THEME}）")
    parser.add_argument("--pages",       type=int, default=8,
                        help="页数（默认: 8）")
    parser.add_argument("--series",      type=str, default="",
                        help="系列名称（默认: 空）")
    parser.add_argument("--output",      type=str, default=None,
                        help="输出路径（默认: stories/概念_时间戳.yaml）")
    parser.add_argument("--review",      choices=["human", "auto", "ai"],
                        default="human", help="审核模式（默认: human）")
    parser.add_argument("--offline",     action="store_true",
                        help="离线模式，不查询 ComfyUI")
    parser.add_argument("--list_assets", action="store_true",
                        help="列出当前可用资产后退出")
    parser.add_argument("--validate",    type=str, default=None,
                        help="只验证指定 YAML 文件，不生成")

    args = parser.parse_args()

    # ── 列出资产 ──────────────────────────────────────────
    if args.list_assets:
        ctx = gather_context(theme_path=args.theme, offline=True)
        print_available_assets(ctx)
        return

    # ── 只验证 ────────────────────────────────────────────
    if args.validate:
        yaml_path = Path(args.validate)
        if not yaml_path.exists():
            print(f"  !! 文件不存在: {yaml_path}")
            sys.exit(1)
        yaml_text = yaml_path.read_text(encoding="utf-8")
        print(f"\n  验证: {yaml_path}")
        is_valid, errors = validate_story(yaml_text)
        can_run, report, updated = run_all_validations(yaml_text)
        if can_run:
            print(f"\n  ✓ {yaml_path.name} 验证通过，可以运行 twophase")
        else:
            print(f"\n  ✗ 有错误需要修复")
            sys.exit(1)
        return

    # ── 生成故事 ──────────────────────────────────────────
    # 批量模式：concepts_file > batch > 单个 concept
    if args.concepts_file:
        concepts_path = Path(args.concepts_file)
        if not concepts_path.exists():
            print(f"  !! concepts_file 不存在: {concepts_path}")
            sys.exit(1)
        concepts = [line.strip() for line in
                     concepts_path.read_text(encoding="utf-8").splitlines()
                     if line.strip() and not line.strip().startswith("#")]
        if not concepts:
            print(f"  !! concepts_file 为空")
            sys.exit(1)
        print(f"  从 {concepts_path} 读取 {len(concepts)} 个 concept，批量生成")
        results = create_story_batch(
            concepts=concepts,
            theme_path=args.theme,
            pages=args.pages,
            series=args.series,
            review_mode=args.review,
            offline=args.offline,
        )
        if all(r is None for r in results):
            sys.exit(1)
        return

    if not args.concept:
        parser.print_help()
        print(f"\n  错误：--concept 是必填项（或用 --concepts_file 批量）")
        sys.exit(1)

    # 单个 concept + --batch N：在 concept 后加序号生成变体
    if args.batch > 1:
        concepts = [f"{args.concept}（第{i+1}个变体）" for i in range(args.batch)]
        print(f"  --batch {args.batch}：基于 '{args.concept}' 生成 {args.batch} 个变体")
        results = create_story_batch(
            concepts=concepts,
            theme_path=args.theme,
            pages=args.pages,
            series=args.series,
            review_mode=args.review,
            offline=args.offline,
        )
        if all(r is None for r in results):
            sys.exit(1)
        return

    # 单个故事
    result = create_story(
        concept=args.concept,
        theme_path=args.theme,
        pages=args.pages,
        output_path=args.output,
        series=args.series,
        review_mode=args.review,
        offline=args.offline,
    )

    if result:
        print(f"\n  完成！生成文件: {result}")
    else:
        print(f"\n  已取消或失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
