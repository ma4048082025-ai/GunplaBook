"""
registry.py ── 资产注册表
==========================
所有生成资产的全生命周期追踪。

核心理念：
  不再用文件名猜测"这一页生成了没有"。
  每一个资产的状态、分数、血缘关系都记录在 SQLite 数据库里。
  这是"断点续跑"和"夜跑模式"的基础。

当前阶段：完整实现。
Stage A 扩展：batch_candidates 表已预留。
Stage B 扩展：series_meta 表已预留。
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class AssetStatus(str, Enum):
    PENDING   = "pending"
    GENERATED = "generated"
    CANDIDATES_READY = "candidates_ready"  # Phase 2b 已生候选未选优
    APPROVED  = "approved"
    REJECTED  = "rejected"
    UPSCALED  = "upscaled"
    DONE      = "done"


class PipelineStage(str, Enum):
    INIT     = "init"
    GENERATE = "generate"
    QUALITY  = "quality"
    UPSCALE  = "upscale"
    PRODUCE  = "produce"
    DONE     = "done"


@dataclass
class Asset:
    id:          int
    story_id:    str
    page_num:    int
    stage:       str
    status:      str
    path:        str
    score:       float = -1.0
    attempt:     int   = 1
    seed:        int   = 0
    cfg:         float = 6.5
    params_json: str   = "{}"
    created_at:  str   = ""

    @property
    def params(self) -> dict:
        return json.loads(self.params_json)

    @property
    def is_approved(self) -> bool:
        return self.status in (AssetStatus.APPROVED, AssetStatus.UPSCALED,
                               AssetStatus.DONE)

    def exists(self) -> bool:
        return bool(self.path) and Path(self.path).exists()


class Registry:
    """
    资产注册表。每个故事对应一个 SQLite 文件。
    存放位置：stories/.registry/<story_stem>.db
    """

    def __init__(self, story_path: str):
        db_dir = Path(story_path).parent / ".registry"
        db_dir.mkdir(exist_ok=True)
        self.db = str(db_dir / f"{Path(story_path).stem}.db")
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id     TEXT NOT NULL,
                page_num     INTEGER NOT NULL,
                stage        TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                path         TEXT,
                score        REAL DEFAULT -1.0,
                attempt      INTEGER DEFAULT 1,
                seed         INTEGER DEFAULT 0,
                cfg          REAL DEFAULT 6.5,
                params_json  TEXT DEFAULT '{}',
                created_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_page
              ON assets(story_id, page_num, stage);

            -- Stage A：batch 候选图
            CREATE TABLE IF NOT EXISTS candidates (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_id   INTEGER REFERENCES assets(id),
                page_num   INTEGER NOT NULL,
                path       TEXT NOT NULL,
                score      REAL DEFAULT -1.0,
                selected   INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- Stage B：系列管理（预留）
            CREATE TABLE IF NOT EXISTS series_meta (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id   TEXT NOT NULL,
                story_id    TEXT NOT NULL,
                episode_num INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now'))
            );
            """)

    # ── 写入 ──────────────────────────────────────

    def record(self, page_num: int, stage: str, path: str,
               story_id: str = "default", score: float = -1.0,
               attempt: int = 1, seed: int = 0, cfg: float = 6.5,
               params: dict = None, status: str = None) -> int:
        if status is None:
            status = (AssetStatus.APPROVED if score >= 0
                      else AssetStatus.GENERATED)
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO assets
                  (story_id, page_num, stage, status, path, score,
                   attempt, seed, cfg, params_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (story_id, page_num, stage, status, path, score,
                  attempt, seed, cfg,
                  json.dumps(params or {}, ensure_ascii=False)))
            return cur.lastrowid

    def update_status(self, asset_id: int, status: str,
                      score: float = None):
        with self._conn() as conn:
            if score is not None:
                conn.execute(
                    "UPDATE assets SET status=?, score=? WHERE id=?",
                    (status, score, asset_id))
            else:
                conn.execute(
                    "UPDATE assets SET status=? WHERE id=?",
                    (status, asset_id))

    def update_path(self, asset_id: int, new_path: str):
        with self._conn() as conn:
            conn.execute("UPDATE assets SET path=? WHERE id=?", (new_path, asset_id))

    # ── 查询 ──────────────────────────────────────

    def best_for_page(self, page_num: int, story_id: str = "default",
                      stage: str = None) -> Optional[Asset]:
        """返回某页最高分且文件存在的资产"""
        with self._conn() as conn:
            q      = """
                SELECT * FROM assets
                WHERE story_id=? AND page_num=?
                  AND status IN ('approved','upscaled','done')
            """
            params = [story_id, page_num]
            if stage:
                q += " AND stage=?"
                params.append(stage)
            q   += " ORDER BY score DESC, created_at DESC LIMIT 1"
            row  = conn.execute(q, params).fetchone()
        if not row:
            return None
        a = Asset(**dict(row))
        return a if a.exists() else None

    def pages_pending(self, story_id: str = "default",
                      total_pages: int = 0) -> list[int]:
        """返回还没有通过资产的页码列表（断点续跑用）"""
        with self._conn() as conn:
            done = {row[0] for row in conn.execute("""
                SELECT DISTINCT page_num FROM assets
                WHERE story_id=?
                  AND status IN ('approved','upscaled','done')
                  AND path IS NOT NULL
            """, (story_id,)).fetchall()}
        return sorted(set(range(1, total_pages + 1)) - done)

    def pages_below_threshold(self, threshold: float,
                              story_id: str = "default") -> list[int]:
        """返回最高分低于阈值的页码（重跑低分页用）"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT page_num, MAX(score) as best_score
                FROM assets
                WHERE story_id=? AND score >= 0
                GROUP BY page_num
                HAVING best_score < ?
            """, (story_id, threshold)).fetchall()
        return [r[0] for r in rows]

    def all_approved(self, story_id: str = "default") -> list[Asset]:
        """返回所有已通过质量门的最优资产，按页码排序"""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY page_num
                        ORDER BY score DESC, created_at DESC
                    ) as rn
                    FROM assets
                    WHERE story_id=?
                      AND status IN ('approved','upscaled','done')
                ) WHERE rn=1
                ORDER BY page_num
            """, (story_id,)).fetchall()
        return [Asset(**{k: v for k, v in dict(r).items() if k != "rn"})
                for r in rows]

    def all_generated(self, story_id: str = "default") -> list[Asset]:
        """
        返回所有 GENERATED 状态的资产（Phase 1 已生图但尚未评分）。
        两阶段模式续跑时使用：若 Phase 1 跑完但 Phase 2 中断，
        可用此方法恢复 Phase 2 的输入。
        """
        with self._conn() as conn:
            rows = conn.execute("""
                                SELECT *
                                FROM assets
                                WHERE story_id = ?
                                  AND status = 'generated'
                                ORDER BY page_num, created_at DESC
                                """, (story_id,)).fetchall()
        assets = [Asset(**dict(r)) for r in rows]
        return [a for a in assets if a.exists()]

    def mark_candidates_ready(self, asset_id: int, score: float = -1.0):
        """
        v2.3.6：标记 asset 为"已生候选未选优"中间状态。
        Phase 2b 单页生完 2 张候选后立刻调用，让 resume 能识别。
        """
        with self._conn() as conn:
            if score >= 0:
                conn.execute(
                    "UPDATE assets SET status=?, score=? WHERE id=?",
                    (AssetStatus.CANDIDATES_READY, score, asset_id))
            else:
                conn.execute(
                    "UPDATE assets SET status=? WHERE id=?",
                    (AssetStatus.CANDIDATES_READY, asset_id))

    def all_candidates_ready(self, story_id: str = "default") -> list[Asset]:
        """
        v2.3.6：返回所有 CANDIDATES_READY 状态的资产（Phase 2b 完成、Phase 2c 未完成）。
        resume 时识别这些页，跳过重新生候选，直接走 Phase 2c。
        """
        with self._conn() as conn:
            rows = conn.execute("""
                                SELECT *
                                FROM assets
                                WHERE story_id = ?
                                  AND status = 'candidates_ready'
                                ORDER BY page_num, created_at DESC
                                """, (story_id,)).fetchall()
        return [Asset(**dict(r)) for r in rows]

    def candidates_for_page(self, page_num: int,
                            story_id: str = "default") -> list[dict]:
        """
        v2.3.6：返回某页所有未选中的候选（resume 时 Phase 2c 用）。
        返回 list[{path, score, id}]
        """
        with self._conn() as conn:
            rows = conn.execute("""
                                SELECT id, path, score
                                FROM candidates
                                WHERE page_num = ?
                                  AND selected = 0
                                ORDER BY created_at DESC
                                """, (page_num,)).fetchall()
        # 过滤文件不存在的（可能磁盘被清过）
        result = []
        for r in rows:
            if r["path"] and Path(r["path"]).exists():
                result.append({"id": r["id"], "path": r["path"],
                               "score": r["score"]})
        return result

    def all_rejected(self, story_id: str = "default") -> list[Asset]:
        """
        v2.3.7：返回所有 REJECTED 状态的资产
        （Phase 2a 评分未通过、Phase 2b 未完成）。
        resume 时识别这些页，跳过 Phase 1 + Phase 2a，
        直接进 Phase 2b 重生候选。
        """
        with self._conn() as conn:
            rows = conn.execute("""
                                SELECT *
                                FROM assets
                                WHERE story_id = ?
                                  AND status = 'rejected'
                                ORDER BY page_num, created_at DESC
                                """, (story_id,)).fetchall()
        assets = [Asset(**dict(r)) for r in rows]
        return [a for a in assets if a.exists()]


    def summary(self, story_id: str = "default") -> dict:
        with self._conn() as conn:
            stats = conn.execute("""
                SELECT status, COUNT(*) as cnt,
                       AVG(CASE WHEN score>=0 THEN score END) as avg_score
                FROM assets WHERE story_id=?
                GROUP BY status
            """, (story_id,)).fetchall()
        return {
            r["status"]: {
                "count":     r["cnt"],
                "avg_score": round(r["avg_score"] or 0, 2),
            }
            for r in stats
        }

    # ── Stage A：batch 候选 ──────────────────────

    def add_candidate(self, page_num: int, path: str,
                      score: float = -1.0,
                      asset_id: int = None) -> int:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT INTO candidates (asset_id, page_num, path, score)
                VALUES (?,?,?,?)
            """, (asset_id, page_num, path, score))
            return cur.lastrowid

    def select_best_candidate(self, page_num: int) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("""
                SELECT id, path FROM candidates
                WHERE page_num=? AND score>=0
                ORDER BY score DESC LIMIT 1
            """, (page_num,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE candidates SET selected=1 WHERE id=?",
                    (row["id"],))
                return row["path"]
        return None

    # ── Stage B：系列管理（预留）────────────────

    def register_series(self, series_id: str, story_id: str,
                        episode_num: int = 1):
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO series_meta
                  (series_id, story_id, episode_num)
                VALUES (?,?,?)
            """, (series_id, story_id, episode_num))

    def series_assets(self, series_id: str) -> list[Asset]:
        with self._conn() as conn:
            story_ids = [r[0] for r in conn.execute("""
                SELECT story_id FROM series_meta
                WHERE series_id=? ORDER BY episode_num
            """, (series_id,)).fetchall()]
        result = []
        for sid in story_ids:
            result.extend(self.all_approved(story_id=sid))
        return result

    # ── 数据清理────────────────
    def reset(self, story_id: str):
        """清空指定故事的所有资产记录。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM assets WHERE story_id=?", (story_id,))
            conn.execute("DELETE FROM candidates WHERE page_num IN ("
                         "  SELECT page_num FROM assets WHERE story_id=?)",
                         (story_id,))

    def reset_all(self):
        """清空所有故事的资产记录。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM candidates")
