"""
_log_utils.py ── 日志档案统一 API（v2.3 新增）
=================================================
所有 LLM 调用、修订决策、生图记录写到统一的 logs/ 目录下，便于事后分析。

目录结构：
  logs/
    prompts/<story_id>/page_<N>_<title>_<timestamp>.json     ← FLUX prompt
    doctors/<story_id>/<chid>_<doctor>_<timestamp>.json      ← 编剧大师
    reviewers/<story_id>/<chid>_<reviewer>_<timestamp>.json  ← 分镜审稿员
    pipeline/<story_id>/phase<N>_run_<timestamp>.jsonl       ← 流式日志

每条记录都是结构化 JSON，字段统一：
  {
    "timestamp": ISO 8601,
    "story_id":  "long_...",
    "stage":     "doctor.continuity" / "reviewer.flux" / "prompt.generation",
    "input":     {...},
    "output":    {...},
    "decision":  {...},
    "model":     "deepseek-v3" / ...,
    "duration_ms": 1234
  }

用法：
  from _log_utils import LogArchive
  arc = LogArchive(story_id="long_xxx")
  
  arc.write("doctor.continuity", chapter_id="ch01",
            input={"body": "..."}, output={...}, decision={...})
  
  arc.write_prompt(page_num=5, page_title="ch01-sh05",
                   positive="...", negative="...", cfg=3.0, ...)
  
  with arc.stream("pipeline.phase1") as stream:
      stream.write({"page": 1, "status": "generating"})
      stream.write({"page": 1, "status": "approved"})
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


LOGS_ROOT = Path("logs")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _safe_filename(text: str, max_len: int = 80) -> str:
    """把任意字符串变成安全文件名"""
    if not text:
        return "untitled"
    bad = '<>:"/\\|?* '
    out = "".join(c if c not in bad else "_" for c in text)
    return out[:max_len]


def _timestamp() -> str:
    """ISO 8601 时间戳，带毫秒"""
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


class _StreamWriter:
    """JSONL 流式写入器（用 with 语句）"""
    def __init__(self, path: Path, story_id: str, stage: str):
        self.path = path
        self.story_id = story_id
        self.stage = stage
        self._fh = None
        self._start_time = None

    def __enter__(self):
        _ensure_dir(self.path.parent)
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1)  # 行缓冲
        self._start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fh:
            self._fh.close()

    def write(self, payload: dict) -> None:
        """每条记录写一行 JSON"""
        if not self._fh:
            return
        record = {
            "timestamp": datetime.now().isoformat(),
            "story_id":  self.story_id,
            "stage":     self.stage,
            **payload,
        }
        try:
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # 日志失败不应影响主流程
            pass


class LogArchive:
    """
    日志档案管家。每个 story_id 一个实例。
    
    核心方法：
      write(stage, **fields)       — 单条记录写一个独立 json 文件（适合慢且重要的事件）
      write_prompt(page_num, ...)  — 写生图 prompt 到 logs/prompts/<story_id>/
      stream(stage)                — 流式 JSONL 写入器（适合密集事件如生图进度）
    """

    def __init__(self, story_id: str, logs_root: Optional[Path] = None):
        self.story_id = story_id or "unknown"
        self.root = (logs_root or LOGS_ROOT).resolve()

    # ── 内部路径解析 ──────────────────────────────────────
    def _path(self, category: str, filename: str) -> Path:
        return self.root / category / self.story_id / filename

    # ── 单条记录写入 ──────────────────────────────────────
    def write(self, stage: str, *,
              chapter_id: Optional[str] = None,
              input: Optional[dict] = None,
              output: Optional[dict] = None,
              decision: Optional[dict] = None,
              model: Optional[str] = None,
              duration_ms: Optional[int] = None,
              extra: Optional[dict] = None) -> Optional[Path]:
        """
        写一条独立 JSON 记录。
        stage: "doctor.continuity" / "reviewer.flux" / etc.
        category 从 stage 第一段推导：doctor.* → doctors/, reviewer.* → reviewers/
        """
        # 决定 category
        prefix = stage.split(".")[0]
        category_map = {
            "doctor":   "doctors",
            "reviewer": "reviewers",
            "prompt":   "prompts",
            "pipeline": "pipeline",
        }
        category = category_map.get(prefix, "misc")

        # 构造文件名
        ts = _timestamp()
        if chapter_id:
            sub = stage.split(".", 1)[1] if "." in stage else stage
            filename = f"{chapter_id}_{_safe_filename(sub)}_{ts}.json"
        else:
            filename = f"{_safe_filename(stage)}_{ts}.json"

        path = self._path(category, filename)
        _ensure_dir(path.parent)

        record = {
            "timestamp":   datetime.now().isoformat(),
            "story_id":    self.story_id,
            "stage":       stage,
            "chapter_id":  chapter_id,
            "input":       input or {},
            "output":      output or {},
            "decision":    decision or {},
            "model":       model,
            "duration_ms": duration_ms,
            "extra":       extra or {},
        }

        try:
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return path
        except Exception:
            return None

    # ── 生图 prompt 专用 ──────────────────────────────────
    def write_prompt(self, page_num: int, page_title: str = "",
                     positive: str = "", negative: str = "",
                     cfg: float = 0.0, steps: int = 0, sampler: str = "",
                     lora: str = "", lora_strength: float = 0.0,
                     extra: Optional[dict] = None) -> Optional[Path]:
        ts = _timestamp()
        title_safe = _safe_filename(page_title or f"page{page_num}")
        filename = f"page_{page_num:04d}_{title_safe}_{ts}.json"
        path = self._path("prompts", filename)
        _ensure_dir(path.parent)

        record = {
            "timestamp": datetime.now().isoformat(),
            "story_id":  self.story_id,
            "page":      page_num,
            "title":     page_title,
            "positive":  positive,
            "negative":  negative,
            "params":    {
                "cfg":      cfg,
                "steps":    steps,
                "sampler":  sampler,
            },
            "lora":      {
                "name":     lora,
                "strength": lora_strength,
            },
            "extra":     extra or {},
        }

        try:
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2),
                encoding="utf-8")
            return path
        except Exception:
            return None

    # ── 流式日志 ──────────────────────────────────────────
    def stream(self, stage: str) -> _StreamWriter:
        """
        返回一个 with 语境的流式写入器，写 JSONL。
        用法：
          with arc.stream("pipeline.phase1") as s:
              s.write({"page": 1, "status": "generating"})
              s.write({"page": 1, "status": "approved", "score": 8.5})
        """
        ts = _timestamp()
        prefix = stage.split(".")[0]
        category = {
            "pipeline": "pipeline",
            "doctor":   "doctors",
            "reviewer": "reviewers",
        }.get(prefix, "misc")

        sub = stage.split(".", 1)[1] if "." in stage else stage
        filename = f"{_safe_filename(sub)}_run_{ts}.jsonl"
        path = self._path(category, filename)
        return _StreamWriter(path, self.story_id, stage)

    # ── 查询/统计辅助 ─────────────────────────────────────
    def list_records(self, category: str,
                     chapter_id: Optional[str] = None) -> list:
        """列出某分类下所有记录路径（最新在前），含 json 和 jsonl"""
        d = self.root / category / self.story_id
        if not d.exists():
            return []
        files = sorted(list(d.glob("*.json")) + list(d.glob("*.jsonl")),
                       key=lambda p: p.name, reverse=True)
        if chapter_id:
            files = [f for f in files if f.name.startswith(chapter_id)]
        return files


# ════════════════════════════════════════════════════════════════
# 便捷函数（不实例化也能用）
# ════════════════════════════════════════════════════════════════

def quick_log(story_id: str, stage: str, **kwargs) -> Optional[Path]:
    """便捷一行调用，给不想新建实例的地方用"""
    return LogArchive(story_id).write(stage, **kwargs)


# ════════════════════════════════════════════════════════════════
# 自检：测试用
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    arc = LogArchive("long_test_xxx", logs_root=tmp)

    # 测 write
    p1 = arc.write("doctor.continuity",
                   chapter_id="ch01",
                   input={"body": "原文..."},
                   output={"issues": [], "patches": []},
                   decision={"applied": 0, "rejected": 0},
                   model="deepseek-v3",
                   duration_ms=1234)
    assert p1.exists(), "doctor 日志写入失败"
    print(f"  ✓ doctor 日志: {p1.relative_to(tmp)}")

    # 测 write_prompt
    p2 = arc.write_prompt(page_num=5, page_title="ch01-sh05",
                          positive="...", negative="...",
                          cfg=3.5, steps=25, sampler="euler")
    assert p2.exists()
    print(f"  ✓ prompt 日志: {p2.relative_to(tmp)}")

    # 测 stream
    with arc.stream("pipeline.phase1") as s:
        s.write({"page": 1, "status": "generating"})
        s.write({"page": 1, "status": "approved", "score": 8.5})
    files = arc.list_records("pipeline")
    assert len(files) == 1
    print(f"  ✓ 流式日志: {files[0].relative_to(tmp)}")

    # 测 list_records
    docs = arc.list_records("doctors", chapter_id="ch01")
    assert len(docs) == 1
    print(f"  ✓ 查询: 找到 {len(docs)} 条 ch01 doctor 记录")

    print("\n══ _log_utils 自检通过 ══")
