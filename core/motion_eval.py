"""
motion_eval.py ── AI 视觉评估 v3.1（二段式 + 方向多样化修正）
================================================================
v3.1 调整（vs v3）：
  ★ 重写 Stage 2 prompt 中的 KB 方向决策规则
    - 删除"无明显方向 → zoom_in"的兜底（导致 8 页全 zoom_in）
    - 强制 AI 先在描述里找 facing direction 关键词
    - 新增 face_size + composition 联合决策矩阵
    - kb_direction 决策完全基于画面观察，不被 motion_hint 干扰

  ★ Stage 2 拆出 kb_direction 的"思维链"
    - 让 AI 先写 facing_direction（从描述提取）
    - 再基于 facing_direction 推 kb_direction
    - 这样 AI 不能"偷懒"直接选默认值

v3 已有：
  - 二段式：minicpm-v 描述 + qwen2.5:14b 决策
  - 双层缓存（Stage 1 + Stage 2）
  - Phase A/B 串行（避免显存冲突）
"""

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

import requests

from config import OLLAMA_BASE, PROXIES


# ── 缓存 ─────────────────────────────────────────────────

_STAGE1_CACHE_FILE = "_motion_stage1_cache.json"
_STAGE2_CACHE_FILE = "_motion_stage2_cache.json"


def _stage1_key(image_path: str, vision_model: str) -> str:
    p = Path(image_path).resolve()
    if not p.exists():
        return ""
    stat = p.stat()
    raw = f"{p}:{int(stat.st_mtime)}:{stat.st_size}:{vision_model}"
    return hashlib.md5(raw.encode()).hexdigest()


def _stage2_key(image_path: str, narration: str,
                vision_model: str, text_model: str) -> str:
    p = Path(image_path).resolve()
    if not p.exists():
        return ""
    stat = p.stat()
    raw = (f"{p}:{int(stat.st_mtime)}:{stat.st_size}:"
           f"{vision_model}:{text_model}:{narration[:100]}")
    return hashlib.md5(raw.encode()).hexdigest()


# ══════════════════════════════════════════════════════════
# Stage 1：视觉描述（与 v3 相同）
# ══════════════════════════════════════════════════════════

_STAGE1_PROMPT = """Look at this image carefully and describe what you see in plain English.

Answer these specific questions in 5-7 sentences total:

1. MAIN SUBJECT: What is the main subject? (a person, multiple people, an object, a landscape)
2. FACE SIZE: If a person is visible, estimate the face size as percentage of frame width (e.g., "face is about 8% wide" or "no visible face").
3. POSITION & DIRECTION: Where is the subject in the frame? (left/center/right) Which direction is the subject facing? (left/right/forward/back)
4. ACTION: What is the subject doing? (standing still, walking, holding object, sitting, etc.)
5. HANDS: Are hands visible? Are they holding or manipulating any object? Be specific.
6. BACKGROUND: What is in the background? (interior, landscape, sky, simple/complex)
7. COMPOSITION: Is the shot a closeup (face fills frame), medium (upper body), wide (whole scene), or extreme wide (tiny figures)?

Be factual and concise. Do NOT recommend anything. Do NOT output JSON. Just describe what you literally see."""


def _call_vision(model: str, prompt: str, img_bytes: bytes,
                  timeout: int = 120) -> str:
    img_b64 = base64.b64encode(img_bytes).decode()
    r = requests.post(
        f"{OLLAMA_BASE}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "images": [img_b64],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 400},
        },
        timeout=timeout,
        proxies=PROXIES,
    )
    r.raise_for_status()
    return r.json().get("response", "").strip()


# ══════════════════════════════════════════════════════════
# Stage 2：文本决策（v3.1 改进 prompt）
# ══════════════════════════════════════════════════════════

_STAGE2_PROMPT_TEMPLATE = """你是机械化的运镜决策器。严格按规则查表，不做主观发挥。

═══════════════════════════════════════
【画面描述】
{stage1_description}

【故事旁白（仅供参考，不影响 kb_direction 决策）】
{narration}
═══════════════════════════════════════

【任务】严格按下面 5 步执行。不要跳步，不要发挥。

━━━ Step 1：提取 facing_direction（主体朝向）━━━
扫描画面描述，找以下关键词：
  "facing left" / "looks to the left" / "left side" / "profile to the left"  →  left
  "facing right" / "looks to the right" / "right side" / "profile to the right"  →  right
  "facing forward" / "looks at camera" / "front view"  →  forward
  "back to camera" / "facing away"  →  back
  描述里完全无人物（"interior"、"landscape"、"empty"）  →  none
若主体在画面右半边但未说朝向，假定 facing left（视线投向中心）。
若主体在画面左半边但未说朝向，假定 facing right。

━━━ Step 2：提取 face_pct（脸占帧宽百分比）━━━
扫描以下关键词：
  "extreme wide" / "tiny figure" / "in the distance"  →  face_pct = 5
  "wide shot" / "middle distance" / "small figure"  →  face_pct = 10
  "medium shot" / "upper body" / "torso"  →  face_pct = 25
  "closeup" / "face fills" / "head and shoulders"  →  face_pct = 50
  无人物  →  face_pct = 0

━━━ Step 3：用决策表查 kb_direction（这一步是机械查表，不要思考"是否合适"）━━━

  ┌─────────────┬──────────────────────────┬──────────────┐
  │ face_pct    │ facing_direction         │ kb_direction │
  ├─────────────┼──────────────────────────┼──────────────┤
  │  ≥ 25       │ left                     │ pan_left     │
  │  ≥ 25       │ right                    │ pan_right    │
  │  ≥ 25       │ forward / back           │ zoom_out     │
  │  10 - 24    │ left                     │ pan_left     │
  │  10 - 24    │ right                    │ pan_right    │
  │  10 - 24    │ forward / back           │ zoom_in      │
  │  < 10       │ any                      │ zoom_in      │
  │  0 (无人)   │ 描述含 "tall" / 山 / 卷轴│ pan_up       │
  │  0 (无人)   │ 室内场景                 │ pan_right    │
  │  0 (无人)   │ 其他                     │ zoom_in      │
  └─────────────┴──────────────────────────┴──────────────┘

★ 这是查表，不是判断。不要因为"剧情重要"或"想强调"就改答案。
★ pan_left 和 pan_right 是首选答案，zoom_in 只是远景兜底。

━━━ Step 4：评 motion_score（0-10）━━━
  face_pct ≥ 25 单主体  →  8.0 ~ 9.0
  face_pct 10-24 单主体 →  6.5 ~ 7.5
  face_pct < 10         →  4.5 ~ 5.5
  描述里有 "two figures" / 多人 → 在上述基础上 -1.5
  描述里有 "holding" + 复杂物体 → -0.5

━━━ Step 5：写 suggested_prompt（英文，12-25 词）━━━
必须基于画面描述里出现的具体物件和细节。例：
  描述里有 "lantern" → "lantern light flickers softly"
  描述里有 "long hair" → "hair gently sways in the wind"
  描述里有 "robe" / "dress" → "robe ripples"
  描述里有 "smoke" / "incense" → "smoke drifts upward"
不要写 "slow head turn"、"breath rises" 这种空洞模板。

═══════════════════════════════════════
仅输出 JSON，不要任何前后文字、不要 markdown 包装：
{{
  "facing_direction": "<left|right|forward|back|none>",
  "face_pct": <数字>,
  "kb_direction": "<查表结果>",
  "motion_score": <0.0-10.0>,
  "face_size": "<small|medium|large|none>",
  "suggested_prompt": "<英文，针对画面物件，12-25 词>",
  "reason": "<中文，格式必须是: face_pct=X, facing=Y, 查表得 Z>"
}}"""


def _call_text(model: str, prompt: str, timeout: int = 90) -> str:
    r = requests.post(
        f"{OLLAMA_BASE}/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 600},
        },
        timeout=timeout,
        proxies=PROXIES,
    )
    r.raise_for_status()
    return r.json().get("response", "").strip()


# ══════════════════════════════════════════════════════════
# JSON 解析
# ══════════════════════════════════════════════════════════

def _parse_json_tolerant(text: str) -> Optional[dict]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', text)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    try:
        return json.loads(cleaned.strip())
    except Exception:
        pass
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        candidate = m.group()
        try:
            return json.loads(candidate)
        except Exception:
            pass
        fixed = candidate
        fixed = re.sub(r',\s*}', '}', fixed)
        fixed = re.sub(r',\s*]', ']', fixed)
        fixed = fixed.replace('“', '"').replace('”', '"')
        fixed = fixed.replace('，', ',').replace('：', ':')
        try:
            return json.loads(fixed)
        except Exception:
            pass
    return None


# ══════════════════════════════════════════════════════════
# 主类
# ══════════════════════════════════════════════════════════

class MotionEvaluator:
    """二段式视觉-文本评估管线 v3.1"""

    def __init__(self,
                 vision_model: str = "minicpm-v:8b",
                 text_model:   str = "qwen2.5:14b",
                 cache_dir:    Optional[str] = None,
                 timeout:      int  = 180):
        self.vision_model = vision_model
        self.text_model   = text_model
        self.cache_dir    = Path(cache_dir) if cache_dir else None
        self.timeout      = timeout

        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._stage1_cache = self._load_cache(_STAGE1_CACHE_FILE)
        self._stage2_cache = self._load_cache(_STAGE2_CACHE_FILE)

        print(f"  [motion-eval] 二段式管线 v3.1")
        print(f"  [motion-eval]   Stage 1: {vision_model}")
        print(f"  [motion-eval]   Stage 2: {text_model}")

    def _cache_path(self, filename: str) -> Optional[Path]:
        return self.cache_dir / filename if self.cache_dir else None

    def _load_cache(self, filename: str) -> dict:
        p = self._cache_path(filename)
        if p and p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_cache(self, filename: str, data: dict):
        p = self._cache_path(filename)
        if p:
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                          encoding="utf-8")

    # Stage 1
    def describe_image(self, image_path: str) -> Optional[str]:
        cache_key = _stage1_key(image_path, self.vision_model)
        if cache_key and cache_key in self._stage1_cache:
            return self._stage1_cache[cache_key]

        try:
            with open(image_path, "rb") as f:
                img_bytes = f.read()
        except Exception as e:
            print(f"  [stage1] 读图失败: {e}")
            return None

        t0 = time.time()
        try:
            description = _call_vision(self.vision_model, _STAGE1_PROMPT,
                                        img_bytes, timeout=self.timeout)
        except Exception as e:
            print(f"  [stage1] 调用失败: {e}")
            return None

        if not description:
            return None

        elapsed = time.time() - t0
        print(f"  [stage1] {Path(image_path).name} ({elapsed:.1f}s)")
        preview = description.split("\n")[0][:120]
        print(f"     描述预览: {preview}...")

        if cache_key:
            self._stage1_cache[cache_key] = description
            self._save_cache(_STAGE1_CACHE_FILE, self._stage1_cache)

        return description

    # Stage 2
    def decide(self, description: str, narration: str = "",
               motion_hint: str = "medium") -> Optional[dict]:
        prompt = _STAGE2_PROMPT_TEMPLATE.format(
            stage1_description=description,
            narration=narration[:300] if narration else "(无)",
            motion_hint=motion_hint,
        )

        t0 = time.time()
        try:
            raw = _call_text(self.text_model, prompt, timeout=self.timeout)
        except Exception as e:
            print(f"  [stage2] 调用失败: {e}")
            return None

        parsed = _parse_json_tolerant(raw)
        if not parsed:
            print(f"  [stage2] JSON 解析失败: {raw[:200]}")
            return None

        elapsed = time.time() - t0
        # 多打印几个调试字段，便于看 AI 推理是否合理
        facing = parsed.get('facing_direction', '?')
        face_pct = parsed.get('face_pct', '?')
        print(f"  [stage2] 决策 ({elapsed:.1f}s) "
              f"facing={facing:8s} face_pct={face_pct} "
              f"→ kb={parsed.get('kb_direction', '?'):10s} "
              f"score={parsed.get('motion_score', 0):.1f}")
        return parsed

    def evaluate(self, image_path: str,
                  narration: str = "",
                  characters: list = None,
                  motion_hint: str = "medium") -> Optional[dict]:
        if not Path(image_path).exists():
            return None

        s2_key = _stage2_key(image_path, narration,
                              self.vision_model, self.text_model)
        if s2_key and s2_key in self._stage2_cache:
            cached = self._stage2_cache[s2_key]
            print(f"  [motion-eval] {Path(image_path).name} (缓存) "
                  f"score={cached.get('motion_score', 0):.1f} "
                  f"kb={cached.get('kb_direction', '?')}")
            return cached

        description = self.describe_image(image_path)
        if not description:
            return None

        decision = self.decide(description, narration, motion_hint)
        if not decision:
            return None

        result = self._normalize_result(decision, description)

        if s2_key:
            self._stage2_cache[s2_key] = result
            self._save_cache(_STAGE2_CACHE_FILE, self._stage2_cache)

        return result

    @staticmethod
    def _normalize_result(decision: dict, description: str) -> dict:
        result = {
            "motion_score":     float(decision.get("motion_score", 5.0)),
            "kb_direction":     decision.get("kb_direction", "zoom_in"),
            "suggested_prompt": decision.get("suggested_prompt", "").strip(),
            "face_size":        decision.get("face_size", "medium"),
            "reason":           decision.get("reason", ""),
            # v3.1 新增调试字段
            "facing_direction": decision.get("facing_direction", "?"),
            "face_pct":         decision.get("face_pct", -1),
            "concerns":         [],
            "observed":         description[:400],
        }
        result["motion_score"] = max(0.0, min(10.0, result["motion_score"]))

        valid_kbs = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up")
        if result["kb_direction"] not in valid_kbs:
            result["kb_direction"] = "zoom_in"

        valid_faces = ("small", "medium", "large", "none")
        if result["face_size"] not in valid_faces:
            result["face_size"] = "medium"

        return result

    def warmup_vision(self):
        try:
            _call_vision(self.vision_model, "ping", b"iVBORw0KGgo=",
                          timeout=30)
            print(f"  [motion-eval] 视觉模型已预热: {self.vision_model}")
        except Exception:
            pass

    def warmup_text(self):
        try:
            _call_text(self.text_model, "ping", timeout=30)
            print(f"  [motion-eval] 文本模型已预热: {self.text_model}")
        except Exception:
            pass

    def free_vision(self):
        try:
            requests.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": self.vision_model, "keep_alive": 0},
                timeout=10, proxies=PROXIES,
            )
            print(f"  [motion-eval] 已卸载视觉模型: {self.vision_model}")
        except Exception:
            pass

    def free_text(self):
        try:
            requests.post(
                f"{OLLAMA_BASE}/api/generate",
                json={"model": self.text_model, "keep_alive": 0},
                timeout=10, proxies=PROXIES,
            )
            print(f"  [motion-eval] 已卸载文本模型: {self.text_model}")
        except Exception:
            pass

    def free_all(self):
        self.free_vision()
        self.free_text()


# ══════════════════════════════════════════════════════════
# 批量接口
# ══════════════════════════════════════════════════════════

def evaluate_all_pages(story, registry,
                        vision_model: str = "minicpm-v:8b",
                        text_model:   str = "qwen2.5:14b",
                        cache_dir:    Optional[str] = None) -> dict:
    approved = registry.all_approved(story.story_id)
    if not approved:
        print("  [motion-eval] 无 APPROVED 资产")
        return {}

    evaluator = MotionEvaluator(
        vision_model=vision_model,
        text_model=text_model,
        cache_dir=cache_dir,
    )

    print(f"\n  ── 二段式 AI 评估 ({len(approved)} 页) ──")

    # Phase A：批量 Stage 1
    print(f"\n  ── Phase A: 视觉描述 ({vision_model}) ──")
    evaluator.warmup_vision()

    descriptions: dict = {}
    for asset in approved:
        page_cfg = story.get_page(asset.page_num)
        if not page_cfg:
            continue
        desc = evaluator.describe_image(asset.path)
        if desc:
            descriptions[asset.page_num] = desc

    print(f"  Phase A 完成: {len(descriptions)}/{len(approved)} 描述成功")

    evaluator.free_vision()
    print(f"  视觉模型已卸载（释放显存给 Phase B）")
    time.sleep(2)

    # Phase B：批量 Stage 2
    print(f"\n  ── Phase B: 文本决策 ({text_model}) ──")
    evaluator.warmup_text()

    results: dict = {}
    for asset in approved:
        page_cfg = story.get_page(asset.page_num)
        if not page_cfg:
            continue

        s2_key = _stage2_key(asset.path,
                              page_cfg.get("narration", ""),
                              vision_model, text_model)
        if s2_key and s2_key in evaluator._stage2_cache:
            results[asset.page_num] = evaluator._stage2_cache[s2_key]
            print(f"  [motion-eval] p{asset.page_num} (缓存) "
                  f"score={results[asset.page_num].get('motion_score', 0):.1f}")
            continue

        description = descriptions.get(asset.page_num)
        if not description:
            print(f"  [motion-eval] p{asset.page_num} 无描述，跳过")
            continue

        decision = evaluator.decide(
            description=description,
            narration=page_cfg.get("narration", ""),
            motion_hint=page_cfg.get("motion_hint", "medium"),
        )
        if decision:
            result = evaluator._normalize_result(decision, description)
            results[asset.page_num] = result
            if s2_key:
                evaluator._stage2_cache[s2_key] = result
                evaluator._save_cache(_STAGE2_CACHE_FILE,
                                       evaluator._stage2_cache)

    print(f"  Phase B 完成: {len(results)}/{len(approved)} 决策成功")
    evaluator.free_text()

    print(f"\n  ── 二段式评估完成: {len(results)}/{len(approved)} 页 ──\n")
    return results


def list_local_vision_models() -> list:
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags",
                          timeout=10, proxies=PROXIES)
        if r.status_code != 200:
            return []
        models = r.json().get("models", [])
        VISION_HINTS = ("vl", "llava", "moondream", "minicpm", "gemma3", "clip")
        vision = []
        for m in models:
            name = m.get("name", "").lower()
            families = str(m.get("details", {}).get("families", "")).lower()
            if any(h in name for h in VISION_HINTS) or "clip" in families:
                vision.append(m["name"])
        return sorted(vision)
    except Exception:
        return []
