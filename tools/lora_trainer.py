"""
lora_trainer.py ── FLUX LoRA 自动化训练管线（v3）
======================================================
v3 新增：
  - 逐张审核改为 getch 单键响应（不依赖窗口焦点，无需点回 PyCharm）
  - 删除时支持原因码：g/f/h/p/q（gender/face/hand/pose/quality）
  - 选图结束后自动做 pose 缺口分析 + 定向补生
  - 数据集版本管理：selections/roundN.json + dataset_vN/
  - 多轮衔接策略：合并 / 替换 / 重选 / 沿用
  - 后悔机制：--reselect round1 重新审核任意一轮

三处复用项目现有能力：
  1. renderer.generate_and_wait + comfy_generate_flux  → 生成训练图
  2. quality.LlavaGate._call_vision                    → caption 生成
  3. quality.LlavaGate.evaluate（solo_distant 模板）   → 训练数据评分筛选

文件同步：
  Mac 生成数据集 → SFTP 推送到 Windows C:/flux_train/ (SSD)
  → sd-scripts 读取训练（所有 IO 密集操作走 SSD）

用法：
  python lora_trainer.py --story stories/haunted_inn.yaml --char innkeeper_ghost
  python lora_trainer.py ... --skip_gen
  python lora_trainer.py ... --skip_train
  python lora_trainer.py ... --reselect round1   # 重新审核第一轮（后悔机制）
  python lora_trainer.py ... --round 2           # 指定轮次（默认自动推断）


lora_trainer.py 断点重训补丁
=============================
改动三处函数 + main() 新增一个参数。全量替换对应函数即可。

改动1：write_train_config()   → toml 新增 save_state / save_last_n_steps_state
改动2：create_train_batch_file() → bat 改为自动检测 state 并注入 resume，支持 --fresh 强制从头
改动3：ssh_run_training_detached() → 签名新增 win_output_dir / fresh_start 参数
改动4：main() → 新增 --fresh 参数，透传给 ssh_run_training_detached

用法：
  # 正常启动（自动检测断点，有 state 就续训）
  python lora_trainer.py --story stories/haunted_inn.yaml --char scholar

  # 强制从头重训（忽略已有 state）
  python lora_trainer.py --story stories/haunted_inn.yaml --char scholar --fresh
# ════════════════════════════════════════════════════════════════
# 用法示例（不需要写入文件，仅供参考）
# ════════════════════════════════════════════════════════════════
#
# 正常启动（自动检测断点）：
#   python lora_trainer.py --story stories/haunted_inn.yaml --char scholar
#
# 断电/中断后续训（无需任何额外操作，自动检测 state）：
#   python lora_trainer.py --story stories/haunted_inn.yaml --char scholar
#
# 强制从头重训（数据集改了，不想续之前的）：
#   python lora_trainer.py --story stories/haunted_inn.yaml --char scholar --fresh
#
# state 目录在 Windows 上的位置：
#   C:\flux_train\workspace\{story}\{char}\output_vN\{char}_flux_lora-state-{step}\
#   → save_last_n_steps_state=1 保证只有一个，不会堆积

state 目录在 Windows 上的位置：
  C:\\flux_train\\workspace\\{story}\\{char}\\output_vN\\{output_name}-step{NNNNNNNN}-state\\
  → save_last_n_steps_state=1 保证只有一个，不会堆积
  → 同一轮训练里，每次 session 使用时间戳 output_name，产物物理隔离
     例: scholar_flux_lora_r20260422_1408-step00000300-state

关于步数语义（重要）：
  kohya sd-scripts 的 max_train_steps 是"本次 session 的步数上限"
  每次续练重启时 global_step 从 0 重新数，不是累积
  所以多次中断后，累积训练步数 = 各 session 步数之和
  最终 LoRA 选取：根据需要的"每图看过次数"挑选合适的 checkpoint
"""


import argparse
import json
import random
import shutil
import sys
import tempfile

import termios
import time
import tty
import stat
from pathlib import Path

import paramiko
import subprocess as _subprocess
import yaml
from PIL import Image as PILImage
from renderer import generate_and_wait, comfy_generate_flux
from comfy_script.runtime import load

from config import COMFY_SERVER, OLLAMA_BASE, PROXIES
load(COMFY_SERVER)

# ── SSH / SFTP 配置 ────────────────────────────────────────────
SSH_HOST = "YOUR_SSH_HOST"  # set via env/config
SSH_PORT = 6000
SSH_USER = "administrator"
SSH_PASS = "170329"

# ── Windows 路径 ──────────────────────────────────────────────
WIN_COMFY_BASE  = "H:/ComfyUI/ComfyUI_windows_portable/ComfyUI/models"
WIN_SD_SCRIPTS  = "H:/sd-scripts"
WIN_SD_VENV_PYTHON = "H:/sd-scripts/venv/Scripts/python.exe"
WIN_LORA_OUTPUT = f"{WIN_COMFY_BASE}/loras/flux"
WIN_TRAIN_BASE  = "C:/flux_train"
WIN_WORKSPACE   = f"{WIN_TRAIN_BASE}/workspace"
WIN_MODEL_DIR   = f"{WIN_TRAIN_BASE}/models"

# ── 训练用模型 ────────────────────────────────────────────────
FLUX_FP8_MODEL = f"{WIN_MODEL_DIR}/flux1-dev-fp8-e4m3fn.safetensors"
CLIP_L         = f"{WIN_MODEL_DIR}/clip_l.safetensors"
T5_FP8         = f"{WIN_MODEL_DIR}/t5xxl_fp8_e4m3fn.safetensors"
VAE            = f"{WIN_MODEL_DIR}/ae.safetensors"

SCORE_THRESHOLD = 7.0

# ── 原因码说明 ────────────────────────────────────────────────
REASON_LABELS = {
    "g": "性别错误",
    "f": "脸部问题",
    "h": "手部变形",
    "p": "姿态不自然",
    "q": "整体画质差",
}

# ── pose 分类（用于缺口分析）─────────────────────────────────
# 0-19 索引对应 make_training_prompts 里的 poses 列表
_FRONT_POSES  = {0, 1, 4, 6, 7, 10, 11, 12, 16, 17}   # 可见脸
_BACK_POSES   = {3, 9, 13}                              # 背面/侧背
_OTHER_POSES  = {2, 5, 8, 14, 15, 18, 19}              # 侧面/动作


def _pose_type(pose_index: int) -> str:
    idx = pose_index % 20
    if idx in _FRONT_POSES: return "正脸"
    if idx in _BACK_POSES:  return "背面"
    return "其他"


# ════════════════════════════════════════════════════════════════
# SSH 连接工厂
# ════════════════════════════════════════════════════════════════

def _make_ssh() -> paramiko.SSHClient:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(SSH_HOST, port=SSH_PORT,
                username=SSH_USER, password=SSH_PASS,
                timeout=30)
    return ssh


# ════════════════════════════════════════════════════════════════
# getch：单键读取，不依赖窗口焦点
# ════════════════════════════════════════════════════════════════

def _getch() -> str:
    """
    从 tty 读取单个字符，不需要回车，不需要焦点在终端窗口。
    PyCharm 内置终端若不支持 raw mode，自动降级为 input() 读一行。
    """
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch
    except (termios.error, AttributeError):
        # PyCharm 内置终端 fallback
        line = input().strip().lower()
        return line[0] if line else "\r"


# ════════════════════════════════════════════════════════════════
# Step 1：生成多样化训练图
# ════════════════════════════════════════════════════════════════

def _infer_gender(char_cfg: dict) -> str:
    desc = " ".join([
        char_cfg.get("desc", ""),
        char_cfg.get("trigger_solo", ""),
        char_cfg.get("key_features", ""),
    ]).lower()
    male_kw = ["书生", "男", "道士", "将军", "boy", "male", "man", "scholar",
               "priest", "warrior", "taoist", "monk", "prince", "his ", "he "]
    female_kw = ["女", "美女", "女子", "鬼女", "girl", "female", "woman",
                 "beauty", "ghost woman", "maiden", "lady", "her ", "she "]
    m = sum(1 for k in male_kw if k in desc)
    f = sum(1 for k in female_kw if k in desc)
    if m > f: return "male"
    if f > m: return "female"
    return ""


def _image_type_for_pose(pose_index: int) -> str:
    return "solo_character" if (pose_index % 20) in _FRONT_POSES else "solo_distant"


# poses / lights 列表（提取为模块级，补生时可复用）
_POSES = [
    "standing upright, full body, centered in frame",
    "turning slightly to the side, three-quarter view",
    "side profile, facing left",
    "back view, facing away from camera",
    "sitting on stone steps",
    "walking forward, mid-stride, full body",
    "arms slightly raised, dramatic pose",
    "leaning against an ancient wooden pillar",
    "looking downward, contemplative",
    "standing in doorway, silhouetted",
    "hands clasped together, formal pose",
    "head tilted slightly, neutral expression",
    "facing camera directly, neutral standing pose",
    "turning away, glancing over shoulder",
    "stepping forward, dynamic movement",
    "arms at sides, relaxed stance, wide shot",
    "reaching forward with one hand, dramatic",
    "kneeling on ground",
    "standing tall, proud posture, low angle",
    "crouching slightly, looking to the side",
]
_LIGHTS = [
    "cold blue moonlight, night atmosphere",
    "warm orange candlelight glow",
    "misty fog, soft diffused light",
    "dramatic side lighting, deep shadows",
    "backlit silhouette, rim light effect",
    "pale dawn light, early morning mist",
    "eerie ghost fire blue illumination",
    "dim interior candlelight, warm shadows",
    "harsh shadow contrast, midnight scene",
    "soft overcast daylight, even lighting",
    "red paper lantern light, warm amber",
    "cold winter light, blue grey tones",
    "dramatic spotlight from above",
    "twilight sky, purple and orange",
    "firelight, warm dancing shadows",
    "silver moonlit courtyard",
    "rainy night, reflective wet stone",
    "torchlight, flickering orange glow",
    "golden sunrise light",
    "cloudy diffused flat lighting",
]


def make_training_prompts(char_cfg: dict, style_prefix: str,
                          n: int = 30,
                          pose_indices: list[int] | None = None,
                          extra_gender_boost: bool = False) -> list[str]:
    """
    v3：支持指定 pose_indices（定向补生用）。
    pose_indices=None 时循环使用全部 20 个 pose。
    extra_gender_boost=True 时加强性别词（用于 gender 原因补生）。
    """
    base_desc = (
        char_cfg.get("trigger_solo") or
        char_cfg.get("trigger_multi") or
        char_cfg.get("desc", "")
    )
    gender = _infer_gender(char_cfg)
    gp = ""
    if gender == "male":
        gp = "1boy, male, masculine features, "
        if extra_gender_boost:
            gp = "1boy, male, masculine jawline, strong male features, no feminine traits, "
    elif gender == "female":
        gp = "1girl, female, feminine features, "
        if extra_gender_boost:
            gp = "1girl, female, feminine face, delicate features, no masculine traits, "
    if gp:
        print(f"  [性别] 检测: {gender}{'（加强）' if extra_gender_boost else ''}，注入性别词")

    if pose_indices:
        # 定向补生：在指定的 pose 里循环
        selected = [_POSES[i % len(_POSES)] for i in pose_indices * ((n // len(pose_indices)) + 2)]
        selected = selected[:n]
    else:
        selected = [_POSES[i % len(_POSES)] for i in range(n)]

    return [
        f"{style_prefix}{gp}{base_desc}, {selected[i]}, "
        f"{_LIGHTS[i % len(_LIGHTS)]}, ancient chinese setting, high quality"
        for i in range(n)
    ]


def generate_training_images(prompts: list[str], theme,
                             save_dir: Path,
                             negative: str = "",
                             cfg_override: float | None = None,
                             steps_override: int | None = None) -> list[Path]:
    """复用 renderer 生图，支持参数覆盖（用于补生时针对性调参）。"""
    from renderer import generate_and_wait, comfy_generate_flux
    from gpu_guard import gpu_guard

    save_dir.mkdir(parents=True, exist_ok=True)
    saved = []

    for i, prompt in enumerate(prompts):
        print(f"  [DataGen] {i+1}/{len(prompts)}: {prompt[:70]}...")
        params = {
            "positive":        prompt,
            "negative":        negative or "modern, western, low quality, blurry",
            "lora":            "",
            "lora_strength":   0.0,
            "lora2":           None,
            "lora2_strength":  0.0,
            "cfg":             cfg_override or 3.5,
            "steps":           steps_override or 20,
            "sampler":         "euler",
            "seed":            random.randint(10000, 9999999),
            "prefix":          f"train_{i+1:03d}",
            "upscale_denoise": 0.42,
            "ip_ref":          None,
            "face_ref":        None,
            "_faceid_model":   "",
            "_faceid_vision":  "",
            "_checkpoint":     theme.model.checkpoint,
            "_ipadapter":      theme.model.ipadapter,
            "_clip_vision":    theme.model.clip_vision,
            "_unet":           getattr(theme.model, "unet",  "") or "",
            "_clip1":          getattr(theme.model, "clip1", "") or "",
            "_clip2":          getattr(theme.model, "clip2", "") or "",
            "_vae":            getattr(theme.model, "vae",   "") or "",
            "depth_ref":       None,
            "pose_action":     None,
        }
        gpu_guard.wait_if_needed(f"训练图 {i+1}")
        files = generate_and_wait(
            comfy_generate_flux, params,
            f"train_{i+1:03d}", str(save_dir))
        if files:
            saved.append(Path(files[-1]))
            print(f"    ✓ {Path(files[-1]).name}")
        else:
            print(f"    ✗ 失败，跳过")
        time.sleep(3)

    print(f"  [GPU] 释放 ComfyUI 显存...")
    gpu_guard.free_comfyui_vram()
    time.sleep(8)
    print(f"  [DataGen] 共生成 {len(saved)}/{len(prompts)} 张")
    return saved


# ════════════════════════════════════════════════════════════════
# Step 2：自动评分筛选
# ════════════════════════════════════════════════════════════════

def auto_screen_images(image_files, vision_model: str,
                       review_focus: str,
                       expected_gender: str = "") -> tuple:
    """
    v3：使用 lora_quality.score_train_batch 替代 quality.LlavaGate。

    trainer 专用评分：
      - 关注服饰准确性和人物整体质量（训练数据的核心要求）
      - 手部不参与评分
      - 面部略模糊不拒绝（训练数据需要多样性）
      - 性别错误是唯一硬否决
    """
    from lora_quality import score_train_batch

    print(f"  [自动筛选] 使用 LoRA 专用评分（服饰/人物质量/风格，无手部否决）")
    passed, rejected = score_train_batch(
        image_paths=image_files,
        vision_model=vision_model,
        expected_gender=expected_gender,
    )
    return passed, rejected


# ════════════════════════════════════════════════════════════════
# Step 3：人工审核（v3：getch + 原因码 + 缺口分析 + 定向补生）
# ════════════════════════════════════════════════════════════════

def _phase_a_annotate(image_files, vision_model: str,
                      review_focus: str, char_cfg: dict) -> list[dict]:
    """
    Phase A：全量评分 + 性别标注。v3 改用 lora_quality.TrainerScorer。

    变化：
      - 评分维度改为服饰准确性 / 人物质量 / 风格匹配
      - 手部不再参与评分和否决
      - 性别检查集成在 TrainerScorer 内部
      - status 判断：ok=False → WARN，ok=True → OK
    """
    from pathlib import Path
    from lora_quality import TrainerScorer

    expected_gender = _infer_gender(char_cfg)
    scorer = TrainerScorer(
        vision_model=vision_model,
        expected_gender=expected_gender,
    )
    scorer.warmup()

    annotations = []
    for i, img_path in enumerate(image_files):
        img_type = _image_type_for_pose(i)
        pose_type = _pose_type(i)

        result = scorer.score(img_path)

        # 构造和原来兼容的 annotation 字典
        status = "OK" if result.ok else "WARN"
        gender_note = "" if result.gender_ok else f"性别错误"

        annotations.append({
            "index": i,
            "path": Path(img_path),
            "score": result.score,
            "feedback": result.reason,
            "gender_ok": result.gender_ok,
            "gender_note": gender_note,
            "status": status,
            "image_type": img_type,
            "face_score": -1.0,  # 训练评分不单独跟踪 face_score
            "pose_type": pose_type,
            "pose_index": i,
        })

        mk = "v" if status == "OK" else "!"
        gs = f"  {gender_note}" if gender_note else ""
        print(f"  {mk} [{i + 1:02d}/{len(image_files)}] {Path(img_path).name}  "
              f"{result.score:.1f}/10 ({img_type})  "
              f"{'✓ 入选' if result.ok else f'✗ {result.reason}'}{gs}")

    scorer.free_model()
    return annotations

def _phase_b_review(annotations: list[dict],
                    existing_kept: list[dict] | None = None) -> list[dict]:
    """
    Phase B：逐张弹图 + getch 单键决策。
    existing_kept：重新审核时已有的保留记录（标绿显示）。
    返回：每张图的决策记录列表 {ann, kept, reason}
    """
    print(f"\n{'='*58}")
    print(f"  人工审核：{len(annotations)} 张图")
    print(f"  按键：回车/y=保留  d=删除（无原因）")
    print(f"        g=删+性别错  f=删+脸部  h=删+手部  p=删+姿态  q=删+画质")
    print(f"        a=后续全保留  z=后续全删除  ←=退回上一张")
    print(f"{'='*58}\n")

    decisions = []  # [{ann, kept, reason}]
    i = 0
    total = len(annotations)

    while i < total:
        ann = annotations[i]
        ip = ann["path"]

        # 构造显示行
        mk = "OK  " if ann["status"] == "OK" else "WARN"
        fs = f" face={ann['face_score']:.1f}" if ann["face_score"] >= 0 else ""
        gs = f"  {ann['gender_note']}" if ann["gender_note"] else ""
        prev = next((d for d in decisions if d["ann"] is ann), None)
        prev_mark = f"  [上次: {'保留' if prev and prev['kept'] else '删除'}]" if prev else ""

        print(f"  [{mk}] [{i+1:02d}/{total}] {ip.name}  "
              f"{ann['score']:.1f}/10  ({ann['pose_type']}){fs}{gs}{prev_mark}")

        # 弹出图片
        try:
            _subprocess.Popen(["open", str(ip)])
        except Exception:
            pass

        print(f"  按键决定 > ", end="", flush=True)
        ch = _getch()
        print(ch if ch.isprintable() else "⏎")  # 回显

        # ── 解析按键 ──────────────────────────────────────────
        if ch in ("\r", "\n", "y", ""):
            # 保留
            decision = {"ann": ann, "kept": True, "reason": ""}
            print(f"    ✓ 保留")

        elif ch in ("d",):
            decision = {"ann": ann, "kept": False, "reason": ""}
            print(f"    ✗ 删除")

        elif ch in REASON_LABELS:
            label = REASON_LABELS[ch]
            decision = {"ann": ann, "kept": False, "reason": ch}
            print(f"    ✗ 删除  原因: {label}")

        elif ch == "a":
            # 后续全部保留
            print(f"    → 后续 {total - i} 张全部保留")
            for j in range(i, total):
                decisions.append({"ann": annotations[j], "kept": True, "reason": ""})
            break

        elif ch == "z":
            # 后续全部删除
            print(f"    → 后续 {total - i} 张全部删除")
            for j in range(i, total):
                decisions.append({"ann": annotations[j], "kept": False, "reason": "z"})
            break

        elif ch in ("\x1b", "\x7f") or ch == "<":
            # 退回上一张（方向键左 / ESC / backspace 均视为退回）
            if i > 0:
                # 移除上一张的决策重新来
                if decisions and decisions[-1]["ann"] is annotations[i - 1]:
                    decisions.pop()
                i -= 1
                print(f"    ← 退回第 {i+1} 张")
            else:
                print(f"    已是第一张，无法退回")
            continue

        else:
            # 未识别按键，默认保留
            decision = {"ann": ann, "kept": True, "reason": ""}
            print(f"    ? 未识别键 '{ch}'，默认保留")

        # 覆盖同张图的旧决策
        decisions = [d for d in decisions if d["ann"] is not ann]
        decisions.append(decision)
        i += 1

    kept_count = sum(1 for d in decisions if d["kept"])
    print(f"\n  审核完成：保留 {kept_count}/{total} 张")
    return decisions


def _analyze_gaps(decisions: list[dict],
                  target_per_type: int = 8) -> dict:
    """
    分析 pose 缺口，返回缺口信息。
    target_per_type：每种 pose 类型的目标数量。
    """
    kept = [d for d in decisions if d["kept"]]
    deleted = [d for d in decisions if not d["kept"]]

    # 统计保留数量
    type_counts = {"正脸": 0, "背面": 0, "其他": 0}
    for d in kept:
        pt = d["ann"]["pose_type"]
        type_counts[pt] = type_counts.get(pt, 0) + 1

    # 按原因统计删除
    reason_counts = {}
    for d in deleted:
        r = d["reason"] or "无"
        reason_counts[r] = reason_counts.get(r, 0) + 1

    # 识别缺口
    gaps = {}
    for pt, count in type_counts.items():
        deficit = target_per_type - count
        if deficit > 0:
            # 找出因为什么原因删了这类 pose
            deleted_of_type = [
                d for d in deleted
                if d["ann"]["pose_type"] == pt
            ]
            reasons = [d["reason"] for d in deleted_of_type if d["reason"]]
            gaps[pt] = {
                "current": count,
                "target": target_per_type,
                "deficit": deficit,
                "deleted_poses": [d["ann"]["pose_index"] for d in deleted_of_type],
                "main_reason": max(set(reasons), key=reasons.count) if reasons else "",
            }

    return {
        "type_counts": type_counts,
        "reason_counts": reason_counts,
        "gaps": gaps,
        "total_kept": len(kept),
        "total_deleted": len(deleted),
    }


def _print_gap_report(gap_info: dict):
    print(f"\n  {'─'*50}")
    print(f"  缺口分析  保留={gap_info['total_kept']}  删除={gap_info['total_deleted']}")
    print(f"  {'─'*50}")
    for pt, cnt in gap_info["type_counts"].items():
        gaps = gap_info["gaps"]
        if pt in gaps:
            g = gaps[pt]
            reason_hint = f"  主因: {REASON_LABELS.get(g['main_reason'], g['main_reason'])}" if g["main_reason"] else ""
            print(f"    {pt:4s}  {cnt:2d} 张  缺 {g['deficit']} 张{reason_hint}")
        else:
            print(f"    {pt:4s}  {cnt:2d} 张  ✓")
    if gap_info["reason_counts"]:
        reasons_str = "  ".join(
            f"{REASON_LABELS.get(r, r)}×{c}"
            for r, c in gap_info["reason_counts"].items() if r not in ("", "z")
        )
        if reasons_str:
            print(f"  删除原因: {reasons_str}")
    print(f"  {'─'*50}")


def _targeted_regen(gap_info: dict,
                    char_cfg: dict,
                    theme,
                    save_dir: Path,
                    vision_model: str,
                    review_focus: str) -> list[dict]:
    """
    针对缺口定向补生，只审核新生图。
    返回补生图的 decisions 列表（已经过审核）。
    """
    gaps = gap_info.get("gaps", {})
    if not gaps:
        print(f"  [补生] 无缺口，跳过")
        return []

    # ── 确认补生 ──────────────────────────────────────────────
    total_deficit = sum(g["deficit"] for g in gaps.values())
    print(f"\n  建议补生 {total_deficit} 张（", end="")
    parts = [f"{pt}{g['deficit']}张" for pt, g in gaps.items()]
    print("、".join(parts) + "）")
    print(f"  补生？(y=补生  n=跳过) > ", end="", flush=True)
    ch = _getch()
    print(ch if ch.isprintable() else "⏎")
    if ch not in ("y", "Y", "\r", "\n"):
        print(f"  [补生] 跳过")
        return []

    new_decisions = []

    for pt, g in gaps.items():
        deficit = g["deficit"]
        main_reason = g["main_reason"]
        deleted_pose_indices = g["deleted_poses"]

        print(f"\n  [补生] {pt}  需要 {deficit} 张  主因: {REASON_LABELS.get(main_reason, '无')}")

        # 根据原因调整生成参数
        extra_gender_boost = (main_reason == "g")
        cfg_override   = 2.8 if main_reason == "f" else None   # 脸部问题降 CFG
        steps_override = 25  if main_reason == "f" else None   # 脸部问题增 steps

        # 定向 pose：从被删图的 pose 里选
        pose_indices = deleted_pose_indices[:deficit] if deleted_pose_indices else None

        prompts = make_training_prompts(
            char_cfg,
            theme.prompts.style_prefix or "",
            n=deficit + 2,   # 多生 2 张备用
            pose_indices=pose_indices,
            extra_gender_boost=extra_gender_boost,
        )

        new_imgs = generate_training_images(
            prompts, theme, save_dir,
            negative=theme.prompts.negative_prefix or "",
            cfg_override=cfg_override,
            steps_override=steps_override,
        )

        if not new_imgs:
            print(f"  !! 补生失败，跳过")
            continue

        # 只审核新图（增量审核）
        print(f"\n  [增量审核] 审核 {len(new_imgs)} 张补生图")
        new_anns = _phase_a_annotate(
            new_imgs, vision_model, review_focus, char_cfg)
        regen_decisions = _phase_b_review(new_anns)
        new_decisions.extend(regen_decisions)

    return new_decisions


# ════════════════════════════════════════════════════════════════
# 选图记录（selections/）
# ════════════════════════════════════════════════════════════════

def _save_selection(workspace: Path, round_num: int,
                    decisions: list[dict], gap_info: dict):
    """把本轮选图结果持久化到 selections/roundN.json。"""
    sel_dir = workspace / "selections"
    sel_dir.mkdir(exist_ok=True)

    record = {
        "round": round_num,
        "kept":    [str(d["ann"]["path"]) for d in decisions if d["kept"]],
        "deleted": {str(d["ann"]["path"]): d["reason"]
                    for d in decisions if not d["kept"]},
        "gap_info": {
            k: v for k, v in gap_info.items()
            if k != "gaps"  # gaps 里有 Path 对象，单独处理
        },
    }
    path = sel_dir / f"round{round_num}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"  [选图记录] 已保存 → {path}")
    return path


def _load_selection(workspace: Path, round_num: int) -> dict | None:
    path = workspace / "selections" / f"round{round_num}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_round(workspace: Path) -> int:
    """返回已有的最大轮次号，无则返回 0。"""
    sel_dir = workspace / "selections"
    if not sel_dir.exists():
        return 0
    nums = []
    for p in sel_dir.glob("round*.json"):
        try:
            nums.append(int(p.stem.replace("round", "")))
        except ValueError:
            pass
    return max(nums) if nums else 0


# ════════════════════════════════════════════════════════════════
# 数据集版本组装
# ════════════════════════════════════════════════════════════════

def _assemble_dataset(workspace: Path,
                      kept_paths: list[Path],
                      round_num: int) -> Path:
    """
    把 kept_paths 里的图复制到 dataset_vN/，保持原始图在 images/ 不动。
    返回 dataset_vN 目录。
    """
    ds_dir = workspace / f"dataset_v{round_num}"
    if ds_dir.exists():
        shutil.rmtree(ds_dir)
    ds_dir.mkdir(parents=True)

    for p in kept_paths:
        dst = ds_dir / p.name
        shutil.copy2(p, dst)

    print(f"  [Dataset] 已组装 dataset_v{round_num}  ({len(kept_paths)} 张)  → {ds_dir}")
    return ds_dir


def _choose_merge_strategy(workspace: Path,
                           round_num: int,
                           new_kept: list[Path]) -> list[Path]:
    """
    多轮衔接：让用户选择数据集组装策略。
    仅在 round_num > 1 时调用。
    """
    prev_sel = _load_selection(workspace, round_num - 1)
    if not prev_sel:
        print(f"  [衔接] 未找到第 {round_num-1} 轮记录，直接用本轮数据")
        return new_kept

    prev_kept = [Path(p) for p in prev_sel["kept"] if Path(p).exists()]
    print(f"\n  {'='*55}")
    print(f"  多轮数据衔接  第 {round_num} 轮")
    print(f"  第 {round_num-1} 轮保留: {len(prev_kept)} 张")
    print(f"  本轮新增:       {len(new_kept)} 张")
    print(f"  {'─'*55}")
    print(f"  1 = 合并   第{round_num-1}轮({len(prev_kept)}) + 本轮({len(new_kept)}) ← 推荐")
    print(f"  2 = 替换   只用本轮数据，丢弃第{round_num-1}轮")
    print(f"  3 = 重选   打开 images/ 全量重新挑（可恢复以前删除的图）")
    print(f"  4 = 沿用   不改动第{round_num-1}轮数据集（调参对比用）")
    print(f"  选择 (1/2/3/4) > ", end="", flush=True)

    ch = _getch()
    print(ch)

    if ch == "2":
        print(f"  → 替换：仅使用本轮 {len(new_kept)} 张")
        return new_kept

    elif ch == "3":
        print(f"  → 重选：进入全量重审模式")
        return None  # 调用者收到 None 时触发全量重审

    elif ch == "4":
        print(f"  → 沿用：使用第 {round_num-1} 轮数据集")
        return prev_kept

    else:  # 默认 1：合并
        merged = list({str(p): p for p in prev_kept + new_kept}.values())
        print(f"  → 合并：{len(prev_kept)} + {len(new_kept)} = {len(merged)} 张")
        return merged


# ════════════════════════════════════════════════════════════════
# 主审核入口（v3）
# ════════════════════════════════════════════════════════════════

def human_review_with_annotations(image_files: list[Path],
                                  vision_model: str,
                                  review_focus: str,
                                  char_cfg: dict,
                                  workspace: Path,
                                  round_num: int,
                                  theme=None,
                                  reselect_round: int | None = None) -> list[Path]:
    """
    v3 完整审核流程：
      Phase A → 全量评分标注
      Phase B → getch 逐张决策（带原因码）
      Gap    → 缺口分析 + 定向补生（可选）
      Save   → 持久化到 selections/roundN.json
      Merge  → 多轮衔接策略选择
    reselect_round：重审指定轮次（后悔机制）。
    """
    # ── 后悔模式：加载旧记录，高亮显示 ───────────────────────
    existing_kept = None
    if reselect_round is not None:
        old = _load_selection(workspace, reselect_round)
        if old:
            existing_kept = old.get("kept", [])
            print(f"  [重审] 加载第 {reselect_round} 轮记录，已删图将标注可恢复")
        round_num = reselect_round

    # ── Phase A：评分标注 ─────────────────────────────────────
    print(f"\n[Phase A] 全量评分标注（第 {round_num} 轮）...")
    annotations = _phase_a_annotate(image_files, vision_model, review_focus, char_cfg)

    # 如果是重审模式，把旧的删除记录标注到 annotations 上
    if existing_kept is not None:
        for ann in annotations:
            ann["_was_kept"] = str(ann["path"]) in existing_kept

    # ── Phase B：逐张人工决策 ─────────────────────────────────
    decisions = _phase_b_review(annotations, existing_kept)

    # ── 缺口分析 ──────────────────────────────────────────────
    gap_info = _analyze_gaps(decisions)
    _print_gap_report(gap_info)

    # ── 定向补生 ──────────────────────────────────────────────
    regen_decisions = []
    if theme is not None and gap_info["gaps"]:
        regen_decisions = _targeted_regen(
            gap_info, char_cfg, theme,
            save_dir=workspace / "images",
            vision_model=vision_model,
            review_focus=review_focus,
        )
        decisions.extend(regen_decisions)

    # ── 持久化选图记录 ────────────────────────────────────────
    _save_selection(workspace, round_num, decisions, gap_info)

    # ── 汇总本轮保留图 ────────────────────────────────────────
    new_kept = [d["ann"]["path"] for d in decisions if d["kept"]]

    # ── 多轮衔接（第 2 轮起） ─────────────────────────────────
    if round_num > 1:
        final_kept = _choose_merge_strategy(workspace, round_num, new_kept)
        if final_kept is None:
            # 用户选了"重选"：对 images/ 所有图重新走一遍审核
            print(f"\n  [重选] 加载 images/ 全部图重新审核...")
            all_imgs = sorted((workspace / "images").glob("*.png"))
            return human_review_with_annotations(
                all_imgs, vision_model, review_focus,
                char_cfg, workspace, round_num, theme,
            )
    else:
        final_kept = new_kept

    print(f"\n  最终训练集（第 {round_num} 轮）: {len(final_kept)} 张")
    return final_kept


def auto_review_with_gender(image_files: list[Path],
                            vision_model: str,
                            review_focus: str,
                            char_cfg: dict,
                            theme,
                            target_count: int = 15) -> list[Path]:
    """全自动审核（--review auto），不变。"""
    from quality import LlavaGate, QualityContext

    gate = LlavaGate(threshold=SCORE_THRESHOLD,
                     vision_model=vision_model,
                     review_focus=review_focus)
    expected_gender = _infer_gender(char_cfg)
    all_passed = []
    current = list(image_files)

    for rnd in range(3):
        if rnd > 0:
            deficit = target_count - len(all_passed)
            print(f"\n  [补生轮次 {rnd}] 需补 {deficit} 张...")
            prompts = make_training_prompts(
                char_cfg, theme.prompts.style_prefix or "", n=min(deficit+5, 20))
            sd = image_files[0].parent if image_files else Path("lora_workspace/tmp")
            current = generate_training_images(
                prompts, theme, sd, negative=theme.prompts.negative_prefix or "")

        gate.warmup()
        for i, ip in enumerate(current):
            if ip in all_passed: continue
            it = _image_type_for_pose(i)
            ctx = QualityContext(page_num=1, page_title=ip.stem,
                                 characters=[], char_features="", image_type=it)
            r = gate.evaluate(str(ip), ctx)
            if not r.passed:
                print(f"  x {ip.name}  {r.score:.1f} 未通过")
                continue
            if expected_gender and it == "solo_character":
                try:
                    ans = gate._call_vision(
                        "Is the main person male or female? One word: male or female",
                        ip.read_bytes()).strip().lower()
                    det = "male" if "male" in ans else "female"
                    if det != expected_gender:
                        print(f"  x {ip.name}  性别错误({det})")
                        continue
                except Exception:
                    pass
            all_passed.append(ip)
            print(f"  v {ip.name}  {r.score:.1f}/10 ({it})")
        gate.free_model()
        if len(all_passed) >= target_count:
            print(f"  已收集 {len(all_passed)} 张，达标")
            break
    return all_passed[:target_count]


# ════════════════════════════════════════════════════════════════
# Step 4：Caption
# ════════════════════════════════════════════════════════════════

def caption_images(image_files: list[Path],
                   trigger_word: str, char_desc: str,
                   vision_model: str = "minicpm-v:8b") -> dict[Path, str]:
    from quality import LlavaGate
    gate = LlavaGate(vision_model=vision_model)
    gate.warmup()
    prompt = (
        "Describe this image in English for AI training. "
        "Focus on: clothing style, body pose, position in frame, "
        "lighting, background. Be concise (20-40 words). "
        "Do NOT describe the face. Write only the description."
    )
    captions = {}
    for i, p in enumerate(image_files):
        print(f"  [Caption] {i+1}/{len(image_files)}: {p.name}")
        try:
            img_bytes = p.read_bytes()
            desc = gate._call_vision(prompt, img_bytes).strip()
            captions[p] = f"{trigger_word}, {desc}"
            print(f"    → {captions[p][:90]}")
        except Exception as e:
            captions[p] = f"{trigger_word}, {char_desc}"
            print(f"    ✗ {e}，使用默认")
    gate.free_model()
    return captions


# ════════════════════════════════════════════════════════════════
# Step 5：整理数据集
# ════════════════════════════════════════════════════════════════

def build_dataset(image_files: list[Path],
                  captions: dict[Path, str],
                  dataset_dir: Path,
                  char_name: str,
                  repeats: int = 10) -> Path:
    img_dir = dataset_dir / f"{repeats}_{char_name}"
    img_dir.mkdir(parents=True, exist_ok=True)
    for i, p in enumerate(image_files):
        dst = img_dir / f"{i+1:03d}.png"
        txt = img_dir / f"{i+1:03d}.txt"
        PILImage.open(p).convert("RGB").save(dst, "PNG")
        txt.write_text(captions.get(p, f"{char_name}, ancient chinese"),
                       encoding="utf-8")
    print(f"  [Dataset] {img_dir}  ({len(image_files)} 张 × {repeats})")
    return dataset_dir


def write_dataset_toml(dataset_subdir_win: str,
                       output_path: Path, resolution: int = 768) -> Path:
    toml = f"""[general]
enable_bucket = true
resolution = [{resolution}, {resolution}]

[[datasets]]
batch_size = 1

  [[datasets.subsets]]
  image_dir = "{dataset_subdir_win}"
  caption_extension = ".txt"
  num_repeats = 10
"""
    output_path.write_text(toml, encoding="utf-8")
    print(f"  [Dataset] dataset.toml 已生成（sd-scripts 格式）")
    return output_path


def write_train_config(char_name: str,
                       dataset_toml_win: str,
                       output_dir_win: str,
                       config_path: Path) -> Path:
    """
    sd-scripts FLUX 训练配置。

    v3.2 新增：
      save_state = true                 → 每 save_every_n_steps 保存一次训练状态
      save_last_n_steps_state = 1       → 只保留最新一个 state（节省磁盘）

    state 目录格式：output_dir/{output_name}-state-{step}/
    resume_train.bat 通过 dir /ad /o-d 自动找最新 state 续训。
    """

    cfg = f"""# FLUX LoRA 训练配置 - RTX 3060 12GB + 32GB RAM
# 角色: {char_name}
# 工具: kohya-ss/sd-scripts (sd3 分支)

pretrained_model_name_or_path = "{FLUX_FP8_MODEL}"
clip_l = "{CLIP_L}"
t5xxl  = "{T5_FP8}"
ae     = "{VAE}"

dataset_config = "{dataset_toml_win}"
output_dir     = "{output_dir_win}"
output_name    = "{char_name}_flux_lora"

# LoRA
network_module = "networks.lora_flux"
network_dim    = 16
network_alpha  = 16

# 训练
optimizer_type     = "adamw8bit"
learning_rate      = 1e-4
lr_scheduler       = "cosine_with_restarts"
lr_warmup_steps    = 100
max_train_steps    = 1500
save_every_n_steps = 100
seed               = 42

# 断点续训（v3.2 新增）
# resume_train.bat 自动检测 state 目录并注入 resume 参数
# 不需要手动填写 resume，bat 负责处理
save_state                = true
save_last_n_steps_state   = 1       # 只保留最新一个，节省 ~20GB 磁盘

# 精度
mixed_precision = "bf16"
save_precision  = "bf16"

# 12GB 显存优化（3060 必备）
fp8_base                      = true
disable_mmap_load_safetensors = true
gradient_checkpointing        = true
cpu_offload_checkpointing     = false
blocks_to_swap                = 8

# FLUX.1 专用
guidance_scale        = 1.0
timestep_sampling     = "sigmoid"
model_prediction_type = "raw"
discrete_flow_shift   = 3.1582
loss_type             = "l2"

# 缓存（充分利用 32GB 内存 + SSD）
cache_latents                      = true
cache_latents_to_disk              = true
cache_text_encoder_outputs         = true
cache_text_encoder_outputs_to_disk = true
vae_batch_size                     = 1
max_data_loader_n_workers          = 2
persistent_data_loader_workers     = true

# attention
sdpa = true
"""
    config_path.write_text(cfg, encoding="utf-8")
    print(f"  [Train] 训练配置已生成（含断点续训支持）")
    return config_path


# ════════════════════════════════════════════════════════════════
# Step 5.5：SFTP 上传
# ════════════════════════════════════════════════════════════════

def sftp_upload(local_dir: Path, remote_base: str, ssh: paramiko.SSHClient):
    sftp = ssh.open_sftp()

    def _mkdir_p(remote_path: str):
        parts = remote_path.replace("\\", "/").split("/")
        cur = ""
        for part in parts:
            if not part: continue
            cur = cur + "/" + part if cur else part
            win_cur = cur.replace("/", "\\") if ":" in cur else cur
            try:
                sftp.stat(win_cur)
            except FileNotFoundError:
                try:
                    sftp.mkdir(win_cur)
                except Exception:
                    pass

    def _upload_dir(local: Path, remote: str):
        _mkdir_p(remote)
        for item in sorted(local.iterdir()):
            r_item = remote.rstrip("/\\") + "\\" + item.name
            if item.is_dir():
                _upload_dir(item, r_item)
            else:
                sftp.put(str(item), r_item)
                print(f"  ↑ {item.name}")

    rel_parts = local_dir.parts
    ws_idx = next((i for i, p in enumerate(rel_parts)
                   if p == "lora_workspace"), None)
    if ws_idx is None:
        raise ValueError(f"本地路径不包含 lora_workspace: {local_dir}")

    rel_path    = "\\".join(rel_parts[ws_idx+1:])
    remote_path = remote_base.rstrip("/\\") + "\\" + rel_path

    print(f"\n  [SFTP] 上传到 Windows...")
    print(f"    本地: {local_dir}")
    print(f"    远端: {remote_path}")
    _upload_dir(local_dir, remote_path)
    sftp.close()
    print(f"  [SFTP] 上传完成")


# ════════════════════════════════════════════════════════════════
# Step 6：SSH 训练
# bat 改为自动检测 state，有则续训，无则从头
# ════════════════════════════════════════════════════════════════

def create_train_batch_file(ssh: paramiko.SSHClient,
                            config_win_path: str,
                            win_output_dir: str,
                            char_name: str,
                            fresh_start: bool = False) -> bool:
    """
    生成 run_train.bat，支持：
    - 自动检测最新 state 并续练（基于 step 编号最大，不是修改时间）
    - 每次 session 用独立时间戳 output_name（物理隔离产物）
    - 命令行传 --resume，TOML 保持纯净不修改
    - fresh_start=True 时强制从头（但不清 TOML，因为 TOML 里本来就没 resume 行）
    """
    output_dir_bs = win_output_dir.replace("/", "\\")
    config_bs = config_win_path.replace("/", "\\")
    sd_scripts_bs = WIN_SD_SCRIPTS.replace("/", "\\")

    # PowerShell 一次性算出 LATEST_STATE 和时间戳 NEW_OUTPUT_NAME
    # 关键：按文件名中的 step 数字降序排，不是按修改时间
    ps_detect = (
        f"$d='{output_dir_bs}'; "
        f"$s = Get-ChildItem -Path $d -Directory -Filter '*-state' -ErrorAction SilentlyContinue "
        f"| Where-Object {{ $_.Name -match 'step(\\d+)-state' }} "
        f"| Sort-Object {{ [int]($_.Name -replace '.*step(\\d+)-state.*','$1') }} -Descending "
        f"| Select-Object -First 1; "
        f"if ($s) {{ 'LATEST_STATE=' + $s.FullName }} else {{ 'LATEST_STATE=' }}; "
        f"'NEW_OUTPUT_NAME={char_name}_flux_lora_r' + (Get-Date -Format 'yyyyMMdd_HHmm')"
    )

    if fresh_start:
        # 强制从头：LATEST_STATE 留空，不 resume
        detect_block = [
            'echo [INFO] 强制从头训练模式',
            f"for /f \"tokens=1,* delims==\" %%a in ('powershell -NoProfile -Command \""
            f"'LATEST_STATE='; 'NEW_OUTPUT_NAME={char_name}_flux_lora_r' + (Get-Date -Format ''yyyyMMdd_HHmm'')\"') do (",
            '    set "%%a=%%b"',
            ')',
            'set "RESUME_ARG="',
        ]
    else:
        detect_block = [
            'REM 自动检测最新 state（按 step 编号降序）+ 生成独立时间戳 output_name',
            f"for /f \"tokens=1,* delims==\" %%a in ('powershell -NoProfile -Command \"{ps_detect}\"') do (",
            '    set "%%a=%%b"',
            ')',
            '',
            'if defined LATEST_STATE (',
            '    echo [INFO] 检测到 state: %LATEST_STATE%',
            '    set "RESUME_ARG=--resume \"%LATEST_STATE%\""',
            ') else (',
            '    echo [INFO] 无 state，从头训练',
            '    set "RESUME_ARG="',
            ')',
        ]

    lines = [
        '@echo off',
        'chcp 65001 >nul',
        f'echo [INFO] FLUX LoRA 训练 ({char_name}) - {"强制从头" if fresh_start else "自动断点检测"}',
        '',
    ] + detect_block + [
        '',
        'echo [INFO] 本次 output_name: %NEW_OUTPUT_NAME%',
        '',
        f'cd /d {sd_scripts_bs}',
        'call venv\\Scripts\\activate.bat',
        'set HF_HUB_OFFLINE=1',
        'set TRANSFORMERS_OFFLINE=1',
        'set PYTHONUNBUFFERED=1',
        '',
        (
            'python -m accelerate.commands.launch'
            ' --num_cpu_threads_per_process 4'
            ' --mixed_precision bf16'
            ' flux_train_network.py'
            f' --config_file "{config_bs}"'
            ' --output_name "%NEW_OUTPUT_NAME%"'
            ' %RESUME_ARG%'
            ' >> C:\\flux_train\\train_log.txt 2>&1'
        ),
        '',
        'echo [INFO] 本次 session 退出码: %ERRORLEVEL%',
    ]

    try:
        tmp_path = Path(tempfile.mktemp(suffix='.bat'))
        bat_text = '\r\n'.join(lines)
        tmp_path.write_bytes(bat_text.encode('ascii', errors='replace'))

        sftp = ssh.open_sftp()
        sftp.put(str(tmp_path), 'C:/flux_train/run_train.bat')
        sftp.close()
        tmp_path.unlink(missing_ok=True)

        mode = "强制从头" if fresh_start else "自动断点检测"
        print(f"  [Batch] run_train.bat 已传输（{mode}模式）")
        return True
    except Exception as e:
        print(f"  !! bat 文件传输失败: {e}")
        return False


def ensure_offline_mode_dependencies(ssh: paramiko.SSHClient) -> bool:
    print("  [Setup] 检查离线模式依赖...")
    commands = [
        f'cd /d {WIN_SD_SCRIPTS}',
        'call venv\\Scripts\\activate.bat',
        'pip install transformers==4.42.4 huggingface_hub==0.23.5'
    ]
    full_command = f'cmd /c "{" && ".join(commands)}"'
    try:
        stdin, stdout, stderr = ssh.exec_command(full_command, timeout=120)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            print("  [Setup] 离线模式依赖已安装")
            return True
        else:
            print(f"  !! 依赖安装失败: {stderr.read().decode()}")
            return False
    except Exception as e:
        print(f"  !! 检查依赖时出错: {e}")
        return False


def ssh_run_training_detached(config_win: str,
                              ssh: paramiko.SSHClient,
                              win_output_dir: str = "",
                              char_name: str = "char",
                              fresh_start: bool = False) -> bool:
    """
    v3.2：支持断点续训。

    win_output_dir：Windows 上的 output 目录，用于检测 state。
    fresh_start：True 时跳过 state 检测，强制从头训练。
    """

    print(f"\n  [Train] 准备后台训练任务...")
    if fresh_start:
        print(f"  [Train] 模式: 强制从头（--fresh）")
    else:
        print(f"  [Train] 模式: 自动断点检测")
        if win_output_dir:
            print(f"  [Train] state 检测目录: {win_output_dir}")

    ensure_offline_mode_dependencies(ssh)

    if not create_train_batch_file(
            ssh, config_win,
            win_output_dir=win_output_dir or "",
            char_name=char_name,
            fresh_start=fresh_start,
    ):
        return False

    # 清理旧任务 + 创建新任务 + 立即运行
    for cmd in ['schtasks /Delete /TN "FluxTrain" /F 2>nul',
                'timeout /T 2 /NOBREAK >nul']:
        ssh.exec_command(cmd, timeout=15)[1].channel.recv_exit_status()

    for cmd in [
        (f'schtasks /Create /TN "FluxTrain"'
         f' /TR "C:\\flux_train\\run_train.bat"'
         f' /SC ONCE /ST 00:00 /F /RL HIGHEST'
         f' /RU {SSH_USER} /RP {SSH_PASS}'),
        'schtasks /Run /TN "FluxTrain"',
    ]:
        print(f"  ▶ {cmd[:80]}...")
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=30)
        if stdout.channel.recv_exit_status() != 0:
            print(f"  !! 任务命令失败: {stderr.read().decode(errors='ignore')}")
            return False
        print(f"  ✓ 执行成功")

    import time
    time.sleep(10)
    _, stdout, _ = ssh.exec_command('tasklist /FI "IMAGENAME eq python.exe"', timeout=15)
    output = stdout.read().decode(errors="ignore")
    if 'python.exe' in output.lower():
        print(f"  ✓ 训练已在后台运行")
        print(f"  日志 → C:\\flux_train\\train_log.txt")
        return True
    else:
        print(f"  ⚠ 未检测到 Python 进程，查看日志确认")
        return False


# ════════════════════════════════════════════════════════════════
# Step 7：写回 story yaml
# ════════════════════════════════════════════════════════════════

def update_story_yaml(story_path: str, char_name: str,
                      lora_filename: str, strength: float = 0.8):
    with open(story_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    lora_rel = f"flux/{lora_filename}"
    if char_name in data.get("characters", {}):
        data["characters"][char_name]["lora"]          = lora_rel
        data["characters"][char_name]["lora_strength"] = strength
        with open(story_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        print(f"  [YAML] {char_name}.lora → {lora_rel}（强度={strength}）")
    else:
        print(f"  [YAML] 角色 {char_name} 不存在，跳过")


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="FLUX LoRA 自动化训练 v3")
    parser.add_argument("--story",      required=True)
    parser.add_argument("--char",       required=True)
    parser.add_argument("--images",     type=int, default=20)
    parser.add_argument("--skip_gen",   action="store_true")
    parser.add_argument("--skip_train", action="store_true")
    parser.add_argument("--review",     choices=["human", "auto"], default="auto")
    parser.add_argument("--round",      type=int, default=None,
                        help="指定训练轮次（默认自动推断为上一轮 +1）")
    parser.add_argument("--reselect",   type=str, default=None,
                        help="重审指定轮次，如 round1（后悔机制）")
    parser.add_argument("--fresh", action="store_true",
                        help="强制从头训练，忽略已有断点 state")
    args = parser.parse_args()

    from story import StoryConfig
    from theme import ThemeConfig

    story     = StoryConfig(args.story)
    theme     = ThemeConfig(story.default_theme_path)
    char_name = args.char
    char_cfg  = story.characters.get(char_name)

    if not char_cfg:
        print(f"  !! 角色 [{char_name}] 不存在于 {args.story}")
        return

    workspace = Path(f"lora_workspace/{story.story_id}/{char_name}")
    workspace.mkdir(parents=True, exist_ok=True)

    # ── 推断轮次 ──────────────────────────────────────────────
    reselect_round = None
    if args.reselect:
        try:
            reselect_round = int(args.reselect.replace("round", ""))
        except ValueError:
            print(f"  !! --reselect 格式错误，应为 round1 / round2 ...")
            return
        round_num = reselect_round
    elif args.round:
        round_num = args.round
    else:
        round_num = _latest_round(workspace) + 1

    print(f"\n{'='*58}")
    print(f"  FLUX LoRA 训练管线  v3")
    print(f"  故事={story.title}  角色={char_name}  轮次={round_num}")
    if reselect_round:
        print(f"  模式=重审第{reselect_round}轮（后悔机制）")
    print(f"{'='*58}")

    # ── Step 1：生图 ───────────────────────────────────────────
    image_dir = workspace / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_gen and reselect_round is None:
        print(f"\n[Step 1] 生成 {args.images} 张训练图...")
        prompts = make_training_prompts(
            char_cfg, theme.prompts.style_prefix or "", n=args.images)
        image_files = generate_training_images(
            prompts, theme, image_dir,
            negative=theme.prompts.negative_prefix or "")
    else:
        image_files = sorted(image_dir.glob("*.png"))
        label = "跳过生图" if args.skip_gen else "重审模式"
        print(f"\n[Step 1] {label}，已有 {len(image_files)} 张")

    # 断点检测
    _ds = workspace / f"dataset_v{round_num}"
    if _ds.exists() and list(_ds.rglob("*.png")) and reselect_round is None:
        _n = len(list(_ds.rglob("*.png")))
        print(f"\n  [断点] dataset_v{round_num}/ 已有 {_n} 张")
        print(f"  跳过审核用现有数据？(y/n) > ", end="", flush=True)
        ch = _getch()
        print(ch)
        if ch in ("y", "Y"):
            final_images = sorted(_ds.rglob("*.png"))
            print(f"  → 跳过审核，用 dataset_v{round_num} ({_n} 张)")
            # 跳到 Step 4
            goto_step4 = True
        else:
            goto_step4 = False
    else:
        goto_step4 = False

    if not image_files:
        print("  !! 没有训练图片，退出")
        return

    # ── Step 2-3：审核 ────────────────────────────────────────
    if not goto_step4:
        review_mode = args.review
        print(f"\n[Step 2-3] 审核模式: {review_mode}  轮次: {round_num}")

        if review_mode == "human":
            final_images = human_review_with_annotations(
                image_files,
                vision_model=theme.model.vision_model,
                review_focus="古风人物画质，服饰风格一致性，构图完整性，人物性别",
                char_cfg=char_cfg,
                workspace=workspace,
                round_num=round_num,
                theme=theme,
                reselect_round=reselect_round,
            )
        else:
            final_images = auto_review_with_gender(
                image_files,
                vision_model=theme.model.vision_model,
                review_focus="古风人物画质，服饰风格一致性，构图完整性",
                char_cfg=char_cfg, theme=theme, target_count=15,
            )
    else:
        final_images = sorted(_ds.rglob("*.png"))

    print(f"\n  最终训练集: {len(final_images)} 张")
    if len(final_images) < 10:
        print(f"  图片不足 10 张，继续？(y/n) > ", end="", flush=True)
        ch = _getch()
        print(ch)
        if ch not in ("y", "Y"):
            return

    # ── Step 4：Caption ────────────────────────────────────────
    print(f"\n[Step 4] 生成 caption...")
    captions = caption_images(
        final_images,
        trigger_word=char_name,
        char_desc=char_cfg.get("desc", char_name),
        vision_model=theme.model.vision_model,
    )

    # ── Step 5：整理数据集（版本化）──────────────────────────
    print(f"\n[Step 5] 整理数据集 dataset_v{round_num}...")
    dataset_dir = workspace / f"dataset_v{round_num}"
    repeats = 10
    build_dataset(final_images, captions, dataset_dir, char_name, repeats)

    win_char_dir    = f"{WIN_WORKSPACE}/{story.story_id}/{char_name}"
    win_dataset_sub = f"{win_char_dir}/dataset_v{round_num}/{repeats}_{char_name}"
    win_dataset_toml= f"{win_char_dir}/dataset.toml"
    win_output_dir  = f"{win_char_dir}/output_v{round_num}"
    win_config      = f"{win_char_dir}/train_config.toml"

    dataset_toml = workspace / "dataset.toml"
    config_path  = workspace / "train_config.toml"
    (workspace / f"output_v{round_num}").mkdir(exist_ok=True)

    write_dataset_toml(win_dataset_sub, dataset_toml)
    write_train_config(
        char_name        = char_name,
        dataset_toml_win = win_dataset_toml,
        output_dir_win   = win_output_dir,
        config_path      = config_path,
    )

    # ── Step 5.5：SFTP 上传 ────────────────────────────────────
    print(f"\n[Step 5.5] SFTP 上传到 Windows (SSD)...")
    ssh = _make_ssh()
    try:
        sftp_upload(workspace, WIN_WORKSPACE, ssh)
    except Exception as e:
        print(f"  !! SFTP 上传失败: {e}")
        ssh.close()
        return

    cache_dir = win_dataset_sub.replace("/", "\\")
    ssh.exec_command(f'del /Q "{cache_dir}\\*.npz" 2>nul', timeout=15)

    if args.skip_train:
        print(f"\n  [跳过训练] 数据已上传，手动训练命令：")
        print(f"    ssh -p {SSH_PORT} {SSH_USER}@{SSH_HOST}")
        print(f"    cd {WIN_SD_SCRIPTS}")
        print(f"    accelerate launch flux_train_network.py "
              f"--config_file \"{win_config.replace('/', chr(92))}\"")
        ssh.close()
        return

    # ── Step 6：SSH 训练 ──────────────────────────────────────
    print(f"\n[Step 6] 开始训练（第 {round_num} 轮）...")
    config_win_bs = win_config.replace("/", "\\")
    success = ssh_run_training_detached(
           config_win_bs, ssh,
           win_output_dir=win_output_dir.replace("/", "\\"),
           char_name=char_name,
           fresh_start=args.fresh,
       )
    ssh.close()

    if not success:
        print(f"  !! 训练失败，请 SSH 到 Windows 查看日志")
        return

    print(f"  ✓ 训练已在后台启动 → C:\\flux_train\\train_log.txt")

    # ── Step 7：写回 yaml ─────────────────────────────────────
    lora_file = f"{char_name}_flux_lora.safetensors"
    print(f"\n[Step 7] 写回 yaml...")
    update_story_yaml(args.story, char_name, lora_file)

    print(f"\n{'='*58}")
    print(f"  ✓ {char_name} LoRA 第 {round_num} 轮训练完成")
    print(f"  LoRA: {WIN_LORA_OUTPUT}/{lora_file}")
    print(f"  数据集: dataset_v{round_num}  选图记录: selections/round{round_num}.json")
    print(f"{'='*58}")


if __name__ == "__main__":
    main()
