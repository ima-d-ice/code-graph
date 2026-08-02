"""
Flight recorder — per-change audit trail.

Every terminal point of the refactor workflow (COMMIT or ABORT) writes an
immutable record: objective -> blast radius -> plan -> diffs -> gates ->
graph delta. This is the "evidence" layer: a change is only trustworthy when
it can be replayed and audited later.

Storage: stdlib SQLite (append-only records). No new dependencies.
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_records (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    outcome TEXT NOT NULL,
    objective TEXT NOT NULL,
    file_name TEXT,
    function_name TEXT,
    plan TEXT,
    blast_radius TEXT,
    affected_files TEXT,
    changes TEXT,
    gates TEXT,
    iterations INTEGER,
    graph_stats TEXT,
    ticket_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_flight_ts ON flight_records (ts);
"""


class FlightRecorder:
    """Append-only audit store for refactor changes."""

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

    # ─────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────

    def record(self, state: Dict[str, Any], outcome: str,
               graph_stats: Optional[Dict[str, Any]] = None,
               ticket_id: Optional[str] = None) -> str:
        """Persist one flight record from a workflow state.

        Returns the record id.
        """
        record_id = uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).isoformat()

        self._conn.execute(
            """
            INSERT INTO flight_records
                (id, ts, outcome, objective, file_name, function_name, plan,
                 blast_radius, affected_files, changes, gates, iterations,
                 graph_stats, ticket_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                ts,
                outcome,
                state.get("objective", ""),
                state.get("file_name"),
                state.get("function_name"),
                self._json(state.get("plan")),
                self._json(state.get("graph_context")),
                self._json(state.get("affected_files")),
                self._json(state.get("proposed_changes")),
                self._json((state.get("validation_report") or {}).get("gates")),
                state.get("iteration_count", 0),
                self._json(graph_stats),
                ticket_id,
            ),
        )
        self._conn.commit()
        logger.info(f"🛫 Flight record {record_id}: {outcome} — {state.get('objective', '')[:60]}")
        return record_id

    # ─────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────

    def list_records(self, limit: int = 50, outcome: Optional[str] = None) -> List[Dict[str, Any]]:
        """Recent records, newest first (metadata only — no full diffs)."""
        if outcome:
            rows = self._conn.execute(
                "SELECT id, ts, outcome, objective, file_name, function_name, iterations, ticket_id "
                "FROM flight_records WHERE outcome = ? ORDER BY ts DESC LIMIT ?",
                (outcome, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, ts, outcome, objective, file_name, function_name, iterations, ticket_id "
                "FROM flight_records ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        """Full record including diffs, gates and graph delta."""
        row = self._conn.execute(
            "SELECT * FROM flight_records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return None
        return {k: self._unjson(v) for k, v in dict(row).items()}

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM flight_records").fetchone()[0]

    # ─────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────

    @staticmethod
    def _json(value) -> Optional[str]:
        if value is None:
            return None
        try:
            return json.dumps(value, default=str)
        except TypeError:
            return json.dumps(str(value))

    @staticmethod
    def _unjson(value) -> Any:
        if value is None:
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
