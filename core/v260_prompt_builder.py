# ════════════════════════════════════════════════════════════════
# v260 多角色 prompt 构造 — 通用版
# ════════════════════════════════════════════════════════════════
# 替换 renderer.py v4 函数里"global_prompt/left_region_prompt/right_region_prompt"
# 这一段(原来你硬编码"Han Yanqing"那一段)。

# ────────────────────────────────────────────────────────────────
# 双人构图 framing 模板 — 按 mask_template 选择合适的 framing 语
# ────────────────────────────────────────────────────────────────
_DUAL_FRAMING_BY_TEMPLATE = {
    "left_right": (
        "two people standing side by side, both fully visible in frame, "
        "dual portrait composition, both faces clearly shown. "
    ),
    "face_to_face": (
        "two people facing each other in profile, both visible in frame, "
        "intimate close-up, both faces clearly shown. "
    ),
    "over_shoulder_left": (
        "over-the-shoulder shot, one person in the foreground left, "
        "the other person facing camera on the right. "
    ),
    "over_shoulder_right": (
        "over-the-shoulder shot, one person in the foreground right, "
        "the other person facing camera on the left. "
    ),
    "foreground_background": (
        "two people, one in the foreground, one in the background, "
        "both clearly visible. "
    ),
}

# ────────────────────────────────────────────────────────────────
# 取角色英文描述 — 优先级:region_prompt > en_name > character 原名
# ────────────────────────────────────────────────────────────────
def _build_char_description(ch: dict, side: str) -> str:
    """从 char 字典构造角色 region 描述。
    
    优先级:
      1. ch['prompt']      — focal_director 写好的角色 region prompt(最好)
      2. ch['en_name']     — character_library 里的英文名 + 基础特征
      3. ch['character']   — 原始中文名(兜底,FLUX 对中文识别差)
    """
    region_prompt = (ch.get("prompt") or "").strip()
    if region_prompt:
        # 已经是完整 region 描述,直接用
        # 加一个位置词强化空间分区
        if side and side not in region_prompt.lower():
            return f"{region_prompt}, on the {side} side of the image"
        return region_prompt

    en_name = (ch.get("en_name") or "").strip()
    name = en_name or ch.get("character") or "person"
    return f"{name}, on the {side} side of the image"


# ────────────────────────────────────────────────────────────────
# 构造三套 prompt:global + left_region + right_region
# ────────────────────────────────────────────────────────────────
def build_v260_prompts(params: dict, char_servers: list,
                        mask_template: str) -> tuple:
    """返回 (global_prompt, left_region_prompt, right_region_prompt)。
    
    global_prompt:    全图共享的背景/光照/风格 + 强制双人 framing
    left_region:      左角色 region 描述
    right_region:     右角色 region 描述
    
    要求 char_servers 至少 2 个,index 0 为左、1 为右。
    """
    if len(char_servers) < 2:
        # 单角色降级:不加 framing,直接用原 prompt
        return params["positive"], "", ""

    left_char = char_servers[0]
    right_char = char_servers[1]

    # ── 1. 双人构图 framing(让 FLUX 允许画两个人) ──
    framing = _DUAL_FRAMING_BY_TEMPLATE.get(
        mask_template, _DUAL_FRAMING_BY_TEMPLATE["left_right"])

    # ── 2. global_prompt = framing + 原 prompt 的"非角色专属"部分 ──
    # 原 prompt 里可能有"Han Yanqing hearing the click"这类"主语暗示单人"的句子,
    # 直接拼会冲突。两种策略:
    #   A. 信任 focal_director:把原 prompt 整个拼上(简单,但单人句会拉回单人)
    #   B. 给 framing 高优先级:framing 放最前,让 FLUX 先看到双人意图
    # 这里用 B,实测有效。
    global_prompt = framing + params["positive"]

    # ── 3. left/right region prompt ──
    left_region_prompt = _build_char_description(left_char, "left")
    right_region_prompt = _build_char_description(right_char, "right")

    return global_prompt, left_region_prompt, right_region_prompt
