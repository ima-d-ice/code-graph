"""Tests for the flight recorder (per-change audit trail).

Fully offline: uses a temp SQLite db, no LLM / Neo4j needed.
"""
import os

from app.core.flight_recorder import FlightRecorder


def _sample_state(validated=True):
    return {
        "objective": "Rename compute_sum to calculate_total in utils.py",
        "file_name": "utils.py",
        "function_name": "compute_sum",
        "plan": {"steps": ["find", "rename", "propagate"]},
        "graph_context": {"symbol": "compute_sum", "blast_radius": 3},
        "affected_files": {"utils.py": "content", "caller.py": "content"},
        "proposed_changes": [
            {"file_path": "utils.py", "content": "def calculate_total(a, b): ..."}
        ],
        "validation_report": {
            "overall": "PASS" if validated else "FAIL",
            "gates": {
                "syntax": {"status": "PASS"},
                "graph_integrity": {"status": "PASS" if validated else "FAIL"},
            },
            "iteration": 0,
        },
        "iteration_count": 1,
    }


def test_record_and_get_roundtrip(tmp_path):
    rec = FlightRecorder(str(tmp_path / "rec.db"))
    state = _sample_state()
    record_id = rec.record(state, outcome="committed")

    record = rec.get_record(record_id)
    assert record is not None
    assert record["outcome"] == "committed"
    assert record["objective"] == state["objective"]
    assert record["file_name"] == "utils.py"
    assert record["function_name"] == "compute_sum"
    assert record["plan"] == state["plan"]
    assert record["blast_radius"] == state["graph_context"]
    assert record["affected_files"] == state["affected_files"]
    assert record["changes"] == state["proposed_changes"]
    assert record["gates"]["graph_integrity"]["status"] == "PASS"
    assert record["iterations"] == 1
    rec.close()


def test_aborted_outcome_recorded(tmp_path):
    rec = FlightRecorder(str(tmp_path / "rec.db"))
    record_id = rec.record(_sample_state(validated=False), outcome="aborted")

    record = rec.get_record(record_id)
    assert record["outcome"] == "aborted"
    assert record["gates"]["graph_integrity"]["status"] == "FAIL"
    rec.close()


def test_graph_delta_and_ticket_link(tmp_path):
    rec = FlightRecorder(str(tmp_path / "rec.db"))
    record_id = rec.record(
        _sample_state(),
        outcome="committed",
        graph_stats={"before": {"nodes": 10}, "after": {"nodes": 11}, "delta": {"nodes": 1}},
        ticket_id="tkt_abc",
    )

    record = rec.get_record(record_id)
    assert record["graph_stats"]["delta"]["nodes"] == 1
    assert record["ticket_id"] == "tkt_abc"
    rec.close()


def test_list_records_newest_first_and_filter(tmp_path):
    rec = FlightRecorder(str(tmp_path / "rec.db"))
    rec.record(_sample_state(), outcome="committed")
    rec.record(_sample_state(validated=False), outcome="aborted")
    rec.record(_sample_state(), outcome="committed")

    all_records = rec.list_records()
    assert len(all_records) == 3
    committed = rec.list_records(outcome="committed")
    assert len(committed) == 2
    assert all(r["outcome"] == "committed" for r in committed)
    # newest first
    assert all_records[0]["ts"] >= all_records[-1]["ts"]
    rec.close()


def test_unknown_record_returns_none(tmp_path):
    rec = FlightRecorder(str(tmp_path / "rec.db"))
    assert rec.get_record("does-not-exist") is None
    rec.close()


def test_recorder_survives_reopen(tmp_path):
    path = str(tmp_path / "rec.db")
    rec = FlightRecorder(path)
    record_id = rec.record(_sample_state(), outcome="committed")
    rec.close()

    rec2 = FlightRecorder(path)
    assert rec2.get_record(record_id)["outcome"] == "committed"
    assert rec2.count() == 1
    rec2.close()
