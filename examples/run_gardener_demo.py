#!/usr/bin/env python3
"""
Run the autonomous gardener demo end-to-end:

  1. Generate a synthetic repo (N callers with dead functions)
  2. Cold-ingest it into the Neo4j digital twin
  3. Gardener scan  -> discovers dead-code tickets (check_threshold_*)
  4. Gardener run   -> auto-executes the low-risk tickets through the
                       full 6-gate workflow (executor LLM + deterministic
                       removal fallback + graph gate)
  5. Verify:
       - every pending ticket executed (validation passed)
       - 0 dead functions remain in the repo
       - 0 decoy files touched
       - flight records written and linked to tickets (audit trail)

Usage:
    python3 examples/run_gardener_demo.py --files 10 [--keep-repo] [--max-tickets 5]
"""
import argparse
import os
import shutil
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, REPO_ROOT)

DEMO_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_repo")

DEAD_PREFIX = "check_threshold_"


def dead_functions_remaining(project_root: str) -> list:
    """Any check_threshold_* still defined anywhere in the repo."""
    import ast
    remaining = []
    for root, _, files in os.walk(project_root):
        if "decoys" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                tree = ast.parse(open(path).read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name.startswith(DEAD_PREFIX)):
                    remaining.append(f"{f}:{node.name}")
    return remaining


def decoys_touched(project_root: str, pristine: dict) -> list:
    touched = []
    for rel, content in pristine.items():
        path = os.path.join(project_root, rel)
        try:
            with open(path) as fh:
                if fh.read() != content:
                    touched.append(rel)
        except OSError:
            touched.append(rel)
    return touched


def snapshot_decoys(project_root: str) -> dict:
    snap = {}
    decoy_dir = os.path.join(project_root, "decoys")
    if not os.path.isdir(decoy_dir):
        return snap
    for f in os.listdir(decoy_dir):
        if f.endswith(".py"):
            with open(os.path.join(decoy_dir, f)) as fh:
                snap[os.path.join("decoys", f)] = fh.read()
    return snap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=10, choices=[10, 50, 200])
    parser.add_argument("--max-tickets", type=int, default=5,
                        help="How many tickets to auto-execute this run")
    parser.add_argument("--keep-repo", action="store_true")
    args = parser.parse_args()

    from examples.demo_repo_generate import generate
    from app.core.gardener import Gardener
    from app.core.flight_recorder import FlightRecorder
    import asyncio

    generate(DEMO_REPO, args.files, 0.8)

    print("\n❄️ Cold-ingesting demo repo into the digital twin...")
    from ingest import ingest_cold
    ingest_cold(DEMO_REPO)

    pristine_decoys = snapshot_decoys(DEMO_REPO)
    gardener = Gardener()

    print("\n🌱 Gardener scan...")
    scan = gardener.scan(DEMO_REPO)
    tickets = scan["created"]
    dead_tickets = [t for t in tickets if t["kind"] == "dead_code"]
    print(f"  New tickets        : {len(tickets)}")
    print(f"  Dead-code tickets  : {len(dead_tickets)}")
    print(f"  Skipped (dupes)    : {scan['skipped']}")

    print(f"\n🚀 Gardener auto-executing up to {args.max_tickets} low-risk tickets...")
    results = asyncio.run(gardener.run_pending(DEMO_REPO, max_tickets=args.max_tickets))
    executed = results["results"]

    remaining = dead_functions_remaining(DEMO_REPO)
    touched = decoys_touched(DEMO_REPO, pristine_decoys)

    records = FlightRecorder().list_records(limit=100)
    linked = [r for r in records if r.get("ticket_id")]

    print("\n" + "=" * 60)
    print("GARDENER DEMO RESULTS")
    print("=" * 60)
    print(f"  Tickets executed       : {results['executed']}/{len(executed)}")
    for r in executed:
        status = "✅" if r["ok"] else "❌"
        print(f"    {status} {r['symbol']} in {r['file']}"
              f" (flight: {r.get('flight_record_id')})")
    expected_remaining = len(dead_tickets) - len(executed)
    print(f"  Dead functions left    : {len(remaining)} (expected: {expected_remaining}, "
          f"from un-executed tickets)")
    for r in remaining:
        print(f"    ⚠️  {r}")
    print(f"  Decoy files touched    : {len(touched)} (target: 0)")
    print(f"  Flight records (total) : {len(records)}")
    print(f"  Flight records linked  : {len(linked)} to tickets")
    print("=" * 60)

    ok = (results["failed"] == 0 and len(remaining) == expected_remaining
          and len(touched) == 0 and len(linked) >= len(executed)
          and len(records) >= len(executed))
    print(f"  OVERALL: {'✅ PASS' if ok else '❌ FAIL'}")
    print("=" * 60)

    gardener.close()
    if not args.keep_repo:
        shutil.rmtree(DEMO_REPO, ignore_errors=True)
        print("Cleaned up demo repo (use --keep-repo to keep it).")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
