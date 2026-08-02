#!/usr/bin/env python3
"""
Standalone end-to-end smoke test for the Graph-Augmented Refactoring Engine.

Runs against the real components:
  1. Environment + LLM Router (Groq, 12-key pool)
  2. Tree-sitter Semantic Parser
  3. AST-aware Diff Engine (strict rename + safe failure)
  4. LangGraph refactoring workflow (full E2E)
  5. Cleanup

Usage:
    python smoke_test.py
"""

import asyncio
import json
import os
import sys
import traceback

# ─────────────────────────────────────────────
# Path / Env Bootstrap
# ─────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(SCRIPT_DIR, "backend")
for p in (BACKEND_DIR, SCRIPT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BACKEND_DIR, ".env"), override=False)
    load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=False)
except ImportError:
    pass

# ─────────────────────────────────────────────
# Console Formatting
# ─────────────────────────────────────────────

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

RESULTS: list = []


def log_info(msg: str):
    print(f"{CYAN}[INFO]{RESET}  {msg}")


def log_pass(msg: str):
    print(f"{GREEN}[PASS]{RESET}  {msg}")


def log_fail(msg: str):
    print(f"{RED}[FAIL]{RESET}  {msg}")


def log_warn(msg: str):
    print(f"{YELLOW}[WARN]{RESET}  {msg}")


def run_step(name: str, fn) -> None:
    print(f"\n{'=' * 72}")
    print(f"{BOLD}{CYAN}STEP: {name}{RESET}")
    print(f"{'=' * 72}")
    try:
        fn()
        RESULTS.append((name, True, None))
        log_pass(f"Step completed: {name}")
    except Exception as e:
        RESULTS.append((name, False, str(e)))
        log_fail(f"{name}: {type(e).__name__}: {e}")
        traceback.print_exc()


# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

TEMP_FILE = "temp_test_file.py"
TEMP_FILE_ABS = os.path.join(BACKEND_DIR, TEMP_FILE)
PROJECT_ROOT = BACKEND_DIR

TEMP_FILE_SRC = """class Calculator:
    def add(self, a, b):
        return self.subtract(a, b)

    def subtract(self, a, b):
        return a - b
"""


# ─────────────────────────────────────────────
# Step 1: Environment + LLM Router
# ─────────────────────────────────────────────

def step1_env_and_llm():
    if not (os.getenv("GROQ_API_KEY_1") or os.getenv("GROQ_KEY_1")):
        raise AssertionError(
            "GROQ_API_KEY_1 not found in environment. Set GROQ_API_KEY_1..12 (backend/.env)"
        )
    log_pass(f"GROQ_API_KEY_1 present (len={len(os.getenv('GROQ_API_KEY_1') or os.getenv('GROQ_KEY_1'))})")

    from app.core.llm_router import LLMRouter, TaskType

    router = LLMRouter()
    if not router.providers:
        raise AssertionError("LLMRouter loaded 0 provider profiles")

    available = sum(1 for p in router.providers if p.is_available())
    log_info(f"Router loaded {len(router.providers)} provider profiles ({available} available now)")

    response = router.route_sync(
        TaskType.QUICK_SEARCH,
        "Say hello in 3 words",
        max_retries=1,
    )
    log_info(f"LLM response: {response!r}")

    if not response or not response.strip():
        raise AssertionError("LLM returned an empty response")

    stats = router.get_session_cost()
    log_info(f"Session: {stats['total_requests']} request(s), {stats['total_tokens']} token(s)")
    log_pass(f"Groq connectivity verified: {response.strip()!r}")


# ─────────────────────────────────────────────
# Step 2: Tree-sitter Parser
# ─────────────────────────────────────────────

def step2_parser():
    from app.services.parser_service import SemanticParser, NodeType, EdgeType

    parser = SemanticParser()
    log_info(f"Parser backend: {parser.backend}")

    result = parser.parse_file(TEMP_FILE_ABS)
    if result.errors:
        raise AssertionError(f"Parse errors: {result.errors}")

    nodes = result.nodes
    edges = result.edges
    log_info(f"Extracted {len(nodes)} nodes, {len(edges)} edges")

    classes = [n for n in nodes if n.node_type == NodeType.CLASS and n.name == "Calculator"]
    funcs = [
        n for n in nodes
        if n.node_type == NodeType.FUNCTION and n.name == "Calculator.add"
    ]
    calls = [
        e for e in edges
        if e.edge_type == EdgeType.CALLS and "subtract" in e.target_name
    ]

    if not classes:
        raise AssertionError(f"'Calculator' class not found. Nodes: {[n.name for n in nodes]}")
    if not funcs:
        raise AssertionError(f"'Calculator.add' function not found. Nodes: {[n.name for n in nodes]}")
    if not calls:
        raise AssertionError(f"CALLS edge to 'subtract' not found. Edges: {[e.target_name for e in edges]}")

    dump = {
        "nodes": [
            {
                "type": n.node_type.value,
                "name": n.name,
                "start_line": n.start_line,
                "end_line": n.end_line,
                "metadata": n.metadata,
            }
            for n in nodes
        ],
        "edges": [
            {"type": e.edge_type.value, "source": e.source_name, "target": e.target_name}
            for e in edges
        ],
    }
    print(json.dumps(dump, indent=2))

    log_pass(
        f"Parser OK — class 'Calculator', method 'Calculator.add', "
        f"CALLS -> '{calls[0].target_name}'"
    )


# ─────────────────────────────────────────────
# Step 3: Diff Engine (strict rename)
# ─────────────────────────────────────────────

def step3_diff_engine():
    from app.core.diff_engine import DiffEngine

    engine = DiffEngine()

    with open(TEMP_FILE_ABS, "r", encoding="utf-8") as f:
        source = f.read()

    # Valid rename: add -> compute_sum
    new_source, diff = engine.apply_transform(
        source, "rename_symbol", {"old_name": "add", "new_name": "compute_sum"}
    )
    if "def compute_sum" not in new_source:
        raise AssertionError("'def compute_sum' missing from transformed source")
    if "def add" in new_source:
        raise AssertionError("'def add' still present after rename")
    log_info(f"Rename diff:\n{diff}")

    with open(TEMP_FILE_ABS, "w", encoding="utf-8") as f:
        f.write(new_source)
    log_info(f"Wrote renamed source to {TEMP_FILE_ABS}")
    log_pass("Valid rename applied and persisted to disk")

    # Invalid patch: must fail safely (equivalent of unmatched search_block)
    try:
        engine.apply_transform(
            new_source,
            "nonexistent_transform",
            {"old_name": "compute_sum", "new_name": "calculate_total"},
        )
    except (ValueError, RuntimeError, NotImplementedError) as e:
        log_pass(f"Invalid patch rejected cleanly: {type(e).__name__}: {e}")
    else:
        raise AssertionError("Invalid patch did not raise (expected ValueError/RuntimeError)")

    # Verify disk state is untouched after the failed patch
    with open(TEMP_FILE_ABS, "r", encoding="utf-8") as f:
        disk_content = f.read()
    if "compute_sum" not in disk_content:
        raise AssertionError("File changed after failed patch")


# ─────────────────────────────────────────────
# Step 4: LangGraph Workflow (E2E)
# ─────────────────────────────────────────────

def step4_workflow():
    from app.core.graph_workflow import build_workflow

    graph = build_workflow()
    log_info("Workflow graph compiled (PLAN -> DISCOVER -> GENERATE -> VALIDATE -> [REPAIR] -> COMMIT)")

    state = {
        "objective": "Rename the function compute_sum to calculate_total",
        "file_name": TEMP_FILE,
        "function_name": "compute_sum",
        "permission_mode": "execute",
        "project_root": PROJECT_ROOT,
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

    final_state = asyncio.run(graph.ainvoke(state))

    passed = final_state.get("validation_passed")
    iterations = final_state.get("iteration_count")
    report = final_state.get("validation_report") or {}

    log_info(f"validation_passed={passed}, iteration_count={iterations}")
    if report:
        for gate_name, gate in (report.get("gates") or {}).items():
            log_info(
                f"  Gate [{gate_name}] -> {gate.get('status', '?')}: "
                f"{gate.get('details', '')[:200]}"
            )

    if passed is not True:
        raise AssertionError(
            f"Workflow did not pass validation. report.overall="
            f"{report.get('overall')}, gates={json.dumps(report.get('gates', {}), indent=2)}"
        )
    if not (0 <= iterations <= state["max_iterations"]):
        raise AssertionError(f"iteration_count out of range: {iterations}")

    with open(TEMP_FILE_ABS, "r", encoding="utf-8") as f:
        final_code = f.read()
    if "calculate_total" not in final_code:
        raise AssertionError("'calculate_total' not found in final file on disk")
    if "compute_sum" in final_code:
        raise AssertionError("'compute_sum' still present in final file on disk")

    log_pass(
        f"Workflow OK — validation passed in {iterations} iteration(s), "
        f"file renamed to 'calculate_total'"
    )


# ─────────────────────────────────────────────
# Step 5: Cleanup
# ─────────────────────────────────────────────

def step5_cleanup():
    if os.path.exists(TEMP_FILE_ABS):
        os.remove(TEMP_FILE_ABS)
    if os.path.exists(TEMP_FILE_ABS):
        raise AssertionError(f"Could not delete {TEMP_FILE_ABS}")
    log_pass(f"Deleted {TEMP_FILE_ABS}")


def cleanup_best_effort():
    if os.path.exists(TEMP_FILE_ABS):
        try:
            os.remove(TEMP_FILE_ABS)
            log_info(f"Best-effort cleanup removed {TEMP_FILE_ABS}")
        except OSError as e:
            log_warn(f"Cleanup failed: {e}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main() -> int:
    print(f"\n{BOLD}Code-Graph Engine — Smoke Test{RESET}")
    print(f"Project root : {PROJECT_ROOT}")
    print(f"Backend dir  : {BACKEND_DIR}")
    print(f"Temp file    : {TEMP_FILE_ABS}")

    # Seed the dummy file for steps 2-4
    with open(TEMP_FILE_ABS, "w", encoding="utf-8") as f:
        f.write(TEMP_FILE_SRC)

    try:
        run_step("1. Environment & LLM Router (Groq)", step1_env_and_llm)
        run_step("2. Tree-sitter Semantic Parser", step2_parser)
        run_step("3. AST-aware Diff Engine", step3_diff_engine)
        run_step("4. LangGraph Workflow (E2E)", step4_workflow)
        run_step("5. Cleanup", step5_cleanup)
    finally:
        cleanup_best_effort()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed

    print(f"\n{'=' * 72}")
    print(f"{BOLD}SUMMARY: {passed} passed, {failed} failed, {len(RESULTS)} total{RESET}")
    for name, ok, err in RESULTS:
        status = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        detail = "" if ok else f" -> {err}"
        print(f"  [{status}] {name}{detail}")
    print("=" * 72)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
