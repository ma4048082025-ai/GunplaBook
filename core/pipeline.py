"""
pipeline.py ── 流水线引擎（精简版）
=====================================
只保留 twophase 核心流程：
  run_two_phase()         → 两阶段生图主入口
  _phase1_generate_all()  → Phase1：逐页生图
  _phase2_score_and_retry() → Phase2：评分+重生
  _generate()             → 生图路由（FLUX / compositor / 普通）
  _maybe_reactor()        → ReActor 人脸后处理
  _save_good_seed()       → 好图 seed 写回 YAML
  _summary()              → 完成汇总

删除的功能：
  run() / _run_page()     → 普通单阶段模式
  run_page() / retry_page() → 单页操作
  upscale_all_pages()     → 批量放大
  produce() / make_video_from_registry() → 视频生产（由 run.py cmd_video 替代）
"""

import logging
import random
from pathlib import Path
from typing import Optional

from config import (
    OUT_DIR, DEFAULT_MAX_RETRIES,
)
from registry import Registry, AssetStatus, PipelineStage
from quality import QualityGate, QualityResult,make_gate

logging.getLogger("comfy_script").setLevel(logging.ERROR)

from gpu_guard import gpu_guard

from core.pipeline_v235_router import (
    prepare_v234_params, is_v234_path_enabled,
)

_SKIP_IPADAPTER = False


def set_skip_ipadapter(v: bool):
    global _SKIP_IPADAPTER
    _SKIP_IPADAPTER = v


class Pipeline:

    def __init__(self,
                 theme,
                 story,
                 mode:        str  = "auto",
                 max_retries: int  = DEFAULT_MAX_RETRIES,
                 do_upscale:  bool = False,
                 batch_n:     int  = 3):

        self.theme       = theme
        self.story       = story
        self.mode        = mode
        self.max_retries = max_retries
        self.do_upscale  = do_upscale
        self.batch_n     = batch_n

        self.out_dir = str(Path(OUT_DIR) / self.story.story_id)
        Path(self.out_dir).mkdir(parents=True, exist_ok=True)
        print(f"  输出目录: {self.out_dir}")

        # v2.3.6：质量门 / 重生决策日志档案（写到 logs/pipeline/<story_id>/）
        # 日志失败不影响主流程（_log_utils 内部已 try/except 兜底）
        try:
            from tools.long_writer._log_utils import LogArchive
            self._arc = LogArchive(self.story.story_id)
        except Exception:
            self._arc = None

        threshold = theme.quality.threshold
        self.gate: QualityGate = make_gate(
            mode,
            threshold,
            batch_n,
            vision_model           = theme.model.vision_model,
            review_focus           = theme.quality.review_focus,
            composite_review_focus = getattr(theme.quality, "composite_review_focus",
                                             "角色是否完整、光线是否统一、融合是否自然"),
        )

        self.reg    = Registry(story.path)
        self._agent = None

        from param_learner import ParamLearner
        self.learner = ParamLearner(story.path)

        self._last_composite_path: str = ""
        self._storyboard: dict = {}   # Phase 0.5 分镜表，Phase1/2 共享

    @property
    def agent(self):
        if self._agent is None:
            from orchestrator import build_decision_agent
            self._agent = build_decision_agent(self.theme, self.story)
        return self._agent

    # ══════════════════════════════════════════════════════════════
    # 主入口：run_two_phase
    # ══════════════════════════════════════════════════════════════

    def run_two_phase(self, resume: bool = False) -> dict:
        """
        两阶段生产模式：
          Phase 1：ComfyUI 常驻，逐页各生 1 张，不评分
          Phase 2a：LLaVA 加载，全员评分
          Phase 2b：失败页 agent+feedback 生 4 张候选
          Phase 2c：候选评分 + 比较选优
        """

        # v2.3：告诉 orchestrator 当前 story_id（用于分级 prompt 日志）
        try:
            from orchestrator import set_current_story_id
            set_current_story_id(self.story.story_id)
        except Exception:
            pass
        total = len(self.story.pages)
        main_lora = ""
        if self.story.pages:
            first_chars = self.story.pages[0].get("characters", [])
            if first_chars:
                main_lora = self.story.characters.get(
                    first_chars[0], {}).get("lora", "")
        self.learner.check_config_changed(self.theme.model.checkpoint, main_lora)

        print(f"\n{'=' * 55}")
        print(f"  {self.story.title}  [{self.theme.name}]")
        print(f"  两阶段模式  {'续跑' if resume else '全量'}  {total}页")
        print(f"  质量阈值={self.theme.quality.threshold}")
        print(f"{'=' * 55}")

        if resume:
            approved_pages   = {a.page_num
                                 for a in self.reg.all_approved(self.story.story_id)}
            generated_assets = self.reg.all_generated(self.story.story_id)
            generated_pages  = {a.page_num for a in generated_assets}
            # v2.3.6：恢复 Phase 2b 已生候选未选优的页
            candidates_ready_assets = self.reg.all_candidates_ready(
                self.story.story_id)
            candidates_ready_pages = {a.page_num for a in candidates_ready_assets}

            # v2.3.7：恢复 Phase 2a 已评不过、Phase 2b 未完成的页
            rejected_assets = self.reg.all_rejected(self.story.story_id)
            rejected_pages = {a.page_num for a in rejected_assets}

            truly_pending = [
                p for p in self.story.pages
                if p["page"] not in approved_pages
                and p["page"] not in generated_pages
                and p["page"] not in candidates_ready_pages
                and p["page"] not in rejected_pages

            ]

            phase1_from_registry = {}
            for asset in generated_assets:
                page_cfg = self.story.get_page(asset.page_num)
                if page_cfg and asset.exists():
                    phase1_from_registry[asset.page_num] = {
                        "file":     asset.path,
                        "params":   asset.params,
                        "page_cfg": page_cfg,
                        "asset_id": asset.id,
                        "decision": {},
                    }

            # v2.3.6：恢复 CANDIDATES_READY 页（Phase 2b 完、Phase 2c 未完）
            # 这些页跳过 Phase 1 + Phase 2a + Phase 2b，直接进 Phase 2c
            phase2b_from_registry = {}
            for asset in candidates_ready_assets:
                page_cfg = self.story.get_page(asset.page_num)
                if not page_cfg:
                    continue
                cand_rows = self.reg.candidates_for_page(
                    asset.page_num, story_id=self.story.story_id)
                if not cand_rows:
                    # 候选丢了（被清磁盘了），降级重生
                    print(f"  ⚠ p{asset.page_num} 标 CANDIDATES_READY 但找不到候选文件，"
                          f"降级重生")
                    continue
                phase2b_from_registry[asset.page_num] = {
                    "candidates": [(c["path"], asset.params) for c in cand_rows],
                    "asset_id": asset.id,
                    "page_cfg": page_cfg,
                }
            # v2.3.7：恢复 REJECTED 页（Phase 2a 评过不通过、Phase 2b 未完）
            # 这些页跳过 Phase 1 + Phase 2a，直接进 Phase 2b 重生候选
            rejected_from_registry = {}
            for asset in rejected_assets:
                page_cfg = self.story.get_page(asset.page_num)
                if not page_cfg:
                    continue
                rejected_from_registry[asset.page_num] = {
                    "file": asset.path,
                    "params": asset.params,
                    "asset_id": asset.id,
                    "score": asset.score,
                    "page_cfg": page_cfg,
                }
            print(f"  已完成: {sorted(approved_pages)}")
            print(f"  Phase1已生→进Phase2a评分: {sorted(generated_pages)}")
            print(f"  Phase2a已评不过→进Phase2b重生: {sorted(rejected_pages)}")
            print(f"  Phase2b已生候选→进Phase2c选优: {sorted(candidates_ready_pages)}")
            print(f"  需要重新生图: {[p['page'] for p in truly_pending]}")

            new_phase1 = (self._phase1_generate_all(truly_pending)
                          if truly_pending else {})
            combined   = {**phase1_from_registry, **new_phase1}

            # v2.3.7：三类 resume 集合任一非空，都要继续 Phase 2
            if (not combined and not phase2b_from_registry
                    and not rejected_from_registry):
                print("  所有页面已完成")
                return self._summary()

            # v2.3.7：传三个集合
            self._phase2_score_and_retry(
                combined,
                phase2b_resumed=phase2b_from_registry,
                phase2a_rejected=rejected_from_registry)

        else:
            phase1_results = self._phase1_generate_all(self.story.pages)
            self._phase2_score_and_retry(phase1_results)

        print(f"\n  [调参] 两阶段完成，执行最终学习分析...")
        changes = self.learner.analyze_and_update(min_confidence="low")
        if changes:
            print(f"  [调参] 更新了 {len(changes)} 个场景参数")
        print(self.learner.report())

        return self._summary()

    # ══════════════════════════════════════════════════════════════
    # Phase 1：逐页生图
    # ══════════════════════════════════════════════════════════════

    def _phase1_generate_all(self, pages_to_run: list) -> dict:
        total   = len(pages_to_run)
        results = {}

        print(f"\n{'=' * 55}")
        print(f"  Phase 1：快速生图（{total} 页，ComfyUI 常驻）")
        print(f"{'=' * 55}")

        # ── Phase 0.5：全片分镜规划（1次 LLM 调用，覆盖所有页）────
        from storyboard import build_storyboard
        self._storyboard = build_storyboard(self.story)   # 提升为实例变量，Phase2 复用
        # ────────────────────────────────────────────────────────────

        for i, page_cfg in enumerate(pages_to_run, 1):
            page_num = page_cfg["page"]
            print(f"\n  ── [{i}/{total}] 第{page_num}页: {page_cfg['title']} ──")

            # v2.3.2 hold 镜头二分类：
            #   extend  → 复用源页图（两层 fallback：内存 results → registry）
            #   cutaway → 不复用，按普通页生图（往下走）
            if page_cfg.get("_hold"):
                source_page = page_cfg.get("_hold_source_page")
                hold_type = (page_cfg.get("_hold_type") or "extend").strip().lower()
                # 兼容：未标 _hold_type 的旧 hold 默认 extend
                if hold_type not in ("extend", "cutaway"):
                    hold_type = "extend"

                if hold_type == "extend":
                    source_path = None
                    source_score = -1.0
                    # 第 1 选：内存 results（同次 phase 1 已生过）
                    if source_page and source_page in results:
                        source_path = results[source_page]["file"]
                    else:
                        # 第 2 选：registry（resume 跨进程）
                        sa = (self.reg.best_for_page(source_page,
                                                      story_id=self.story.story_id)
                              if source_page else None)
                        if sa and sa.exists():
                            source_path = sa.path
                            source_score = sa.score

                    if source_path and Path(source_path).exists():
                        print(f"  [hold-extend] 复用第{source_page}页画面: "
                              f"{Path(source_path).name}")
                        asset_id = self.reg.record(
                            page_num = page_num,
                            stage    = PipelineStage.GENERATE,
                            path     = source_path,
                            story_id = self.story.story_id,
                            score    = source_score,
                            seed     = 0,
                            cfg      = 0.0,
                            params   = {"_hold": True, "_hold_type": "extend",
                                        "_source_page": source_page},
                            status   = AssetStatus.GENERATED,
                        )
                        results[page_num] = {
                            "file":     source_path,
                            "params":   {"_hold": True, "_hold_type": "extend",
                                         "_source_page": source_page},
                            "page_cfg": page_cfg,
                            "asset_id": asset_id,
                            "decision": {"_hold": True, "_hold_type": "extend"},
                        }
                        continue
                    else:
                        print(f"  [hold-extend] ⚠ 找不到第{source_page}页 asset，"
                              f"降级按普通页生图")
                else:
                    # cutaway：不复用，往下走普通生图分支
                    print(f"  [hold-cutaway] 第{page_num}页按普通页生图"
                          f"（切到反应/道具/环境，源页 p{source_page}）")

            # 旁白对照分析（v2：注入分镜表）
            from orchestrator import analyze_narration_alignment
            scene_cfg   = self.story.get_scene(page_cfg["scene_type"])
            board_entry = self._storyboard.get(page_num)   # 读实例变量
            agent_overrides = analyze_narration_alignment(
                page_cfg, scene_cfg, self.theme, self.story,
                board_entry=board_entry)                     # ← v2 新增
            page_cfg = {**page_cfg, "agent_overrides": agent_overrides}

            # Agent 决策
            gpu_guard.wait_if_needed("Agent决策前")
            result = self.agent.invoke({
                "page_config":    page_cfg,
                "messages":       [],
                "decision":       {},
                "generated":      False,
                "feedback_context": None,
            })
            params = result["decision"]["final_params"]

            # 生图（失败时换 seed 重试一次）
            gpu_guard.wait_if_needed("生图前")
            file_768 = self._generate(params, page_cfg)

            if not file_768:
                print(f"  !! 第{page_num}页失败，换 seed 重试一次")
                params["seed"] = random.randint(10000, 99999)
                gpu_guard.wait_if_needed("生图前（重试）")
                file_768 = self._generate(params, page_cfg)

            if not file_768:
                print(f"  !! 第{page_num}页彻底失败，跳过")
                continue

            file_768 = self._maybe_reactor(file_768, params)

            asset_id = self.reg.record(
                page_num = page_num,
                stage    = PipelineStage.GENERATE,
                path     = file_768,
                story_id = self.story.story_id,
                score    = -1.0,
                seed     = params["seed"],
                cfg      = params["cfg"],
                params   = params,
                status   = AssetStatus.GENERATED,
            )

            results[page_num] = {
                "file":     file_768,
                "params":   params,
                "page_cfg": page_cfg,
                "asset_id": asset_id,
                "decision": result["decision"],
            }
            print(f"  ✓ Phase1 完成: {Path(file_768).name}")

        print(f"\n  Phase 1 结束: {len(results)}/{total} 页成功")
        return results

    # ══════════════════════════════════════════════════════════════
    # Phase 2：评分 + 重生
    # ══════════════════════════════════════════════════════════════

    def _phase2_score_and_retry(self, phase1_results: dict,
                                phase2b_resumed: dict = None,
                                phase2a_rejected: dict = None):

        """
                v2.3.6：phase2b_resumed 是 resume 时从 registry 恢复的、
                "已生候选未选优"的页。这些页跳过 Phase 2a/2b，直接进 Phase 2c。

                v2.3.7：phase2a_rejected 是 resume 时从 registry 恢复的、
                "已评不过、未生候选"的页。这些页跳过 Phase 2a，直接进 Phase 2b。
                """
        phase2b_resumed = phase2b_resumed or {}
        phase2a_rejected = phase2a_rejected or {}
        if (not phase1_results and not phase2b_resumed
                and not phase2a_rejected):
            print("  Phase 2：无可评分资产，跳过")
            return

        from quality import LlavaGate, QualityContext
        llava = LlavaGate(
            threshold              = self.theme.quality.threshold,
            vision_model           = self.theme.model.vision_model,
            review_focus           = self.theme.quality.review_focus,
            composite_review_focus = getattr(self.theme.quality,
                                             "composite_review_focus",
                                             "角色是否完整、光线是否统一"),
        )

        def _make_context(page_cfg: dict, attempt: int = 1,
                          max_att: int = 1) -> QualityContext:
            scene = self.story.get_scene(page_cfg["scene_type"])
            board_entry = self._storyboard.get(page_cfg["page"], {})
            from storyboard import get_narration_keywords
            must_haves  = get_narration_keywords(board_entry)

            # v2.3.6.1：按 focal 内容动态覆盖 image_type
            # 旧版无脑用 scene.image_type → 物件特写误判为人物镜，
            # 评分用面部/服饰/手部模板 → "看不清人脸"扣到 0 不通过。
            # 新版按 page_cfg.characters 是否为空决定：
            #   characters=[] → solo_distant 模板（按位/构/氛/环评分）
            #   characters 非空 → 走 scene.image_type 原配置
            image_type = scene.get("image_type", "solo_character")
            visible_chars = page_cfg.get("characters") or []

            # v2.10: 画面预期人脸数 = render_characters 长度 (双人同框=2)
            #        没有 render_characters 时回退用 characters 长度。
            render_chars = page_cfg.get("render_characters") or []
            if isinstance(render_chars, list) and render_chars:
                expected_faces = len(render_chars)
                has_person = expected_faces > 0
            else:
                expected_faces = max(1, len(visible_chars))
                has_person = bool(visible_chars)

            # 无人物镜 → solo_distant 模板 (按位/构/氛/环评分)
            if not has_person:
                image_type = "solo_distant"
                expected_faces = 1
                print(f"  [质量门] p{page_cfg['page']} 无人物 → 用 solo_distant 模板")
            elif expected_faces >= 2:
                print(f"  [质量门] p{page_cfg['page']} 双人同框, 预期 {expected_faces} 张脸")

            return QualityContext(
                page_num           = page_cfg["page"],
                page_title         = page_cfg["title"],
                characters         = page_cfg["characters"],
                char_features      = self.story.char_features(page_cfg["characters"]),
                attempt            = attempt,
                max_attempts       = max_att,
                image_type         = image_type,
                narration_keywords = must_haves,
                quality_profile    = self.theme.quality.profile,
                expected_faces     = expected_faces,   # v2.10
            )
        # ── Step 2a：全员评分 ─────────────────────────────────────
        print(f"\n{'=' * 55}")
        print(f"  Phase 2a：全员评分（LLaVA 常驻，{len(phase1_results)} 页）")
        print(f"{'=' * 55}")

        # v2.3.6：free_and_wait 替代 free + sleep(3)
        # 发 /free 后轮询确认显存真降下来再继续；降不下来会报警提示
        # （通常是 PuLid InsightFace 不归 ComfyUI /free 管）
        gpu_guard.free_and_wait("Phase2a 评分前")
        llava.warmup()

        passed = {}
        failed = {}
        # v2.3.2 hold 同步队列：只有 extend 类需要末尾同步
        pending_hold_sync = {}   # {hold_page_num: source_page_num}

        for page_num, data in sorted(phase1_results.items()):
            page_cfg = data["page_cfg"]

            # v2.3.6：phase2b_resumed 里的页已经评过、生过候选了，
            # 跳过 Phase 2a 评分（不浪费 LLaVA 调用）
            if page_num in phase2b_resumed:
                print(f"\n  [resume] p{page_num} 已生候选，跳过 Phase 2a 评分")
                continue
            # v2.3.2 hold 处理：
            #   extend  → 跳过评分，进同步队列（等源页评分通过后同步 path + APPROVED）
            #   cutaway → 正常评分（独立画面有独立评分）
            if page_cfg.get("_hold"):
                hold_type = (page_cfg.get("_hold_type") or
                             data["params"].get("_hold_type") or
                             "extend").strip().lower()
                if hold_type == "extend":
                    source_page = (page_cfg.get("_hold_source_page") or
                                   data["params"].get("_source_page"))
                    print(f"\n  [hold-extend] 第{page_num}页跳过评分"
                          f"（待同步源页第{source_page}）")
                    pending_hold_sync[page_num] = source_page
                    continue
                # cutaway 走下面普通评分流程

            ctx      = _make_context(page_cfg)
            print(f"\n  评分 第{page_num}页: {page_cfg['title']}")
            quality = llava.evaluate(data["file"], ctx)

            # v2.3.6：补点1 — 记录质量门判决（每页一条）
            if self._arc:
                self._arc.write(
                    "pipeline.quality_gate",
                    input={"page": page_num, "title": page_cfg.get("title", ""),
                           "image": str(data["file"]),
                           "image_type": ctx.image_type,
                           "narration_keywords": ctx.narration_keywords},
                    output={"passed": quality.passed,
                            "score": quality.score,
                            "feedback": quality.feedback,
                            "tags": quality.tags,
                            "face_score": getattr(quality, "face_score", None)},
                    decision={"phase": "2a", "result":
                              "passed" if quality.passed else "rejected"},
                    model=self.theme.model.vision_model)

            if quality.passed:
                passed[page_num] = (data["file"], data["params"],
                                    data["asset_id"], quality.score)
                print(f"  ✓ 通过 ({quality.score:.1f})")
            else:
                failed[page_num] = (data["file"], data["params"],
                                    data["asset_id"], quality, page_cfg)
                # v2.3.7：未通过立刻落盘 REJECTED + 真实分数
                # 避免下次 resume 时 LLaVA 重评一遍这页
                self.reg.update_status(data["asset_id"],
                                       AssetStatus.REJECTED,
                                       score=quality.score)
                print(f"  ✗ 未通过 ({quality.score:.1f}) — {quality.feedback}")

        llava.free_model()

        for page_num, (file, params, asset_id, score) in passed.items():
            self.reg.update_status(asset_id, AssetStatus.APPROVED, score=score)
            if score >= self.theme.quality.threshold:
                self._save_good_seed(page_num, params["seed"], score)
            page_cfg   = phase1_results[page_num]["page_cfg"]
            scene_type = page_cfg.get("scene_type", "default")
            self.learner.record(scene_type=scene_type, params=params,
                                score=score, page_num=page_num,
                                is_explore=False, is_composite=False)

        # v2.3.7：合并 resume 恢复的"已评不过"页到 failed
        # 这些页跳过了 Phase 2a（评分结果已落盘），直接进 Phase 2b 重生
        if phase2a_rejected:
            for page_num, info in phase2a_rejected.items():
                # 重建一个最小的 quality 对象（feedback 丢失，仅保留 score）
                fake_quality = QualityResult(
                    passed=False,
                    score=info["score"],
                    feedback="(resume: 上次评分未通过，feedback 已丢失)",
                    tags=[],
                )
                failed[page_num] = (info["file"], info["params"],
                                    info["asset_id"], fake_quality,
                                    info["page_cfg"])
            print(f"\n  [resume] {len(phase2a_rejected)} 页 Phase 2a "
                  f"已评不过 → 直接进 Phase 2b: "
                  f"{sorted(phase2a_rejected.keys())}")

        if not failed:
            # v2.3.7：仍可能有 phase2b_resumed 要走 Phase 2c
            if not phase2b_resumed:
                print(f"\n  Phase 2a：全部通过，无需重生")
                return
            print(f"\n  Phase 2a：全部通过/已处理，"
                  f"仍有 {len(phase2b_resumed)} 页候选待选优")
        else:
            print(f"\n  {len(failed)} 页未通过: {sorted(failed.keys())}")

        # ── Step 2b：重生候选 ─────────────────────────────────────
        print(f"\n{'=' * 55}")
        print(f"  Phase 2b：重生（2 张/页，ComfyUI 常驻）")
        print(f"{'=' * 55}")

        gpu_guard.wait_if_needed("Phase2b 生图前")
        retry_data = {}

        for page_num, (orig_file, orig_params, asset_id, quality, page_cfg) in failed.items():
            print(f"\n  重生 第{page_num}页...")
            # ★ 第一步：补剧情（与 Phase1 保持一致）
            from orchestrator import analyze_narration_alignment
            from storyboard import build_storyboard
            scene_cfg   = self.story.get_scene(page_cfg["scene_type"])
            board_entry = build_storyboard(self.story).get(page_num)  # 读缓存，无 LLM 调用
            agent_overrides = analyze_narration_alignment(
                page_cfg, scene_cfg, self.theme, self.story,
                board_entry=board_entry)
            page_cfg = {**page_cfg, "agent_overrides": agent_overrides}

            # 第二步：构建反馈上下文
            feedback_ctx = {
                "tags": quality.tags,
                "feedback_text": quality.feedback,
                "last_score": quality.score,
                "last_cfg": orig_params.get("cfg", 3.5 if orig_params.get("_unet") else 7.0),
            }




            gpu_guard.wait_if_needed("重生 Agent 前")
            # 第三步：Agent 决策（此时 page_cfg 已有完整剧情 + 反馈信息）
            retry_result = self.agent.invoke({
                "page_config":    page_cfg,
                "messages":       [],
                "decision":       {},
                "generated":      False,
                "feedback_context": feedback_ctx,
            })
            agent_params = retry_result["decision"]["final_params"]

            # 第四步：fb_translate 在完整 Prompt 基础上微调风格参数
            if quality.tags:
                from feedback import translate as fb_translate
                agent_params = fb_translate(quality.tags, "moderate", agent_params)
                print(f"  [参数合并] agent 决策 + feedback 词汇修正已合并")
                # ★ 添加 FLUX 参数范围保护 ★
                if agent_params.get("_unet"):  # 检测是否为 FLUX 模型
                    agent_params["cfg"] = max(1.0, min(5.0, agent_params["cfg"]))
                    agent_params["steps"] = max(15, min(30, agent_params["steps"]))
                    if agent_params.get("sampler") not in ("euler", "euler_ancestral"):
                        agent_params["sampler"] = "euler"

            # v2.3.6：补点2 — 记录 Phase2b 重生决策
            # input=上次为什么失败；output=重生 prompt；decision=最终参数
            if self._arc:
                ov = page_cfg.get("agent_overrides", {}) or {}
                self._arc.write(
                    "pipeline.retry_decision",
                    input={"page": page_num,
                           "last_score": feedback_ctx["last_score"],
                           "last_feedback": feedback_ctx["feedback_text"],
                           "last_tags": feedback_ctx["tags"],
                           "last_cfg": feedback_ctx["last_cfg"]},
                    output={"retry_positive": agent_params.get("positive", ""),
                            "retry_negative": agent_params.get("negative", ""),
                            "scene_prompt": ov.get("scene_prompt", "")},
                    decision={"phase": "2b",
                              "cfg": agent_params.get("cfg"),
                              "steps": agent_params.get("steps"),
                              "sampler": agent_params.get("sampler"),
                              "fb_translate_applied": bool(quality.tags)},
                    model="agent+fb_translate")

            candidates = []
            for shot in range(2):
                shot_params          = agent_params.copy()
                shot_params["seed"]  = random.randint(10000, 9999999)
                shot_params["prefix"] = f"page{page_num:02d}_r"

                gpu_guard.wait_if_needed("重生候选生图前")
                cfile = self._generate(shot_params, page_cfg)
                if cfile:
                    cfile = self._maybe_reactor(cfile, shot_params)
                    candidates.append((cfile, shot_params))
                    print(f"    候选 {shot + 1}/2: {Path(cfile).name}")
                else:
                    print(f"    候选 {shot + 1}/2: 生图失败")

            if candidates:
                retry_data[page_num] = {
                    "candidates": candidates,
                    "asset_id":   asset_id,
                    "page_cfg":   page_cfg,
                }
                # 1. 把候选路径写入 candidates 表
                for cfile, _cparams in candidates:
                    self.reg.add_candidate(page_num=page_num, path=cfile,
                                           asset_id=asset_id)
                # 2. asset 状态改 CANDIDATES_READY（resume 识别）
                self.reg.mark_candidates_ready(asset_id, score=quality.score)
                print(f"  [retry-persist] p{page_num} {len(candidates)} 候选已持久化")
            else:
                print(f"  !! 第{page_num}页 2 张候选全部失败，保留原图")
                self.reg.update_status(asset_id, AssetStatus.APPROVED,
                                       score=quality.score)

        # v2.3.6：合并 resume 恢复的"已生候选未选优"页
        if phase2b_resumed:
            print(f"\n  [resume] 合并 {len(phase2b_resumed)} 页已生候选到 Phase 2c")
            retry_data.update(phase2b_resumed)

        if not retry_data:
            return

        # ── Step 2c：候选评分 + 选优 ──────────────────────────────
        print(f"\n{'=' * 55}")
        print(f"  Phase 2c：候选评分 + 比较选优（LLaVA 常驻）")
        print(f"{'=' * 55}")

        # v2.3.6：同 Phase2a，free_and_wait 替代 free + sleep(3)
        gpu_guard.free_and_wait("Phase2c 评分前")
        llava.warmup()

        for page_num, data in retry_data.items():
            candidates = data["candidates"]
            page_cfg   = data["page_cfg"]
            asset_id   = data["asset_id"]
            print(f"\n  候选评分 第{page_num}页 ({len(candidates)} 张)...")

            scored = []
            for cfile, cparams in candidates:
                ctx = _make_context(page_cfg, attempt=1, max_att=2)
                q   = llava.evaluate(cfile, ctx)
                scored.append((cfile, cparams, q))
                # v2.3.6：score=-1.0 是"评分失败"（网络/超时），不是"图差"
                tag = "评分失败" if q.score < 0 else ("✓" if q.passed else "✗")
                print(f"    {Path(cfile).name}: {q.score:.1f}  {tag}")

            # v2.3.6：选优时优先级 通过 > 真实低分 > 评分失败(-1.0)
            # 旧版把 -1.0 当低分参与排序，导致"在两张没评成的图里硬选一张"
            passed_scored  = [s for s in scored if s[2].passed]
            real_scored    = [s for s in scored if s[2].score >= 0]
            if passed_scored:
                pool = passed_scored
            elif real_scored:
                pool = real_scored          # 没通过的，但至少评分成功了
            else:
                pool = scored               # 全部评分失败，只能矬子里拔将军
                print(f"  ⚠ 第{page_num}页所有候选评分均失败，"
                      f"保留首张（非质量结论）")
            pool.sort(key=lambda x: x[2].score, reverse=True)
            top_score     = pool[0][2].score
            top_group     = [s for s in pool if s[2].score == top_score]

            if len(top_group) > 1:
                print(f"  {len(top_group)} 张同分 ({top_score:.1f})，触发比较评估...")
                top_paths = [s[0] for s in top_group]
                ctx_cmp   = _make_context(page_cfg)
                best_path = llava.compare_candidates(top_paths, ctx_cmp)
                best_item = next(
                    (s for s in top_group if s[0] == best_path), top_group[0])
            else:
                best_item = pool[0]

            best_file, best_params, best_quality = best_item
            print(f"  最优: {Path(best_file).name} ({best_quality.score:.1f})")

            # v2.3.6：补点3 — 记录 Phase2c 候选评分 + 选优结果
            if self._arc:
                self._arc.write(
                    "pipeline.candidate_scoring",
                    input={"page": page_num,
                           "n_candidates": len(candidates)},
                    output={"candidates": [
                                {"file": Path(c[0]).name,
                                 "score": c[2].score,
                                 "passed": c[2].passed,
                                 "feedback": c[2].feedback,
                                 "tags": c[2].tags}
                                for c in scored]},
                    decision={"phase": "2c",
                              "best_file": Path(best_file).name,
                              "best_score": best_quality.score,
                              "reached_threshold":
                                  best_quality.score >= self.theme.quality.threshold},
                    model=self.theme.model.vision_model)

            self.reg.update_status(asset_id, AssetStatus.APPROVED,
                                   score=best_quality.score)
            self.reg.update_path(asset_id, best_file)

            if best_quality.score >= self.theme.quality.threshold:
                self._save_good_seed(page_num, best_params["seed"],
                                     best_quality.score)
                print(f"  ✓ 第{page_num}页重生通过 ({best_quality.score:.1f})")
            else:
                print(f"  ⚠ 第{page_num}页重生未达标，保留最高分 ({best_quality.score:.1f})")

            scene_type = page_cfg.get("scene_type", "default")
            # v2.3.6：评分失败(-1.0)不是真实质量，不写入 learner，避免污染调参
            if best_quality.score >= 0:
                self.learner.record(scene_type=scene_type, params=best_params,
                                    score=best_quality.score, page_num=page_num,
                                    is_explore=False, is_composite=False)
            else:
                print(f"  [learner] 第{page_num}页评分失败，跳过学习记录")

        # ── v2.3.2 Hold 同步阶段：把 extend 类 hold 页 path 升级到源页最终 path ─
        if pending_hold_sync:
            print(f"\n{'=' * 55}")
            print(f"  Hold 同步：{len(pending_hold_sync)} 个 extend hold 页")
            print(f"{'=' * 55}")
            for hold_page, source_page in sorted(pending_hold_sync.items()):
                hold_data = phase1_results[hold_page]
                hold_aid  = hold_data["asset_id"]

                # 查源页最终 APPROVED asset
                src_asset = self.reg.best_for_page(source_page,
                                                    story_id=self.story.story_id)
                if src_asset and src_asset.exists():
                    # 同步 path（源页可能在 phase 2b 重生过，path 变了）
                    self.reg.update_path(hold_aid, src_asset.path)
                    self.reg.update_status(hold_aid, AssetStatus.APPROVED,
                                            score=src_asset.score)
                    print(f"  [hold-extend] 第{hold_page}页 → 源页第{source_page} "
                          f"({Path(src_asset.path).name}, {src_asset.score:.1f})")
                else:
                    # 源页彻底失败 —— 仍升 APPROVED + 警告（避免 video 缺帧）
                    print(f"  [hold-extend] ⚠ 第{hold_page}页源页第{source_page}失败，"
                          f"保留 phase 1 复用 path（低分图警告）")
                    self.reg.update_status(hold_aid, AssetStatus.APPROVED,
                                            score=0.0)

        llava.free_model()
        print(f"\n  Phase 2 全部完成")

    # ══════════════════════════════════════════════════════════════
    # 生图路由
    # ══════════════════════════════════════════════════════════════

    def _generate(self, params: dict, page_cfg: dict) -> Optional[str]:
        """
        路由优先级：
          1. FLUX（_unet 有值）→ comfy_generate_flux
          2. compositor（multi_scene_types）→ SD1.5 专用，FLUX主题设为空则不触发
          3. 普通生图（兜底）→ comfy_generate
        """
        from renderer import generate_and_wait, comfy_generate, comfy_generate_flux

        scene      = self.story.get_scene(page_cfg["scene_type"])
        real_chars = [c for c in page_cfg["characters"]
                      if c not in self.theme.style_only_chars]

        # FLUX 路由
        if params.get("_unet"):
            print(f"  [路由] FLUX 生图")
            self._last_composite_path = ""

            # v2.6: 多角色路径优先尝试(双 PuLID + Regional Prompter)
            # 失败/不适用时自然 fallback 到 v2.3.5 单 PuLID
            workflow_func = comfy_generate_flux
            v260_engaged = False
            try:
                from core.pipeline_v260_router import (
                    is_v260_enabled, should_use_regional, prepare_v260_params,
                )
                if is_v260_enabled() and should_use_regional(page_cfg, self.story):
                    new_params, used = prepare_v260_params(
                        params, page_cfg, self.story.story_id,
                        story=self.story, project_root=None,
                    )
                    if "regional" in used:
                        from core.renderer import comfy_generate_flux_v260_multichar
                        params = new_params
                        workflow_func = comfy_generate_flux_v260_multichar
                        v260_engaged = True
                        print(f"  [v260] p{page_cfg['page']} 启用多角色路径: {used}")
            except ImportError as e:
                print(f"  [v260] 模块不可用,fallback 到 v2.3.5: {e}")

            # v2.3.5/v2.3.6:Redux + PuLid 路由(v2.6 没启用时才走这条)
            if not v260_engaged and is_v234_path_enabled():
                new_params, used = prepare_v234_params(
                    params, page_cfg, self.story.story_id,
                    project_root=None,  # None=用 cwd
                    out_dir=None,  # None=用 config.OUT_DIR/<story_id>
                )
                if used:
                    from core.renderer import comfy_generate_flux_v234
                    params = new_params
                    workflow_func = comfy_generate_flux_v234
                    print(f"  [v235] p{page_cfg['page']} 启用路径: {used}")

            saved = generate_and_wait(
                workflow_func,  # 这里改成 workflow_func
                params,
                params["prefix"],
                self.out_dir)
            return saved[-1] if saved else None

        # compositor 路由（SD1.5 专用）
        is_multi = (
            scene.get("type") in self.theme.multi_scene_types
            and len(real_chars) >= 2
            and not _SKIP_IPADAPTER
        )
        if is_multi:
            from compositor import multi_char_pipeline
            print(f"  [路由] 多角色 compositor: {real_chars[:2]}")
            result, composite_path = multi_char_pipeline(
                base_params      = params,
                char_names       = page_cfg["characters"],
                scene_type       = page_cfg["scene_type"],
                scene_cfg        = scene,
                out_dir          = self.out_dir,
                characters       = self.story.characters,
                theme            = self.theme,
                interactive      = (self.mode == "human"),
                return_composite = True,
            )
            if result:
                self._last_composite_path = composite_path or ""
                return result
            self._last_composite_path = ""
            print(f"  多角色失败，降级单张")

        # 普通生图（兜底）
        self._last_composite_path = ""
        saved = generate_and_wait(
            comfy_generate, params, params["prefix"], self.out_dir)
        return saved[-1] if saved else None

    # ── 人脸后处理（ReActor）─────────────────────────────────────

    def _maybe_reactor(self, img_path: str, params: dict) -> str:
        face_ref = params.get("face_ref")
        if not face_ref or not Path(img_path).exists():
            return img_path
        try:
            from renderer import generate_and_wait, comfy_reactor_swap
            r_params = {
                "_source_path": img_path,
                "face_ref":     face_ref,
                "prefix":       params["prefix"],
            }
            saved = generate_and_wait(
                comfy_reactor_swap, r_params,
                prefix   = params["prefix"] + "_fx",
                save_dir = self.out_dir,
                timeout  = 120,
            )
            if saved:
                print(f"  [ReActor] ✓ {Path(saved[-1]).name}")
                return saved[-1]
            print(f"  [ReActor] 未生成输出，保留原图")
        except RuntimeError as e:
            if "未安装" in str(e) or "ReActor" in str(e):
                print(f"  [ReActor] 节点未安装，跳过")
            else:
                print(f"  [ReActor] 运行时错误: {e}")
        except Exception as e:
            print(f"  [ReActor] 失败（非致命），保留原图: {e}")
        return img_path

    # ── 辅助方法 ─────────────────────────────────────────────────

    def _save_good_seed(self, page_num: int, seed: int, score: float):
        """将好图的 seed 写回故事 YAML，方便以后复现。"""
        try:
            import yaml as _yaml
            with open(self.story.path, "r", encoding="utf-8") as f:
                data = _yaml.safe_load(f)
            for page in data.get("pages", []):
                if page["page"] == page_num:
                    old_seed = page.get("seed", -1)
                    if old_seed != seed:
                        page["seed"] = seed
                        with open(self.story.path, "w", encoding="utf-8") as f:
                            _yaml.dump(data, f, allow_unicode=True,
                                       default_flow_style=False, sort_keys=False)
                        print(f"  [seed] p{page_num} 好图seed已写回: "
                              f"{old_seed} → {seed} (分={score:.1f})")
                    break
        except Exception as e:
            print(f"  [seed] 写回失败（非致命）: {e}")

    def _summary(self) -> dict:
        assets  = self.reg.all_approved(self.story.story_id)
        summary = self.reg.summary(self.story.story_id)
        print(f"\n{'='*55}\n  完成汇总\n{'='*55}")
        for a in assets:
            s = f" 评:{a.score:.1f}" if a.score >= 0 else ""
            print(f"  p{a.page_num}{s} → {Path(a.path).name}")
        print(f"\n  资产统计: {summary}")
        return {"story": self.story.title, "summary": summary}


    # ══════════════════════════════════════════════════════════════
    # Phase 3：动/静决策（图片评分完成后）
    # ══════════════════════════════════════════════════════════════

    def _phase3_motion_select(self, 
                              max_dynamic: int = 5,
                              score_threshold: float = 7.5) -> dict:
        """
        根据 motion_hint + 图片质量分决定每页的运动类型。
        返回 motion_plan。
        """
        from motion_selector import select_motion, write_motion_plan_to_yaml
        # ★ 首先尝试加载已有的 motion plan ★
        existing_plan = self._try_load_existing_motion_plan(self.story.path)
        if existing_plan:
            print(f"  [motion] ✓ 复用已有的 motion plan（{len(existing_plan)} 页）")
            return existing_plan

        # 如果没有现成的 plan，才重新执行 AI 评估
        print(f"\n{'=' * 55}")
        print(f"  Phase 3：动/静决策")
        print(f"{'=' * 55}")

        plan = select_motion(
            self.story, self.reg,
            max_dynamic=max_dynamic,
            score_threshold=score_threshold,
        )

        # 写回 YAML（方便后续 cmd_video_v2 单独调用时读取）
        write_motion_plan_to_yaml(self.story.path, plan)

        return plan

    # ══════════════════════════════════════════════════════════════
    # Phase 4：AI 视频 clip 生成
    # ══════════════════════════════════════════════════════════════

    def _phase4_generate_clips(self, motion_plan: dict,
                                model_size: str = "14B-fast",
                                resume: bool = True) -> dict:
        """
        对 motion_plan 中标记为 ai_video 的页面，调用 Wan 2.1 生成视频 clip。
        返回 {page_num: video_path}。

        resume=True（默认）：已存在的 page{NN}_wan*_*.mp4 跳过，复用文件
        resume=False：强制重新生成所有
        """
        from renderer_video import generate_video_clip

        video_pages = {pn: mp for pn, mp in motion_plan.items()
                       if mp.get("motion") == "ai_video"}

        if not video_pages:
            print(f"\n  Phase 4：无动态页面，跳过")
            return {}

        print(f"\n{'=' * 55}")
        print(f"  Phase 4：AI 视频生成（{len(video_pages)} 页，Wan 2.1 {model_size}）"
              f"  resume={resume}")
        print(f"{'=' * 55}")

        approved = self.reg.all_approved(self.story.story_id)
        asset_map = {a.page_num: a for a in approved}
        results = {}
        skipped = 0
        generated = 0

        for pn, mp in sorted(video_pages.items()):
            asset = asset_map.get(pn)
            if not asset or not asset.exists():
                print(f"  p{pn} 无 APPROVED 图片，跳过")
                continue

            # ── 缓存检查（resume=True 才跳过）─────────────────────
            if resume:
                # 命名约定：page05_wan14b_*.mp4 / page05_wan_*.mp4 / page05_i2v_*.mp4
                # 与 producer_v2._find_video_clip 一致
                patterns = [
                    f"page{pn:02d}_wan14b_*.mp4",
                    f"page{pn:02d}_wan_*.mp4",
                    f"page{pn:02d}_i2v_*.mp4",
                    f"page{pn:02d}_video_*.mp4",
                ]
                existing = []
                for pat in patterns:
                    existing.extend(Path(self.out_dir).glob(pat))
                # 过滤合法文件（>100KB 表示真生成出来了）
                valid = [p for p in existing if p.stat().st_size > 100_000]
                if valid:
                    latest = max(valid, key=lambda p: p.stat().st_mtime)
                    size_mb = latest.stat().st_size / 1024 / 1024
                    print(f"  [cache] ✓ p{pn} 已有视频，跳过 Wan 生成: "
                          f"{latest.name} ({size_mb:.1f} MB)")
                    results[pn] = str(latest)
                    skipped += 1
                    continue

            # ── 没缓存（或强制重生），调 Wan ──────────────────────
            prompt = mp.get("video_prompt", "")
            if not prompt:
                page_cfg = self.story.get_page(pn)
                prompt = page_cfg.get("motion_seed", "") if page_cfg else ""
            if not prompt:
                prompt = "gentle motion, atmospheric"

            from gpu_guard import gpu_guard
            gpu_guard.wait_if_needed("Wan 2.1 生成前")

            tier = mp.get("tier") or model_size
            clip_path = generate_video_clip(
                image_path  = asset.path,
                prompt      = prompt,
                out_dir     = self.out_dir,
                page_num    = pn,
                model_size  = tier,
                seed        = asset.seed or 42,
            )

            if clip_path:
                results[pn] = clip_path
                generated += 1
                print(f"  ✓ p{pn} 视频 clip: {Path(clip_path).name}")
            else:
                print(f"  ✗ p{pn} 视频生成失败，降级到 Ken Burns")
                motion_plan[pn]["motion"] = "ken_burns"
                motion_plan[pn]["kb_direction"] = "zoom_in"

        print(f"\n  Phase 4 完成: {len(results)}/{len(video_pages)} 个 clip 成功 "
              f"(新生成 {generated}, 缓存复用 {skipped})")
        return results

    # ══════════════════════════════════════════════════════════════
    # 视频生产入口
    # ══════════════════════════════════════════════════════════════

    def make_video_v2(self, platform: str = "youtube",
                      sovits_host: str = "",
                      bgm_path: str = "",
                      wan_model: str = "14B-fast",
                      max_dynamic: int = 6,
                      score_threshold: float = 7.0,
                      resume: bool = True) -> str:
        """
        完整视频生产流程：
          Phase 3 → motion_selector 决策
          Phase 4 → Wan 2.1 视频 clip 生成
          Phase 5 → producer_v2 组装最终视频

        参数:
          platform:        youtube / douyin / xiaohongshu
          sovits_host:     GPT-SoVITS API 地址（留空则全部用 edge_tts）
          bgm_path:        背景音乐路径
          wan_model:       Wan 2.2 模型档位，
                         "14B-fast" (默认/横版) /
                         "14B-fast-vertical" (竖版抖音/快手)
          max_dynamic:     每部片子最多动态 clip 数量
          score_threshold: 质量分达标才做动态

        返回:
          视频文件路径
        """
        # Phase 3: 动/静决策
        motion_plan = self._phase3_motion_select(
            max_dynamic=max_dynamic,
            score_threshold=score_threshold,
        )

        # Phase 4: AI 视频 clip 生成（resume=True 时已存在的 clip 跳过 Wan 调用）
        self._phase4_generate_clips(motion_plan, model_size=wan_model, resume=resume)

        # Phase 5: 组装最终视频
        print(f"\n{'=' * 55}")
        print(f"  Phase 5：视频组装 (producer_v2)")
        print(f"{'=' * 55}")

        from producer_v2 import ProducerV2
        producer = ProducerV2(
            self.story, self.theme,
            sovits_host=sovits_host,
            bgm_path=bgm_path if bgm_path else None,
        )

        return producer.make_video(
            motion_plan=motion_plan,
            platform=platform,
            out_dir=self.out_dir,
        )

    def _try_load_existing_motion_plan(self, story_path: str) -> dict:
        """
        尝试从 story YAML 文件中加载已有的 motion plan
        """
        import yaml

        with open(story_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        plan = {}
        for page in data.get("pages", []):
            pn = page["page"]
            # 检查是否已有 motion 相关字段（这些是在上次运行时写入的）
            if "motion" in page:
                mp = {"motion": page["motion"]}

                if page["motion"] == "ken_burns":
                    if "kb_direction" in page:
                        mp["kb_direction"] = page["kb_direction"]

                elif page["motion"] == "ai_video":
                    if "video_prompt" in page:
                        mp["video_prompt"] = page["video_prompt"]
                    if "video_duration" in page:
                        mp["video_duration"] = page["video_duration"]
                    if "video_tier" in page:
                        mp["tier"] = page["video_tier"]

                plan[pn] = mp

        # ── v2.5.1: 加载后的 hold 一致性补救 ──────────────────
        # 背景:旧版 yaml 里可能 page["motion"] = "ken_burns" 而同时
        # _hold=true + _hold_type=extend(老 motion_selector 没标 hold_skip,
        # 或 yaml 是分镜大师直接写的)。这种情况下 producer 会把 extend hold
        # 当独立镜头渲染,造成"画面重复 + 音频不合并"。
        # 修复:扫一遍 yaml,把所有 _hold_type=extend 的页的 motion 强制覆写为 hold_skip。
        # cutaway hold 不动 —— cutaway 本来就该有自己的 ken_burns 运动。
        overrides = 0
        for page in data.get("pages", []):
            pn = page["page"]
            if pn not in plan:
                continue
            if not page.get("_hold"):
                continue
            hold_type = (page.get("_hold_type") or "extend").strip().lower()
            if hold_type == "extend" and plan[pn].get("motion") != "hold_skip":
                old_motion = plan[pn].get("motion", "?")
                plan[pn] = {
                    "motion": "hold_skip",
                    "_hold": True,
                    "_hold_type": "extend",
                    "_hold_source_page": page.get("_hold_source_page"),
                    "_eval_source": "consistency_override",
                }
                overrides += 1
                print(f"  [motion/hold-fix] p{pn} extend hold 强制覆写 "
                      f"motion: {old_motion} → hold_skip")
        if overrides:
            print(f"  [motion/hold-fix] 补救 {overrides} 个 extend hold 镜头的 motion 标记")

        return plan if plan else {}
