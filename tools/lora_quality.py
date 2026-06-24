"""
lora_quality.py ── LoRA 专用评分模块
======================================
与 quality.py 的核心区别：

  quality.py        → pipeline 用，目标是"这张图能不能出现在最终视频"
                       面部/手部严格，一票否决，宁可重生也不要烂图

  lora_quality.py   → trainer + tester 专用，目标是两件完全不同的事：
    trainer 用途：  "这张图适合进训练数据集吗"
                    要多样性而非完美，面部略模糊可接受，手部不关心
                    性别错误是唯一硬否决
    tester 用途：   "这个 checkpoint 比上一个更好吗"
                    评的是角色一致性，没有通过/不通过概念，只有横向排序
                    手部完全不参与评分，面部模糊降分但不否决

底层复用 quality.LlavaGate._call_vision（避免重复维护 Ollama 调用逻辑）。

用法：
  from lora_quality import TrainerScorer, TesterScorer

  # trainer 用：判断一张训练图是否合格
  scorer = TrainerScorer(vision_model="minicpm-v:8b", expected_gender="female")
  result = scorer.score(img_path)
  # result.ok       → 是否进数据集
  # result.score    → 0-10 参考分
  # result.reason   → 拒绝原因（仅 ok=False 时有意义）

  # tester 用：评估一张测试图的 checkpoint 质量
  scorer = TesterScorer(vision_model="minicpm-v:8b", char_desc="红衣女鬼，飘逸黑发")
  result = scorer.score(img_path, prompt_type="front_close")
  # result.consistency  → 角色一致性分（0-10，最重要）
  # result.style        → 风格匹配分（0-10）
  # result.overfit_flag → 是否疑似过拟合（远景变特写）
  # result.total        → 综合分（0-10，用于 checkpoint 横向比较）
"""

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

from config import OLLAMA_BASE, PROXIES


# ════════════════════════════════════════════════════════════════
# 底层：复用 Ollama 调用（不重复写，从 quality 借）
# ════════════════════════════════════════════════════════════════

def _call_vision(vision_model: str, prompt: str, img_bytes: bytes,
                 num_predict: int = 400) -> str:
    """直接调用 Ollama 视觉模型，返回原始文本。"""
    r = requests.post(
        f"{OLLAMA_BASE}/api/generate",
        json={
            "model":  vision_model,
            "prompt": prompt,
            "images": [base64.b64encode(img_bytes).decode()],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": num_predict},
        },
        timeout=180,
        proxies=PROXIES,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def _parse_json(text: str) -> dict:
    """宽松 JSON 解析，兼容模型输出不规整的情况。"""
    text = text.strip()
    for pattern in [r'\{[^{}]*\}', r'\{.*?\}']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                continue
    raise ValueError(f"无法解析 JSON: {text[:120]}")


def _free_model(vision_model: str):
    """卸载 Ollama 视觉模型，释放显存。"""
    try:
        requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": vision_model, "keep_alive": 0},
            timeout=10, proxies=PROXIES,
        )
        print(f"  [视觉模型] 已卸载: {vision_model}")
    except Exception:
        pass


def _warmup(vision_model: str):
    try:
        _call_vision(vision_model, "你好", b"iVBORw0KGgo=", num_predict=5)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════
# Trainer 评分
# ════════════════════════════════════════════════════════════════

@dataclass
class TrainImageResult:
    ok:        bool          # 是否进数据集
    score:     float         # 0-10 综合参考分（不是硬门槛，只是辅助显示）
    gender_ok: bool  = True  # 性别是否符合预期（唯一硬否决）
    reason:    str   = ""    # 拒绝原因


class TrainerScorer:
    """
    训练图筛选评分器。

    目标：判断一张图是否适合进入 LoRA 训练数据集。
    原则：
      - 多样性优先于完美，面部略模糊可以接受
      - 手部完全不参与评分（FLUX 手部问题和 LoRA 无关）
      - 性别错误是唯一硬否决（会直接影响 LoRA 学到错误的人物特征）
      - 图片整体崩坏（人物缺失/比例严重错误）也拒绝

    评分维度（合计 100 分）：
      costume_accuracy  服饰准确性  0-35  ← 最重要，训练目标
      figure_quality    人物整体质量 0-30  包含姿态、比例、轮廓完整性
      style_match       风格匹配    0-20  古风水墨感
      composition       构图合理性  0-15  人物在画面中位置是否合适
    """

    # 触发拒绝的分数线（低于此值认为图片崩坏）
    HARD_FAIL_THRESHOLD = 4.0   # 综合分低于 4.0 直接拒绝

    PROMPT_TRAINER = """你是 LoRA 训练数据筛选专家。评审这张图是否适合进入角色 LoRA 训练集。

训练集要求：
- 服饰特征明确清晰（最重要）
- 人物比例正常，无严重变形
- 古风画风一致
- 不要求面部完美，不评手部

评分标准（合计 100 分）：
- costume_accuracy:  服饰、配饰特征是否清晰完整，是否符合古风设定 (0-35)
- figure_quality:    人物整体质量——比例、轮廓、姿态是否正常，无严重变形 (0-30)
- style_match:       画风是否为古风/水墨/东方美学 (0-20)
- composition:       人物在画面中的位置是否合适，不被截断 (0-15)

硬拒绝规则（任意一条 → reject=true）：
- figure_quality < 10：人物严重变形/缺失
- costume_accuracy < 10：完全看不清服饰

只返回 JSON，不输出其他：
{"costume_accuracy":0,"figure_quality":0,"style_match":0,"composition":0,"total":0,"reject":false,"feedback":"中文10字内"}"""

    def __init__(self, vision_model: str = "minicpm-v:8b",
                 expected_gender: str = ""):
        self.vision_model    = vision_model
        self.expected_gender = expected_gender  # "male" / "female" / ""

    def warmup(self):
        _warmup(self.vision_model)

    def free_model(self):
        _free_model(self.vision_model)

    def score(self, img_path: "str | Path") -> TrainImageResult:
        """
        评估一张训练候选图。
        返回 TrainImageResult，ok=True 表示可以进数据集。
        """
        img_bytes = Path(img_path).read_bytes()

        try:
            raw = _call_vision(self.vision_model, self.PROMPT_TRAINER, img_bytes)
            data = _parse_json(raw)

            costume = max(0.0, min(35.0, float(data.get("costume_accuracy", 0))))
            figure  = max(0.0, min(30.0, float(data.get("figure_quality",   0))))
            style   = max(0.0, min(20.0, float(data.get("style_match",      0))))
            comp    = max(0.0, min(15.0, float(data.get("composition",      0))))
            total   = round((costume + figure + style + comp) / 10.0, 1)
            reject  = data.get("reject", False)
            feedback = data.get("feedback", "")

            # 硬否决：整体分数过低
            if total < self.HARD_FAIL_THRESHOLD:
                reject = True

            ok = not reject
            reason = "" if ok else feedback

            print(f"  [训练评分] 服={costume:.0f} 人={figure:.0f} 风={style:.0f} 构={comp:.0f}"
                  f" → {total:.1f}/10  {'✓ 入选' if ok else f'✗ 拒绝 {reason}'}")

        except Exception as e:
            print(f"  [训练评分] 评分失败: {e}，默认入选")
            return TrainImageResult(ok=True, score=5.0, reason="评分失败，默认入选")

        # 性别检查（唯一硬否决，单独做）
        gender_ok = True
        if self.expected_gender and ok:
            gender_ok = self._check_gender(img_bytes)
            if not gender_ok:
                ok = False
                reason = f"性别错误（期望{self.expected_gender}）"
                print(f"  [训练评分] ✗ 性别不符，拒绝")

        return TrainImageResult(
            ok=ok, score=total, gender_ok=gender_ok, reason=reason)

    def _check_gender(self, img_bytes: bytes) -> bool:
        """快速性别检查，只问 male/female。"""
        try:
            ans = _call_vision(
                self.vision_model,
                "Is the main person in this image male or female? "
                "Answer exactly one word: male or female",
                img_bytes, num_predict=10,
            ).strip().lower()
            detected = "male" if "male" in ans else "female" if "female" in ans else "unknown"
            if detected == "unknown":
                return True  # 检测不出，不否决
            return detected == self.expected_gender
        except Exception:
            return True  # 检测失败，不否决


# ════════════════════════════════════════════════════════════════
# Tester 评分
# ════════════════════════════════════════════════════════════════

@dataclass
class TestImageResult:
    """单张测试图的评分结果"""
    consistency:  float        # 角色一致性 0-10（核心维度）
    style:        float        # 风格匹配 0-10
    composition:  float        # 构图 0-10
    total:        float        # 综合分 0-10（用于 checkpoint 横向排序）
    overfit_flag: bool  = False # 疑似过拟合（远景图变成近景特写）
    feedback:     str   = ""
    raw:          dict  = field(default_factory=dict)  # LLM 原始输出，供调试


class TesterScorer:
    """
    LoRA Checkpoint 对比评分器。

    目标：区分不同 checkpoint 的角色一致性质量，用于横向排序。
    原则：
      - 没有通过/不通过概念，只有分数高低
      - 手部完全不参与（不相关）
      - 面部模糊降分但不否决（模糊是欠拟合信号，反映在分数差异上）
      - 角色一致性是最重要的维度，权重最高
      - 远景图额外检测过拟合（远景应该是小人，变成特写说明过拟合）

    评分维度（近景/特写）：
      char_consistency  角色一致性  0-40  服饰特征、外观描述符合度（最重要）
      face_recognizable 面部可辨识  0-25  能认出是同一个角色（不要求完美清晰）
      style_fidelity    风格保持    0-20  古风水墨感是否保持
      composition       构图自然    0-15  人物在画面中的呈现

    评分维度（远景/侧面）：
      char_consistency  角色一致性  0-40  服饰轮廓、颜色特征符合度
      figure_natural    人物自然度  0-30  比例、轮廓、姿态是否正常
      style_fidelity    风格保持    0-20  
      spatial_sense     空间感      0-10  小人在大环境中的比例是否合适
    """

    PROMPT_CLOSE = """你是 LoRA 角色一致性评审专家。评估这张图中的角色是否与描述一致。

角色描述：{char_desc}

评分标准（合计 100 分，用于对比不同训练阶段的质量差异）：
- char_consistency:   服饰特征、外观细节与角色描述的符合程度 (0-40) ← 最重要
- face_recognizable:  面部是否可辨识为同一角色（不要求完美，略模糊可得中分） (0-25)
- style_fidelity:     画面风格是否保持古风/水墨/东方美学 (0-20)
- composition:        构图是否自然，人物呈现是否完整 (0-15)

注意：
- face_recognizable 评的是"可辨识性"，不是"清晰度"——略模糊但能认出角色得 15+
- 不评手部质量

只返回 JSON：
{{"char_consistency":0,"face_recognizable":0,"style_fidelity":0,"composition":0,"total":0,"feedback":"中文10字内"}}"""

    PROMPT_DISTANT = """你是 LoRA 角色一致性评审专家。这是一张远景/全身/背影图，评估角色特征保持情况。

角色描述：{char_desc}

评分标准（合计 100 分）：
- char_consistency:  服饰颜色、轮廓特征与角色描述的符合程度 (0-40) ← 最重要
- figure_natural:    人物比例、轮廓、姿态是否自然，无明显变形 (0-30)
- style_fidelity:    画面风格是否保持古风/水墨美学 (0-20)
- spatial_sense:     人物在环境中的比例是否合适（远景应有空间感） (0-10)

过拟合检测：
- 如果这是"远景全身"图但人物占画面 70% 以上（像特写），overfit=true

只返回 JSON：
{{"char_consistency":0,"figure_natural":0,"style_fidelity":0,"spatial_sense":0,"total":0,"overfit":false,"feedback":"中文10字内"}}"""

    def __init__(self, vision_model: str = "minicpm-v:8b",
                 char_desc: str = ""):
        self.vision_model = vision_model
        self.char_desc    = char_desc   # 角色外观描述，来自 trigger_solo 或 desc

    def warmup(self):
        _warmup(self.vision_model)

    def free_model(self):
        _free_model(self.vision_model)

    def score(self, img_path: "str | Path",
              prompt_type: str = "front_close") -> TestImageResult:
        """
        评估一张测试图。
        prompt_type：front_close / three_quarter / side_profile /
                     distant_wide / action_dynamic
        """
        img_bytes = Path(img_path).read_bytes()
        is_distant = prompt_type in ("distant_wide",)

        try:
            if is_distant:
                prompt = self.PROMPT_DISTANT.format(char_desc=self.char_desc[:200])
                raw = _call_vision(self.vision_model, prompt, img_bytes)
                data = _parse_json(raw)

                consist  = max(0.0, min(40.0, float(data.get("char_consistency", 0))))
                fig_nat  = max(0.0, min(30.0, float(data.get("figure_natural",   0))))
                style    = max(0.0, min(20.0, float(data.get("style_fidelity",   0))))
                spatial  = max(0.0, min(10.0, float(data.get("spatial_sense",    0))))
                total    = round((consist + fig_nat + style + spatial) / 10.0, 1)
                overfit  = bool(data.get("overfit", False))
                feedback = data.get("feedback", "")

                # 一致性转换为 0-10（满分 40 → 10）
                consist_norm = round(consist / 4.0, 1)
                style_norm   = round(style   / 2.0, 1)
                comp_norm    = round(spatial  / 1.0, 1)   # spatial_sense 当 composition

                print(f"  [测试评分|远景] 一致={consist:.0f} 人物={fig_nat:.0f} "
                      f"风格={style:.0f} 空间={spatial:.0f} → {total:.1f}/10"
                      f"{'  ⚠过拟合' if overfit else ''}  {feedback}")

                return TestImageResult(
                    consistency=consist_norm, style=style_norm,
                    composition=comp_norm, total=total,
                    overfit_flag=overfit, feedback=feedback, raw=data,
                )

            else:
                prompt = self.PROMPT_CLOSE.format(char_desc=self.char_desc[:200])
                raw = _call_vision(self.vision_model, prompt, img_bytes)
                data = _parse_json(raw)

                consist  = max(0.0, min(40.0, float(data.get("char_consistency",   0))))
                face_rec = max(0.0, min(25.0, float(data.get("face_recognizable",  0))))
                style    = max(0.0, min(20.0, float(data.get("style_fidelity",     0))))
                comp     = max(0.0, min(15.0, float(data.get("composition",        0))))
                total    = round((consist + face_rec + style + comp) / 10.0, 1)
                feedback = data.get("feedback", "")

                consist_norm = round(consist  / 4.0, 1)
                style_norm   = round(style    / 2.0, 1)
                comp_norm    = round(comp     / 1.5, 1)

                print(f"  [测试评分|近景] 一致={consist:.0f} 可辨={face_rec:.0f} "
                      f"风格={style:.0f} 构图={comp:.0f} → {total:.1f}/10  {feedback}")

                return TestImageResult(
                    consistency=consist_norm, style=style_norm,
                    composition=comp_norm, total=total,
                    overfit_flag=False, feedback=feedback, raw=data,
                )

        except Exception as e:
            print(f"  [测试评分] 评分失败: {e}，返回 0 分")
            return TestImageResult(
                consistency=0.0, style=0.0, composition=0.0,
                total=0.0, feedback=f"评分失败: {e}",
            )


# ════════════════════════════════════════════════════════════════
# 便捷函数：批量评分（tester 主流程用）
# ════════════════════════════════════════════════════════════════

def score_test_batch(image_records: list[dict],
                     vision_model: str,
                     char_desc: str) -> list[dict]:
    """
    批量评分 tester 生成的测试图。
    image_records: [{"image_path": str, "prompt_id": str, ...}, ...]
    返回：每条记录新增 score / consistency / style / overfit_flag / feedback 字段
    """
    scorer = TesterScorer(vision_model=vision_model, char_desc=char_desc)
    scorer.warmup()

    for i, rec in enumerate(image_records):
        img_path = rec.get("image_path", "")
        if not img_path or not Path(img_path).exists():
            rec["score"] = 0.0
            rec["consistency"] = 0.0
            rec["style"] = 0.0
            rec["overfit_flag"] = False
            rec["feedback"] = "文件不存在"
            continue

        result = scorer.score(img_path, prompt_type=rec.get("prompt_id", "front_close"))
        rec["score"]        = result.total
        rec["consistency"]  = result.consistency
        rec["style"]        = result.style
        rec["overfit_flag"] = result.overfit_flag
        rec["feedback"]     = result.feedback

        print(f"  [{i+1}/{len(image_records)}] {Path(img_path).name}: "
              f"{result.total:.1f}  一致性={result.consistency:.1f}  {result.feedback[:40]}")

    scorer.free_model()
    return image_records


def score_train_batch(image_paths: list[Path],
                      vision_model: str,
                      expected_gender: str = "") -> tuple[list[Path], list[Path]]:
    """
    批量评分训练候选图，返回 (passed, rejected)。
    替代 lora_trainer.py 里的 auto_screen_images()。
    """
    scorer = TrainerScorer(vision_model=vision_model,
                           expected_gender=expected_gender)
    scorer.warmup()

    passed, rejected = [], []
    for i, img_path in enumerate(image_paths):
        print(f"  [{i+1}/{len(image_paths)}] {img_path.name}")
        result = scorer.score(img_path)
        (passed if result.ok else rejected).append(img_path)

    scorer.free_model()
    print(f"  入选={len(passed)}  拒绝={len(rejected)}")
    return passed, rejected
