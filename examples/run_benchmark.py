#!/usr/bin/env python3
"""
RefactorBench — internal eval harness for code-graph.

Runs parameterized refactor tasks (rename, dead-code removal) across repo
sizes and discovery modes (graph-first vs prompt-only grep), verifying each
trial with gold checks (0 call sites left, 0 decoys touched, gates passed).

The graph-vs-grep A/B is the moat proof: identical gates, executor and
deterministic fallback — only the discovery strategy differs.

Usage:
    python3 examples/run_benchmark.py --tasks rename,remove_dead \
        --sizes 10,50 --modes graph,grep --trials 1
"""
import argparse
import asyncio
import os
import shutil
import sys
import time

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, REPO_ROOT)

DEMO_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo_repo")
DEAD_PREFIX = "check_threshold_"


# ───────────────────────────── gold checks ─────────────────────────────

def call_sites_remaining(project_root: str, symbol: str) -> int:
    """AST-level call sites still referencing `symbol` (decoys excluded)."""
    import ast
    count = 0
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
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == symbol):
                    count += 1
    return count


def symbol_defined_remaining(project_root: str, symbol: str) -> int:
    """Still-defined occurrences of a removed symbol anywhere in the repo."""
    import ast
    count = 0
    for root, _, files in os.walk(project_root):
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
                        and node.name == symbol):
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


# ───────────────────────────── trial runner ─────────────────────────────

async def run_trial(task: str, size: int, mode: str, trial: int) -> dict:
    """One benchmark trial: generate repo, ingest, run workflow, gold-verify."""
    from examples.demo_repo_generate import generate
    from app.core.graph_workflow import run_refactor
    from app.core.telemetry import estimate_cost
    from app.core.benchmark import BenchmarkStore, TASKS, expected_blast_radius

    generate(DEMO_REPO, size, 0.8)
    try:
        from ingest import ingest_cold
        ingest_cold(DEMO_REPO)
    except Exception as e:
        # grep mode is Neo4j-free (gates fail open); keep the trial going
        if mode == "graph":
            raise
        print(f"   (ingest skipped: {e})")

    pristine_decoys = snapshot_decoys(DEMO_REPO)

    if task == "rename":
        from app.core.benchmark import rename_task
        spec = rename_task()
        check_symbol = "compute_sum"
    elif task == "remove_dead":
        from app.core.benchmark import remove_dead_task
        caller_idx = max(1, int(size * 0.8))  # remove from the LAST caller
        spec = remove_dead_task(caller_idx)
        check_symbol = spec["function_name"]
    else:
        raise ValueError(f"Unknown task: {task}")

    state = {
        "objective": spec["objective"],
        "file_name": spec["file_name"],
        "function_name": spec["function_name"],
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
        "discovery_mode": mode,
    }

    t0 = time.perf_counter()
    final_state = await run_refactor(state)
    duration_ms = (time.perf_counter() - t0) * 1000
    usage = final_state.get("_usage", {})

    passed = bool(final_state.get("validation_passed"))
    remaining = (
        call_sites_remaining(DEMO_REPO, check_symbol)
        if task == "rename" else
        symbol_defined_remaining(DEMO_REPO, check_symbol)
    )
    touched = decoys_touched(DEMO_REPO, pristine_decoys)

    passed = passed and remaining == 0 and len(touched) == 0

    tokens_by_model = usage.get("tokens_by_model", {})
    cost = sum(estimate_cost(t, m) for m, t in tokens_by_model.items())
    blast_found = len(final_state.get("affected_files", {}))
    blast_expected = expected_blast_radius(task, size)

    result = {
        "task": task, "size": size, "mode": mode, "trial": trial,
        "passed": passed, "duration_ms": duration_ms,
        "tokens_total": usage.get("tokens_total", 0),
        "cost_usd": cost, "fallback_used": bool(final_state.get("fallback_used")),
        "blast_expected": blast_expected, "blast_found": blast_found,
        "remaining": remaining, "decoys_touched": touched,
        "changes": len(final_state.get("proposed_changes", [])),
        "iterations": final_state.get("iteration_count", 0),
    }
    return result


# ───────────────────────────── main ─────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="rename,remove_dead")
    parser.add_argument("--sizes", default="10,50")
    parser.add_argument("--modes", default="graph,grep")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()

    tasks = [t.strip() for t in args.tasks.split(",")]
    sizes = [int(s) for s in args.sizes.split(",")]
    modes = [m.strip() for m in args.modes.split(",")]

    from app.core.benchmark import BenchmarkStore, TASKS
    store = BenchmarkStore()

    print("=" * 66)
    print(f"REFACTORBENCH — {len(tasks)} task(s) × {len(sizes)} size(s) "
          f"× {len(modes)} mode(s) × {args.trials} trial(s)")
    print("=" * 66)

    for task in tasks:
        if task not in TASKS:
            print(f"❌ Unknown task: {task} (known: {list(TASKS)})")
            continue
        for size in sizes:
            for mode in modes:
                for trial in range(1, args.trials + 1):
                    print(f"\n▶ {task} @ {size} files [{mode}] trial {trial}...")
                    result = asyncio.run(run_trial(task, size, mode, trial))
                    store.record(
                        task=result["task"], size=result["size"], mode=result["mode"],
                        trial=result["trial"], passed=result["passed"],
                        duration_ms=result["duration_ms"],
                        tokens_total=result["tokens_total"],
                        cost_usd=result["cost_usd"],
                        fallback_used=result["fallback_used"],
                        blast_expected=result["blast_expected"],
                        blast_found=result["blast_found"],
                        details={k: result[k] for k in
                                 ("remaining", "decoys_touched", "changes", "iterations")},
                    )
                    status = "✅ PASS" if result["passed"] else "❌ FAIL"
                    print(f"   {status}  {result['duration_ms']:.0f}ms  "
                          f"{result['tokens_total']} tokens  ${result['cost_usd']:.4f}  "
                          f"blast {result['blast_found']}/{result['blast_expected']}"
                          f"  fallback={result['fallback_used']}")

    print("\n" + "=" * 66)
    print("SCOREBOARD")
    print("=" * 66)
    for row in store.summary():
        print(f"  {row['task']:<12} size={row['size']:<4} {row['mode']:<6} "
              f"rate={row['resolution_rate']:.0%}  n={row['trials']}  "
              f"avg={row['avg_duration_ms']:.0f}ms  ${row['avg_cost_usd']:.4f}/run")

    print("\n" + "=" * 66)
    print("BLAST-RADIUS ACCURACY (found / expected)")
    print("=" * 66)
    for row in store.blast_accuracy():
        acc = f"{row['blast_accuracy']:.0%}" if row["blast_accuracy"] is not None else "n/a"
        print(f"  {row['task']:<12} size={row['size']:<4} {row['mode']:<6} {acc}"
              f"  (found {row['avg_found']:.1f} / expected {row['avg_expected']:.1f})")

    moat = store.moat_summary()
    print("\n" + "=" * 66)
    print("MOAT A/B — graph-first vs prompt-only")
    print("=" * 66)
    print(f"  graph: resolution {moat['graph']['resolution_rate']:.0%}  "
          f"blast accuracy {moat['graph']['blast_accuracy'] or 'n/a'}")
    print(f"  grep : resolution {moat['grep']['resolution_rate']:.0%}  "
          f"blast accuracy {moat['grep']['blast_accuracy'] or 'n/a'}")
    print(f"  delta: resolution {moat['delta']['resolution_rate']:+.0%}  "
          f"blast accuracy {moat['delta']['blast_accuracy'] or 'n/a'}")
    print(f"  verdict: {moat['verdict']}")
    print("=" * 66)

    store.close()
    shutil.rmtree(DEMO_REPO, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
