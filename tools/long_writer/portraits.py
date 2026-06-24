"""
portraits.py ── v2.3.5 定妆照生成工序
==========================================================
位置：stories/long_xxx.yaml 已生成（convert 之后），twophase 之前。

用途：为故事里的 lead 角色生成定妆照候选，供你人工挑选。
挑好之后，pipeline 走 PuLid 路径会用这张定妆照锁住每张图里的角色脸。

用法：
  # 给所有 lead 角色生成定妆照候选（每角色 4 张）
  python -m tools.long_writer.portraits generate \
      stories/long_20260512_xxx.yaml

  # 也给 extra 角色生成（默认只 lead）
  python -m tools.long_writer.portraits generate \
      stories/long_20260512_xxx.yaml --include-extras

  # 指定要给哪个角色生成（不跑全部）
  python -m tools.long_writer.portraits generate \
      stories/long_20260512_xxx.yaml --character 周建军

  # 看候选列表 + 提示挑选
  python -m tools.long_writer.portraits list stories/long_20260512_xxx.yaml

  # 帮你选某个候选（把 vN 移到正式位置 + 写回 outline.yaml）
  python -m tools.long_writer.portraits pick \
      stories/long_20260512_xxx.yaml --character 周建军 --pick v3

工作流：
  1. portraits generate → 生成候选到 refs/character_portraits/<story>/<角色>_candidates/
  2. 你打开看图，决定哪张
  3. portraits pick → 把选中的图复制到 refs/character_portraits/<story>/<角色>.png
                       并写回 outline.yaml.characters[].portrait_ref
  4. 重跑 convert（让 to_pipeline 把 portrait_ref 透传到 story.yaml）
  5. twophase 时 pipeline 自动走 PuLid 路径
"""

import argparse
import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

import yaml


# ════════════════════════════════════════════════════════════════
# 定妆照 prompt 模板 (v2.3.6 重构)
# ════════════════════════════════════════════════════════════════
#
# 历史问题:旧模板只用 key_features(服装/道具),没注入性别/年龄/民族,
# FLUX 拿"军绿外套 1990s 中国"会画出一张最通用的中年东亚男性面孔,
# 同一故事的三个角色看起来像同一个人。
#
# 新模板按"由抽象到具体"的金字塔注入主体信息:
#   1. subject_anchor: a {age}-year-old {ethnicity} {gender}    ← 锁人种性别年龄
#   2. identity:       身份/职业 (从 desc 提炼)                  ← 给气质
#   3. facial_anchor:  脸型/胡须/眼神 (从 desc/key_features 推断) ← 给"是这个人"
#   4. key_features:   服装/道具 (原有)                          ← 给造型
#   5. era_context:    时代                                      ← 给氛围
# ════════════════════════════════════════════════════════════════

PORTRAIT_PROMPT_TEMPLATE = """{subject_anchor}, {identity}, {facial_anchor},
{key_features}, {era_context},
upper body portrait, neutral studio background, soft front lighting,
sharp focus on face, detailed facial features, distinctive facial structure,
professional photography, realistic photo, 4k, cinematic"""

PORTRAIT_NEGATIVE = """blurry, low quality, watermark, text, signature,
cartoon, anime, painting, multiple people, full body, distant,
weird hands, mutated fingers, deformed face, extra heads,
generic face, plastic skin, doll-like, uncanny"""

# 时代上下文（按 theme_id 选）
ERA_CONTEXTS = {
    "chinese_horror_tales": "1990s mainland China atmosphere",
    "republic_shanghai":    "1930s Republican-era Shanghai",
    "qing_supernatural":    "late Qing dynasty China",
    "modern_urban":         "contemporary urban setting",
    # 兜底
    "default":              "atmospheric historical setting",
}

# 时代对应的"民族/族裔"主语词(给 subject_anchor 用)
# 默认所有 theme 都是 east asian/Chinese,但留 hook 给将来非中国主题
ERA_ETHNICITY = {
    "chinese_horror_tales": "Han Chinese",
    "republic_shanghai":    "Han Chinese",
    "qing_supernatural":    "Han Chinese",
    "modern_urban":         "East Asian",
    "default":              "East Asian",
}


# ── 角色信息推断启发式 ─────────────────────────────────────
# 从 desc / 角色名 / key_features 反推性别和年龄段。
# 用规则而非 LLM:确定性、零调用成本、容易调试。
# 推断失败时回退默认值(中年男性),并打 warning,让用户自己补 outline。

# 性别推断:中文身份词大都隐含性别
_GENDER_KEYWORDS = {
    "male": [
        "退伍兵", "士兵", "军人", "战士", "男人", "男孩", "小伙", "汉子",
        "和尚", "道士", "书生", "举人", "秀才", "公子", "少爷", "老爷",
        "父亲", "爹", "爸", "祖父", "爷爷", "兄长", "哥哥", "弟弟",
        "守墓人", "更夫", "屠夫", "船夫", "农夫", "渔夫", "车夫",
        "管家", "门卫", "保安", "司机", "工人",
        "皇帝", "王爷", "将军", "县令", "知府", "捕头",
    ],
    "female": [
        "女人", "女孩", "姑娘", "少女", "妇人", "妻", "妾",
        "尼姑", "女道", "丫鬟", "婢女", "歌女", "舞女", "妓女",
        "母亲", "娘", "妈", "祖母", "奶奶", "姐姐", "妹妹",
        "皇后", "公主", "王妃", "格格",
        "穿.*?裙", "穿.*?旗袍",
    ],
}

# 年龄段推断:从身份词或 desc 里挑信号
_AGE_KEYWORDS = {
    "young":      ["少女", "少年", "小伙", "姑娘", "学生", "丫鬟", "书生",
                   "公子", "小姐", "童", "幼", "年轻",
                   # 家族晚辈词通常意味着年轻一代
                   "女儿", "儿子", "侄子", "侄女", "孙子", "孙女",
                   "小哥", "小妹", "弟弟", "妹妹"],
    "middle":     ["退伍兵", "壮年", "中年", "父亲", "母亲", "干部", "工人",
                   "记者", "医生", "法医", "警察", "队长", "教师"],
    "old":        ["老人", "老者", "老头", "老婆婆", "老妇", "祖父", "祖母",
                   "爷爷", "奶奶", "守墓人后代", "白发", "老拐", "老头子"],
}

# 年龄段 → 具体年龄区间(给 prompt 用)
_AGE_RANGE = {
    "young":  "20-year-old",
    "middle": "40-year-old",
    "old":    "65-year-old",
}


def _infer_gender(character: dict) -> str:
    """从 outline 显式字段 + desc/key_features 启发式推断性别。
    返回 'male' / 'female';无法判断时返回 ''(让上层兜底)。
    """
    # 1. 优先用 outline 里的显式 gender 字段
    explicit = (character.get("gender") or "").strip().lower()
    if explicit in ("male", "m", "男"):
        return "male"
    if explicit in ("female", "f", "女"):
        return "female"

    # 2. 从 desc 和 key_features 文本里找性别关键词
    haystack = " ".join([
        character.get("desc", ""),
        character.get("key_features", ""),
        character.get("_name_hint", ""),  # 内部传入的角色名,用于辅助
    ]).lower()

    for word in _GENDER_KEYWORDS["female"]:
        if re.search(word, haystack):
            return "female"
    for word in _GENDER_KEYWORDS["male"]:
        if re.search(word, haystack):
            return "male"
    return ""


def _infer_age_band(character: dict) -> str:
    """从 desc/key_features 推断年龄段:'young' / 'middle' / 'old'。
    无法判断返回 'middle'(最常见的兜底)。
    """
    # 1. outline 里的显式 age 字段(数字或字符串都吃)
    age_raw = character.get("age")
    if age_raw is not None:
        try:
            n = int(str(age_raw).strip())
            if n < 30:
                return "young"
            elif n < 55:
                return "middle"
            else:
                return "old"
        except (ValueError, TypeError):
            pass

    haystack = " ".join([
        character.get("desc", ""),
        character.get("key_features", ""),
        character.get("_name_hint", ""),
    ])

    # 用计数法,哪段命中关键词多就归哪段(防止"父亲是退伍兵"被同时归 old+middle)
    scores = {"young": 0, "middle": 0, "old": 0}
    for band, words in _AGE_KEYWORDS.items():
        for w in words:
            if w in haystack:
                scores[band] += 1
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return "middle"   # 完全没信号,默认中年
    return best[0]


def _build_identity_from_desc(character: dict) -> str:
    """从中文 desc 抽出"职业身份"作为英文 prompt 片段。

    策略:
      - desc 短(< 25 字):整段当 identity 描述
      - desc 长:取第一个逗号/句号前的片段
    这里不做翻译——desc 已是中文,FLUX 对夹杂中英文的 prompt 仍能识别"村民/将军"
    等常见词;但我们把英文 hint(en_name 末尾的英文 occupation)也带上以增强可控性。
    实战发现 FLUX 的中文支持其实还行,关键是"主语锚点"是英文。
    """
    desc = (character.get("desc") or "").strip()
    if not desc:
        return "ordinary person"
    # 取第一个停顿前的片段
    first = re.split(r"[，。,.;；]", desc, maxsplit=1)[0].strip()
    if not first:
        return "ordinary person"
    # 限长(避免主语锚 + identity 加起来太长稀释)
    if len(first) > 30:
        first = first[:30]
    return first


def _build_facial_anchor(character: dict, gender: str, age_band: str) -> str:
    """为角色构造英文面部锚点,让每个人脸有可辨识的差异。

    优先级:
      1. outline 里显式的 face_features 字段(如有)
      2. 从 key_features 里挑出脸/胡子/眼睛相关词
      3. 按 gender + age_band 给一个合理默认(只决定"轮廓",不决定"长相")
    """
    explicit = (character.get("face_features") or "").strip()
    if explicit:
        return explicit

    # 从 key_features 里挑面部词
    kf = (character.get("key_features") or "").lower()
    facial_bits = []
    face_keywords = [
        "beard", "mustache", "goatee", "stubble",        # 胡须
        "scar", "wrinkle", "freckle", "mole",            # 痕迹
        "round face", "long face", "square face",        # 脸型
        "narrow eyes", "wide eyes", "almond eyes",       # 眼型
        "thin lips", "full lips",                         # 唇型
        "high cheekbones", "sharp jaw", "soft jaw",      # 骨相
        "weathered skin", "smooth skin", "pale skin",
        "白发", "白须", "胡须", "络腮胡", "山羊胡",
        "疤痕", "皱纹", "雀斑",
    ]
    for kw in face_keywords:
        if kw in kf:
            facial_bits.append(kw)

    # 没挑到面部词,按 gender + age_band 给一个"骨相默认"
    # 注意:这只决定面部气质(粗糙/年轻/沧桑),不画死长相
    if not facial_bits:
        defaults = {
            ("male", "young"):     "smooth skin, fresh face, lean jaw",
            ("male", "middle"):    "weathered skin, sharp jaw, slight stubble",
            ("male", "old"):       "deeply wrinkled face, gray beard, thinning hair",
            ("female", "young"):   "smooth skin, gentle features, soft jaw",
            ("female", "middle"):  "subtle wrinkles, high cheekbones",
            ("female", "old"):     "deeply wrinkled face, gray hair, kind eyes",
        }
        return defaults.get((gender, age_band),
                            "natural facial features, realistic skin texture")
    return ", ".join(facial_bits)


# ════════════════════════════════════════════════════════════════
# 旧/已废弃常量名兼容(防止有外部脚本 import)
# ════════════════════════════════════════════════════════════════


def _build_portrait_prompt(character: dict, theme_id: str = "default",
                            char_name: str = "") -> str:
    """构造单个角色的定妆照 prompt(v2.3.6 重构)。

    新模板由 5 段构成,自抽象到具体:
      1. subject_anchor: "a 40-year-old Han Chinese male"
      2. identity:       "county cultural center veteran soldier"(从 desc 抽)
      3. facial_anchor:  "weathered skin, sharp jaw, slight stubble"
      4. key_features:   原有的服装/道具描述
      5. era_context:    时代氛围

    任何一段缺失时都有合理兜底,不会让 prompt 报错;
    但 subject_anchor 缺失(性别推断完全失败)时会打 warning,
    建议用户在 outline 里补 `gender:` 字段。
    """
    # 把角色名也传进去帮助性别推断(如"林小荷"末字"荷"是女性常用字 —— 不过这里不做,
    # 我们靠 desc + key_features 推,因为名字推断容易踩坑)
    char_for_infer = dict(character)
    if char_name:
        char_for_infer["_name_hint"] = char_name

    gender   = _infer_gender(char_for_infer)
    age_band = _infer_age_band(char_for_infer)
    ethnicity = ERA_ETHNICITY.get(theme_id, ERA_ETHNICITY["default"])

    if not gender:
        print(f"  ⚠ [{char_name or 'unknown'}] 性别推断失败,默认 male。"
              f"建议在 outline.yaml 的该角色加 `gender: female` 或 `male`。")
        gender = "male"

    # 主语锚点 —— 这是最关键的一环
    subject_anchor = f"a {_AGE_RANGE[age_band]} {ethnicity} {gender}"

    # 身份(从 desc 抽)
    identity = _build_identity_from_desc(character)

    # 面部锚点
    facial_anchor = _build_facial_anchor(character, gender, age_band)

    # 服装/道具
    # 注意:当 key_features 为空时,我们 fall back 到 desc。但此时 identity
    # 已经从 desc 抽过一段了,直接整段 desc 进 key_features 会跟 identity 重复
    # (例:孙老拐 desc="守墓人后代，知晓血祠祭祀秘密",identity 抽出"守墓人后代",
    #  fall back 时若用整段 desc,会出现"守墓人后代...守墓人后代，知晓..." 重复)。
    # 因此 fallback 时要剥掉 identity 已覆盖的那部分。
    raw_kf = (character.get("key_features") or "").strip()
    if raw_kf:
        key_features = raw_kf
    else:
        desc_full = (character.get("desc") or "").strip()
        if desc_full and identity and desc_full.startswith(identity):
            # 剥掉前缀 identity,留剩余部分作为造型/状态描述
            rest = desc_full[len(identity):].lstrip("，,。.;； ")
            key_features = rest if rest else "plain clothing"
        else:
            key_features = desc_full or "plain clothing"

    era_context = ERA_CONTEXTS.get(theme_id, ERA_CONTEXTS["default"])

    prompt = PORTRAIT_PROMPT_TEMPLATE.format(
        subject_anchor = subject_anchor,
        identity       = identity,
        facial_anchor  = facial_anchor,
        key_features   = key_features,
        era_context    = era_context,
    )

    # 打调试日志,让用户能看到 prompt 是怎么拼出来的
    print(f"     [prompt-debug] {char_name or '?'}: "
          f"gender={gender}, age={age_band}({_AGE_RANGE[age_band]})")
    print(f"     [prompt-debug]   anchor: {subject_anchor}")
    print(f"     [prompt-debug]   identity: {identity}")
    print(f"     [prompt-debug]   facial: {facial_anchor}")
    return prompt


# ════════════════════════════════════════════════════════════════
# generate 子命令：为角色生成候选定妆照
# ════════════════════════════════════════════════════════════════

def _generate_portraits_for_character(
        char_name: str, character: dict, theme_id: str,
        candidates_dir: Path, n_candidates: int = 4):
    """为单个角色生成 N 张候选定妆照"""
    from core.renderer import comfy_generate_flux, generate_and_wait

    candidates_dir.mkdir(parents=True, exist_ok=True)

    prompt = _build_portrait_prompt(character, theme_id, char_name=char_name)
    print(f"\n  ── 生成 {char_name} 的定妆照({n_candidates} 张候选)──")
    print(f"     prompt: {prompt[:160]}...")

    # 从 theme yaml 取 unet / vae / clip 配置
    theme_cfg = _load_theme_for_id(theme_id)

    for i in range(n_candidates):
        seed = 100000 + (hash(char_name) % 800000) + i * 10007
        prefix = f"{char_name}_v{i+1}_seed{seed}"
        params = {
            "positive":   prompt,
            "negative":   PORTRAIT_NEGATIVE,
            "seed":       seed,
            "cfg":        3.5,
            "steps":      25,
            "sampler":    "euler",
            "_unet":      theme_cfg["unet"],
            "_clip1":     theme_cfg.get("clip1", "clip_l.safetensors"),
            "_clip2":     theme_cfg.get("clip2", "t5xxl_fp8_e4m3fn.safetensors"),
            "_vae":       theme_cfg.get("vae", "ae.safetensors"),
            "prefix":     prefix,
        }
        print(f"     [{i+1}/{n_candidates}] seed={seed}  prefix={prefix}")
        try:
            # v2.3.5 修复：generate_and_wait 是同步函数(内部用 asyncio.run),
            # 不能 await；prefix/save_dir 是必填位置参数,不是 params 里的字段
            generate_and_wait(
                comfy_generate_flux,
                params,
                prefix=prefix,
                save_dir=str(candidates_dir),
            )
        except Exception as e:
            print(f"     ⚠ 候选 {i+1} 生成失败: {e}")
            import traceback
            traceback.print_exc()


def _load_theme_for_id(theme_id: str) -> dict:
    """读 themes/<id>.yaml，提取 model 字段（含 unet/clip/vae）"""
    theme_path = Path("themes") / f"{theme_id}.yaml"
    if not theme_path.exists():
        # 兜底：默认 FLUX 模型
        print(f"  ⚠ 主题文件不存在: {theme_path}，用默认 FLUX 模型")
        return {
            "unet":  "flux1-dev-Q4_K_S.gguf",   # 按你常用 FLUX 模型名改
            "clip1": "clip_l.safetensors",
            "clip2": "t5xxl_fp8_e4m3fn.safetensors",
            "vae":   "ae.safetensors",
        }
    data = yaml.safe_load(theme_path.read_text(encoding="utf-8"))
    model = data.get("model", {})
    return {
        "unet":  model.get("unet", "flux1-dev-Q4_K_S.gguf"),
        "clip1": model.get("clip1", "clip_l.safetensors"),
        "clip2": model.get("clip2", "t5xxl_fp8_e4m3fn.safetensors"),
        "vae":   model.get("vae", "ae.safetensors"),
    }


def _load_outline_chars(story_yaml_path: Path, story_id: str) -> dict:
    """
    从 scripts/<story_id>_outline.yaml 把 characters 信息加载成 {name: cfg} 形式。

    背景: to_pipeline 把 outline → story 时只透传了部分字段(key_features / voice
    / _role),而 outline.characters 里的 gender / age / en_name / desc 是
    portrait prompt 拼装的关键。为避免改动 to_pipeline 引起下游连锁反应,
    portraits 这里直接回查 outline 把丢失字段补上。

    几种可能的 outline 路径(按优先级):
      1. scripts/<story_id>_outline.yaml      ← 标准位置
      2. {story_yaml_path 同目录}/<story_id>_outline.yaml
      3. {story_yaml_path 父目录的 scripts/}<story_id>_outline.yaml

    都没找到 → 返回空 dict,流程继续(只是 portrait 拿不到额外字段)。
    """
    candidates = [
        Path("scripts") / f"{story_id}_outline.yaml",
        story_yaml_path.parent / f"{story_id}_outline.yaml",
        story_yaml_path.parent.parent / "scripts" / f"{story_id}_outline.yaml",
    ]
    outline_path = None
    for p in candidates:
        if p.exists():
            outline_path = p
            break
    if outline_path is None:
        print(f"  [portraits] ⚠ 未找到 outline.yaml(尝试过 "
              f"{[str(p) for p in candidates]}),portrait prompt 只能用 story.yaml 里的字段。"
              f"建议手动指定路径或确认目录结构。")
        return {}

    try:
        data = yaml.safe_load(outline_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"  [portraits] ⚠ 读取 outline 失败: {e}")
        return {}

    # outline.characters 是 list,每项含 name/role/gender/desc/key_features/en_name 等
    raw = data.get("characters") or []
    result = {}
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if not name:
            continue
        # 显式复制感兴趣的字段(不全盘 merge,免得 outline 里某个字段不小心覆盖了 story 的)
        result[name] = {
            k: c[k] for k in
            ("gender", "age", "en_name", "desc", "key_features",
             "face_features", "role")
            if k in c and c[k] not in (None, "")
        }
    print(f"  [portraits] outline 路径: {outline_path}")
    return result


def cmd_generate(args):
    """为故事里的 lead 角色(可选 extra)生成定妆照候选

    v2.3.5 修复：改为同步函数。原 async 版本会因为
    generate_and_wait 内部已经 asyncio.run 导致 event loop 嵌套报错。

    v2.3.5.2 修复：comfy_script 必须先 load(COMFY_SERVER) 才能 import 节点。
    portraits.py 是独立入口（不通过 run.py），所以要自己初始化。
    """
    # ── v2.3.5.2：初始化 comfy_script 连接 ────────────────
    # 没这一步，from comfy_script.runtime.nodes import CLIPTextEncode 会报
    # ImportError: cannot import name 'CLIPTextEncode' ...
    # 因为 comfy_script 的节点是从 ComfyUI /object_info 动态生成的
    try:
        from comfy_script.runtime import load
        from config import COMFY_SERVER
        print(f"  [portraits] 连接 ComfyUI: {COMFY_SERVER}")
        load(COMFY_SERVER)
        print(f"  [portraits] ComfyUI 节点已加载")
    except Exception as e:
        print(f"  ❌ comfy_script 初始化失败: {e}")
        print(f"     请确认 ComfyUI 在 {COMFY_SERVER} 可访问")
        sys.exit(1)

    story_yaml_path = Path(args.story_yaml)
    if not story_yaml_path.exists():
        print(f"  ❌ story 文件不存在: {story_yaml_path}")
        sys.exit(1)
    story = yaml.safe_load(story_yaml_path.read_text(encoding="utf-8"))

    story_id = story.get("story_id") or story_yaml_path.stem
    theme_path = story.get("theme", "themes/default.yaml")
    theme_id = Path(theme_path).stem

    # 从 story.yaml 的 characters 拿（已经被 to_pipeline 透传过 role/_role）
    chars_map = story.get("characters", {})

    # v2.3.6 新增:合并 outline.yaml 的角色字段(gender / age / en_name / desc)
    # 因为 to_pipeline 转换时只透传了部分字段(key_features / voice / _role),
    # gender 和 en_name 这种对 portrait prompt 至关重要的字段丢了。
    # 这里从 scripts/<story_id>_outline.yaml 把丢失的字段补回来。
    outline_chars = _load_outline_chars(story_yaml_path, story_id)
    if outline_chars:
        print(f"  [portraits] 从 outline 合并 {len(outline_chars)} 个角色的扩展字段"
              f"(gender / age / en_name 等)")
        for cname, ocfg in outline_chars.items():
            if cname not in chars_map:
                continue
            # 仅合并 story.yaml 里【缺失】的字段,不覆盖已有的
            for k, v in ocfg.items():
                if k in chars_map[cname]:
                    continue
                if v in (None, "", [], {}):
                    continue
                chars_map[cname][k] = v

    # 筛选要做的角色
    targets = []
    for name, cfg in chars_map.items():
        if name in ("narrator", "narrator_quote"):
            continue
        role = cfg.get("_role") or cfg.get("role", "lead")
        if args.character and name != args.character:
            continue
        if role == "group":
            continue   # group 不需要 portrait
        # v2.8: 默认生 extras,除非显式 --no-extras
        no_extras = getattr(args, "no_extras", False)
        if role == "extra" and no_extras:
            continue
        # 已经有 portrait_ref 且文件存在 → 跳过（除非 --force）
        existing = cfg.get("portrait_ref")
        if existing and Path(existing).exists() and not args.force:
            print(f"  [skip] {name} 已有 portrait_ref: {existing}（--force 重新生成）")
            continue
        targets.append((name, cfg, role))

    if not targets:
        print(f"  ✓ 没有需要生成的角色（全部已有 portrait_ref，或都是 group）")
        return

    print(f"\n  [portraits generate] 故事: {story_id}")
    print(f"  待生成: {[t[0] for t in targets]}")
    print(f"  每角色候选: {args.n_candidates}")
    print(f"  主题: {theme_id}")

    base_dir = Path("refs/character_portraits") / story_id
    for char_name, char_cfg, role in targets:
        cands_dir = base_dir / f"{char_name}_candidates"
        _generate_portraits_for_character(
            char_name, char_cfg, theme_id, cands_dir,
            n_candidates=args.n_candidates,
        )

    print(f"\n  ✓ 全部候选生成完毕")
    print(f"\n  下一步：")
    print(f"    1. 打开看图: {base_dir}")
    print(f"    2. 挑选满意的候选，记下 v 编号（如 v3）")
    print(f"    3. 跑 pick 命令把选中的图固化:")
    for name, _, _ in targets:
        print(f"       python -m tools.long_writer.portraits pick "
              f"{story_yaml_path} --character {name} --pick v<N>")
    print(f"    4. 全部 pick 完后，重跑 convert：")
    print(f"       python -m tools.long_writer.cli convert "
          f"scripts/{story_id}_storyboard.yaml")


# ════════════════════════════════════════════════════════════════
# list 子命令：看候选 + 已选状态
# ════════════════════════════════════════════════════════════════

def cmd_list(args):
    story_yaml_path = Path(args.story_yaml)
    story = yaml.safe_load(story_yaml_path.read_text(encoding="utf-8"))
    story_id = story.get("story_id") or story_yaml_path.stem
    base_dir = Path("refs/character_portraits") / story_id

    print(f"\n  [portraits list] 故事: {story_id}")
    print(f"  base 目录: {base_dir}")
    if not base_dir.exists():
        print(f"  ⚠ 目录不存在，请先跑 generate")
        return

    chars_map = story.get("characters", {})
    for name, cfg in chars_map.items():
        if name in ("narrator", "narrator_quote"):
            continue
        role = cfg.get("_role", "?")
        portrait = cfg.get("portrait_ref", "")
        cands_dir = base_dir / f"{name}_candidates"

        print(f"\n  {name} ({role}):")
        if portrait and Path(portrait).exists():
            print(f"    ✅ 已选: {portrait}")
        else:
            print(f"    ❌ 未选")
        if cands_dir.exists():
            cands = sorted(cands_dir.glob("*.png"))
            for c in cands:
                print(f"    候选: {c.name}")
        else:
            print(f"    （还没生成候选）")


# ════════════════════════════════════════════════════════════════
# pick 子命令：固化某候选 + 写回 outline
# ════════════════════════════════════════════════════════════════

def cmd_pick(args):
    story_yaml_path = Path(args.story_yaml)
    story = yaml.safe_load(story_yaml_path.read_text(encoding="utf-8"))
    story_id = story.get("story_id") or story_yaml_path.stem
    char_name = args.character
    pick = args.pick   # "v3" 或类似

    base_dir = Path("refs/character_portraits") / story_id
    cands_dir = base_dir / f"{char_name}_candidates"
    if not cands_dir.exists():
        print(f"  ❌ 候选目录不存在: {cands_dir}")
        sys.exit(1)

    # 找匹配 pick 编号的图
    cands = sorted(cands_dir.glob(f"{char_name}_{pick}_*.png"))
    if not cands:
        # 兜底：用 vN 模糊匹配
        cands = sorted(cands_dir.glob(f"*{pick}*.png"))
    if not cands:
        print(f"  ❌ 找不到 {pick} 对应的候选图")
        print(f"     可用候选:")
        for c in sorted(cands_dir.glob("*.png")):
            print(f"       {c.name}")
        sys.exit(1)
    src = cands[0]

    # 目标路径
    dst = base_dir / f"{char_name}.png"
    shutil.copy2(src, dst)
    print(f"  ✓ 已选: {src.name} → {dst}")

    # 写回 outline.yaml.characters[].portrait_ref
    # 路径选用相对 mac 项目根（最干净）
    rel_dst = dst.as_posix()
    if rel_dst.startswith("./"):
        rel_dst = rel_dst[2:]

    # 找 outline 路径
    outline_path = Path(f"scripts/{story_id}_outline.yaml")
    if not outline_path.exists():
        # 兜底：从 story 元数据找
        for p in story.get("pages", []):
            anc = p.get("_outline_path", "")
            if anc:
                outline_path = Path(anc)
                break
    if not outline_path.exists():
        print(f"  ⚠ outline.yaml 找不到，无法自动写回 portrait_ref")
        print(f"     请手工编辑 outline.yaml 加: characters[{char_name}].portrait_ref: {rel_dst}")
        return

    outline = yaml.safe_load(outline_path.read_text(encoding="utf-8"))
    written = False
    for c in outline.get("characters", []):
        if c.get("name") == char_name:
            c["portrait_ref"] = rel_dst
            written = True
            break
    if not written:
        print(f"  ⚠ outline.yaml 里找不到角色 {char_name}")
        return
    outline_path.write_text(
        yaml.dump(outline, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    print(f"  ✓ 已写回 {outline_path}: {char_name}.portrait_ref = {rel_dst}")
    print(f"\n  下一步：重跑 convert 让 to_pipeline 透传 portrait_ref：")
    print(f"    python -m tools.long_writer.cli convert scripts/{story_id}_storyboard.yaml")


# ════════════════════════════════════════════════════════════════
# main
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="定妆照生成工序（v2.3.5）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="为 lead 角色生成定妆照候选")
    p_gen.add_argument("story_yaml")
    p_gen.add_argument("-n", "--n-candidates", type=int, default=4)
    p_gen.add_argument("--character", default=None,
                       help="只为指定角色生成（默认全部 lead）")
    p_gen.add_argument("--include-extras", action="store_true",
                       help="也给 extra 配角生成")
    p_gen.add_argument("--force", action="store_true",
                       help="即使已有 portrait_ref 也重新生成")

    p_list = sub.add_parser("list", help="看候选 + 已选状态")
    p_list.add_argument("story_yaml")

    p_pick = sub.add_parser("pick", help="固化某候选为正式定妆照")
    p_pick.add_argument("story_yaml")
    p_pick.add_argument("--character", required=True)
    p_pick.add_argument("--pick", required=True,
                        help="候选编号（如 v3）")

    args = parser.parse_args()

    if args.cmd == "generate":
        cmd_generate(args)   # v2.3.5 同步
    elif args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "pick":
        cmd_pick(args)


if __name__ == "__main__":
    main()
