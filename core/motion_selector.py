"""
motion_selector.py ── 动/静决策引擎（v4：支持二段式 AI 评估）
=================================================================
v4 调整（vs v3）：
  - 新增 text_model 参数，透传给 motion_eval
  - 决策逻辑不变（motion_hint=high → 候选 → 排序 → 选 top N）

v3 已有：
  motion_hint （叙事意图）= 创作者决定，AI 不可推翻
  motion_score（技术适配）= AI 看图决定，不依赖人工标注

用法：
  plan = select_motion(story, registry,
                       max_dynamic=5,
                       score_threshold=7.0,
                       motion_eval="ai_vision",
                       vision_model="minicpm-v:8b",
                       text_model="qwen2.5:14b")
"""

import random
from pathlib import Path
from typing import Optional


SHOT_TYPE_BONUS: dict = {
    "closeup": 1.0,
    "medium":  0.5,
    "wide":   -0.5,
    "none":    0.0,
}

_KB_DIRECTIONS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"]


def _pick_kb_direction(page_cfg: dict, scene_cfg: dict) -> str:
    image_type = scene_cfg.get("image_type", "")
    if image_type == "background_only":
        return random.choice(["pan_left", "pan_right", "zoom_in"])
    if image_type == "solo_distant":
        return "zoom_in"
    return random.choice(_KB_DIRECTIONS)


def _diversify_kb_directions(plan: dict) -> dict:
    pages_sorted = sorted(plan.keys())
    for i in range(1, len(pages_sorted)):
        curr_pn = pages_sorted[i]
        prev_pn = pages_sorted[i - 1]
        curr = plan[curr_pn]
        prev = plan[prev_pn]
        if (curr.get("motion") == "ken_burns"
                and prev.get("motion") == "ken_burns"
                and curr.get("kb_direction") == prev.get("kb_direction")):
            alternatives = [d for d in _KB_DIRECTIONS
                             if d != prev["kb_direction"]]
            curr["kb_direction"] = random.choice(alternatives)
    return plan


def _resolve_shot_type(page_cfg: dict, scene_cfg: dict) -> str:
    page_shot = page_cfg.get("shot_type")
    if page_shot:
        return page_shot
    scene_shot = scene_cfg.get("shot_type")
    if scene_shot:
        return scene_shot
    image_type = scene_cfg.get("image_type", "")
    if image_type == "background_only":
        return "none"
    if image_type == "solo_distant":
        return "wide"
    return "medium"


def _run_ai_evaluation(story, registry,
                        vision_model: str,
                        text_model:   str,
                        cache_dir:    Optional[str]) -> dict:
    """
    返回 {page_num: ai_eval_dict}。
    优先尝试二段式接口（带 text_model）；如果 motion_eval 是老版无 text_model，自动降级。
    """
    try:
        from motion_eval import evaluate_all_pages
    except ImportError as e:
        print(f"  [motion] motion_eval 模块不可用: {e}")
        return {}

    try:
        import inspect
        sig = inspect.signature(evaluate_all_pages)
        kwargs = {
            "vision_model": vision_model,
            "cache_dir":    cache_dir,
        }
        if "text_model" in sig.parameters:
            kwargs["text_model"] = text_model
        return evaluate_all_pages(story, registry, **kwargs)
    except Exception as e:
        print(f"  [motion] AI 评估异常，降级 shot_type: {e}")
        return {}


def select_motion(story, registry,
                   max_dynamic: int = 5,
                   score_threshold: float = 7.0,
                   motion_eval: str = "ai_vision",
                   vision_model: str = "minicpm-v:8b",
                   text_model:   str = "qwen2.5:14b",
                   cache_dir: Optional[str] = None) -> dict:
    """
    决策入口。

    参数:
      motion_eval:
        "ai_vision" : 二段式 AI 评估（vision + text）
        "shot_type" : YAML 标记软加权（无 LLM 调用）
        "hybrid"    : 优先 ai_vision，失败降级 shot_type
      vision_model: Stage 1 视觉模型
      text_model:   Stage 2 文本模型
    """
    print(f"\n  [motion] 决策模式: {motion_eval}, "
          f"max_dynamic={max_dynamic}, threshold={score_threshold}")
    if motion_eval in ("ai_vision", "hybrid"):
        print(f"  [motion]   视觉: {vision_model}")
        print(f"  [motion]   文本: {text_model}")

    approved = registry.all_approved(story.story_id)
    score_map = {a.page_num: a.score for a in approved}

    use_ai = motion_eval in ("ai_vision", "hybrid")
    ai_results: dict = {}
    if use_ai:
        ai_results = _run_ai_evaluation(
            story, registry,
            vision_model=vision_model,
            text_model=text_model,
            cache_dir=cache_dir,
        )
        if not ai_results:
            print(f"  [motion] AI 评估为空，使用 shot_type 兜底")

    plan: dict = {}
    candidates: list = []

    for page_cfg in story.pages:
        pn        = page_cfg["page"]
        scene_cfg = story.get_scene(page_cfg.get("scene_type", "")) or {}
        hint      = page_cfg.get("motion_hint", "low")
        has_chars = bool(page_cfg.get("characters"))
        ai_data   = ai_results.get(pn)
        eval_src  = "ai_vision" if ai_data else "shot_type"
        shot_type = _resolve_shot_type(page_cfg, scene_cfg)

        # v2.3.2：hold 镜头特殊处理
        if page_cfg.get("_hold"):
            hold_type = (page_cfg.get("_hold_type") or "extend").strip().lower()
            if hold_type not in ("extend", "cutaway"):
                hold_type = "extend"

            if hold_type == "extend":
                # extend：不分配 motion，producer 会把 audio 拼到上一页
                # 用 "hold_skip" 作为标记（producer 识别）
                plan[pn] = {
                    "motion":        "hold_skip",
                    "_hold":         True,
                    "_hold_type":    "extend",
                    "_hold_source_page": page_cfg.get("_hold_source_page"),
                    "shot_type":     shot_type,
                    "_eval_source":  "hold_skip",
                }
                print(f"  [motion] p{pn} → hold_skip (extend，audio 拼上一页)")
                continue
            else:
                # cutaway：当作普通 KB（反应/道具/环境特写，一般 closeup/medium）
                # 强制 ken_burns，不进 ai_video 候选
                kb_dir = (ai_data["kb_direction"] if ai_data
                           else _pick_kb_direction(page_cfg, scene_cfg))
                plan[pn] = {
                    "motion":        "ken_burns",
                    "shot_type":     shot_type,
                    "kb_direction":  kb_dir,
                    "kb_duration":   0,
                    "_hold":         True,
                    "_hold_type":    "cutaway",
                    "_hold_source_page": page_cfg.get("_hold_source_page"),
                    "_eval_source":  eval_src,
                }
                print(f"  [motion] p{pn} → ken_burns (cutaway, {kb_dir})")
                continue

        # 硬约束 1：纯背景或无角色 → KB
        if (not has_chars
                or scene_cfg.get("image_type") == "background_only"):
            kb_dir = (ai_data["kb_direction"] if ai_data
                       else _pick_kb_direction(page_cfg, scene_cfg))
            plan[pn] = {
                "motion":       "ken_burns",
                "shot_type":    shot_type,
                "kb_direction": kb_dir,
                "kb_duration":  0,
                "_eval_source": eval_src,
            }
            continue

        # 硬约束 2：motion_hint != high → KB
        if hint != "high":
            kb_dir = (ai_data["kb_direction"] if ai_data
                       else _pick_kb_direction(page_cfg, scene_cfg))
            plan[pn] = {
                "motion":       "ken_burns",
                "shot_type":    shot_type,
                "kb_direction": kb_dir,
                "kb_duration":  0,
                "_eval_source": eval_src,
            }
            continue

        # motion_hint == high：进候选池
        if ai_data:
            effective = ai_data["motion_score"]
            print(f"  [motion] p{pn} hint=high → 候选 "
                  f"(motion_score={effective:.1f}, src=AI)")
        else:
            score = score_map.get(pn, 0.0)
            bonus = SHOT_TYPE_BONUS.get(shot_type, 0.0)
            effective = score + bonus
            print(f"  [motion] p{pn} hint=high → 候选 "
                  f"(score={score:.1f}+{bonus:+.1f}={effective:.1f}, "
                  f"src=shot_type)")

        if effective < score_threshold:
            kb_dir = (ai_data["kb_direction"] if ai_data
                       else _pick_kb_direction(page_cfg, scene_cfg))
            plan[pn] = {
                "motion":        "ken_burns",
                "shot_type":     shot_type,
                "kb_direction":  kb_dir,
                "kb_duration":   0,
                "_motion_score": effective,
                "_eval_source":  eval_src,
            }
            print(f"     ↑ 分数 {effective:.1f} < 阈值 {score_threshold} → KB")
            continue

        candidates.append((pn, effective, page_cfg, scene_cfg, ai_data))

    candidates.sort(key=lambda x: x[1], reverse=True)
    selected = candidates[:max_dynamic]
    rejected = candidates[max_dynamic:]

    for pn, eff, page_cfg, scene_cfg, ai_data in selected:
        if ai_data and ai_data.get("suggested_prompt"):
            video_prompt = ai_data["suggested_prompt"]
            prompt_src = "AI"
        elif page_cfg.get("motion_seed"):
            video_prompt = page_cfg["motion_seed"]
            prompt_src = "YAML"
        else:
            video_prompt = "gentle motion, atmospheric"
            prompt_src = "default"

        plan[pn] = {
            "motion":         "ai_video",
            "tier":           page_cfg.get("video_tier", "14B-fast"),
            "shot_type":      _resolve_shot_type(page_cfg, scene_cfg),
            "video_prompt":   video_prompt,
            "video_duration": page_cfg.get("video_duration", 0),
            "_motion_score":  eff,
            "_eval_source":   "ai_vision" if ai_data else "shot_type",
            "_prompt_source": prompt_src,
        }
        print(f"  [motion] p{pn} → ai_video "
              f"(score={eff:.1f}, prompt_src={prompt_src})")

    for pn, eff, page_cfg, scene_cfg, ai_data in rejected:
        kb_dir = (ai_data["kb_direction"] if ai_data
                   else _pick_kb_direction(page_cfg, scene_cfg))
        plan[pn] = {
            "motion":        "ken_burns",
            "shot_type":     _resolve_shot_type(page_cfg, scene_cfg),
            "kb_direction":  kb_dir,
            "kb_duration":   0,
            "_motion_score": eff,
            "_eval_source":  "ai_vision" if ai_data else "shot_type",
        }
        print(f"  [motion] p{pn} → ken_burns "
              f"(score={eff:.1f}, 超出动态上限)")

    plan = _diversify_kb_directions(plan)

    n_video = sum(1 for v in plan.values() if v["motion"] == "ai_video")
    n_kb    = sum(1 for v in plan.values() if v["motion"] == "ken_burns")
    n_ai    = sum(1 for v in plan.values()
                   if v.get("_eval_source") == "ai_vision")
    print(f"\n  [motion] 决策完成: {n_video} 动态 + {n_kb} 静态 = "
          f"{len(plan)} 页")
    print(f"  [motion] 决策来源: AI={n_ai}, shot_type={len(plan) - n_ai}")
    return plan


def write_motion_plan_to_yaml(story_path: str, plan: dict):
    import yaml

    with open(story_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    for page in data.get("pages", []):
        pn = page["page"]
        if pn not in plan:
            continue
        mp = plan[pn]
        page["motion"] = mp["motion"]
        if mp["motion"] == "ken_burns":
            page["kb_direction"] = mp.get("kb_direction", "zoom_in")
        elif mp["motion"] == "ai_video":
            page["video_prompt"] = mp.get("video_prompt", "")
            if "video_duration" in mp and mp["video_duration"]:
                page["video_duration"] = mp["video_duration"]
            page["video_tier"] = mp.get("tier", "14B-fast")

    with open(story_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True,
                   default_flow_style=False, sort_keys=False)
    print(f"  [motion] 已写回 {story_path}")
