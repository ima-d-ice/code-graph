"""
RefactorBench — internal evaluation harness (store + task definitions).

The industry pattern (SWE-bench / OpenHands research): "public benchmarks are
filters, internal evals are the verdict." RefactorBench is the internal eval:
parameterized tasks (rename, dead-code removal) × repo sizes × discovery mode
(graph-first vs prompt-only grep), each trial gold-verified (call sites == 0,
decoys untouched, gates passed).

The graph-vs-grep A/B isolates the variable that matters: discovery strategy.
Everything else (gates, executor, deterministic fallback) is identical.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    task TEXT NOT NULL,
    size INTEGER NOT NULL,
    mode TEXT NOT NULL,
    trial INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    tokens_total INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    blast_expected INTEGER,
    blast_found INTEGER,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_bench_task ON benchmark_runs (task, size, mode);
"""

# Task definitions. Each returns (objective, file_name, function_name).
TASKS = {
    "rename": "rename",
    "remove_dead": "remove_dead",
}


def rename_task() -> Dict[str, str]:
    return {
        "objective": ("Rename compute_sum to calculate_total across the codebase "
                      "and update every call site"),
        "file_name": "lib/utils.py",
        "function_name": "compute_sum",
    }


def remove_dead_task(caller_index: int = 1) -> Dict[str, str]:
    return {
        "objective": (f"Remove the dead function check_threshold_{caller_index} "
                      f"from callers/caller_{caller_index:03d}.py"),
        "file_name": f"callers/caller_{caller_index:03d}.py",
        "function_name": f"check_threshold_{caller_index}",
    }


def expected_blast_radius(task: str, size: int) -> int:
    """Ground-truth affected files for a task at a repo size (0.8 caller ratio)."""
    n_callers = max(1, int(size * 0.8))
    if task == "rename":
        return n_callers + 1  # callers + lib/utils.py
    if task == "remove_dead":
        return 1  # just the caller file
    return 0


class BenchmarkStore:
    """Scoreboard for RefactorBench trials."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                "data", "codegraph.db",
            )
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, task: str, size: int, mode: str, trial: int, passed: bool,
               duration_ms: int, tokens_total: int, cost_usd: float,
               fallback_used: bool, blast_expected: Optional[int],
               blast_found: Optional[int], details: Optional[Dict] = None) -> str:
        import uuid
        record_id = uuid.uuid4().hex[:12]
        self._conn.execute(
            """
            INSERT INTO benchmark_runs
                (id, ts, task, size, mode, trial, passed, duration_ms,
                 tokens_total, cost_usd, fallback_used, blast_expected,
                 blast_found, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id, datetime.now(timezone.utc).isoformat(),
                task, size, mode, trial, 1 if passed else 0,
                int(duration_ms), tokens_total, round(cost_usd, 6),
                1 if fallback_used else 0, blast_expected, blast_found,
                json.dumps(details) if details else None,
            ),
        )
        self._conn.commit()
        return record_id

    def summary(self) -> List[Dict[str, Any]]:
        """Resolution rate + blast accuracy by (task, size, mode)."""
        rows = self._conn.execute(
            """
            SELECT task, size, mode, COUNT(*) as trials,
                   SUM(passed) as passed,
                   AVG(duration_ms) as avg_duration_ms,
                   AVG(tokens_total) as avg_tokens,
                   AVG(cost_usd) as avg_cost_usd,
                   SUM(fallback_used) as fallbacks
            FROM benchmark_runs
            GROUP BY task, size, mode
            ORDER BY task, size, mode
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["resolution_rate"] = round(d["passed"] / d["trials"], 3) if d["trials"] else 0
            out.append(d)
        return out

    def blast_accuracy(self) -> List[Dict[str, Any]]:
        """How close did each discovery mode come to the true blast radius?"""
        rows = self._conn.execute(
            """
            SELECT task, size, mode, AVG(blast_found) as avg_found,
                   AVG(blast_expected) as avg_expected
            FROM benchmark_runs
            WHERE blast_expected IS NOT NULL
            GROUP BY task, size, mode
            ORDER BY task, size, mode
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            expected = d.get("avg_expected") or 0
            found = d.get("avg_found") or 0
            d["blast_accuracy"] = (
                round(min(found / expected, 1.0), 3) if expected else None
            )
            out.append(d)
        return out

    def moat_summary(self) -> Dict[str, Any]:
        """The A/B: graph-first vs prompt-only, everything else held equal."""
        summary = self.summary()
        graph = [s for s in summary if s["mode"] == "graph"]
        grep = [s for s in summary if s["mode"] == "grep"]

        def rate(rows):
            return (sum(r["passed"] for r in rows) / sum(r["trials"] for r in rows)
                    if rows and sum(r["trials"] for r in rows) else 0)

        g_rate, p_rate = rate(graph), rate(grep)
        g_blast = self.blast_accuracy()
        ga = [b for b in g_blast if b["mode"] == "graph"]
        pa = [b for b in g_blast if b["mode"] == "grep"]
        g_acc = sum(b["blast_accuracy"] or 0 for b in ga) / len(ga) if ga else None
        p_acc = sum(b["blast_accuracy"] or 0 for b in pa) / len(pa) if pa else None

        return {
            "graph": {
                "resolution_rate": round(g_rate, 3),
                "blast_accuracy": round(g_acc, 3) if g_acc is not None else None,
                "trials": sum(r["trials"] for r in graph),
            },
            "grep": {
                "resolution_rate": round(p_rate, 3),
                "blast_accuracy": round(p_acc, 3) if p_acc is not None else None,
                "trials": sum(r["trials"] for r in grep),
            },
            "delta": {
                "resolution_rate": round(g_rate - p_rate, 3),
                "blast_accuracy": round(g_acc - p_acc, 3)
                if g_acc is not None and p_acc is not None else None,
            },
            "verdict": (
                "GRAPH WINS"
                if g_rate > p_rate or (g_acc is not None and p_acc is not None
                                       and g_acc > p_acc)
                else "NO ADVANTAGE DETECTED"
            ),
        }

    def recent_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM benchmark_runs ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
