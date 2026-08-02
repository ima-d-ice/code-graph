"""
Code Health — codebase quality scoring over the knowledge graph.

Industry-standard metric family (SonarQube / CodeScene nomenclature):

  - complexity stats: mean / P90 / max cyclomatic complexity
  - hotspots:        complexity × fan-in (CodeScene's "Hotspot" model —
                     files with high complexity AND many callers are where
                     maintenance cost concentrates)
  - dead-code density: orphaned functions / total functions
  - technical debt:  estimated remediation minutes over the hotspot set
  - debt ratio:      debt / cost-to-develop, SonarQube formula
  - maintainability rating: A–E from the debt ratio (SonarQube scale)

Health snapshots are persisted so trends can be tracked (CodeScene: "prioritize
trends over absolute values") — the gardener's removals must show up as an
improving series, not a single number.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

# SonarQube maintainability-rating scale (debt ratio thresholds)
DEBT_RATIO_BANDS = [
    (0.05, "A"),
    (0.10, "B"),
    (0.20, "C"),
    (0.50, "D"),
    (float("inf"), "E"),
]

# Estimated minutes to remediate one complexity point over the threshold
MINUTES_PER_COMPLEXITY_POINT = 15
# Complexity beyond this is "expensive" (SonarQube default: functions > 10)
COMPLEXITY_EXPENSIVE = 10
# Estimated development minutes per line of code (SonarQube default: 30)
MINUTES_PER_LINE = 30

_SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS health_snapshots (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health_snapshots (ts);
"""


def _rating_for(debt_ratio: float) -> str:
    for threshold, rating in DEBT_RATIO_BANDS:
        if debt_ratio < threshold:
            return rating
    return "E"


class CodeHealth:
    """Computes codebase health from the Neo4j digital twin."""

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
        self._conn.executescript(_SNAPSHOT_SCHEMA)
        self._conn.commit()

    # ─────────────────────────────────────────
    # Compute
    # ─────────────────────────────────────────

    def compute(self, project_root: Optional[str] = None) -> Dict[str, Any]:
        """Compute a health snapshot from the graph (and parser fallback).

        Returns a dict with complexity stats, hotspots, debt and rating.
        """
        from app.services.neo4j_service import Neo4jService

        neo4j = Neo4jService()
        try:
            stats = neo4j.get_stats()
            orphans = neo4j.get_orphan_functions()
            hotspots = self._hotspots(neo4j)
            complexities = self._complexities(neo4j)
        except Exception as e:
            logger.warning(f"Graph unavailable for health compute: {e}")
            complexities = self._complexities_from_files(project_root)
            stats, orphans, hotspots = {"functions": len(complexities)}, [], []
        finally:
            neo4j.close()

        return self._score(complexities, orphans, hotspots, stats)

    @staticmethod
    def _hotspots(neo4j) -> List[Dict[str, Any]]:
        """CodeScene model: complexity × fan-in, top offenders."""
        rows = neo4j.session_run(
            """
            MATCH (fn:Function)
            OPTIONAL MATCH (caller)-[:CALLS]->(fn)
            WITH fn, COUNT(DISTINCT caller) as fan_in
            WHERE fan_in > 0
            RETURN fn.name as name, fn.file as file, fn.complexity as complexity,
                   fan_in
            ORDER BY (fn.complexity * fan_in) DESC
            LIMIT 20
            """
        )
        return [
            {
                "name": r.get("name"),
                "file": r.get("file"),
                "complexity": r.get("complexity") or 1,
                "fan_in": r.get("fan_in") or 0,
                "score": (r.get("complexity") or 1) * (r.get("fan_in") or 0),
            }
            for r in rows
        ]

    @staticmethod
    def _complexities(neo4j) -> List[int]:
        rows = neo4j.session_run(
            "MATCH (fn:Function) RETURN fn.complexity as complexity"
        )
        return [r.get("complexity") or 1 for r in rows]

    @staticmethod
    def _complexities_from_files(project_root: Optional[str]) -> List[int]:
        """Parser fallback when Neo4j is down (fail-open, like the rest)."""
        if not project_root or not os.path.isdir(project_root):
            return []
        from app.services.parser_service import ParserService
        parser = ParserService()
        complexities = []
        for root, _, files in os.walk(project_root):
            if any(part.startswith(".") for part in root.split(os.sep)):
                continue
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(root, f)
                    try:
                        meta = parser.parse_file(path)
                        for fn in meta.get("functions", []):
                            complexities.append(fn.get("complexity", 1))
                    except Exception:
                        continue
        return complexities

    @staticmethod
    def _score(complexities: List[int], orphans: List[Dict],
               hotspots: List[Dict], stats: Dict[str, int]) -> Dict[str, Any]:
        n = len(complexities) or 1
        mean = sum(complexities) / n
        sorted_c = sorted(complexities)
        p90 = sorted_c[int(n * 0.9) - 1] if sorted_c else 0
        expensive = [c for c in complexities if c > COMPLEXITY_EXPENSIVE]

        total_functions = stats.get("functions", n)
        dead_density = len(orphans) / total_functions if total_functions else 0.0

        # Technical debt: hotspots are where maintenance cost concentrates
        debt_minutes = sum(
            max(0, h["complexity"] - COMPLEXITY_EXPENSIVE) * MINUTES_PER_COMPLEXITY_POINT
            for h in hotspots
        )
        lines_of_code = sum(stats.get(k, 0) for k in ("functions", "classes", "modules"))
        cost_to_develop = lines_of_code * MINUTES_PER_LINE or 1
        debt_ratio = debt_minutes / cost_to_develop
        rating = _rating_for(debt_ratio)

        # Composite health score (0–10, CodeScene-style scale)
        complexity_score = max(0.0, 10 - 2.0 * max(0.0, (mean - 4)))
        dead_code_score = max(0.0, 10 - 25.0 * dead_density)
        debt_score = max(0.0, 10 - 40.0 * debt_ratio)
        health_score = round(
            0.5 * complexity_score + 0.25 * dead_code_score + 0.25 * debt_score,
            2,
        )

        return {
            "functions": total_functions,
            "complexity": {
                "mean": round(mean, 2),
                "p90": p90,
                "max": max(complexities) if complexities else 0,
                "over_threshold": len(expensive),
            },
            "dead_code": {
                "orphans": len(orphans),
                "density": round(dead_density, 4),
            },
            "hotspots": hotspots[:10],
            "tech_debt": {
                "minutes": int(debt_minutes),
                "ratio": round(debt_ratio, 4),
                "rating": rating,
            },
            "health_score": health_score,
        }

    # ─────────────────────────────────────────
    # Snapshots & trends
    # ─────────────────────────────────────────

    def snapshot(self, project_root: Optional[str] = None) -> Dict[str, Any]:
        """Compute and persist a health snapshot. Returns the snapshot."""
        import uuid
        health = self.compute(project_root)
        snap_id = uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO health_snapshots (id, ts, snapshot) VALUES (?, ?, ?)",
            (snap_id, datetime.now(timezone.utc).isoformat(), json.dumps(health)),
        )
        self._conn.commit()
        return {"id": snap_id, "ts": health["ts"] if "ts" in health else None,
                **health}

    def trends(self, days: int = 30) -> List[Dict[str, Any]]:
        """Health snapshots within the window, oldest first."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT id, ts, snapshot FROM health_snapshots "
            "WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,),
        ).fetchall()
        out = []
        for r in rows:
            snap = json.loads(r["snapshot"])
            out.append({
                "id": r["id"],
                "ts": r["ts"],
                "health_score": snap.get("health_score"),
                "complexity_mean": snap.get("complexity", {}).get("mean"),
                "dead_code_density": snap.get("dead_code", {}).get("density"),
                "debt_ratio": snap.get("tech_debt", {}).get("ratio"),
                "rating": snap.get("tech_debt", {}).get("rating"),
            })
        return out

    def latest(self) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT id, ts, snapshot FROM health_snapshots ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "ts": row["ts"], **json.loads(row["snapshot"])}

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


def main():
    """CLI: python3 -m app.core.code_health compute|snapshot|trends|latest"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Code health CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("compute", help="Compute health (no persistence)")
    snap_p = sub.add_parser("snapshot", help="Compute + persist a snapshot")
    snap_p.add_argument("--root", default=None, help="Project root for parser fallback")
    trend_p = sub.add_parser("trends", help="Show persisted snapshot trends")
    trend_p.add_argument("--days", type=int, default=30)
    sub.add_parser("latest", help="Latest persisted snapshot")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ch = CodeHealth()

    if args.command == "compute":
        print(json.dumps(ch.compute(), indent=2, default=str))
    elif args.command == "snapshot":
        print(json.dumps(ch.snapshot(args.root), indent=2, default=str))
    elif args.command == "trends":
        print(json.dumps(ch.trends(args.days), indent=2, default=str))
    elif args.command == "latest":
        print(json.dumps(ch.latest(), indent=2, default=str))
    ch.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
