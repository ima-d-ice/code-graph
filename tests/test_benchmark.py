"""Tests for the RefactorBench store + scoreboard math (offline)."""
from app.core.benchmark import (
    BenchmarkStore,
    expected_blast_radius,
    rename_task,
    remove_dead_task,
)


def test_task_definitions():
    r = rename_task()
    assert r["function_name"] == "compute_sum"
    assert "calculate_total" in r["objective"]

    d = remove_dead_task(3)
    assert d["function_name"] == "check_threshold_3"
    assert d["file_name"] == "callers/caller_003.py"


def test_expected_blast_radius():
    assert expected_blast_radius("rename", 10) == 9      # 8 callers + utils.py
    assert expected_blast_radius("rename", 50) == 41
    assert expected_blast_radius("remove_dead", 10) == 1


def test_record_and_summary(tmp_path):
    store = BenchmarkStore(str(tmp_path / "b.db"))
    store.record("rename", 10, "graph", 1, True, 12_000, 50_000, 0.004, False,
                 blast_expected=9, blast_found=9)
    store.record("rename", 10, "graph", 2, True, 11_000, 40_000, 0.003, False,
                 blast_expected=9, blast_found=9)
    store.record("rename", 10, "grep", 1, False, 20_000, 60_000, 0.005, True,
                 blast_expected=9, blast_found=5)

    summary = store.summary()
    graph_row = [s for s in summary if s["mode"] == "graph"][0]
    grep_row = [s for s in summary if s["mode"] == "grep"][0]
    assert graph_row["resolution_rate"] == 1.0
    assert grep_row["resolution_rate"] == 0.0
    assert grep_row["fallbacks"] == 1
    assert graph_row["avg_cost_usd"] > 0

    blast = store.blast_accuracy()
    bm = {b["mode"]: b for b in blast}
    assert bm["graph"]["blast_accuracy"] == 1.0
    assert bm["grep"]["blast_accuracy"] == round(5 / 9, 3)

    moat = store.moat_summary()
    assert moat["graph"]["resolution_rate"] == 1.0
    assert moat["grep"]["resolution_rate"] == 0.0
    assert moat["delta"]["resolution_rate"] == 1.0
    assert moat["delta"]["blast_accuracy"] > 0
    assert moat["verdict"] == "GRAPH WINS"
    store.close()


def test_moat_no_advantage_when_equal(tmp_path):
    store = BenchmarkStore(str(tmp_path / "b.db"))
    store.record("rename", 10, "graph", 1, True, 1_000, 1_000, 0.0, False, 9, 9)
    store.record("rename", 10, "grep", 1, True, 1_000, 1_000, 0.0, False, 9, 9)

    moat = store.moat_summary()
    assert moat["delta"]["resolution_rate"] == 0.0
    assert moat["delta"]["blast_accuracy"] == 0.0
    assert moat["verdict"] == "NO ADVANTAGE DETECTED"
    store.close()


def test_recent_runs_order(tmp_path):
    store = BenchmarkStore(str(tmp_path / "b.db"))
    store.record("rename", 10, "graph", 1, True, 1_000, 1_000, 0.0, False, 9, 9)
    store.record("remove_dead", 10, "graph", 1, True, 1_000, 1_000, 0.0, False, 1, 1)
    recent = store.recent_runs()
    assert len(recent) == 2
    assert recent[0]["task"] == "remove_dead"  # newest first
    store.close()
