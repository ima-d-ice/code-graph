"""Tests for telemetry + code health (offline; no LLM/Neo4j needed)."""
import json

from app.core.telemetry import Telemetry, estimate_cost, MODEL_PRICING
from app.core.code_health import CodeHealth, _rating_for


# ───────────────────────── telemetry ─────────────────────────

def test_estimate_cost_uses_model_pricing():
    pi, po = MODEL_PRICING["llama-3.1-8b-instant"]
    tokens = 1_000_000
    expected = (tokens * 0.75 * pi + tokens * 0.25 * po) / 1_000_000
    assert abs(estimate_cost(tokens, "llama-3.1-8b-instant") - expected) < 1e-9


def test_estimate_cost_default_model():
    assert estimate_cost(0, "unknown-model") == 0.0
    assert estimate_cost(1_000_000, "unknown-model") > 0


def test_record_and_aggregate(tmp_path):
    t = Telemetry(str(tmp_path / "m.db"))
    t.record_run("run_1", "Rename x to y", "committed", 12_000, 1,
                 {"llama-3.1-8b-instant": 50_000}, 3,
                 {"PLAN": 1000, "COMMIT": 200}, {"syntax": {"status": "PASS"}},
                 fallback_used=True)
    t.record_run("run_2", "Remove dead fn", "aborted", 6_000, 3,
                 {"llama-3.1-8b-instant": 10_000, "gpt-oss-120b": 5_000}, 2,
                 {}, None, fallback_used=False)

    agg = t.aggregate()
    assert agg["runs"] == 2
    assert agg["outcome_breakdown"] == {"committed": 1, "aborted": 1}
    assert agg["fallback_runs"] == 1
    assert agg["total_tokens"] == 65_000
    assert agg["total_cost_usd"] > 0
    assert agg["median_duration_ms"] == 9_000
    t.close()


def test_cost_summary_per_model(tmp_path):
    t = Telemetry(str(tmp_path / "m.db"))
    t.record_run("r1", "o", "committed", 1_000, 0,
                 {"llama-3.1-8b-instant": 10_000}, 1, {}, None, False)
    t.record_run("r2", "o", "committed", 1_000, 0,
                 {"llama-3.1-8b-instant": 20_000}, 1, {}, None, False)

    summary = t.cost_summary()
    assert summary["total_tokens"] == 30_000
    assert len(summary["per_model"]) == 1
    assert summary["per_model"][0]["tokens"] == 30_000
    t.close()


def test_prometheus_export(tmp_path):
    t = Telemetry(str(tmp_path / "m.db"))
    t.record_run("r1", "o", "committed", 1_000, 0, {"m": 100}, 1, {}, None, False)
    text = t.to_prometheus()
    assert 'codegraph_runs_total{outcome="committed"} 1' in text
    assert "# TYPE codegraph_runs_total counter" in text
    t.close()


# ───────────────────────── code health ─────────────────────────

def test_rating_bands():
    assert _rating_for(0.04) == "A"
    assert _rating_for(0.08) == "B"
    assert _rating_for(0.19) == "C"
    assert _rating_for(0.49) == "D"
    assert _rating_for(0.99) == "E"


def test_score_health_math():
    health = CodeHealth._score(
        complexities=[2, 3, 4, 5, 6, 11, 12, 13, 14, 20],
        orphans=[{"name": "x"}],
        hotspots=[{"complexity": 14, "fan_in": 5, "name": "h", "file": "f.py",
                   "score": 70}],
        stats={"functions": 10, "classes": 1, "modules": 1},
    )
    assert health["complexity"]["mean"] > 5
    assert health["complexity"]["max"] == 20
    assert health["complexity"]["p90"] >= 13
    assert health["dead_code"]["orphans"] == 1
    assert health["dead_code"]["density"] == 0.1
    assert health["hotspots"][0]["name"] == "h"
    assert health["tech_debt"]["minutes"] > 0
    assert health["tech_debt"]["rating"] in "ABCDE"
    assert 0 <= health["health_score"] <= 10


def test_score_healthy_codebase_scores_high():
    health = CodeHealth._score(
        complexities=[1, 1, 2, 2, 2, 3, 3, 3, 3, 4],
        orphans=[],
        hotspots=[],
        stats={"functions": 10, "classes": 0, "modules": 1},
    )
    assert health["health_score"] > 7
    assert health["tech_debt"]["rating"] == "A"


def test_snapshots_and_trends(tmp_path):
    ch = CodeHealth(str(tmp_path / "h.db"))
    snap1 = ch._score([2, 3, 4], [], [], {"functions": 3, "classes": 0, "modules": 1})
    snap2 = ch._score([2, 3, 4, 20], [], [], {"functions": 4, "classes": 0, "modules": 1})
    ch._conn.execute(
        "INSERT INTO health_snapshots (id, ts, snapshot) VALUES (?, ?, ?)",
        ("s1", "2026-01-01T00:00:00+00:00", json.dumps(snap1)),
    )
    ch._conn.execute(
        "INSERT INTO health_snapshots (id, ts, snapshot) VALUES (?, ?, ?)",
        ("s2", "2026-01-02T00:00:00+00:00", json.dumps(snap2)),
    )
    ch._conn.commit()

    trends = ch.trends(days=365)
    assert [t["id"] for t in trends] == ["s1", "s2"]
    assert trends[0]["health_score"] is not None

    latest = ch.latest()
    assert latest["id"] == "s2"
    ch.close()
