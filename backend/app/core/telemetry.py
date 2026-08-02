"""
Telemetry — run-level metrics for the refactor pipeline.

Captures per-run economics and behavior:
  - wall-clock duration + per-node timings
  - real token counts per model (from model-reported usage_metadata)
  - estimated cost per run (Groq public pricing, blended in:out = 3:1,
    the typical agent-loop ratio; documented assumption)
  - repair iterations, gate outcomes, deterministic-fallback usage

Stored in SQLite alongside the flight recorder (same codegraph.db).
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# Groq public pricing, USD per 1M tokens (input / output).
# Unknown models fall back to a conservative mid-tier blend.
MODEL_PRICING = {
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.15, 0.60),
    "meta-llama/llama-4-maverick-17b-128e-instruct": (0.25, 1.00),
    "gpt-oss-120b": (0.88, 0.88),
    "gpt-oss-20b": (0.15, 0.15),
    "meta-llama/llama-prompt-guard-2-86m": (0.0, 0.0),
    "default": (0.15, 0.60),
}

# Documented split of total tokens into input/output for cost estimation
# when only totals are available (in:out ≈ 3:1 for agent loops).
INPUT_TOKEN_FRACTION = 0.75

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_metrics (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    run_id TEXT,
    objective TEXT,
    outcome TEXT,
    duration_ms INTEGER NOT NULL,
    iterations INTEGER NOT NULL DEFAULT 0,
    tokens_total INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    tokens_by_model TEXT,
    node_timings TEXT,
    gates TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_run_metrics_ts ON run_metrics (ts);
"""


def estimate_cost(tokens_total: int, model: str) -> float:
    """Estimate USD cost for a token total under a documented in:out split."""
    price_in, price_out = MODEL_PRICING.get(model, MODEL_PRICING["default"])
    tokens_in = tokens_total * INPUT_TOKEN_FRACTION
    tokens_out = tokens_total - tokens_in
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


class Telemetry:
    """Append-only run metrics store."""

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

    def record_run(self, run_id: str, objective: str, outcome: str,
                   duration_ms: int, iterations: int,
                   tokens_by_model: Dict[str, int], requests: int,
                   node_timings: Dict[str, float], gates: Optional[Dict],
                   fallback_used: bool) -> str:
        """Persist one run's metrics. Returns the record id."""
        record_id = uuid.uuid4().hex[:12]
        tokens_total = sum(tokens_by_model.values())
        cost = sum(
            estimate_cost(t, model) for model, t in tokens_by_model.items()
        )

        self._conn.execute(
            """
            INSERT INTO run_metrics
                (id, ts, run_id, objective, outcome, duration_ms, iterations,
                 tokens_total, requests, cost_usd, tokens_by_model,
                 node_timings, gates, fallback_used)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                datetime.now(timezone.utc).isoformat(),
                run_id,
                objective[:200],
                outcome,
                int(duration_ms),
                iterations,
                tokens_total,
                requests,
                round(cost, 6),
                json.dumps(tokens_by_model),
                json.dumps(node_timings),
                json.dumps(gates),
                1 if fallback_used else 0,
            ),
        )
        self._conn.commit()
        return record_id

    # ─────────────────────────────────────────
    # Aggregates
    # ─────────────────────────────────────────

    def aggregate(self, limit: int = 50, outcome: Optional[str] = None) -> Dict[str, Any]:
        """Summary statistics over recent runs."""
        where = "WHERE outcome = ?" if outcome else ""
        params = (outcome,) if outcome else ()

        row = self._conn.execute(
            f"""
            SELECT COUNT(*) as runs,
                   AVG(duration_ms) as avg_duration_ms,
                   AVG(tokens_total) as avg_tokens,
                   SUM(tokens_total) as total_tokens,
                   SUM(cost_usd) as total_cost_usd,
                   AVG(cost_usd) as avg_cost_usd,
                   AVG(iterations) as avg_iterations,
                   SUM(fallback_used) as fallback_runs,
                   AVG(requests) as avg_requests
            FROM run_metrics {where}
            """,
            params,
        ).fetchone()

        durations = [
            r[0]
            for r in self._conn.execute(
                f"SELECT duration_ms FROM run_metrics {where} ORDER BY duration_ms",
                params,
            ).fetchall()
        ]
        agg = dict(row) or {}
        n = len(durations)
        if n:
            if n % 2 == 1:
                agg["median_duration_ms"] = durations[n // 2]
            else:
                agg["median_duration_ms"] = (
                    durations[n // 2 - 1] + durations[n // 2]
                ) / 2
        else:
            agg["median_duration_ms"] = None

        recent = self._conn.execute(
            f"""
            SELECT outcome, COUNT(*) as n FROM run_metrics {where}
            GROUP BY outcome ORDER BY n DESC
            """,
            params,
        ).fetchall()
        agg["outcome_breakdown"] = {r["outcome"]: r["n"] for r in recent}

        latest = self._conn.execute(
            "SELECT id, ts, objective, outcome, duration_ms, tokens_total, "
            "cost_usd, iterations, fallback_used "
            "FROM run_metrics ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        agg["recent_runs"] = [dict(r) for r in latest]
        return agg

    def cost_summary(self) -> Dict[str, Any]:
        """Cost and usage per model over all runs."""
        rows = self._conn.execute(
            "SELECT tokens_by_model FROM run_metrics WHERE tokens_by_model IS NOT NULL"
        ).fetchall()
        per_model: Dict[str, int] = {}
        for r in rows:
            for model, tokens in (json.loads(r["tokens_by_model"]) or {}).items():
                per_model[model] = per_model.get(model, 0) + tokens
        breakdown = [
            {
                "model": model,
                "tokens": tokens,
                "cost_usd": round(estimate_cost(tokens, model), 6),
            }
            for model, tokens in sorted(per_model.items(), key=lambda kv: -kv[1])
        ]
        return {
            "per_model": breakdown,
            "total_cost_usd": round(sum(b["cost_usd"] for b in breakdown), 6),
            "total_tokens": sum(b["tokens"] for b in breakdown),
        }

    def to_prometheus(self) -> str:
        """Expose run metrics in Prometheus text exposition format (zero deps)."""
        agg = self.aggregate()
        lines = [
            "# HELP codegraph_runs_total Total workflow runs recorded.",
            "# TYPE codegraph_runs_total counter",
            f'codegraph_runs_total{{outcome="committed"}} '
            f"{agg['outcome_breakdown'].get('committed', 0)}",
            f'codegraph_runs_total{{outcome="aborted"}} '
            f"{agg['outcome_breakdown'].get('aborted', 0)}",
            "# HELP codegraph_run_duration_ms Refactor workflow wall-clock duration.",
            "# TYPE codegraph_run_duration_ms gauge",
            f'codegraph_run_duration_ms{{stat="avg"}} '
            f"{agg['avg_duration_ms'] or 0}",
            f'codegraph_run_duration_ms{{stat="median"}} '
            f"{agg['median_duration_ms'] or 0}",
            "# HELP codegraph_cost_usd_total Estimated total LLM spend.",
            "# TYPE codegraph_cost_usd_total counter",
            f"codegraph_cost_usd_total {agg['total_cost_usd'] or 0}",
            "# HELP codegraph_tokens_total Total tokens consumed.",
            "# TYPE codegraph_tokens_total counter",
            f"codegraph_tokens_total {agg['total_tokens'] or 0}",
            "# HELP codegraph_fallback_runs Runs where the deterministic engine"
            " rescued a failed LLM.",
            "# TYPE codegraph_fallback_runs counter",
            f"codegraph_fallback_runs {agg['fallback_runs'] or 0}",
        ]
        return "\n".join(lines) + "\n"

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
