from config import SOVITS_URL
"""
to_pipeline.py ── 长故事 → pipeline 兼容格式（Step 6）v2.3.3
==========================================================
v2.0 改造：消费新版 storyboard.yaml 的 shots[] 结构
v2.3.3：
  - 透传 shot.dialogue → page.dialogue（voice_engine 触发角色专属配音）
  - focal_subject 一致性审计（防止 visible_characters 跟 focal 主体冲突）
  - cutaway focal_subject 注入 scene_anchor（消除纯黑背景）

旧版：1 segment → 1 page
新版：1 shot → 1 page（每页带独立 narration + transition_in + kb_direction）

producer_v2.py 的契约（已确认）：
  - page_cfg["narration"] → TTS → audio_info.audio_path
  - page_cfg["dialogue"]  → 每条 dialogue 走角色专属 voice
  - clip_dur = max(audio + 0.3, 4.0)  ← 谁长用谁
  - motion_plan[pn] 必含 "motion" 和 "kb_direction"
  - bgm_mood 从 motion_plan 读
  - storyboard.py 检测 _prebuilt_storyboard 跳过 LLM

我们做的事：
  1. 把 shots 平铺成 pages（page_num 全片连续编号）
  2. 每页填 narration（LLM 改写后的，1-2 句）
  3. 每页填 dialogue（v2.3.3：从 shot 透传，给 producer 走角色配音）
  4. 每页填 _prebuilt_storyboard（主管线 storyboard.py 直接消费）
  5. 每页填 motion_hint + motion + kb_direction（producer 直接消费）
  6. transition_in 透传到 page._transition_in（给未来扩展用，producer 暂不读）
  7. v2.3.3：cutaway focal_subject 自动注入 scene_anchor
"""

import argparse
import random
from pathlib import Path

import yaml


SCRIPTS_DIR = Path("scripts")
STORIES_DIR = Path("stories")


# 通用场景模板（与旧版一致——主管线已依赖这些 key）
GENERIC_SCENES = {
    "narrative_wide": {
        "desc":       "叙事远景",
        "image_type": "background_only",
        "scene":      "atmospheric establishing shot, wide composition, environmental detail",
        "neg_add":    "modern, daylight, cheerful",
        "cfg":        3.0,
        "steps":      20,
        "sampler":    "euler",
    },
    "narrative_medium": {
        "desc":       "叙事中景",
        "image_type": "solo_distant",
        "scene":      "medium shot, character in environment, moody atmosphere",
        "neg_add":    "modern clothing, bright daylight",
        "cfg":        3.0,
        "steps":      20,
        "sampler":    "euler",
    },
    "narrative_closeup": {
        "desc":       "叙事特写",
        "image_type": "solo_character",
        "scene":      "close-up shot, emotional expression, detailed face",
        "neg_add":    "blurry, distant, modern",
        "cfg":        3.5,
        "steps":      25,
        "sampler":    "euler",
    },
    "key_moment": {
        "desc":       "关键瞬间（拟用动态镜头）",
        "image_type": "solo_character",
        "scene":      "dramatic key moment, intense atmosphere, dynamic composition",
        "neg_add":    "static, dull, modern",
        "cfg":        3.5,
        "steps":      25,
        "sampler":    "euler",
    },
}


def _shot_to_scene_type(shot_type: str, dynamic: bool) -> str:
    if dynamic:
        return "key_moment"
    if shot_type in ("extreme_wide", "wide"):
        return "narrative_wide"
    if shot_type in ("closeup", "extreme_closeup"):
        return "narrative_closeup"
    return "narrative_medium"


def _normalize_must_haves(raw: list) -> list:
    """v2.1 兜底清洗：兼容字符串/dict 两种格式，确保下游拿到统一 dict 结构。
    long_storyboard.py 已做过一次，但万一是旧分镜文件喂进来这里再兜一次。
    """
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw[:3]:
        if isinstance(m, dict) and m.get("element"):
            out.append({
                "element":    str(m.get("element", "")).strip(),
                "attributes": str(m.get("attributes", "")).strip(),
                "state":      str(m.get("state", "")).strip(),
                "mood":       str(m.get("mood", "")).strip(),
                "exclude":    str(m.get("exclude", "")).strip(),
            })
        elif isinstance(m, str) and m.strip():
            out.append({
                "element":    m.strip(),
                "attributes": "",
                "state":      "",
                "mood":       "",
                "exclude":    "",
            })
    return out


def _motion_hint_for(shot_type: str, transition: str) -> str:
    """决定 motion_hint（影响 KB 速度强度）"""
    if transition in ("push_in", "pull_out", "whip_pan"):
        return "high"
    if shot_type in ("closeup", "extreme_closeup"):
        return "medium"
    if transition == "hold":
        return "low"
    return "low"


# ════════════════════════════════════════════════════════════════
# v2.3.3 辅助：一致性审计 + scene_anchor 注入
# ════════════════════════════════════════════════════════════════

def _detect_focal_main_character(focal_subject: str,
                                    char_names: list) -> str:
    """
    从 focal_subject 文本中检测"画面主体是哪个角色"。
    简单启发式：找 focal_subject 里命中的第一个角色名（中文）或其英文 trigger 短语。
    没命中返回 ""。
    """
    if not focal_subject or not char_names:
        return ""
    # 1. 中文角色名直接匹配
    for name in char_names:
        if name and name in focal_subject:
            return name
    # 2. 英文 trigger 启发式匹配（拼音/英文名映射，留给未来扩展）
    # 当前先只做中文名匹配，已经能解决 p10/p11 类问题
    return ""


def _audit_focal_consistency(shot: dict, char_names: list) -> tuple:
    """
    检查 focal_subject 主体跟 visible_characters 是否一致。

    返回 (is_consistent, focal_subject_main, message)
      - 一致：(True, focal_subject_main_or_None, "")
      - 冲突：(False, focal_subject_main, "warning message")

    仅做检测和警告，不自动修改（修改策略由调用方决定）。
    """
    focal = shot.get("focal_subject", "")
    visible = shot.get("visible_characters", []) or []
    if not focal:
        return True, "", ""

    focal_main = _detect_focal_main_character(focal, char_names)
    if not focal_main:
        # focal 里没命中任何角色名——可能是道具/环境特写，OK
        return True, "", ""

    if not visible:
        # focal 里有角色但 visible_characters 是空——LLM 不诚实，警告
        return False, focal_main, \
               f"focal 主体='{focal_main}' 但 visible_characters=[]"

    if focal_main not in visible:
        # 冲突：focal 里的人不在 visible_characters 里
        return False, focal_main, \
               f"focal 主体='{focal_main}' 不在 visible_characters={visible} 中"

    return True, focal_main, ""


def _extract_scene_anchor(prev_focal: str) -> str:
    """
    从前一镜的 focal_subject 里提取场景锚点（去掉人物描述，只留环境）。
    简化策略：保留逗号分隔的片段中**不含人物描述**的部分。
    """
    if not prev_focal:
        return ""
    # 按逗号切片，去掉明显是人物描述的片段
    person_keywords = [
        # 所有格 + 身体部位
        "'s ", " face", " hand", " eyes", " brow", " expression",
        " smile", " posture", "'s body", " head", " hair", " mouth",
        " lip", " lips", " lipstick", " skin", " ear", " nose",
        # 动作/姿态
        " standing", " sitting", " crouching", " looking", " lying",
        " holding", " gripping", " pointing", " walking",
        # 服饰
        " wearing", " cheongsam", " hanfu", " glasses",
        # 景别词（这些跟主体绑定）
        " in closeup", "in closeup",
    ]
    parts = [p.strip() for p in prev_focal.split(",")]
    env_parts = []
    for p in parts:
        p_low = p.lower()
        if any(kw in p_low for kw in person_keywords):
            continue
        env_parts.append(p)
    return ", ".join(env_parts[:4])  # 留前 4 段足够锚定场景


def convert_to_pipeline(storyboard_path: str,
                        output_path: str = None) -> str:
    sb_path = Path(storyboard_path)
    if not sb_path.exists():
        print(f"  ❌ 找不到分镜文件: {sb_path}")
        return ""

    with open(sb_path, encoding="utf-8") as f:
        sb_data = yaml.safe_load(f)

    schema_version = sb_data.get("schema_version", "1.0")
    if schema_version != "2.0":
        print(f"  ⚠ 检测到旧版 schema ({schema_version})，建议重跑 long_storyboard.py")
        # 仍尝试运行（向下兼容）

    outline_path = sb_data.get("outline_path")
    if not outline_path or not Path(outline_path).exists():
        print(f"  ❌ 找不到 outline: {outline_path}")
        return ""

    with open(outline_path, encoding="utf-8") as f:
        outline = yaml.safe_load(f)

    story_id = sb_data["story_id"]
    title    = sb_data["title"]
    theme_id = outline.get("theme_id", "chinese_horror_tales")

    # ── 角色：透传 outline 全部字段（含 LoRA），不丢失任何关键信息 ──
    # v2.1 修复：旧版只取 4 个字段，丢了 lora_ref/lora/lora_strength/lora_status/
    # trigger_multi 等，导致主管线生图时角色一致性失效。
    # v2.3.3：引入 role 字段（lead / extra / group），区分视觉锁定 vs 仅配音
    # v2.4.2：透传 demographics(gender/age/en_name/face_features),
    #         让下游(尤其是 portraits.py)直接读 story.yaml 拼 portrait prompt,
    #         不再需要回查 outline.yaml。彻底修复"全片角色长一张通用脸"问题。
    CHAR_PASSTHROUGH_FIELDS = (
        "desc", "key_features",
        "trigger_solo", "trigger_multi",
        "lora_ref", "lora", "lora_strength", "lora_status",
        "portrait_ref",   # v2.3.5：定妆照路径（PuLid 用），相对 mac 项目根
        # ↓ v2.4.2 新增:portrait prompt 拼装必需字段
        "gender",         # 'male' | 'female'(outline 阶段已规范化,不会是 unknown)
        "age",            # 整数年龄
        "en_name",        # 英文名,用于英文 prompt 主语
        "face_features",  # 面部独特性英文短语(脸型/眉眼/胡须等)
    )
    characters = {}
    # v2.3.3：分组追踪
    leads_set  = set()    # 进 page.characters → 触发 LoRA + 严格人脸评分
    extras_set = set()    # 仅作 voice 用，不进 page.characters
    groups_set = set()    # 既不进 page.characters 也不进 dialogue.speaker
    for c in outline.get("characters", []):
        name = c.get("name")
        if not name:
            continue
        # v2.3.3：role 字段（旧大纲默认 lead 兼容）
        role = (c.get("role", "lead") or "lead").lower()
        if role not in ("lead", "extra", "group"):
            print(f"  [to_pipeline] ⚠ 角色 {name} role='{role}' 非法 → 当 lead")
            role = "lead"

        char_entry = {"_role": role}   # 角色分级标记，后续模块可读
        for fld in CHAR_PASSTHROUGH_FIELDS:
            if fld in c:
                char_entry[fld] = c[fld]
        # voice：优先用 outline 已有的 voice dict，否则按 voice_ref_id 兼容旧字段
        if isinstance(c.get("voice"), dict):
            char_entry["voice"] = c["voice"]
        elif role == "group":
            # group 不需要个人 voice
            pass
        else:
            char_entry["voice"] = {
                "engine": "gpt_sovits",
                "ref_id": c.get("voice_ref_id", "narrator_male_storyteller"),
            }
        # trigger_solo 兜底（lead 必填，extra 可选，group 不要）
        if role == "lead" and not char_entry.get("trigger_solo"):
            char_entry["trigger_solo"] = char_entry.get("key_features", "")

        characters[name] = char_entry
        if role == "lead":
            leads_set.add(name)
        elif role == "extra":
            extras_set.add(name)
        else:
            groups_set.add(name)

    characters["narrator"] = {
        "_role": "narrator",
        "voice": {
            "engine":   "edge_tts",
            "voice_id": "zh-CN-YunjianNeural",
        }
    }
    # v2.3.3：narrator_quote 是"代述角色台词的旁白"
    # 用 GPT-SoVITS 说书人腔，比平静 edge_tts 更适合念引语
    # 触发场景：dialogue.speaker 是 group/未建模 → 降级时优选 narrator_quote
    characters["narrator_quote"] = {
        "_role": "narrator",
        "voice": {
            "engine": "gpt_sovits",
            "ref_id": "narrator_male_storyteller",
        }
    }

    print(f"  [to_pipeline] 角色分级: "
          f"lead={sorted(leads_set)}, "
          f"extra={sorted(extras_set)}, "
          f"group={sorted(groups_set)}")

    # ── 平铺 shots → pages ───────────────────────────────────
    pages = []
    page_num = 1
    chapter_breaks = []   # 记录每章起始页，便于调试
    prev_non_hold_page = None   # 用于 hold 镜头复用前帧
    # v2.3.3：维护"当前场景锚点"——非 hold 页生成时更新，cutaway 页注入
    current_scene_anchor = ""
    # v2.3.3：一致性审计统计
    audit_warnings = []
    audit_fixes    = []
    # 角色名白名单（中文）
    all_char_names = [n for n in characters.keys() if n != "narrator"]
    # v2.3.3：未建模角色追踪（visible_characters / dialogue.speaker 里出现的未建模名）
    unmodeled_names = {}      # {name: count}
    # v2.3.3：dialogue/narration 重复消减追踪
    n_narration_stripped = 0  # 多少页因为 dialogue 拆出而清洗了 narration

    for chapter in sb_data.get("chapters", []):
        # v2 新结构：shots[]；兜底兼容 v1 segments[]
        shots = chapter.get("shots") or chapter.get("segments", [])
        if not shots:
            continue
        chapter_breaks.append({"id": chapter["id"], "start_page": page_num})
        # v2.3.4 B 线：本章首页编号，用于 visual_ref.from_shot → from_page 换算
        chapter_first_page = page_num

        for shot_idx, shot in enumerate(shots):
            seed = random.randint(1_000_000, 9_999_999)
            shot_type = shot.get("shot_type", "medium")
            dynamic   = bool(shot.get("dynamic", False))
            transition = shot.get("transition_in", "match_cut")
            kb_dir     = shot.get("kb_direction", "zoom_in")

            # narration 在 v2 schema 是必填的；v1 兼容用 text
            narration = shot.get("narration") or shot.get("text", "")

            # v2.3.3：dialogue 透传 + speaker 分级处理
            # role 体系：
            #   lead/extra → 保留 speaker，走对应 voice
            #   group → 降级 narrator（群体没有个人 voice）
            #   未建模 → 记录到 unmodeled_names + 降级 narrator
            raw_dialogue = shot.get("dialogue", []) or []
            dialogue_clean = []
            for dl in raw_dialogue:
                if not isinstance(dl, dict):
                    continue
                sp = str(dl.get("speaker", "")).strip()
                tx = str(dl.get("text", "")).strip()
                if not sp or not tx:
                    continue
                if sp == "narrator":
                    pass  # 旁白合法
                elif sp == "narrator_quote":
                    pass  # 旁白代述合法
                elif sp in groups_set:
                    # group 类不应该被当作 dialogue speaker（无个人 voice）
                    # 降级到 narrator_quote（带情绪的旁白代述）
                    print(f"  [to_pipeline] ⚠ p{page_num} dialogue speaker '{sp}' "
                          f"是 group 群体，降级 narrator_quote（旁白代述）")
                    sp = "narrator_quote"
                elif sp not in characters:
                    # 未建模配角——分镜大师/编剧大师编造的角色
                    unmodeled_names[sp] = unmodeled_names.get(sp, 0) + 1
                    print(f"  [to_pipeline] ⚠ p{page_num} dialogue speaker '{sp}' "
                          f"未在大纲建模，降级 narrator_quote")
                    sp = "narrator_quote"
                # 其他情况（lead/extra）通过
                dialogue_clean.append({"speaker": sp, "text": tx})

            # v2.3.3：narration 去重——若 dialogue.text 已包含在 narration 里，
            # 从 narration 剥离对应引号片段，防止音频播放两遍同一台词
            if dialogue_clean and narration:
                stripped = narration
                for dl in dialogue_clean:
                    text = dl["text"]
                    # 尝试多种引号包裹形式
                    variants = [
                        f'"{text}"', f"'{text}'",
                        f'"{text}"', f"'{text}'",
                        f"「{text}」", f"『{text}』",
                        text,  # 裸文本（最后兜底）
                    ]
                    for v in variants:
                        if v and v in stripped:
                            stripped = stripped.replace(v, "")
                            break
                # 清理孤立的标点
                import re as _re
                stripped = _re.sub(r'^[，。、！？\s"\'"\']+', '', stripped)
                stripped = _re.sub(r'[，、\s"\'"\']+$', '', stripped)
                stripped = _re.sub(r'[，。]{2,}', '。', stripped)
                stripped = stripped.strip()
                if stripped != narration.strip():
                    n_narration_stripped += 1
                    narration = stripped

            scene_type = _shot_to_scene_type(shot_type, dynamic)

            # v2.3：visible_characters → page.characters 回灌
            # 触发 LoRA + trigger_solo + 严格人脸评分
            # v2.3.3：只保留 lead 角色（extra/group 不进 page.characters）
            #         配角的视觉由 focal_subject 描述承担，不走 LoRA
            #
            # v2.9：同时算 valid_render(画面所有角色,含 extras),供
            #       page._render_chars 用。这条 list 喂给 v2.6 多角色 PuLID
            #       路由,但【不】触发 LoRA / LlavaGate / compositor。
            #       两个字段职责严格分离:
            #         characters    = LoRA 触发名单(只 lead)
            #         _render_chars = 画面角色名单(lead + extras)
            # v2.10: 优先消费 storyboard 产出的 render_characters (完整 dict,含 en/region)。
            #   - page_render_characters: 透传给下游 v260 router (它读 page.render_characters)
            #   - valid_visible: LoRA 触发名单 (只 lead, ≤1)
            #   - valid_render: 画面角色名 list[str] (lead+extra), 兼容老 _render_chars 字段
            # 老 yaml 无 render_characters 时, 回退从 visible_characters 反推。
            raw_render = shot.get("render_characters")
            page_render_characters = []   # list[dict] 透传给 router
            valid_visible = []            # 进 page.characters (仅 lead, LoRA)
            valid_render = []             # 画面角色名 (lead + extra)

            if isinstance(raw_render, list) and raw_render:
                for rc in raw_render:
                    if isinstance(rc, dict):
                        name = str(rc.get("name", "")).strip()
                    elif isinstance(rc, str):
                        name = rc.strip()
                        rc = {"name": name}
                    else:
                        continue
                    if not name or name == "narrator":
                        continue
                    if name in leads_set:
                        valid_visible.append(name)
                        valid_render.append(name)
                        page_render_characters.append(rc)
                    elif name in extras_set:
                        valid_render.append(name)
                        page_render_characters.append(rc)
                    elif name in groups_set:
                        pass
                    else:
                        unmodeled_names[name] = unmodeled_names.get(name, 0) + 1
            else:
                # ── 老 yaml 兜底: 从 visible_characters 反推 ──
                visible_chars = shot.get("visible_characters", [])
                if not isinstance(visible_chars, list):
                    visible_chars = []
                for c in visible_chars:
                    if not isinstance(c, str) or not c.strip():
                        continue
                    if c == "narrator":
                        continue
                    if c in leads_set:
                        valid_visible.append(c)
                        valid_render.append(c)
                        page_render_characters.append({"name": c})
                    elif c in extras_set:
                        valid_render.append(c)
                        page_render_characters.append({"name": c})
                    elif c in groups_set:
                        pass
                    else:
                        unmodeled_names[c] = unmodeled_names.get(c, 0) + 1

            # v2.10: LoRA 名单收敛到 ≤1 (派生侧已做, 这里再保险)
            if len(valid_visible) > 1:
                valid_visible = valid_visible[:1]

            # v2.3：场景延续上下文
            prev_anchor = shot.get("previous_shot_anchor", "").strip()
            is_hold = (transition == "hold")
            # v2.3.2：hold 类型（"extend" / "cutaway" / ""）
            hold_type = (shot.get("_hold_type", "") or "").strip().lower()
            if is_hold and hold_type not in ("extend", "cutaway"):
                # 向下兼容：没标 _hold_type 的旧 hold 默认为 extend
                hold_type = "extend"
            if not is_hold:
                hold_type = ""

            # v2.3.3 ────────────────────────────────────────────
            # focal_subject 一致性审计 + scene_anchor 注入
            # ───────────────────────────────────────────────────
            focal_raw = shot.get("focal_subject", "")
            focal_for_prompt = focal_raw

            # 1) cutaway 单人原则二次防御（storyboard 已做，这里兜底）
            if is_hold and hold_type == "cutaway" and len(valid_visible) > 1:
                print(f"  [to_pipeline] ⚠ p{page_num} cutaway visible_characters "
                      f"多人 {valid_visible} → 兜底保留首个")
                valid_visible = valid_visible[:1]

            # 2) 一致性审计：focal 主体 vs visible_characters
            is_consistent, focal_main, msg = _audit_focal_consistency(
                {"focal_subject": focal_raw,
                 "visible_characters": valid_visible},
                all_char_names,
            )
            if not is_consistent:
                # 策略：以 visible_characters 为准（信任 v2.3 已建立的角色绑定）
                # 把 focal_main（错误的主体名）从 focal_subject 里删除
                # 这能消除"focal=胡三娘 但 visible=[林陌]"导致的 prompt 互殴
                if valid_visible:
                    # 有 visible：把 focal 主体改成 visible[0] 对应的人
                    audit_warnings.append({
                        "page": page_num,
                        "issue": msg,
                        "action": f"focal 主体 '{focal_main}' → 改为 '{valid_visible[0]}'",
                    })
                    # 替换 focal 里第一处出现的 focal_main 为 visible[0]
                    focal_for_prompt = focal_raw.replace(
                        focal_main, valid_visible[0], 1)
                    audit_fixes.append(page_num)
                else:
                    # 没 visible：把 focal 里的人物描述删掉，留环境
                    # 简单策略：截到第一个逗号后的内容（人物在前，环境在后）
                    audit_warnings.append({
                        "page": page_num,
                        "issue": msg,
                        "action": f"focal 主体 '{focal_main}' 删除（无 visible）",
                    })
                    parts = focal_raw.split(",", 1)
                    focal_for_prompt = parts[1].strip() if len(parts) > 1 else ""

            # 3) scene_anchor 注入（cutaway 镜头）
            #    cutaway 的 focal 即使经过 storyboard 增强，也可能跟前镜场景脱节
            #    把 current_scene_anchor 拼到末尾，给 FLUX 一个稳定的场景锚
            # v2.3.6：去重——orchestrator 会用 _prev_shot_anchor 另加
            #    "continuing scene from ..."，与本段语义重叠时会挤占 CLIP-L token，
            #    若 prev_anchor 已覆盖同一场景锚则不重复拼。
            if is_hold and hold_type == "cutaway" and current_scene_anchor:
                anchor_lc = current_scene_anchor.lower()
                prev_lc = (prev_anchor or "").lower()
                already = (
                    anchor_lc in focal_for_prompt.lower()
                    or (prev_lc and (prev_lc in anchor_lc
                                     or anchor_lc in prev_lc))
                )
                if not already:
                    focal_for_prompt = (
                        f"{focal_for_prompt}, "
                        f"scene continues from: {current_scene_anchor}"
                    )

            page = {
                "page":       page_num,
                "title":      f"{chapter['id']}-sh{shot_idx+1:02d}",
                "characters": valid_visible,   # v2.3：从 visible_characters 回灌(仅 lead)
                "_render_chars": valid_render,  # v2.9：画面里所有角色(lead + extras),给 v2.6 多 PuLID 路由用
                "render_characters": page_render_characters,  # v2.10：完整dict(含en/region),router 读此字段
                "scene_type": scene_type,
                "seed":       seed,
                "narration":  narration,
                "dialogue":   dialogue_clean,   # v2.3.3：透传角色台词
                "bgm_mood":   shot.get("bgm_mood", "tension"),
                # 给主管线 storyboard.py 用
                "_prebuilt_storyboard": {
                    "shot_type":         shot_type,
                    "focal_subject":     focal_for_prompt,   # v2.3.3：审计/注入后版本
                    "visual_must_haves": _normalize_must_haves(
                        shot.get("visual_must_haves", [])),
                    "composition_hint":  "",
                    "transition_from":   transition,
                },
                # v2.2 字段
                "transition_in":    transition,
                "_source_chapter":  chapter["id"],
                "_source_seg":      shot.get("source_seg", ""),
                "_revision_notes":  shot.get("_revision_notes", []),
                # v2.3 新增：让主线程 orchestrator 走快速通道，不调 LLM 翻译 narration
                "_skip_llm_alignment": True,
                # v2.3 新增：场景延续锚点
                "_prev_shot_anchor":   prev_anchor,
                # v2.3 新增：hold 镜头标记（pipeline 跳过生图，producer 拼 audio）
                "_hold":               is_hold,
                "_hold_source_page":   prev_non_hold_page if is_hold else None,
                # v2.3.2 新增：hold 类型
                #   "extend"  → 画面延续上一页，不生图，audio 拼上一页
                #   "cutaway" → 切到新画面（反应/道具/环境），正常生图、正常评分、独立 clip
                #   ""        → 非 hold 页
                "_hold_type":          hold_type,
                # v2.3.4 新增：分镜大师指定的"画面停留秒数"（音频结束后画面额外保留）
                # producer 计算公式：clip_dur = max(audio + 0.3 + intended_duration_sec, 4.0)
                # 缺失/0 时行为完全等同 v2.3.3，零回归风险
                "intended_duration_sec": float(shot.get("intended_duration_sec", 0.0) or 0.0),
                "scene_audio": shot.get("scene_audio") or {"ambient": [], "sfx": []},
                "narration_emotion": shot.get("narration_emotion", "neutral"),
            }

            # v2.3.4 B 线：visual_ref 字段透传（Redux 视觉参考链）
            # 把 storyboard 里"章内 1-based shot 序号"换算为"全片 page 序号"
            # 字段不存在时不输出 _visual_ref，pipeline 收不到就走普通 FLUX
            vr = shot.get("visual_ref")
            if isinstance(vr, dict):
                try:
                    from_shot = int(vr.get("from_shot", 0))
                    vr_strength = float(vr.get("strength", 0.5))
                    if 1 <= from_shot < shot_idx + 1 and 0.0 < vr_strength <= 1.0:
                        from_page = chapter_first_page + from_shot - 1
                        if from_page < page_num:   # 双重防御
                            page["_visual_ref"] = {
                                "from_page": from_page,
                                "strength":  vr_strength,
                            }
                except (TypeError, ValueError):
                    pass  # 非法值忽略，本镜走普通 FLUX

            # v2.3.5：portrait_refs 透传（PuLid 角色锁脸）
            # v2.9 升级：列出本镜【画面里所有角色】(lead + extras with portrait)
            # pipeline:
            #   - 单角色镜(len=1) → v2.3.5 单 PuLID
            #   - 多角色镜(len>=2) → v2.6 双 PuLID + Regional Prompter
            # extras 没 LoRA 但有 portrait,FLUX 用 portrait 给配角锁脸,
            # 避免配角的脸塌缩到 lead 上(双胞胎问题的真正修复)
            portrait_refs = []
            for char_name in valid_render:   # v2.9: 改成 lead + extras
                char_cfg = characters.get(char_name, {})
                portrait = char_cfg.get("portrait_ref", "")
                if portrait:
                    portrait_refs.append({
                        "character": char_name,
                        "path":      portrait,
                    })
            if portrait_refs:
                page["_portrait_refs"] = portrait_refs

            # 动态/静态决策
            # v2.3.2：cutaway 类 hold 永远不做 ai_video（它是 closeup/medium 反应镜头）
            #         extend 类 hold 不分配 motion（producer 会拼 audio 不出独立 clip）
            if is_hold and hold_type == "cutaway":
                # cutaway：当作普通 KB 页（焦点是反应/道具/环境）
                page["motion_hint"]   = _motion_hint_for(shot_type, transition)
                page["motion"]        = "ken_burns"
                page["kb_direction"]  = kb_dir
            elif is_hold and hold_type == "extend":
                # extend：保留 ken_burns 默认值（producer 不会单独用），仅为兼容字段
                page["motion_hint"]   = "low"
                page["motion"]        = "ken_burns"
                page["kb_direction"]  = kb_dir
            elif dynamic:
                page["motion_hint"]   = "high"
                page["motion"]        = "ai_video"
                page["video_tier"]    = "14B-fast"
                page["video_prompt"]  = focal_for_prompt or "atmospheric scene with subtle motion"
            else:
                page["motion_hint"]   = _motion_hint_for(shot_type, transition)
                page["motion"]        = "ken_burns"
                page["kb_direction"]  = kb_dir

            pages.append(page)

            # 记录上一非 hold 页号（hold 镜头要引用前一帧）
            if not is_hold:
                prev_non_hold_page = page_num
                # v2.3.3：非 hold 页 = 新场景，提取并更新 scene_anchor
                #         后续连续 cutaway 都用这个锚
                current_scene_anchor = _extract_scene_anchor(focal_for_prompt)

            page_num += 1

    if not pages:
        print(f"  ❌ 没有生成任何 page，请检查分镜文件")
        return ""

    # ── 总字数（统计用）─────────────────────────────────────
    total_words = 0
    seg_path = sb_data.get("segments_path", "")
    if seg_path and Path(seg_path).exists():
        try:
            with open(seg_path, encoding="utf-8") as f:
                seg_data = yaml.safe_load(f)
            total_words = sum(c.get("word_count", 0)
                              for c in seg_data.get("chapters", []))
        except Exception:
            pass

    # v2.3.4：把 storyboard 顶层的 scene_registry 透传到 story.yaml
    # 当前用于：人类调试参考；后续 v2.3.4 B 线（Redux）会用它解析 visual_ref.from_page
    sb_scene_registry = sb_data.get("scene_registry", {}) or {}

    # ── 构造 story yaml ─────────────────────────────────────
    out_yaml = {
        "title":           title,
        "series":          outline.get("series", "long_tales"),
        "theme":           f"themes/{theme_id}.yaml",
        "story_id":        story_id,
        "_long_story":     True,
        "_schema_version": "2.0",
        "_total_words":    total_words,
        "_chapter_breaks": chapter_breaks,
        "characters":      characters,
        "scene_templates": GENERIC_SCENES,
        "pages":           pages,
        # v2.3.4：跨章节场景注册表（铁律 18/19）
        "_scene_registry": sb_scene_registry,
    }

    STORIES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = output_path or str(STORIES_DIR / f"{story_id}.yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(out_yaml, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)

    n_dynamic = sum(1 for p in pages if p.get("motion") == "ai_video")
    n_kb      = len(pages) - n_dynamic

    # 统计镜头语言分布
    transition_counts = {}
    for p in pages:
        t = p.get("transition_in", "match_cut")
        transition_counts[t] = transition_counts.get(t, 0) + 1
    transition_summary = ", ".join(f"{k}×{v}" for k, v in
                                     sorted(transition_counts.items(),
                                            key=lambda x: -x[1]))

    # v2.3.3：dialogue / hold 统计
    n_dialogue_pages = sum(1 for p in pages if p.get("dialogue"))
    n_dialogue_lines = sum(len(p.get("dialogue", []) or []) for p in pages)
    speakers_used = set()
    for p in pages:
        for dl in p.get("dialogue", []) or []:
            speakers_used.add(dl.get("speaker", ""))
    n_hold_extend  = sum(1 for p in pages if p.get("_hold_type") == "extend")
    n_hold_cutaway = sum(1 for p in pages if p.get("_hold_type") == "cutaway")

    print(f"\n  ✓ 已转换为 pipeline 兼容格式: {out_path}")
    print(f"     总页数: {len(pages)} (KB {n_kb} + AI视频 {n_dynamic})")
    print(f"     章节数: {len(chapter_breaks)}")
    print(f"     主题:   {out_yaml['theme']}")
    print(f"     镜头语言分布: {transition_summary}")
    print(f"     hold 分布:    extend×{n_hold_extend}, cutaway×{n_hold_cutaway}")
    print(f"     dialogue:     {n_dialogue_pages} 页含台词 / "
          f"{n_dialogue_lines} 条台词 / "
          f"speakers={sorted(speakers_used) or '无'}")
    if n_narration_stripped:
        print(f"     ✂ narration 去重: {n_narration_stripped} 页因 dialogue "
              f"拆出而清洗了 narration（防止音频重复）")

    # v2.3.3：一致性审计报告
    if audit_warnings:
        print(f"\n  ⚠ v2.3.3 一致性审计警告 ({len(audit_warnings)} 处):")
        for w in audit_warnings[:10]:
            print(f"     p{w['page']}: {w['issue']}")
            print(f"            → {w['action']}")
        if len(audit_warnings) > 10:
            print(f"     ... 还有 {len(audit_warnings) - 10} 处")
        print(f"     这些是 storyboard 输出的 focal_subject 跟 visible_characters")
        print(f"     不一致的情况（典型 p10/p11 类问题），已自动以 visible 为准修正。")

    # v2.3.3：未建模角色报告（用户应回去补 outline）
    if unmodeled_names:
        print(f"\n  ⚠ v2.3.3 未建模角色 ({len(unmodeled_names)} 个):")
        for name, count in sorted(unmodeled_names.items(),
                                  key=lambda x: -x[1]):
            print(f"     '{name}' 出现 {count} 次")
        print(f"     这些角色被分镜大师/编剧大师提及但未在 outline.yaml 建模。")
        print(f"     已自动处理：")
        print(f"       - dialogue.speaker → 降级 narrator（旁白代述）")
        print(f"       - visible_characters → 不进 page.characters（不触发 LoRA）")
        print(f"     建议把它们补到 outline.yaml.characters[]，标 role=extra（有戏份）")
        print(f"     或 role=group（群体），重跑 storyboard 即可。")

    # v2.3.6/v2.9：按定妆照状态给出对应的下一步提示
    # v2.9 增强:加诊断信息(哪些角色有 portrait,哪些没有)+演员库选项
    n_portrait_pages = sum(1 for p in pages if p.get("_portrait_refs"))

    # 诊断: 列出每个 lead/extra 角色的 portrait 状态
    chars_with_portrait = []     # 已有 portrait 的角色
    chars_without_portrait = []  # 缺 portrait 的角色
    all_render_chars = set()
    for p in pages:
        for c in (p.get("_render_chars") or []):
            all_render_chars.add(c)
    for ch in sorted(all_render_chars):
        cfg = characters.get(ch, {})
        if cfg.get("portrait_ref"):
            chars_with_portrait.append(ch)
        else:
            chars_without_portrait.append(ch)

    print(f"\n  下一步:")

    if n_portrait_pages > 0 and not chars_without_portrait:
        # 全员就位
        print(f"    ✓ portrait_ref 已注入 {n_portrait_pages} 页 / "
              f"{len(chars_with_portrait)} 角色,可进入主管线:")
        print(f"    python run.py twophase {out_path}")
        print(f"    python run.py produce  {out_path} --platform douyin "
              f"--sovits {SOVITS_URL}")
    elif chars_with_portrait and chars_without_portrait:
        # 部分就位
        print(f"    ⚠ portrait_ref 部分就位:")
        print(f"      ✓ 已就位 ({len(chars_with_portrait)}): "
              f"{', '.join(chars_with_portrait)}")
        print(f"      ✗ 缺失   ({len(chars_without_portrait)}): "
              f"{', '.join(chars_without_portrait)}")
        print(f"")
        print(f"    缺失角色二选一:")
        print(f"")
        print(f"    【选项 A: 新生定妆照】")
        for ch in chars_without_portrait:
            print(f"      python -m tools.long_writer.cli portraits "
                  f"{out_path} --character {ch} -n 4")
            print(f"      python -m tools.long_writer.cli portraits_pick "
                  f"{out_path} --character {ch} --pick v1")
        print(f"")
        print(f"    【选项 B: 从演员库复用】")
        print(f"      # 看库里有哪些演员适合:")
        print(f"      python -m tools.long_writer.actor_cli suggest {out_path}")
        for ch in chars_without_portrait:
            print(f"      python -m tools.long_writer.actor_cli cast {out_path} "
                  f"--character {ch} --actor <actor_id>")
        print(f"")
        print(f"    选完后重跑 convert 让 portrait_ref 透传:")
        print(f"      python -m tools.long_writer.cli convert "
              f"scripts/{story_id}_storyboard.yaml")
    else:
        # 全员没有
        print(f"    ⚠ 尚未检测到定妆照（_portrait_refs 全空）。")
        if all_render_chars:
            print(f"    画面角色: {', '.join(sorted(all_render_chars))}")
        print(f"    PuLid 锁脸需先做定妆照工序。二选一:")
        print(f"")
        print(f"    【选项 A: 新生所有角色定妆(含 extras)】")
        print(f"      1) python -m tools.long_writer.cli portraits "
              f"{out_path} -n 4")
        print(f"      2) 看图后逐个 portraits_pick 固化:")
        print(f"         python -m tools.long_writer.cli portraits_pick "
              f"{out_path} --character <名> --pick v1")
        print(f"")
        print(f"    【选项 B: 用演员库已有角色复用(v2.8 新)】")
        print(f"      1) python -m tools.long_writer.actor_cli suggest {out_path}")
        print(f"      2) 按推荐 cast:")
        print(f"         python -m tools.long_writer.actor_cli cast {out_path} "
              f"--character <名> --actor <actor_id>")
        print(f"")
        print(f"    【混合模式: 主角新生 + 配角用库】也可以")
        print(f"")
        print(f"    完成后重跑 convert + twophase:")
        print(f"      python -m tools.long_writer.cli convert "
              f"scripts/{story_id}_storyboard.yaml")
        print(f"      python run.py twophase {out_path}")

    return out_path


def main():
    parser = argparse.ArgumentParser(description="长故事 → pipeline 格式 v2.0")
    parser.add_argument("storyboard",
                        help="scripts/<id>_storyboard.yaml 路径")
    parser.add_argument("--output", default=None,
                        help="输出路径（默认 stories/<story_id>.yaml）")
    args = parser.parse_args()

    convert_to_pipeline(args.storyboard, args.output)


if __name__ == "__main__":
    main()
