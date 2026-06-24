"""
param_learner.py ── 自动调参引擎
====================================
核心思路：
  1. 每张图生成后，自动记录"参数组合 + 视觉评分"
  2. 积累足够样本后（默认10张），分析哪组参数出图最好
  3. 自动将最优参数写回故事YAML的场景模板
  4. 探索模式：样本不足时，自动追加变体参数尝试

可调参数范围（基于ComfyUI实际效果经验）：
  CFG:     5.0 - 9.0（Pony模型甜点6.0-7.5）
  steps:   20 - 40（30是稳健值）
  sampler: dpmpp_2m / euler_ancestral / dpmpp_sde / dpmpp_2m_sde

用法：
  learner = ParamLearner("stories/xxx.yaml")
  learner.record("standoff", params, score=7.8, page_num=2)
  learner.analyze_and_update()   # 自动分析并更新YAML
  print(learner.report())        # 打印学习状态
"""

import json
import random
import sqlite3
from pathlib import Path
from typing import Optional

import yaml

EXPLORE_CFG           = [5.5, 6.0, 6.5, 7.0, 7.5, 8.0]
EXPLORE_STEPS         = [20, 25, 30, 35, 40]
EXPLORE_SAMPLERS      = ["dpmpp_2m", "euler_ancestral", "dpmpp_sde", "dpmpp_2m_sde"]
EXPLORE_LORA_STRENGTH = [0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]

MIN_SAMPLES_TO_ANALYZE = 10
MIN_HIGH_SCORE_SAMPLES = 3
HIGH_SCORE_THRESHOLD   = 7.0


class ParamLearner:

    def __init__(self, story_path: str):
        self.story_path = story_path
        db_dir = Path(story_path).parent / ".param_db"
        db_dir.mkdir(exist_ok=True)
        self.db = str(db_dir / f"{Path(story_path).stem}.db")
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS param_records (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_type    TEXT NOT NULL,
                cfg           REAL NOT NULL,
                steps         INTEGER NOT NULL,
                sampler       TEXT NOT NULL DEFAULT 'dpmpp_2m',
                scheduler     TEXT NOT NULL DEFAULT 'karras',
                lora_strength REAL DEFAULT 1.0,
                score         REAL DEFAULT -1.0,
                is_explore    INTEGER DEFAULT 0,
                is_composite  INTEGER DEFAULT 0,
                page_num      INTEGER,
                params_json   TEXT DEFAULT '{}',
                created_at    TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_scene
              ON param_records(scene_type, score);

            CREATE TABLE IF NOT EXISTS update_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_type  TEXT NOT NULL,
                old_cfg     REAL, new_cfg     REAL,
                old_steps   INTEGER, new_steps INTEGER,
                old_sampler TEXT, new_sampler TEXT,
                reason      TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            -- 配置签名表：checkpoint/LoRA 变更时自动检测并清空历史数据
            CREATE TABLE IF NOT EXISTS config_meta (
                id         INTEGER PRIMARY KEY,
                checkpoint TEXT DEFAULT '',
                lora_hash  TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now'))
            );
            """)
            # 兼容旧数据库：如果 is_composite 列不存在则添加
            try:
                conn.execute("ALTER TABLE param_records ADD COLUMN is_composite INTEGER DEFAULT 0")
            except Exception:
                pass  # 列已存在，忽略

    # ── 配置变更检测 ──────────────────────────────────────

    def check_config_changed(self, checkpoint: str, main_lora: str) -> bool:
        """
        检查 checkpoint 或主要 LoRA 是否变更。
        变更时清空 param_records 历史，避免旧数据误导新模型。
        返回 True 表示发生了变更并已清空。
        """
        import hashlib
        lora_hash = hashlib.md5(main_lora.encode()).hexdigest()[:8]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT checkpoint, lora_hash FROM config_meta WHERE id=1"
            ).fetchone()
            if row is None:
                # 首次运行，写入签名
                conn.execute(
                    "INSERT INTO config_meta (id, checkpoint, lora_hash) VALUES (1,?,?)",
                    (checkpoint, lora_hash))
                return False
            if row[0] == checkpoint and row[1] == lora_hash:
                return False
            # 配置变更 → 清空历史
            print(f"\n  [调参] 检测到配置变更（checkpoint 或 LoRA 已更换）")
            print(f"  [调参] 旧: {row[0]} / {row[1]}  新: {checkpoint} / {lora_hash}")
            print(f"  [调参] 清空历史参数数据，重新积累...")
            conn.execute("DELETE FROM param_records")
            conn.execute(
                "UPDATE config_meta SET checkpoint=?, lora_hash=?, updated_at=datetime('now') WHERE id=1",
                (checkpoint, lora_hash))
            return True

    # ── 记录 ─────────────────────────────────────────────

    def record(self, scene_type: str, params: dict,
               score: float, page_num: int = 0,
               is_explore: bool = False,
               is_composite: bool = False):
        """
        每页生图完成后调用，记录参数+评分。
        is_composite=True 的记录不参与单张生图参数分析，
        单独积累用于未来的合成参数学习。
        """
        safe_params = {k: v for k, v in params.items()
                       if k not in ("positive", "negative",
                                    "_checkpoint", "_ipadapter", "_clip_vision")}
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO param_records
                  (scene_type, cfg, steps, sampler, scheduler,
                   lora_strength, score, is_explore, is_composite, page_num, params_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                scene_type,
                params.get("cfg", 6.5),
                params.get("steps", 30),
                params.get("sampler", "dpmpp_2m"),
                params.get("scheduler", "karras"),
                params.get("lora_strength", 1.0),
                score,
                1 if is_explore else 0,
                1 if is_composite else 0,
                page_num,
                json.dumps(safe_params, ensure_ascii=False),
            ))

    # ── 查询 ─────────────────────────────────────────────

    def sample_count(self, scene_type: str,
                     scored_only: bool = True) -> int:
        """只统计单张生图的样本（排除合成页）"""
        q = ("SELECT COUNT(*) FROM param_records "
             "WHERE scene_type=? AND is_composite=0"
             + (" AND score >= 0" if scored_only else ""))
        with self._conn() as conn:
            return conn.execute(q, [scene_type]).fetchone()[0]

    def all_scene_types(self) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT scene_type FROM param_records"
            ).fetchall()
        return [r[0] for r in rows]

    def records_for(self, scene_type: str,
                    scored_only: bool = True,
                    include_composite: bool = False) -> list:
        """
        include_composite=False（默认）：只返回单张生图记录，用于参数分析。
        include_composite=True：包含合成页记录，用于探索参数覆盖检查。
        """
        q = ("SELECT * FROM param_records WHERE scene_type=?"
             + ("" if include_composite else " AND is_composite=0")
             + (" AND score >= 0" if scored_only else "")
             + " ORDER BY created_at DESC")
        with self._conn() as conn:
            rows = conn.execute(q, [scene_type]).fetchall()
        return [dict(r) for r in rows]

    # ── 分析 ─────────────────────────────────────────────

    def analyze(self, scene_type: str) -> Optional[dict]:
        """分析最优参数，样本不足时返回 None"""
        records = self.records_for(scene_type, scored_only=True)
        if len(records) < MIN_SAMPLES_TO_ANALYZE:
            return None

        # CFG 分桶分析（步长0.5）
        cfg_buckets: dict = {}
        for r in records:
            bucket = round(r["cfg"] * 2) / 2
            cfg_buckets.setdefault(bucket, []).append(r["score"])
        best_cfg = max(
            cfg_buckets,
            key=lambda k: (sum(cfg_buckets[k]) / len(cfg_buckets[k])
                           if len(cfg_buckets[k]) >= 2 else -1)
        )

        # Sampler 分析
        sampler_scores: dict = {}
        for r in records:
            s = r.get("sampler", "dpmpp_2m")
            sampler_scores.setdefault(s, []).append(r["score"])
        best_sampler = max(
            sampler_scores,
            key=lambda k: (sum(sampler_scores[k]) / len(sampler_scores[k])
                           if len(sampler_scores[k]) >= 2 else -1)
        )

        # LoRA 强度分析（步长0.1，分桶）
        lora_buckets: dict = {}
        for r in records:
            bucket = round(r.get("lora_strength", 1.0) * 10) / 10
            lora_buckets.setdefault(bucket, []).append(r["score"])
        if len(lora_buckets) >= 2:
            best_lora = max(
                lora_buckets,
                key=lambda k: (sum(lora_buckets[k]) / len(lora_buckets[k])
                               if len(lora_buckets[k]) >= 2 else -1)
            )
        else:
            best_lora = None  # 没有足够变体，不建议修改

        # Steps：只看高分样本的均值
        high_records = [r for r in records
                        if r["score"] >= HIGH_SCORE_THRESHOLD]
        if len(high_records) >= MIN_HIGH_SCORE_SAMPLES:
            raw = sum(r["steps"] for r in high_records) / len(high_records)
            best_steps = int(round(raw / 5) * 5)
            best_steps = max(20, min(40, best_steps))
        else:
            best_steps = None

        n   = len(records)
        hi  = len(high_records)
        avg = sum(r["score"] for r in records) / n

        confidence = ("high"   if n >= 30 and hi >= 8 else
                      "medium" if n >= 15 and hi >= 4 else
                      "low")

        return {
            "scene_type":    scene_type,
            "best_cfg":      float(best_cfg),
            "best_steps":    best_steps,
            "best_sampler":  best_sampler,
            "best_lora":     float(best_lora) if best_lora else None,
            "avg_score":     round(avg, 2),
            "sample_count":  n,
            "high_score_n":  hi,
            "confidence":    confidence,
        }

    # ── 更新 YAML ─────────────────────────────────────

    def analyze_and_update(self,
                           min_confidence: str = "medium",
                           dry_run: bool = False) -> dict:
        """
        分析所有场景类型并更新 YAML。

        min_confidence:
          "low"    需要10个样本（激进）
          "medium" 需要15个+4个高分（稳健，默认）
          "high"   需要30个+8个高分（保守）

        dry_run=True 只打印变化，不写文件
        返回变更字典
        """
        confidence_rank = {"low": 0, "medium": 1, "high": 2}
        min_rank        = confidence_rank.get(min_confidence, 1)
        changes         = {}

        with open(self.story_path, "r", encoding="utf-8") as f:
            story_data = yaml.safe_load(f)

        templates = story_data.get("scene_templates", {})

        for scene_type in self.all_scene_types():
            result = self.analyze(scene_type)
            if not result:
                continue
            if confidence_rank.get(result["confidence"], 0) < min_rank:
                continue
            if scene_type not in templates:
                continue

            tmpl        = templates[scene_type]
            old_cfg     = tmpl.get("cfg", 6.5)
            old_steps   = tmpl.get("steps", 30)
            old_sampler = tmpl.get("sampler", "dpmpp_2m")
            new_cfg     = result["best_cfg"]
            new_steps   = result["best_steps"] or old_steps
            new_sampler = result["best_sampler"]

            # 只有显著改善才更新，避免抖动
            if (abs(new_cfg - old_cfg) <= 0.4
                    and abs(new_steps - old_steps) < 5
                    and new_sampler == old_sampler):
                continue

            changes[scene_type] = {
                "old": {"cfg": old_cfg, "steps": old_steps, "sampler": old_sampler},
                "new": {"cfg": new_cfg, "steps": new_steps, "sampler": new_sampler},
                "avg_score":  result["avg_score"],
                "confidence": result["confidence"],
                "samples":    result["sample_count"],
            }

            if not dry_run:
                tmpl["cfg"]     = new_cfg
                tmpl["steps"]   = new_steps
                tmpl["sampler"] = new_sampler
                with self._conn() as conn:
                    conn.execute("""
                        INSERT INTO update_history
                          (scene_type, old_cfg, new_cfg, old_steps, new_steps,
                           old_sampler, new_sampler, reason)
                        VALUES (?,?,?,?,?,?,?,?)
                    """, (scene_type, old_cfg, new_cfg, old_steps, new_steps,
                          old_sampler, new_sampler,
                          f"avg={result['avg_score']:.1f} n={result['sample_count']}"))

        if not dry_run and changes:
            with open(self.story_path, "w", encoding="utf-8") as f:
                yaml.dump(story_data, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)
            print(f"  [调参] 已更新 {len(changes)} 个场景模板")

        return changes

    # ── 探索逻辑 ─────────────────────────────────────────

    def need_explore(self, scene_type: str) -> bool:
        """已由多变体主流程取代，保留作兼容接口，始终返回 False"""
        return False

    def make_variants(self, scene_type: str,
                      base_params: dict, n: int,
                      phase: str) -> list:
        """
        生成 n 套参数变体，供 pipeline 三阶段使用。
        phase="early"  → 随机覆盖未尝试的参数空间
        phase="mid"    → 在 suggest() 甜点附近小幅变体
        """
        # include_composite=True：探索参数空间时把合成页也算进"已尝试"
        records = self.records_for(scene_type, scored_only=False, include_composite=True)
        tried_cfgs     = {r["cfg"] for r in records}
        tried_samplers = {r["sampler"] for r in records}
        tried_loras    = {round(r.get("lora_strength", 1.0) * 10) / 10 for r in records}

        variants = []

        # 变体1：Agent 原始决策（必须保留）
        p1 = base_params.copy()
        p1["prefix"] = base_params["prefix"] + "_v1"
        variants.append(p1)

        if phase == "early":
            untried_cfgs     = [c for c in EXPLORE_CFG if c not in tried_cfgs]
            untried_samplers = [s for s in EXPLORE_SAMPLERS if s not in tried_samplers]
            untried_loras    = [l for l in EXPLORE_LORA_STRENGTH if l not in tried_loras]

            if untried_cfgs and len(variants) < n:
                p = base_params.copy()
                p["cfg"]    = random.choice(untried_cfgs)
                p["seed"]   = random.randint(10000, 99999)
                p["prefix"] = base_params["prefix"] + f"_v{len(variants)+1}"
                variants.append(p)

            if untried_samplers and len(variants) < n:
                p = base_params.copy()
                p["sampler"] = random.choice(untried_samplers)
                p["seed"]    = random.randint(10000, 99999)
                p["prefix"]  = base_params["prefix"] + f"_v{len(variants)+1}"
                variants.append(p)

            if untried_loras and len(variants) < n:
                p = base_params.copy()
                p["lora_strength"] = random.choice(untried_loras)
                p["seed"]          = random.randint(10000, 99999)
                p["prefix"]        = base_params["prefix"] + f"_v{len(variants)+1}"
                variants.append(p)

            # 不够 n 张时随机 CFG 补足
            while len(variants) < n:
                p = base_params.copy()
                p["cfg"]    = random.choice(EXPLORE_CFG)
                p["seed"]   = random.randint(10000, 99999)
                p["prefix"] = base_params["prefix"] + f"_v{len(variants)+1}"
                variants.append(p)

        else:  # mid：在甜点附近定向变体
            suggestion = self.suggest(scene_type, base_params)
            best_cfg   = suggestion.get("cfg", base_params["cfg"])

            if len(variants) < n:
                p = base_params.copy()
                p["cfg"]    = round(max(4.0, best_cfg - 0.3), 1)
                p["seed"]   = random.randint(10000, 99999)
                p["prefix"] = base_params["prefix"] + f"_v{len(variants)+1}"
                variants.append(p)

            if len(variants) < n:
                p = suggestion.copy()
                p["prefix"] = base_params["prefix"] + f"_v{len(variants)+1}"
                p["seed"]   = random.randint(10000, 99999)
                variants.append(p)

            if len(variants) < n:
                p = suggestion.copy()
                p["cfg"]    = round(min(9.0, best_cfg + 0.3), 1)
                p["seed"]   = random.randint(10000, 99999)
                p["prefix"] = base_params["prefix"] + f"_v{len(variants)+1}"
                variants.append(p)

        return variants[:n]

    # ── 建议参数 ─────────────────────────────────────────

    def suggest(self, scene_type: str, current_params: dict) -> dict:
        """用已学到的最优参数替换当前参数。样本不足或置信度低时返回原参数。"""
        if self.sample_count(scene_type) < MIN_SAMPLES_TO_ANALYZE:
            return current_params
        result = self.analyze(scene_type)
        if not result or result["confidence"] == "low":
            return current_params
        suggested = current_params.copy()
        if abs(result["best_cfg"] - current_params.get("cfg", 6.5)) > 0.4:
            suggested["cfg"] = result["best_cfg"]
        if result["best_sampler"] != current_params.get("sampler", "dpmpp_2m"):
            suggested["sampler"] = result["best_sampler"]
        if (result.get("best_lora") is not None
                and abs(result["best_lora"]
                        - current_params.get("lora_strength", 1.0)) > 0.1):
            suggested["lora_strength"] = result["best_lora"]
        return suggested

    # ── 报告 ─────────────────────────────────────────────

    def report(self) -> str:
        lines = ["\n" + "="*55,
                 "  参数学习状态报告",
                 "="*55]
        for scene_type in self.all_scene_types():
            n = self.sample_count(scene_type)
            lines.append(f"\n  【{scene_type}】有效样本 {n} 个")
            if n < MIN_SAMPLES_TO_ANALYZE:
                lines.append(f"  → 还需 {MIN_SAMPLES_TO_ANALYZE-n} 个样本")
                continue
            r = self.analyze(scene_type)
            if r:
                lora_str = f"{r['best_lora']}" if r.get("best_lora") else "不变"
                lines.append(
                    f"  → 最优: CFG={r['best_cfg']}  "
                    f"Steps={r['best_steps'] or '不变'}  "
                    f"Sampler={r['best_sampler']}  "
                    f"LoRA={lora_str}")
                lines.append(
                    f"     平均分={r['avg_score']}  "
                    f"置信={r['confidence']}  高分={r['high_score_n']}")

        with self._conn() as conn:
            history = conn.execute(
                "SELECT * FROM update_history "
                "ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
        if history:
            lines.append("\n  【最近更新历史（最多5条）】")
            for h in history:
                lines.append(
                    f"  {h['created_at'][:16]} {h['scene_type']}: "
                    f"cfg {h['old_cfg']}→{h['new_cfg']}  "
                    f"sampler {h['old_sampler']}→{h['new_sampler']}")
        lines.append("="*55)
        return "\n".join(lines)
