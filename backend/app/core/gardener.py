"""
Autonomous Gardener — self-improving codebase.

Scans the digital twin (Neo4j) for low-risk improvement opportunities
(dead code, high complexity), persists them as tickets, and auto-executes
the safe ones through the full 6-gate refactor workflow. Every execution
writes a flight record (audit evidence).

Execution is conservative by design:
  - only functions with ZERO callers are ever auto-removed (risk 0.0),
  - decorated functions are never auto-removed (framework registration risk),
  - high-complexity functions are tickets only (proposals, human review),
  - Gate 6 (graph integrity) is the backstop: any removal of a symbol that
    is actually referenced fails and the ticket is marked failed.
"""

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

RISK_AUTO_RUN_THRESHOLD = 0.2

_TICKETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    file TEXT NOT NULL,
    complexity INTEGER NOT NULL DEFAULT 1,
    caller_count INTEGER NOT NULL DEFAULT 0,
    risk_score REAL NOT NULL,
    status TEXT NOT NULL,
    flight_record_id TEXT,
    created_at TEXT NOT NULL,
    executed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ticket_unique
    ON tickets (kind, symbol, file);
"""


def _db_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "codegraph.db",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Gardener:
    """Scans the graph for improvement tickets and executes safe ones."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or _db_path()
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_TICKETS_SCHEMA)
        self._conn.commit()

    # ─────────────────────────────────────────
    # Scan
    # ─────────────────────────────────────────

    def scan(self, project_root: str, complexity_threshold: int = 10,
             force: bool = False) -> Dict[str, Any]:
        """Query the digital twin and create tickets for opportunities.

        Requires the Neo4j graph (the gardener is graph-evidence-first).
        """
        from app.services.neo4j_service import Neo4jService

        neo4j = Neo4jService()
        try:
            orphans = neo4j.get_orphan_functions()
            complex_fns = neo4j.get_high_complexity_functions(threshold=complexity_threshold)
        finally:
            neo4j.close()

        created, skipped = [], 0
        for fn in orphans:
            if self._ticket_exists("dead_code", fn["name"], fn["file"]):
                skipped += 1
                continue
            ticket = self._make_ticket(
                kind="dead_code",
                symbol=fn["name"],
                file=fn["file"],
                complexity=fn.get("complexity", 1),
                caller_count=0,
                risk_score=self._risk_dead_code(fn),
            )
            if ticket:
                self._insert_ticket(ticket)
                created.append(ticket)
            else:
                skipped += 1

        for fn in complex_fns:
            if self._ticket_exists("high_complexity", fn["name"], fn["file"]):
                skipped += 1
                continue
            ticket = self._make_ticket(
                kind="high_complexity",
                symbol=fn["name"],
                file=fn["file"],
                complexity=fn.get("complexity", complexity_threshold),
                caller_count=fn.get("caller_count", 0),
                risk_score=self._risk_high_complexity(fn, complexity_threshold),
            )
            if ticket:
                self._insert_ticket(ticket)
                created.append(ticket)
            else:
                skipped += 1

        logger.info(f"🌱 Gardener scan: {len(created)} new ticket(s), {skipped} skipped")
        return {"created": created, "skipped": skipped}

    @staticmethod
    def _risk_dead_code(fn: Dict[str, Any]) -> float:
        """Dead code is risk 0.0 unless decorated (framework registration)."""
        decorators = fn.get("decorators") or []
        if decorators:
            return 0.4
        return 0.0

    @staticmethod
    def _risk_high_complexity(fn: Dict[str, Any], threshold: int) -> float:
        """Complexity tickets are proposals: always above auto-run threshold."""
        return min(0.9, 0.5 + 0.05 * max(0, fn.get("complexity", threshold) - threshold))

    # ─────────────────────────────────────────
    # Execute
    # ─────────────────────────────────────────

    async def run_pending(self, project_root: str,
                          max_tickets: int = 5) -> Dict[str, Any]:
        """Auto-execute every pending ticket below the risk threshold."""
        results = []
        pending = self.pending_low_risk()
        for ticket in pending[:max_tickets]:
            results.append(await self._execute_ticket(ticket, project_root))
        return {"results": results, "executed": sum(1 for r in results if r["ok"]),
                "failed": sum(1 for r in results if not r["ok"])}

    async def _execute_ticket(self, ticket: Dict[str, Any], project_root: str) -> Dict[str, Any]:
        """Run one ticket through the full gated workflow."""
        from app.core.graph_workflow import build_workflow

        objective = (f"Remove the dead function {ticket['symbol']} from "
                     f"{ticket['file']} (it has zero callers in the codebase)")
        state = {
            "objective": objective,
            "file_name": ticket["file"],
            "function_name": ticket["symbol"],
            "permission_mode": "execute",
            "project_root": project_root,
            "plan": None,
            "affected_files": {},
            "graph_context": {},
            "proposed_changes": [],
            "validation_report": None,
            "validation_passed": False,
            "iteration_count": 0,
            "max_iterations": 3,
            "history": [],
            "ticket_id": ticket["id"],
        }

        logger.info(f"🌱 Executing ticket {ticket['id']}: {objective}")
        workflow = build_workflow()
        final_state = await workflow.ainvoke(state)

        ok = bool(final_state.get("validation_passed"))
        record_id = final_state.get("flight_record_id")
        self._update_ticket(ticket["id"], status="executed" if ok else "failed",
                            flight_record_id=record_id)

        result = {
            "ticket_id": ticket["id"],
            "symbol": ticket["symbol"],
            "file": ticket["file"],
            "ok": ok,
            "flight_record_id": record_id,
            "changes": final_state.get("proposed_changes", []),
            "validation": (final_state.get("validation_report") or {}).get("gates"),
        }
        logger.info(f"🌱 Ticket {ticket['id']}: {'✅ executed' if ok else '❌ failed'}")
        return result

    # ─────────────────────────────────────────
    # Query
    # ─────────────────────────────────────────

    def list_tickets(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM tickets WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_low_risk(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tickets WHERE status = 'pending' AND risk_score < ? "
            "ORDER BY risk_score ASC",
            (RISK_AUTO_RUN_THRESHOLD,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ─────────────────────────────────────────
    # Internal
    # ─────────────────────────────────────────

    def _ticket_exists(self, kind: str, symbol: str, file: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM tickets WHERE kind = ? AND symbol = ? AND file = ?",
            (kind, symbol, file),
        ).fetchone()
        return row is not None

    @staticmethod
    def _make_ticket(kind: str, symbol: str, file: str, complexity: int,
                     caller_count: int, risk_score: float) -> Optional[Dict[str, Any]]:
        if not symbol or not file:
            return None
        return {
            "id": "tkt_" + uuid.uuid4().hex[:10],
            "kind": kind,
            "symbol": symbol,
            "file": file,
            "complexity": complexity,
            "caller_count": caller_count,
            "risk_score": round(risk_score, 2),
            "status": "pending",
            "flight_record_id": None,
            "created_at": _now(),
            "executed_at": None,
        }

    def _insert_ticket(self, ticket: Dict[str, Any]):
        self._conn.execute(
            "INSERT OR IGNORE INTO tickets "
            "(id, kind, symbol, file, complexity, caller_count, risk_score, status, "
            " flight_record_id, created_at, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket["id"], ticket["kind"], ticket["symbol"], ticket["file"],
             ticket["complexity"], ticket["caller_count"], ticket["risk_score"],
             ticket["status"], ticket["flight_record_id"], ticket["created_at"],
             ticket["executed_at"]),
        )
        self._conn.commit()

    def _update_ticket(self, ticket_id: str, status: str, flight_record_id: Optional[str]):
        self._conn.execute(
            "UPDATE tickets SET status = ?, flight_record_id = ?, executed_at = ? WHERE id = ?",
            (status, flight_record_id, _now(), ticket_id),
        )
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def main():
    """CLI: python3 -m app.core.gardener scan|run --root <repo> [--max-tickets N]"""
    import argparse
    import asyncio
    import sys

    parser = argparse.ArgumentParser(description="Autonomous gardener CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan", help="Scan the graph for improvement tickets")
    scan_p.add_argument("--root", required=True, help="Project root (already ingested)")
    scan_p.add_argument("--threshold", type=int, default=10, help="Complexity threshold")
    run_p = sub.add_parser("run", help="Auto-execute pending low-risk tickets")
    run_p.add_argument("--root", required=True, help="Project root")
    run_p.add_argument("--max-tickets", type=int, default=5)
    list_p = sub.add_parser("list", help="List tickets")
    list_p.add_argument("--status", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gardener = Gardener()

    if args.command == "scan":
        result = gardener.scan(args.root, complexity_threshold=args.threshold)
        print(json.dumps(result, indent=2, default=str))
    elif args.command == "run":
        result = asyncio.run(gardener.run_pending(args.root, max_tickets=args.max_tickets))
        print(json.dumps(result, indent=2, default=str))
    elif args.command == "list":
        print(json.dumps(gardener.list_tickets(status=args.status), indent=2, default=str))
    gardener.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
