"""Tests for the autonomous gardener: ticket lifecycle + risk scoring.

Offline: ticket store and risk scoring need no LLM / Neo4j; the graph
queries themselves are integration-tested via the demo instead.
"""
import pytest

from app.core.gardener import Gardener, RISK_AUTO_RUN_THRESHOLD


@pytest.fixture
def gardener(tmp_path):
    g = Gardener(str(tmp_path / "codegraph.db"))
    yield g
    g.close()


def test_scan_dedupes_across_calls(gardener):
    # Directly insert via scan-mocking: call the internal pieces instead
    t1 = gardener._make_ticket("dead_code", "check_threshold_1", "callers/caller_001.py",
                               2, 0, 0.0)
    assert t1 is not None
    gardener._insert_ticket(t1)
    assert gardener._ticket_exists("dead_code", "check_threshold_1", "callers/caller_001.py")
    assert not gardener._ticket_exists("dead_code", "other_fn", "callers/caller_001.py")


def test_risk_dead_code_plain_vs_decorated():
    assert Gardener._risk_dead_code({"decorators": []}) == 0.0
    assert Gardener._risk_dead_code({"decorators": ["staticmethod"]}) == 0.4


def test_risk_high_complexity_always_proposal():
    risk = Gardener._risk_high_complexity({"complexity": 12}, threshold=10)
    assert risk >= RISK_AUTO_RUN_THRESHOLD
    assert 0.5 <= risk <= 0.9


def test_pending_low_risk_only_below_threshold(gardener):
    low = gardener._make_ticket("dead_code", "low_fn", "a.py", 1, 0, 0.0)
    high = gardener._make_ticket("dead_code", "deco_fn", "b.py", 1, 0, 0.4)
    gardener._insert_ticket(low)
    gardener._insert_ticket(high)

    pending = gardener.pending_low_risk()
    assert [t["symbol"] for t in pending] == ["low_fn"]


def test_ticket_status_transitions(gardener):
    t = gardener._make_ticket("dead_code", "fn", "a.py", 1, 0, 0.0)
    gardener._insert_ticket(t)
    gardener._update_ticket(t["id"], status="executed", flight_record_id="rec_123")

    rows = gardener.list_tickets(status="executed")
    assert len(rows) == 1
    assert rows[0]["flight_record_id"] == "rec_123"
    assert rows[0]["executed_at"] is not None

    all_rows = gardener.list_tickets()
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "executed"


def test_make_ticket_rejects_empty_symbol(gardener):
    assert gardener._make_ticket("dead_code", "", "a.py", 1, 0, 0.0) is None
    assert gardener._make_ticket("dead_code", "fn", "", 1, 0, 0.0) is None


def test_insert_or_ignore_preserves_first_ticket(gardener):
    t = gardener._make_ticket("dead_code", "fn", "a.py", 1, 0, 0.0)
    t2 = dict(t)
    t2["id"] = "tkt_other"
    gardener._insert_ticket(t)
    gardener._insert_ticket(t2)

    rows = gardener.list_tickets()
    assert len(rows) == 1
    assert rows[0]["id"] == t["id"]
