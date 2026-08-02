#!/usr/bin/env python3
"""
Run the rename demo end-to-end: generate a synthetic repo (N files), optionally
cold-ingest it into Neo4j, run the graph-native refactor workflow
(Rename compute_sum -> calculate_total), and verify the result:

  - 100% of compute_sum call sites updated
  - 0 decoy files touched

Usage:
    python3 examples/run_rename_demo.py --files 50 [--ingest] [--keep-repo]
"""
import argparse
import os
import shutil
import subprocess
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, REPO_ROOT)

DEMO_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_repo")


def call_sites_remaining(project_root: str) -> int:
    """Count AST-level call sites still referencing the old symbol."""
    import ast
    count = 0
    for root, _, files in os.walk(project_root):
        if "decoys" in root:
            continue
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                try:
                    tree = ast.parse(open(path).read())
                except (OSError, SyntaxError):
                    continue
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "compute_sum"):
                        count += 1
    return count


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
    parser.add_argument("--callers", type=float, default=0.8)
    parser.add_argument("--ingest", action="store_true",
                        help="Cold-ingest the demo repo into Neo4j first (graph-first)")
    parser.add_argument("--keep-repo", action="store_true")
    args = parser.parse_args()

    from examples.demo_repo_generate import generate
    generate(DEMO_REPO, args.files, args.callers)

    if args.ingest:
        print("\n❄️ Cold-ingesting demo repo into the digital twin...")
        from ingest import ingest_cold
        ingest_cold(DEMO_REPO)

    pristine_decoys = snapshot_decoys(DEMO_REPO)

    from app.core.graph_workflow import build_workflow

    state = {
        "objective": ("Rename compute_sum to calculate_total across the codebase "
                      "and update every call site"),
        "file_name": "lib/utils.py",
        "function_name": "compute_sum",
        "permission_mode": "execute",
        "project_root": DEMO_REPO,
        "plan": None,
        "affected_files": {},
        "graph_context": {},
        "proposed_changes": [],
        "validation_report": None,
        "validation_passed": False,
        "iteration_count": 0,
        "max_iterations": 3,
        "history": [],
    }

    print(f"\n🚀 Running graph-native refactor on {args.files} files...")
    workflow = build_workflow()
    import asyncio
    final_state = asyncio.run(workflow.ainvoke(state))

    remaining = call_sites_remaining(DEMO_REPO)
    touched = decoys_touched(DEMO_REPO, pristine_decoys)
    passed = final_state.get("validation_passed", False)
    changed = final_state.get("proposed_changes", [])
    graph_ctx = final_state.get("graph_context", {}) or {}

    print("\n" + "=" * 60)
    print("RENAME DEMO RESULTS")
    print("=" * 60)
    print(f"  Files in repo          : {args.files}")
    print(f"  Files changed          : {len(changed)}")
    print(f"  Validation passed      : {passed}")
    print(f"  Iterations (repair)    : {final_state.get('iteration_count', 0)}")
    print(f"  Call sites remaining   : {remaining} (target: 0)")
    print(f"  Decoy files touched    : {len(touched)} (target: 0)")
    print(f"  Blast radius (graph)   : {len(graph_ctx.get('subgraph', {}).get('affected_files', graph_ctx.get('affected_files', [])))} files")
    print(f"  Affected file count    : {len(final_state.get('affected_files', {}))}")
    print("=" * 60)

    ok = passed and remaining == 0 and len(touched) == 0
    print(f"  OVERALL: {'✅ PASS' if ok else '❌ FAIL'}")
    print("=" * 60)

    if not args.keep_repo:
        shutil.rmtree(DEMO_REPO, ignore_errors=True)
        print("Cleaned up demo repo (use --keep-repo to keep it).")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
