"""
quality.py ── 可插拔质量门系统
================================
v4 变更 (基于 v3): 新增 SoftGate
  --------------------------------------------------------------
  问题背景:
    LlavaGate 的硬阈值 (face<10, hand<5) 频繁误伤好图:
    - p9 (手部特写但 PuLID 拉回正脸像) → 手不在画面 → hand=4 一票否决 → 重生反而退步
    - p43 (钥匙特写) → face_score=2 触发面部修复 → 重生后纯钥匙图反而过
    - p13 (远景踢门) → 远景模板对人物变形太宽容 → 解剖学崩坏漏过

  根因: 质量门 "全维度审美仲裁" 角色定位错误。8B 视觉模型
  做不好审美判断,做 "硬伤检测" 还行。focal_subject 由 PuLID
  覆盖是生成阶段的客观现实,质量门事后管不动。

  SoftGate 设计原则:
    1. 只管硬伤 (脸/手畸形、多头多肢),不管构图氛围 focus 一致性
    2. 检查前先问 "有没有",没有就跳过该项 (不扣分不加分)
    3. 远景/合成单段评分 (沿用 LlavaGate);人物两阶段评分
    4. 接口与 LlavaGate 完全一致,可旁路启用
    5. 按主题 profile 分流 Stage 2 评分尺子 (人物/拟人动物/动物/机甲...)

  启用:make_gate(mode="soft", ...);老调用 mode="auto" 一字未改。
  详见 core/SOFTGATE_NOTES.md。

v4 新增字段:
  QualityContext.quality_profile - 主体类型 profile
    由 pipeline._make_context 从 theme.quality.profile 注入
    LlavaGate 不读此字段,SoftGate 用它选 Stage 2 评分规则
    缺省 "human_realistic",已有调用全自动走默认

v3 已有:
  QualityContext 新增 narration_keywords 字段
    → 由 pipeline._make_context 从分镜缓存注入 (storyboard.visual_must_haves)
    → 有值时,evaluate() 在评分 prompt 末尾追加叙事一致性检查
    → SoftGate 默认忽略此字段 (不查 focal 一致性)

v2 已有:
  QualityResult 新增 face_score 字段
  face_score < face_hard_threshold → hard fail
  SoftGate 里:face_score = -1 表示 "未评" (脸不可见),
  让 face_needs_repair 属性自然返回 False,零接口变更。
"""

import base64
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import requests

from config import OLLAMA_BASE, PROXIES


# ════════════════════════════════════════
# 数据类型
# ════════════════════════════════════════
@dataclass
class QualityContext:
    page_num:           int
    page_title:         str
    characters:         list
    char_features:      str  = ""
    attempt:            int  = 1
    max_attempts:       int  = 3
    image_type:         str  = "solo_mecha"
    narration_keywords: list = field(default_factory=list)   # v3 新增
    quality_profile:    str  = "human_realistic"             # v4 新增 (SoftGate 用)
    expected_faces:     int  = 1   # v2.10: 本镜预期人脸数(=render_characters长度),
                                   #        双人同框=2,防 multi_head 误杀。默认1=旧行为


@dataclass
class QualityResult:
    passed:        bool
    score:         float = -1.0
    feedback:      str   = ""
    selected_path: str   = ""
    detail:        dict  = field(default_factory=dict)
    tags:          list  = field(default_factory=list)
    face_score:    float = -1.0

    @property
    def has_feedback(self) -> bool:
        return bool(self.feedback.strip())

    @property
    def face_needs_repair(self) -> bool:
        return 0 <= self.face_score < 7.0


# ════════════════════════════════════════
# 质量门协议
# ════════════════════════════════════════
@runtime_checkable
class QualityGate(Protocol):
    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult: ...
    @property
    def name(self) -> str: ...


# ════════════════════════════════════════
# AutoAcceptGate
# ════════════════════════════════════════
class AutoAcceptGate:
    name = "auto_accept"

    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult:
        return QualityResult(passed=True, score=-1.0,
                             selected_path=image_path)


# ════════════════════════════════════════
# HumanGate
# ════════════════════════════════════════
class HumanGate:
    name = "human"

    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult:
        try:
            subprocess.Popen(["open", image_path])
        except Exception:
            pass

        print(f"\n  ┌─ 质量确认 p{context.page_num}·{context.page_title} "
              f"({context.attempt}/{context.max_attempts}) {'─'*16}")
        print(f"  │  {Path(image_path).name}")
        print(f"  │  回车=通过  r=重生成  描述=调参  s=强制  q=中止")
        print(f"  └{'─'*48}")
        choice = input(f"  > ").strip()

        if choice.lower() in ("q", "quit"):
            raise KeyboardInterrupt("用户中止")
        if choice.lower() in ("s", "skip"):
            return QualityResult(passed=True, score=-1.0,
                                 selected_path=image_path, feedback="强制通过")
        if not choice or choice.lower() in ("y", "ok"):
            return QualityResult(passed=True, score=-1.0,
                                 selected_path=image_path)
        if choice.lower() in ("r", "retry"):
            return QualityResult(passed=False, score=-1.0,
                                 selected_path=image_path)
        return QualityResult(passed=False, score=-1.0,
                             feedback=choice, selected_path=image_path)


# ════════════════════════════════════════
# LlavaGate
# ════════════════════════════════════════
class LlavaGate:
    """
    视觉评分门。根据 image_type 选不同的评分维度。

    v3 变更：
      context.narration_keywords 有值时，在评分 prompt 末尾追加叙事一致性检查。
      narration_match=false → 直接返回 hard fail，无视数值分。
    """
    name = "llava"

    PROMPT_SOLO_MECHA = """你是机甲漫画图片评审。只返回JSON，不输出其他文字。
评审关注：{review_focus}

评分标准（合计 100 分，系统自动换算为 10 分制）：
- face_accuracy:  头部面部识别度，v-fin形状/感应眼颜色/面甲设计 (0-20)
- body_accuracy:  机体整体特征，配色/体型/标志性部件 (0-15)
- composition:    构图质量 (0-25)
- detail_quality: 细节精细度 (0-25)
- atmosphere:     氛围感 (0-15)

重要：face_accuracy 低于 10 时，pass 必须为 false。

返回格式（严格JSON，不要换行注释）：
{{"face_accuracy":0,"body_accuracy":0,"composition":0,"detail_quality":0,"atmosphere":0,"total":0,"issues":[],"feedback":"","pass":false}}

issues 可用标签：too_blurry/wrong_bg/weak_character/bad_composition/has_watermark/too_dark/over_saturated/has_human/lack_detail/bad_lighting/too_bright/weak_face
feedback 中文10字内"""

    PROMPT_COMPOSITE = """你是漫画合成图片评审。只返回JSON，不输出其他文字。
评审关注：{review_focus}

评分标准（合计 100 分，系统自动换算为 10 分制）：
- char_completeness: 每个角色是否完整，无截断无抠图残影 (0-30)
- scale_consistency: 两个角色比例是否合理协调 (0-20)
- lighting_match:    光线方向和色温是否统一 (0-25)
- blend_quality:     融合边缘是否自然，无明显接缝 (0-25)

重要：char_completeness 低于 15 时，pass 必须为 false。
重要：图中出现真人面孔时，issues 必须包含 has_human，pass 必须为 false。

返回格式（严格JSON，不要换行注释）：
{{"char_completeness":0,"scale_consistency":0,"lighting_match":0,"blend_quality":0,"total":0,"issues":[],"feedback":"","pass":false}}

issues 可用标签：char_truncated/rembg_residue/scale_mismatch/lighting_mismatch/composite_seam/has_human/wrong_bg/too_dark/too_bright
feedback 中文10字内"""

    PROMPT_SOLO_CHARACTER = """你是漫画图片评审。只返回JSON，不输出其他文字。
评审关注：{review_focus}

评分标准（合计 100 分，系统自动换算为 10 分制）：
- face_clarity:      面部是否清晰自然，五官比例正确，无模糊无变形 (0-25)
- face_consistency:  面部特征是否吻合角色设定（亚洲人脸型/古风气质）(0-15)
- costume_accuracy:  服饰/外观是否符合角色设定 (0-15)
- hand_quality:      手部是否正常——手指数量（5根）、比例、形态，无粘连/断裂/扭曲 (0-15)
- body_pose:         除手部外的肢体姿态是否自然流畅 (0-10)
- atmosphere:        氛围感，情绪与场景描述是否匹配 (0-20)

一票否决规则（任意一条成立，pass 必须为 false）：
- face_clarity 低于 10：面部明显模糊或变形
- hand_quality 低于 5：手部严重损坏（多余手指/粘连/缺失）
- face_consistency 低于 5：issues 含 wrong_ethnicity

返回格式（严格JSON，不要换行注释）：
{{"face_clarity":0,"face_consistency":0,"costume_accuracy":0,"hand_quality":0,"body_pose":0,"atmosphere":0,"total":0,"issues":[],"feedback":"","pass":false}}

issues 可用标签：too_blurry/face_deformed/wrong_ethnicity/wrong_costume/bad_hands/hand_deformed/bad_pose/has_watermark/wrong_bg/too_dark/over_saturated/lack_detail/bad_lighting
feedback 中文15字内，面部或手部有问题时必须在feedback中明确指出"""

    PROMPT_SOLO_DISTANT = """你是漫画图片评审。只返回JSON，不输出其他文字。
    评审关注：{review_focus}

    此图为远景、侧面、背影、或人物低头等非正脸构图，不要苛求面部细节。

    评分标准（合计 100 分，系统自动换算为 10 分制）：
    - figure_placement: 人物在画面中的位置和比例是否恰当 (0-20)
    - composition:      构图层次感，空间纵深 (0-25)
    - atmosphere:       情绪氛围是否到位，色调匹配 (0-25)
    - environment:      环境细节质量 (0-20)
    - figure_quality:   人物整体质感——服饰、轮廓、姿态是否自然，
                        侧脸/背影是否正常，无明显变形崩坏 (0-10)

    一票否决：figure_quality 低于 3 时（人物严重变形/崩坏），pass 必须为 false。

    返回格式（严格JSON，不要换行注释）：
    {{"figure_placement":0,"composition":0,"atmosphere":0,"environment":0,"figure_quality":0,"total":0,"issues":[],"feedback":"","pass":false}}

    issues 可用标签：figure_missing/bad_composition/wrong_bg/too_dark/over_saturated/bad_lighting/figure_deformed
    feedback 中文10字内"""

    # v2.3.6：叙事元素不全时的扣分值（不再一票否决）
    # 2.0 分：足以让"勉强及格"的图跌破阈值触发重生，
    # 但不会把"构图氛围优秀只缺次要道具"的图直接打死。
    NARRATION_PENALTY = 2.0

    # ── 叙事一致性追加模板 ──────────────────────────────────────
    _NARRATION_CHECK_SUFFIX = """

【叙事一致性检查】
本页旁白要求图中尽量出现以下视觉元素：
{must_haves_str}

请仔细观察图片，判断以上元素是否在图中可见（出现一半以上即视为符合）。
在 JSON 中额外输出：
  "narration_match": true 或 false
  "narration_note": "中文10字内，说明哪个元素出现/未出现"
"""

    def __init__(self,
                 threshold:              float = 7.0,
                 vision_model:           str   = "llava:7b",
                 review_focus:           str   = "画面质量和角色准确性",
                 composite_review_focus: str   = "两个机甲是否完整、光线是否统一、融合是否自然",
                 face_hard_threshold:    float = 6.0):
        self.threshold              = threshold
        self.vision_model           = vision_model
        self.review_focus           = review_focus
        self.composite_review_focus = composite_review_focus
        self.face_hard_threshold    = face_hard_threshold

    _FACE_VETO_WORDS = (
        "面部模糊", "脸部模糊", "五官模糊", "面部不清晰",
        "面部变形", "脸部变形", "五官变形", "面部不清楚",
    )
    _HAND_VETO_WORDS = (
        "手部变形", "手指变形", "手指异常", "多余手指",
        "手指粘连", "手指数量异常", "手指数量错误",
        "手部问题", "手部比例异常",
        "手臂变形", "手指缺失", "手指错误",
    )

    def _veto_check(self, feedback: str, issues: list,
                    hand_score: float) -> tuple:
        for word in self._FACE_VETO_WORDS:
            if word in feedback:
                return True, f"[面部一票否决] {word}"
        if 0 <= hand_score < 5.0:
            return True, f"[手部一票否决] hand_quality={hand_score:.1f}/15 < 5"
        if "bad_hands" in issues or "hand_deformed" in issues:
            return True, f"[手部一票否决] issues={[i for i in issues if 'hand' in i]}"
        for word in self._HAND_VETO_WORDS:
            if word in feedback:
                return True, f"[手部一票否决] {word}"
        return False, ""

    def warmup(self):
        try:
            self._call_vision("你好", b"iVBORw0KGgo=")
        except Exception:
            pass

    def free_model(self):
        try:
            requests.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": self.vision_model, "keep_alive": 0},
                timeout=10, proxies=PROXIES,
            )
            print(f"  [视觉模型] 已卸载: {self.vision_model}")
        except Exception:
            pass

    def _call_vision(self, prompt: str,
                     img_input) -> str:
        if isinstance(img_input, (bytes, bytearray)):
            images_b64 = [base64.b64encode(img_input).decode()]
        else:
            images_b64 = [base64.b64encode(b).decode() for b in img_input]

        # v2.3.6：3 次重试 + 超时 240s
        # PuLid 接入后生图侧显存基线抬高，/free 异步回收 + InsightFace
        # 的 onnxruntime CUDA session 不归 ComfyUI 管，评分切换初期显存
        # 可能仍紧张导致 Ollama 卡死。重试给显存腾挪留时间。
        last_err = None
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{OLLAMA_BASE}/api/generate",
                    json={
                        "model":  self.vision_model,
                        "prompt": prompt,
                        "images": images_b64,
                        "stream": False,
                        "options": {"temperature": 0.1, "num_predict": 350},
                    },
                    timeout=240,
                    proxies=PROXIES,
                )
                r.raise_for_status()
                resp = r.json().get("response", "")
                if resp.strip():
                    return resp
                last_err = "空响应"
            except Exception as e:
                last_err = e
                print(f"  [视觉] 第{attempt+1}/3 次调用失败: {str(e)[:60]}")
            if attempt < 2:
                time.sleep(15)
        raise RuntimeError(f"视觉模型 3 次均失败: {last_err}")

    def _parse_json_tolerant(self, text: str) -> dict:
        import re
        text = text.strip()
        for pattern in [r'\{[^{}]*\}', r'\{.*?\}']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group())
                except Exception:
                    continue
        raise ValueError(f"无法解析 JSON: {text[:100]}")

    # v2.3.6：叙事关键词清洗——视觉模型只能验证"看得见的东西"
    # 人名（画里没名牌）和抽象叙事关系（"保护""揭示"）模型无法判断，
    # 留在 must_haves 里只会造成 narration_match=false 误判。这里剔除。
    _ABSTRACT_NARRATION_WORDS = {
        "叙事", "元素", "情节", "剧情", "保护", "揭示", "暗示", "象征",
        "回忆", "对峙", "冲突", "关系", "出现", "未出现", "缺失",
        "narrative", "element", "plot", "reveal", "protect", "symbolize",
    }

    @staticmethod
    def _clean_narration_keywords(keywords: list) -> list:
        """剔除人名和抽象叙事词，只保留视觉可验证的物件/场景/动作"""
        import re
        cleaned = []
        for kw in keywords:
            if not isinstance(kw, str):
                continue
            k = kw.strip()
            if not k:
                continue
            low = k.lower()
            # 含抽象叙事词 → 整条丢弃
            if any(w in k or w in low
                   for w in LlavaGate._ABSTRACT_NARRATION_WORDS):
                continue
            # 疑似人名：含大写驼峰英文名（Chen Yuanyuan / Lin Hongying）
            if re.search(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", k):
                continue
            cleaned.append(k)
        return cleaned

    def _build_prompt(self, image_type: str,
                      narration_keywords: list) -> str:
        """
        根据 image_type 选基础 prompt，有叙事关键词时追加一票否决指令。
        """
        is_composite = image_type == "composite"
        is_distant   = image_type in ("background_only", "solo_distant")
        is_character = image_type == "solo_character"

        if is_composite:
            prompt = self.PROMPT_COMPOSITE.format(
                review_focus=self.composite_review_focus)
        elif is_distant:
            prompt = self.PROMPT_SOLO_DISTANT.format(
                review_focus=self.review_focus)
        elif is_character:
            prompt = self.PROMPT_SOLO_CHARACTER.format(
                review_focus=self.review_focus)
        else:
            prompt = self.PROMPT_SOLO_MECHA.format(
                review_focus=self.review_focus)

        # 叙事一致性追加（清洗后仍有视觉可验证关键词才追加）
        clean_kw = self._clean_narration_keywords(narration_keywords or [])
        if clean_kw:
            must_haves_str = "、".join(clean_kw)
            prompt += self._NARRATION_CHECK_SUFFIX.format(
                must_haves_str=must_haves_str)

        return prompt

    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult:
        raw_text = ""
        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()

            prompt   = self._build_prompt(context.image_type,
                                          context.narration_keywords)
            raw_text = self._call_vision(prompt, img_bytes)

            if not raw_text.strip():
                raise ValueError("API 返回空响应")

            data = self._parse_json_tolerant(raw_text)

            # ── v2.3.6：叙事不符改为"软扣分"，不再一票否决 ──────
            # 旧版 narration_match=false 直接 return 3.0，导致构图氛围
            # 满分的图因少个次要道具被打死。新版：记录 flag，等数值分
            # 算完后扣 NARRATION_PENALTY 分并打 tag，交给重生逻辑去修。
            narration_penalty = 0.0
            narration_note    = ""
            if context.narration_keywords:
                # 经 _build_prompt 清洗后仍有关键词时模型才会输出 narration_match
                narration_match = data.get("narration_match", True)
                narration_note  = data.get("narration_note", "")
                if not narration_match:
                    narration_penalty = self.NARRATION_PENALTY
                    print(f"  [{self.vision_model}] ⚠ 叙事元素不全"
                          f"（扣 {narration_penalty} 分，不否决）: {narration_note}")
                else:
                    print(f"  [{self.vision_model}] ✓ 叙事一致: {narration_note}")

            # ── 评分计算（各分支，与 v2 完全一致）───────────────
            face_score = -1.0

            is_composite = context.image_type == "composite"
            is_distant   = context.image_type in ("background_only", "solo_distant")
            is_character = context.image_type == "solo_character"

            if is_composite:
                char_c  = max(0.0, min(30.0, float(data.get("char_completeness", 0))))
                scale_c = max(0.0, min(20.0, float(data.get("scale_consistency", 0))))
                light   = max(0.0, min(25.0, float(data.get("lighting_match",    0))))
                blend   = max(0.0, min(25.0, float(data.get("blend_quality",     0))))
                total   = round((char_c + scale_c + light + blend) / 10.0, 1)
                issues  = data.get("issues", [])
                hard_fail = char_c < 15.0 or "has_human" in issues
                passed  = (total >= self.threshold) and not hard_fail
                print(f"  [{self.vision_model}|合成] "
                      f"完={char_c:.0f} 比={scale_c:.0f} 光={light:.0f} 融={blend:.0f}"
                      f"={char_c+scale_c+light+blend:.0f}/100 → {total:.1f}/10"
                      f"  {'[硬不通过]' if hard_fail else ''}"
                      f"  {'通过' if passed else '不通过'}  {data.get('feedback','')}")

            elif is_distant:
                fig   = max(0.0, min(20.0, float(data.get("figure_placement", 0))))
                comp  = max(0.0, min(25.0, float(data.get("composition",      0))))
                atmo  = max(0.0, min(25.0, float(data.get("atmosphere",       0))))
                env   = max(0.0, min(20.0, float(data.get("environment",      0))))
                fig_q = max(0.0, min(10.0, float(data.get("figure_quality",  10))))
                total = round((fig + comp + atmo + env + fig_q) / 10.0, 1)
                issues    = data.get("issues", [])
                hard_fail = ("figure_missing" in issues) or (fig_q < 3.0)
                passed    = (total >= self.threshold) and not hard_fail
                print(f"  [{self.vision_model}|远景] "
                      f"位={fig:.0f} 构={comp:.0f} 氛={atmo:.0f} 环={env:.0f} 人={fig_q:.0f}"
                      f"={fig+comp+atmo+env+fig_q:.0f}/100 → {total:.1f}/10"
                      f"  {'通过' if passed else '不通过'}  {data.get('feedback','')}")

            elif is_character:
                face_c = max(0.0, min(25.0, float(data.get("face_clarity",     0))))
                face_s = max(0.0, min(15.0, float(data.get("face_consistency", 0))))
                cost_c = max(0.0, min(15.0, float(data.get("costume_accuracy", 0))))
                hand   = max(0.0, min(15.0, float(data.get("hand_quality",     0))))
                pose   = max(0.0, min(10.0, float(data.get("body_pose",        0))))
                atmo   = max(0.0, min(20.0, float(data.get("atmosphere",       0))))
                total  = round((face_c + face_s + cost_c + hand + pose + atmo) / 10.0, 1)
                issues        = data.get("issues", [])
                feedback_text = data.get("feedback", "")
                face_score    = round(face_c / 2.5, 1)
                hard_fail     = face_c < 10.0 or "wrong_ethnicity" in issues
                if "wrong_ethnicity" in issues:
                    print(f"  [{self.vision_model}] ⚠ 人种不符，强制不通过")
                passed = (total >= self.threshold) and not hard_fail
                if passed:
                    vetoed, veto_reason = self._veto_check(feedback_text, issues, hand)
                    if vetoed:
                        passed    = False
                        hard_fail = True
                        print(f"  [{self.vision_model}] ⚡ {veto_reason}")
                print(f"  [{self.vision_model}|人物] "
                      f"面清={face_c:.0f} 面符={face_s:.0f} 服={cost_c:.0f} "
                      f"手={hand:.0f} 姿={pose:.0f} 氛={atmo:.0f}"
                      f"={face_c+face_s+cost_c+hand+pose+atmo:.0f}/100 → {total:.1f}/10"
                      f"  face_score={face_score:.1f}"
                      f"  {'[不通过]' if hard_fail else ''}"
                      f"  {'通过' if passed else '不通过'}  {feedback_text}")
                if passed and face_score < 7.0:
                    print(f"  [FaceAlert] face_score={face_score:.1f} < 7.0，"
                          f"建议启用 ReActor 或配置 face_ref")

            else:
                face   = max(0.0, min(20.0, float(data.get("face_accuracy",  0))))
                body   = max(0.0, min(15.0, float(data.get("body_accuracy",  0))))
                comp   = max(0.0, min(25.0, float(data.get("composition",    0))))
                detail = max(0.0, min(25.0, float(data.get("detail_quality", 0))))
                atmo   = max(0.0, min(15.0, float(data.get("atmosphere",     0))))
                total  = round((face + body + comp + detail + atmo) / 10.0, 1)
                issues    = data.get("issues", [])
                face_score = round(face / 2.0, 1)
                hard_fail  = face < 10.0
                passed     = (total >= self.threshold) and not hard_fail
                print(f"  [{self.vision_model}|单角色] "
                      f"面={face:.0f} 体={body:.0f} 构={comp:.0f} 细={detail:.0f} 氛={atmo:.0f}"
                      f"={face+body+comp+detail+atmo:.0f}/100 → {total:.1f}/10"
                      f"  face_score={face_score:.1f}"
                      f"  {'[面部不通过]' if hard_fail else ''}"
                      f"  {'通过' if passed else '不通过'}  {data.get('feedback','')}")

            total    = max(0.0, min(10.0, total))
            feedback = data.get("feedback", "")

            # v2.3.6：叙事不符 → 扣分 + 打 tag（不否决，passed 仍按数值分判）
            if narration_penalty > 0:
                total = max(0.0, round(total - narration_penalty, 1))
                issues = list(issues) + ["narration_mismatch"]
                feedback = (feedback + f" [叙事:{narration_note}]").strip()
                # 扣分后若跌破阈值，自然会重生；未跌破则保留（不强制 fail）
                passed = (total >= self.threshold) and passed

            return QualityResult(
                passed        = passed,
                score         = total,
                feedback      = " ".join(issues) + (f" {feedback}" if feedback else ""),
                selected_path = image_path,
                detail        = data,
                tags          = issues,
                face_score    = face_score,
            )

        except Exception as e:
            print(f"  [{self.vision_model}] 评分失败: {e}")
            print(f"  → 原始响应前120字: {raw_text[:120] if raw_text else '(无响应)'}")
            return QualityResult(passed=False, score=-1.0,
                                 selected_path=image_path,
                                 feedback="评分失败",
                                 face_score=-1.0)

    def compare_candidates(self, paths: list,
                           context: QualityContext) -> str:
        if len(paths) <= 1:
            return paths[0] if paths else ""

        print(f"  [比较评估] {len(paths)} 张同分图，提交多图比较...")
        try:
            imgs_bytes = []
            for p in paths:
                with open(p, "rb") as f:
                    imgs_bytes.append(f.read())

            prompt = (
                f"你是图片质量评审专家。我给你展示{len(paths)}张候选图片"
                f"（图1到图{len(paths)}），请选出综合质量最好的一张。\n"
                f"评判标准（按重要度）：\n"
                f"1. 面部清晰度和准确性（最重要，有明显模糊/变形的排除）\n"
                f"2. 手部是否自然（手指数量正确、无粘连）\n"
                f"3. 细节质量和整体清晰度\n"
                f"4. 构图和氛围\n"
                f"只返回JSON，不输出其他："
                f"{{\"best_index\": 1, \"reason\": \"中文15字内\"}}\n"
                f"best_index 从1开始。"
            )

            raw  = self._call_vision(prompt, imgs_bytes)
            data = self._parse_json_tolerant(raw)
            idx  = max(0, min(len(paths) - 1,
                              int(data.get("best_index", 1)) - 1))
            print(f"  [比较评估] 选图{idx+1}: {Path(paths[idx]).name}"
                  f"  原因: {data.get('reason', '')}")
            return paths[idx]

        except Exception as e:
            print(f"  [比较评估] 失败: {e}，回退到第一张")
            return paths[0]


# ════════════════════════════════════════
# BatchSelectGate
# ════════════════════════════════════════
class BatchSelectGate:
    name = "batch_select"

    def __init__(self, n: int = 3,
                 inner: QualityGate = None,
                 generate_fn=None):
        self.n           = n
        self.inner       = inner or LlavaGate(threshold=6.5)
        self.generate_fn = generate_fn

    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult:
        import random
        candidates = [image_path]

        if self.generate_fn and self.n > 1:
            for i in range(self.n - 1):
                print(f"  [batch] 候选图 {i+2}/{self.n}")
                extra = self.generate_fn(extra_seed=random.randint(10000, 99999))
                if extra:
                    candidates.append(extra)

        results = []
        for path in candidates:
            r = self.inner.evaluate(path, context)
            results.append((path, r))
            print(f"  [batch] {Path(path).name}: {r.score:.1f}")

        best_path, best_result = max(results, key=lambda x: x[1].score)
        print(f"  [batch] 最优: {Path(best_path).name} ({best_result.score:.1f})")
        return QualityResult(
            passed        = best_result.passed,
            score         = best_result.score,
            feedback      = best_result.feedback,
            selected_path = best_path,
            detail        = {"candidates": len(results)},
            face_score    = best_result.face_score,
        )


# ════════════════════════════════════════
# EnsembleGate
# ════════════════════════════════════════
class EnsembleGate:
    name = "ensemble"

    def __init__(self, gates: list, strategy: str = "all"):
        self.gates    = gates
        self.strategy = strategy

    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult:
        results  = []
        feedback = []
        for gate in self.gates:
            r = gate.evaluate(image_path, context)
            results.append(r)
            if r.feedback:
                feedback.append(f"[{gate.name}] {r.feedback}")
            if self.strategy == "all" and not r.passed:
                break

        passed      = (all(r.passed for r in results)
                       if self.strategy == "all"
                       else any(r.passed for r in results))
        scores      = [r.score for r in results if r.score >= 0]
        face_scores = [r.face_score for r in results if r.face_score >= 0]
        return QualityResult(
            passed        = passed,
            score         = min(scores) if scores else -1.0,
            feedback      = " | ".join(feedback),
            selected_path = results[-1].selected_path if results else image_path,
            face_score    = min(face_scores) if face_scores else -1.0,
        )


# ════════════════════════════════════════
# SoftGate ── v4 软质量门 (只管硬伤,按 profile 分流)
# ════════════════════════════════════════
class SoftGate:
    """
    v4 软质量门:只管硬伤 (脸/手/解剖学崩坏),构图氛围 focus 一概不管。

    评分策略:
      - 远景 (solo_distant / background_only):单次评分 (沿用 LlavaGate 远景模板)
      - 合成 (composite):单次评分 (沿用 LlavaGate 合成模板)
      - 人物 (solo_character):两阶段评分
          Stage 1: 存在性识别 (脸/肢/镜头/焦点),只问事实,不评分
          Stage 2: 按 quality_profile 选评分尺子,不可见的项跳过

    Profile 由主题 yaml 声明 (theme.quality.profile),pipeline 注入:
      human_realistic    人/古风/写实  → 严判 5 指人手 + 真人脸
      human_stylized     二次元/插画风 → 5 指人手 + 二次元脸放宽
      anthro_creature    拟人动物绘本  → 卡通脸对称即可,不数指头
      realistic_animal   真实动物      → 物种解剖,四肢数量正确
      mecha              机甲          → v-fin/感应眼/面甲 (沿用 PROMPT_SOLO_MECHA)
      object_focus       物件特写       → 不评脸不评手,只查水印/截断
      none               跳过硬伤检查   → 直接 pass (极端情况留口)

    接口与 LlavaGate 100% 一致:
      - evaluate(image_path, context) → QualityResult
      - QualityResult 全字段保留含义
      - face_score = -1 表示 "未评" (脸不可见或 profile 不查脸),
        让 face_needs_repair 自然失效
      - tags / feedback / detail 照常输出

    依赖:内部复用 LlavaGate 实例做远景/合成评分、模型调用、JSON 解析。
    """
    name = "soft"

    # SoftGate 内置默认阈值 (覆盖 theme.quality.threshold)
    # 理由:SoftGate 只算硬伤分,主题级别的 threshold 微调意义不大,统一阈值省心
    DEFAULT_THRESHOLD = 5.5

    # Stage 1 观察 prompt — 只问事实,不评分,不识别 profile (profile 由主题给)
    STAGE1_PROMPT = """你是图片观察员。只观察事实,不评分,不评价美感。

返回严格 JSON (示例):
{"face_visible":"clear","face_count":1,"limbs_visible":"two","limb_in_focus":false,"shot_distance":"medium","main_subject_focus":"face"}

字段定义:
- face_visible: "none" | "partial" | "clear"
    none = 完全看不到脸 (背影/被完全遮挡/画外)
    partial = 只能看到部分 (侧脸轮廓/半遮/低头看不清五官)
    clear = 至少一只眼+一边脸颊清晰可辨
    (注:拟人动物的脸/动物的脸也算脸)
- face_count: 画面里能数清的脸数量 (0/1/2/3,超过 3 填 3)
- limbs_visible: "none" | "one" | "two" | "many_or_overlap"
    肢端=人的手 / 动物的爪、掌、足 / 拟人动物的前肢末端
    none = 完全看不到肢端
    one = 一只肢端在画面内
    two = 两只肢端分开可辨
    many_or_overlap = 多只交叠 / 多角色肢端交错 / 重叠
- limb_in_focus: true/false 肢端是否是画面焦点 (占比大、清晰特写)
- shot_distance: "extreme_closeup" | "closeup" | "medium" | "wide"
- main_subject_focus: "face" | "limbs" | "object" | "full_body" | "scene"

只返回 JSON,不要任何解释文字。"""

    # Stage 1 解析失败时的保守默认值
    # (倾向"完整人物镜",让 Stage 2 按正常规则评分,
    # 至少不会因 Stage 1 失败而误放行)
    STAGE1_FALLBACK = {
        "face_visible":       "clear",
        "face_count":         1,
        "limbs_visible":      "two",
        "limb_in_focus":      False,
        "shot_distance":      "medium",
        "main_subject_focus": "face",
    }

    # Profile 注册表 — 决定 Stage 2 的评分规则
    # 新增 profile 时只需要加一条
    SUPPORTED_PROFILES = {
        "human_realistic",
        "human_stylized",
        "anthro_creature",
        "realistic_animal",
        "mecha",
        "object_focus",
        "none",
    }

    def __init__(self,
                 threshold:              float = None,
                 vision_model:           str   = "minicpm-v:8b",
                 review_focus:           str   = "硬伤检测:面部/手部/解剖学完整性",
                 composite_review_focus: str   = "两个角色是否完整、光线是否统一、融合是否自然",
                 face_hard_threshold:    float = 6.0):
        self.threshold              = threshold if threshold is not None else self.DEFAULT_THRESHOLD
        self.vision_model           = vision_model
        self.review_focus           = review_focus
        self.composite_review_focus = composite_review_focus
        self.face_hard_threshold    = face_hard_threshold

        # 内部复用 LlavaGate:远景/合成评分、模型调用、JSON 解析全用它
        self._llava = LlavaGate(
            threshold              = self.threshold,
            vision_model           = vision_model,
            review_focus           = review_focus,
            composite_review_focus = composite_review_focus,
            face_hard_threshold    = face_hard_threshold,
        )

    # ── 对外协议 ────────────────────────────────────────────
    def warmup(self):
        self._llava.warmup()

    def free_model(self):
        self._llava.free_model()

    def compare_candidates(self, paths: list,
                           context: QualityContext) -> str:
        return self._llava.compare_candidates(paths, context)

    def evaluate(self, image_path: str,
                 context: QualityContext) -> QualityResult:
        # profile 校验:不识别的 profile 退化为 human_realistic
        profile = context.quality_profile or "human_realistic"
        if profile not in self.SUPPORTED_PROFILES:
            print(f"  [soft] ⚠ 未识别的 profile={profile},退化为 human_realistic")
            profile = "human_realistic"

        # profile=none 直接放行 (极端情况留口)
        if profile == "none":
            print(f"  [soft|profile=none] 跳过硬伤检查,直接通过")
            return QualityResult(passed=True, score=10.0,
                                 selected_path=image_path,
                                 face_score=-1.0,
                                 detail={"profile": "none"})

        # profile=mecha 走 LlavaGate 的机甲模板 (现成的)
        # 通过临时改 image_type 让 LlavaGate 走 PROMPT_SOLO_MECHA
        if profile == "mecha":
            mecha_ctx = QualityContext(
                page_num=context.page_num, page_title=context.page_title,
                characters=context.characters, char_features=context.char_features,
                attempt=context.attempt, max_attempts=context.max_attempts,
                image_type="solo_mecha",
                narration_keywords=[],     # SoftGate 不查 narration
                quality_profile=profile,
            )
            return self._llava.evaluate(image_path, mecha_ctx)

        # profile=object_focus:不评脸不评手,只看水印/截断
        # 用 LlavaGate 远景模板 (figure_quality 那套,对物件友好)
        if profile == "object_focus":
            obj_ctx = QualityContext(
                page_num=context.page_num, page_title=context.page_title,
                characters=context.characters, char_features=context.char_features,
                attempt=context.attempt, max_attempts=context.max_attempts,
                image_type="solo_distant",
                narration_keywords=[],
                quality_profile=profile,
            )
            return self._llava.evaluate(image_path, obj_ctx)

        # 远景/合成 → 单段评分,直接走 LlavaGate
        # (远景模板已够宽松,合成镜本身也是硬伤检测导向)
        if context.image_type != "solo_character":
            return self._llava.evaluate(image_path, context)

        # 人物镜 → 两阶段,按 profile 选评分尺子
        return self._evaluate_two_stage(image_path, context, profile)

    # ── Stage 1:存在性观察 ─────────────────────────────────
    def _stage1_observe(self, image_path: str) -> dict:
        """
        调用模型做事实性观察。失败时返回保守默认值。
        """
        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
            raw  = self._llava._call_vision(self.STAGE1_PROMPT, img_bytes)
            data = self._llava._parse_json_tolerant(raw)
            return self._sanity_check_stage1(data)
        except Exception as e:
            print(f"  [soft|stage1] 观察失败 ({str(e)[:40]}) → 用保守默认值")
            return dict(self.STAGE1_FALLBACK)

    @staticmethod
    def _sanity_check_stage1(data: dict) -> dict:
        """对 Stage 1 输出做枚举值校验,非法值替换为保守默认。"""
        out = dict(SoftGate.STAGE1_FALLBACK)

        fv = data.get("face_visible", "clear")
        if fv in ("none", "partial", "clear"):
            out["face_visible"] = fv

        try:
            fc = int(data.get("face_count", 1))
            out["face_count"] = max(0, min(3, fc))
        except (TypeError, ValueError):
            pass

        lv = data.get("limbs_visible", "two")
        if lv in ("none", "one", "two", "many_or_overlap"):
            out["limbs_visible"] = lv

        out["limb_in_focus"] = bool(data.get("limb_in_focus", False))

        sd = data.get("shot_distance", "medium")
        if sd in ("extreme_closeup", "closeup", "medium", "wide"):
            out["shot_distance"] = sd

        ms = data.get("main_subject_focus", "face")
        if ms in ("face", "limbs", "object", "full_body", "scene"):
            out["main_subject_focus"] = ms

        # 矛盾纠错
        # 远景却说焦点是肢端 → 大概率分类错,降级为非焦点
        if out["shot_distance"] == "wide" and out["limb_in_focus"]:
            out["limb_in_focus"] = False
        # face=none 但 main_subject=face → 模型自相矛盾,按 partial 处理
        if out["face_visible"] == "none" and out["main_subject_focus"] == "face":
            out["face_visible"] = "partial"

        return out

    # ── Stage 2:按 profile 拼装条件化 prompt ───────────────
    def _build_stage2_prompt(self, stage1: dict, profile: str) -> str:
        face_v  = stage1["face_visible"]
        limbs_v = stage1["limbs_visible"]
        limb_f  = stage1["limb_in_focus"]
        shot    = stage1["shot_distance"]

        # 各 profile 的规则差异主要在三处:
        #   1) face 评什么 (人脸结构 / 卡通脸结构 / 物种结构)
        #   2) limb 评什么 (5 指 / 爪形 / 四肢数量)
        #   3) 一票否决标准 (严格度)
        rules = self._profile_rules(profile, face_v, limbs_v, limb_f, shot)

        wide_note = ""
        if shot == "wide" and face_v == "clear":
            wide_note = "\n注意:本图是远景镜头,面部要求可适度放宽。"

        # v2.10: 多头否决根据预期人脸数调整。
        #   expected=1 (单人镜): 出现多头 → 崩坏, pass=false (旧行为)
        #   expected>=2 (双人同框): 预期就有多张脸, 超出预期才算崩坏
        exp_faces = int(stage1.get("expected_faces", 1) or 1)
        if exp_faces >= 2:
            multi_head_rule = (
                f"  * 本镜预期 {exp_faces} 个人物同框, {exp_faces} 张脸属正常。\n"
                f"    仅当脸数明显超过 {exp_faces} (多头/鬼影/脸数对不上) → pass=false\n"
                f"  * 多肢交叠崩坏 (单人却长多臂等) → pass=false"
            )
        else:
            multi_head_rule = "  * 出现多头/多肢 → pass=false"
        common_veto = (multi_head_rule + "\n"
                       "  * 出现明显水印/文字签名 → pass=false")

        face_phrase  = {'none':'不可见','partial':'部分可见','clear':'清晰可见'}[face_v]
        limbs_phrase = {'none':'不可见','one':'一只','two':'两只',
                        'many_or_overlap':'交叠/多只'}[limbs_v]

        prompt = f"""你是图片硬伤检测员。**只检测硬伤,不评判美学**。{wide_note}

主体类型:{rules['profile_desc']}

已知事实 (不要质疑,按此评分):
- 面部状态:{face_v} ({face_phrase})
- 肢端状态:{limbs_v} ({limbs_phrase})
- 镜头:{shot}

评分维度 (设为 -1 表示该项不评分):
{rules['face_rule']}
{rules['limb_rule']}
- costume_accuracy (0-15): 服饰/外观是否符合主体设定 (参考分,不参与 pass 判定)
- body_pose (0-10): 除肢端外的整体姿态是否自然
- atmosphere (0-20): 氛围感 (参考分,不参与 pass 判定)

一票否决 (任一成立则 pass=false):
{rules['face_veto']}
{rules['limb_veto']}
{common_veto}
  * issues 含 wrong_ethnicity → pass=false

返回严格 JSON (不评分的项填 -1):
{{"face_clarity":0,"face_consistency":0,"costume_accuracy":0,"hand_quality":0,"body_pose":0,"atmosphere":0,"total":0,"issues":[],"feedback":"","pass":false}}

issues 可用标签:too_blurry/face_deformed/wrong_ethnicity/wrong_costume/bad_hands/hand_deformed/bad_pose/multi_head/multi_limb/has_watermark/wrong_anatomy
feedback 中文 15 字内。"""
        return prompt

    def _profile_rules(self, profile: str, face_v: str, limbs_v: str,
                       limb_f: bool, shot: str) -> dict:
        """
        按 profile 拼装四块规则:
          profile_desc / face_rule / face_veto / limb_rule / limb_veto
        """
        # ── 面部规则 (按 profile + face_v 组合) ──
        if face_v == "none":
            face_rule = "- face_clarity: 设为 -1 (脸不可见,跳过)\n- face_consistency: 设为 -1 (同上)"
            face_veto = ""
        elif profile in ("human_realistic", "human_stylized"):
            if face_v == "partial":
                face_rule = ("- face_clarity (0-15): 可见部分 (侧脸/半脸) 是否自然,不要求完整五官\n"
                             "- face_consistency (0-10): 可见特征是否符合人脸设定")
                face_veto = "  * face_clarity < 5 → pass=false"
            else:
                # human_stylized 对完整脸的判定比 realistic 宽松
                if profile == "human_stylized":
                    face_rule = ("- face_clarity (0-25): 二次元/插画风格的五官,允许风格化变形,但要协调\n"
                                 "- face_consistency (0-15): 符合二次元/插画美学")
                    face_veto = "  * face_clarity < 8 → pass=false (二次元也不该糊成一团)"
                else:
                    face_rule = ("- face_clarity (0-25): 面部清晰度,五官比例正确,无模糊无变形\n"
                                 "- face_consistency (0-15): 是否符合亚洲人脸型/古风气质")
                    face_veto = "  * face_clarity < 10 → pass=false"
        elif profile == "anthro_creature":
            # 拟人动物 — 卡通脸,关键是对称、五官位置合理,不要求逼真
            if face_v == "partial":
                face_rule = ("- face_clarity (0-15): 卡通脸的可见部分是否清晰\n"
                             "- face_consistency (0-10): 卡通风格是否一致")
                face_veto = "  * face_clarity < 4 → pass=false"
            else:
                face_rule = ("- face_clarity (0-25): 卡通脸是否对称、五官位置合理 (眼/鼻/嘴)\n"
                             "- face_consistency (0-15): 友好可爱的卡通气质,无诡异扭曲")
                face_veto = "  * face_clarity < 8 → pass=false (卡通脸不该崩)"
        elif profile == "realistic_animal":
            # 真实动物 — 物种解剖
            if face_v == "partial":
                face_rule = ("- face_clarity (0-15): 动物脸部可见部分是否符合物种解剖\n"
                             "- face_consistency (0-10): 物种特征是否一致")
                face_veto = "  * face_clarity < 5 → pass=false"
            else:
                face_rule = ("- face_clarity (0-25): 动物面部解剖正确 (眼鼻嘴位置、耳朵数量)\n"
                             "- face_consistency (0-15): 物种特征符合 (毛色/头型/比例)")
                face_veto = "  * face_clarity < 10 → pass=false"
        else:
            # 兜底
            face_rule = "- face_clarity (0-25): 面部清晰自然\n- face_consistency (0-15): 设定一致"
            face_veto = "  * face_clarity < 10 → pass=false"

        # ── 肢端规则 (按 profile + limbs_v + limb_f 组合) ──
        if limbs_v == "none":
            limb_rule = "- hand_quality: 设为 -1 (肢端不可见,跳过)"
            limb_veto = ""
        elif profile in ("human_realistic", "human_stylized"):
            if limbs_v == "many_or_overlap":
                limb_rule = ("- hand_quality (0-15): 仅检查明显畸形 (6指/缺指/手指融合)\n"
                             "- 双手交叠/重叠是正常构图,不算畸形")
                limb_veto = "  * 明显多指或缺指 → pass=false"
            elif limb_f:
                limb_rule = "- hand_quality (0-25): 手是画面焦点,严格检查手指数量 (5根)、比例、形态"
                limb_veto = "  * hand_quality < 10 → pass=false"
            else:
                limb_rule = "- hand_quality (0-15): 手指数量 (5根)、比例、形态,无粘连/断裂/扭曲"
                limb_veto = "  * hand_quality < 5 → pass=false"
        elif profile == "anthro_creature":
            # 拟人动物 — 爪/掌,不数指头
            if limbs_v == "many_or_overlap":
                limb_rule = "- hand_quality (0-15): 卡通爪/掌的基本形态是否完整,不要求数指头"
                limb_veto = "  * 明显多肢或断肢 → pass=false"
            elif limb_f:
                limb_rule = ("- hand_quality (0-20): 卡通爪/掌作为焦点,要求形态完整、轮廓清晰\n"
                             "- 不要求人手的 5 指结构")
                limb_veto = "  * hand_quality < 8 → pass=false"
            else:
                limb_rule = ("- hand_quality (0-15): 卡通爪/掌形态完整即可,无奇怪突起\n"
                             "- 不要求 5 指,不要求人手解剖")
                limb_veto = "  * hand_quality < 4 → pass=false"
        elif profile == "realistic_animal":
            # 真实动物 — 四肢数量与关节方向
            if limbs_v == "many_or_overlap":
                limb_rule = "- hand_quality (0-15): 四肢/爪可见,只检查数量是否合理"
                limb_veto = "  * 多肢/缺肢 → pass=false"
            elif limb_f:
                limb_rule = ("- hand_quality (0-20): 四肢/爪作为焦点,关节方向正确,无反折\n"
                             "- 数量符合物种 (四足动物 4 肢、鸟类 2 翅+2 足)")
                limb_veto = "  * hand_quality < 8 → pass=false"
            else:
                limb_rule = "- hand_quality (0-15): 四肢/爪形态自然,关节方向合理"
                limb_veto = "  * 明显反关节/多肢 → pass=false"
        else:
            limb_rule = "- hand_quality (0-15): 肢端形态自然"
            limb_veto = "  * hand_quality < 5 → pass=false"

        # ── profile 描述 ──
        profile_desc = {
            "human_realistic":  "真人/古风人物",
            "human_stylized":   "二次元/插画风人物",
            "anthro_creature":  "拟人化动物 (卡通风)",
            "realistic_animal": "真实动物",
            "mecha":            "机甲/机器人",
            "object_focus":     "物件特写",
        }.get(profile, "未知主体")

        return {
            "profile_desc": profile_desc,
            "face_rule":    face_rule,
            "face_veto":    face_veto,
            "limb_rule":    limb_rule,
            "limb_veto":    limb_veto,
        }

    def _stage2_score(self, image_path: str, stage1: dict, profile: str) -> dict:
        """调用模型做条件化评分。失败抛异常,由 _evaluate_two_stage 兜底。"""
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        prompt = self._build_stage2_prompt(stage1, profile)
        raw    = self._llava._call_vision(prompt, img_bytes)
        return self._llava._parse_json_tolerant(raw)

    # ── 主流程:组装两阶段结果 ─────────────────────────────
    def _evaluate_two_stage(self, image_path: str,
                            context: QualityContext,
                            profile: str) -> QualityResult:
        # Stage 1
        stage1 = self._stage1_observe(image_path)
        # v2.10: 把本镜预期人脸数注入 stage1, 供 Stage2 prompt + 硬伤否决判断
        #        (双人同框 expected_faces=2, 则 2 张脸不算 multi_head)
        stage1["expected_faces"] = max(1, int(getattr(context, "expected_faces", 1) or 1))
        print(f"  [soft|profile={profile}|stage1] "
              f"face={stage1['face_visible']} "
              f"count={stage1.get('face_count','?')}/exp{stage1['expected_faces']} "
              f"limbs={stage1['limbs_visible']} "
              f"shot={stage1['shot_distance']} "
              f"focus={stage1['main_subject_focus']}"
              f"{' [肢端焦点]' if stage1['limb_in_focus'] else ''}")

        # Stage 2
        try:
            data = self._stage2_score(image_path, stage1, profile)
        except Exception as e:
            print(f"  [soft|stage2] 评分失败: {e}")
            return QualityResult(passed=False, score=-1.0,
                                 selected_path=image_path,
                                 feedback="评分失败",
                                 face_score=-1.0,
                                 detail={"stage1": stage1, "profile": profile})

        return self._compute_result(image_path, stage1, data, profile)

    def _compute_result(self, image_path: str,
                        stage1: dict, data: dict,
                        profile: str) -> QualityResult:
        """把 Stage 2 的 JSON 转成 QualityResult,跳过未评项。"""
        face_v  = stage1["face_visible"]
        limbs_v = stage1["limbs_visible"]
        limb_f  = stage1["limb_in_focus"]

        def _read(key, max_val):
            try:
                v = float(data.get(key, 0))
            except (TypeError, ValueError):
                v = 0.0
            if v < 0:
                return -1.0
            return max(0.0, min(float(max_val), v))

        # 各维度上限按 profile + stage1 浮动
        # (与 _profile_rules 里的 max 值保持同步)
        face_max_clear = 25.0
        if profile in ("human_stylized", "anthro_creature", "realistic_animal", "human_realistic"):
            face_max_clear = 25.0
        face_max  = 15.0 if face_v == "partial" else face_max_clear

        # face_consistency 上限
        fcons_max = 10.0 if face_v == "partial" else 15.0

        # hand_quality 上限
        if limbs_v == "none":
            hand_max = 0.0  # 跳过
        elif limbs_v == "many_or_overlap":
            hand_max = 15.0
        elif limb_f:
            # 焦点肢端:human 严判 25,动物类略宽 20
            hand_max = 25.0 if profile in ("human_realistic", "human_stylized") else 20.0
        else:
            hand_max = 15.0

        face_c = _read("face_clarity",     face_max)  if face_v  != "none" else -1.0
        face_s = _read("face_consistency", fcons_max) if face_v  != "none" else -1.0
        hand   = _read("hand_quality",     hand_max)  if limbs_v != "none" else -1.0
        cost_c = _read("costume_accuracy", 15.0)
        pose   = _read("body_pose",        10.0)
        atmo   = _read("atmosphere",       20.0)

        issues       = list(data.get("issues", []) or [])
        feedback_txt = data.get("feedback", "") or ""

        # ── 硬伤一票否决 (按 profile 阈值) ─────────────────
        hard_fail   = False
        veto_reason = ""

        # 面部硬伤阈值表 (跟 _profile_rules 里 face_veto 同步)
        face_veto_thresholds = {
            ("human_realistic",  "clear"):   10.0,
            ("human_realistic",  "partial"): 5.0,
            ("human_stylized",   "clear"):   8.0,
            ("human_stylized",   "partial"): 5.0,
            ("anthro_creature",  "clear"):   8.0,
            ("anthro_creature",  "partial"): 4.0,
            ("realistic_animal", "clear"):   10.0,
            ("realistic_animal", "partial"): 5.0,
        }
        face_thr = face_veto_thresholds.get((profile, face_v))
        if face_thr is not None and 0 <= face_c < face_thr:
            hard_fail   = True
            veto_reason = f"面部硬伤 face_clarity={face_c:.1f}<{face_thr}"

        # 肢端硬伤阈值
        if not hard_fail and limbs_v != "none":
            limb_veto_thresholds = {
                ("human_realistic",  "many_or_overlap"): None,   # 看 issues
                ("human_realistic",  "focus"):           10.0,
                ("human_realistic",  "normal"):          5.0,
                ("human_stylized",   "many_or_overlap"): None,
                ("human_stylized",   "focus"):           10.0,
                ("human_stylized",   "normal"):          5.0,
                ("anthro_creature",  "many_or_overlap"): None,
                ("anthro_creature",  "focus"):           8.0,
                ("anthro_creature",  "normal"):          4.0,
                ("realistic_animal", "many_or_overlap"): None,
                ("realistic_animal", "focus"):           8.0,
                ("realistic_animal", "normal"):          5.0,
            }
            limb_kind = ("many_or_overlap" if limbs_v == "many_or_overlap"
                         else "focus" if limb_f
                         else "normal")
            limb_thr = limb_veto_thresholds.get((profile, limb_kind))
            if limb_thr is not None and 0 <= hand < limb_thr:
                hard_fail   = True
                veto_reason = f"肢端硬伤 hand_quality={hand:.1f}<{limb_thr}"

        # 通用硬伤 (跨 profile)
        # v2.10: multi_head 对双人同框镜放宽 —— 预期 ≥2 人时不把 multi_head 当崩坏
        #        (Stage2 prompt 已告知模型, 此处兜底防模型误打标签)
        exp_faces = int(stage1.get("expected_faces", 1) or 1)
        if not hard_fail:
            generic_veto_tags = ["multi_limb", "wrong_ethnicity", "has_watermark"]
            if exp_faces < 2:
                generic_veto_tags.insert(0, "multi_head")
            for tag in generic_veto_tags:
                if tag in issues:
                    hard_fail   = True
                    veto_reason = f"通用硬伤 issues={tag}"
                    break

        # feedback 关键词二次兜底 (复用 LlavaGate 的词典,接住模型没打标签的情况)
        if not hard_fail:
            for word in LlavaGate._FACE_VETO_WORDS:
                if word in feedback_txt and face_v != "none":
                    hard_fail   = True
                    veto_reason = f"面部硬伤 (feedback={word})"
                    break
        if not hard_fail and limbs_v != "none":
            for word in LlavaGate._HAND_VETO_WORDS:
                if word in feedback_txt:
                    hard_fail   = True
                    veto_reason = f"肢端硬伤 (feedback={word})"
                    break
            if not hard_fail and ("bad_hands" in issues or "hand_deformed" in issues):
                hard_fail   = True
                veto_reason = "肢端硬伤 (issues=bad_hands/hand_deformed)"

        # ── 总分 = 各项已评维度之和 / 10 (跟 LlavaGate 同口径) ─
        # 未评项 (-1) 不参与求和;参与的项按各自上限归一为 100 分制
        contribs = []
        if face_c >= 0: contribs.append(face_c)
        if face_s >= 0: contribs.append(face_s)
        if hand   >= 0: contribs.append(hand)
        contribs += [cost_c, pose, atmo]
        # 各项目上限之和 (动态)
        max_sum = 0.0
        if face_c >= 0: max_sum += face_max
        if face_s >= 0: max_sum += fcons_max
        if hand   >= 0: max_sum += hand_max
        max_sum += 15.0 + 10.0 + 20.0   # cost_c / pose / atmo

        if max_sum > 0:
            raw_total = sum(contribs)
            # 归一化到 10 分制 (100 分总满分换算)
            total = round((raw_total / max_sum) * 10.0, 1)
        else:
            total = 0.0

        passed = (total >= self.threshold) and not hard_fail

        # face_score (供下游 face_needs_repair 判断)
        # 不可见 → -1 (自动失效);可见 → face_c 归一为 10 分制
        if face_v == "none":
            face_score = -1.0
        else:
            face_score = round((face_c / face_max) * 10.0, 1) if face_c >= 0 else -1.0

        # ── 日志 ─────────────────────────────────────────────
        def _fmt(v, label): return f"{label}=N/A" if v < 0 else f"{label}={v:.0f}"
        print(f"  [soft|profile={profile}|stage2] "
              f"{_fmt(face_c,'面清')} {_fmt(face_s,'面符')} "
              f"{_fmt(hand,'手')} 服={cost_c:.0f} 姿={pose:.0f} 氛={atmo:.0f} "
              f"→ {total:.1f}/10  face_score={face_score:.1f}  "
              f"{'[硬伤否决] ' + veto_reason if hard_fail else ''}"
              f"  {'通过' if passed else '不通过'}  {feedback_txt}")

        return QualityResult(
            passed        = passed,
            score         = total,
            feedback      = " ".join(issues) + (f" {feedback_txt}" if feedback_txt else ""),
            selected_path = image_path,
            detail        = {"stage1": stage1, "profile": profile,
                             "veto_reason": veto_reason, "raw": data},
            tags          = issues,
            face_score    = face_score,
        )


# ════════════════════════════════════════
# 工厂函数
# ════════════════════════════════════════
def make_gate(mode: str, threshold: float = 7.0,
              batch_n: int = 3,
              vision_model: str = "llava:7b",
              review_focus: str = "画面质量和角色准确性",
              composite_review_focus: str = "两个机甲是否完整、光线是否统一、融合是否自然") -> QualityGate:
    def _llava(thr=threshold):
        return LlavaGate(
            threshold              = thr,
            vision_model           = vision_model,
            review_focus           = review_focus,
            composite_review_focus = composite_review_focus,
        )

    def _soft(thr=None):
        # SoftGate 内置默认阈值 5.5 (覆盖 threshold 参数,统一阈值省心)
        return SoftGate(
            threshold              = thr,
            vision_model           = vision_model,
            review_focus           = review_focus,
            composite_review_focus = composite_review_focus,
        )

    gates = {
        "normal":      AutoAcceptGate(),
        "human":       HumanGate(),
        "auto":        _llava(),
        "soft":        _soft(),                                          # v4 新增
        "batch":       BatchSelectGate(n=batch_n, inner=_llava(max(threshold - 0.5, 5.0))),
        "batch_soft":  BatchSelectGate(n=batch_n, inner=_soft()),        # v4 新增
        "strict":      EnsembleGate([_llava(), HumanGate()]),
    }
    gate = gates.get(mode, AutoAcceptGate())
    print(f"  [质量门] 模式={mode}  评估器={gate.name}  阈值={threshold}  视觉模型={vision_model}")
    return gate
