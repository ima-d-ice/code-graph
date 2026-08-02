"""
LangGraph state machine for the refactoring process.

Workflow:
[START] → [PLAN] → [DISCOVER] → [GENERATE] → [VALIDATE] → [COMMIT] → [END]
              ↑__________[REPAIR]←_________↓ (if validation fails)
"""

import logging
from typing import Dict, Any, List, Optional
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# State Definition
# ─────────────────────────────────────────────

class RefactorState(TypedDict):
    """The state shared across all nodes in the workflow."""
    objective: str
    file_name: str
    function_name: str
    permission_mode: str
    project_root: str
    
    # Populated by PLAN
    plan: Optional[Dict[str, Any]]
    
    # Populated by DISCOVER
    affected_files: Dict[str, str]  # file_path -> current_content
    graph_context: Dict[str, Any]
    
    # Populated by GENERATE / REPAIR
    proposed_changes: List[Dict[str, str]]  # list of {"file_path": "...", "content": "..."}
    
    # Populated by VALIDATE
    validation_report: Optional[Dict[str, Any]]
    validation_passed: bool
    
    # Workflow metadata
    iteration_count: int
    max_iterations: int
    history: List[Dict[str, Any]]

    # Gardener link (set when the workflow was launched from a ticket)
    ticket_id: Optional[str] = None

    # Populated by COMMIT/ABORT (flight recorder audit id)
    flight_record_id: Optional[str] = None


# ─────────────────────────────────────────────
# Workflow Nodes
# ─────────────────────────────────────────────

async def plan_node(state: RefactorState) -> Dict[str, Any]:
    """Analyze the objective and create a refactoring plan."""
    logger.info("🟢 [Workflow] Entering PLAN node")
    from app.agents.planner_agent import PlannerAgent
    
    agent = PlannerAgent(state["project_root"])
    plan = await agent.run(state["objective"], state["file_name"], state["function_name"])
    
    return {"plan": plan, "iteration_count": 0}


async def discover_node(state: RefactorState) -> Dict[str, Any]:
    """Find all affected files using the graph. Graph-first, grep-second."""
    logger.info("🟢 [Workflow] Entering DISCOVER node")
    from app.agents.planner_agent import PlannerAgent
    from app.tools.file_tools import read_file

    # Use planner agent's discovery tools (graph blast radius w/ grep fallback)
    agent = PlannerAgent(state["project_root"])
    impact = agent.analyze_impact(state["function_name"])

    # Graph-first enrichment: pull the subgraph neighborhood around the symbol
    graph_context = impact
    try:
        from app.services.neo4j_service import Neo4jService
        neo4j = Neo4jService()
        subgraph = neo4j.get_subgraph(state["function_name"])
        neo4j.close()
        graph_context["subgraph"] = subgraph
        logger.info(
            f"🕸️ Subgraph: {len(subgraph.get('affected_files', []))} files, "
            f"{len(subgraph.get('edges', []))} edges, "
            f"{len(subgraph.get('direct_callers', []))} direct callers"
        )
    except Exception as e:
        logger.warning(f"Subgraph enrichment unavailable ({e}); proceeding with impact only")

    # Read all affected files
    affected_files = {}

    # Always include the trigger file
    trigger_content = read_file(state["file_name"], state["project_root"])
    if not trigger_content.startswith("Error"):
        # Remove line numbers from tool output for raw content
        raw_lines = []
        for line in trigger_content.splitlines():
            if " | " in line:
                raw_lines.append(line.split(" | ", 1)[1])
            else:
                raw_lines.append(line)
        affected_files[state["file_name"]] = "\n".join(raw_lines)

    for file_info in impact.get("affected_files", []):
        f = file_info if isinstance(file_info, str) else file_info.get("file", "")
        if f and f not in affected_files:
            content = read_file(f, state["project_root"])
            if not content.startswith("Error"):
                raw_lines = []
                for line in content.splitlines():
                    if " | " in line:
                        raw_lines.append(line.split(" | ", 1)[1])
                    else:
                        raw_lines.append(line)
                affected_files[f] = "\n".join(raw_lines)

    # Graph-first metric: how much context did we spare the LLM?
    total_tokens_affected = sum(len(c) // 4 for c in affected_files.values())
    try:
        import os
        repo_bytes = sum(
            os.path.getsize(os.path.join(root, f))
            for root, _, files in os.walk(state["project_root"])
            for f in files
        )
    except Exception:
        repo_bytes = 0
    logger.info(
        f"📐 Graph-first context: {len(affected_files)} file(s), ~{total_tokens_affected} "
        f"estimated tokens (repo {repo_bytes} bytes total)"
    )

    return {"affected_files": affected_files, "graph_context": graph_context}


def _trigger_change_valid(state: RefactorState, changes: List[Dict[str, str]]) -> bool:
    """True if the trigger file is changed and the target symbol was removed
    from its definitions (i.e. the rename actually happened)."""
    import os
    from app.core.rename_propagation import defined_symbols
    tc = [c for c in changes if c["file_path"] == state.get("file_name")]
    if not tc:
        return False
    symbol = state.get("function_name") or ""
    if not symbol:
        return True
    try:
        old_path = os.path.join(state["project_root"], state["file_name"])
        with open(old_path, "r", encoding="utf-8", errors="replace") as fh:
            old_defs = defined_symbols(fh.read())
        return symbol not in defined_symbols(tc[0]["content"])
    except OSError:
        return False


def _apply_deterministic_rename(state: RefactorState,
                                changes: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Override unusable changes with the deterministic objective-driven rename."""
    if not changes or not _trigger_change_valid(state, changes):
        from app.core.rename_propagation import apply_objective_rename
        deterministic = apply_objective_rename(
            state["objective"], state["file_name"],
            state["project_root"], state["affected_files"],
        )
        if deterministic:
            logger.info(f"⚙️ Applying deterministic rename ({len(deterministic)} file(s))")
            return deterministic
    return changes


def _apply_deterministic_removal(state: RefactorState,
                                 changes: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Override unusable changes with the deterministic dead-code removal."""
    if not changes or not _trigger_change_valid(state, changes):
        from app.core.rename_propagation import apply_objective_removal
        deterministic = apply_objective_removal(
            state["objective"], state["file_name"],
            state["project_root"], state["affected_files"],
        )
        if deterministic:
            logger.info(f"⚙️ Applying deterministic removal ({len(deterministic)} file(s))")
            return deterministic
    return changes


async def generate_node(state: RefactorState) -> Dict[str, Any]:
    """Generate AST-aware edits based on the plan and affected files."""
    logger.info("🟢 [Workflow] Entering GENERATE node")
    from app.agents.executor_agent import ExecutorAgent
    from app.core.rename_propagation import propagate_renames
    
    agent = ExecutorAgent(state["project_root"])
    try:
        changes = await agent.run(
            state["objective"], 
            state["plan"], 
            state["affected_files"], 
            state["graph_context"]
        )
    except Exception as e:
        logger.warning(f"⚠️ Executor failed ({e}); falling back to deterministic rename")
        changes = []

    # Graph-native guarantee: if the executor's trigger-file change didn't
    # actually perform the rename, override with the deterministic
    # objective-driven rename (AST-semantic, verified by the diff engine).
    changes = _apply_deterministic_rename(state, changes)
    changes = _apply_deterministic_removal(state, changes)

    # Graph-native guarantee: deterministically propagate every rename the LLM
    # missed across the blast radius found during DISCOVER.
    if changes:
        changes = propagate_renames(changes, state["project_root"], state["affected_files"])
        logger.info(f"🔄 After propagation: {len(changes)} change(s)")
    
    return {"proposed_changes": changes}


async def validate_node(state: RefactorState) -> Dict[str, Any]:
    """Run the 6-gate validation pipeline + objective-coverage check."""
    logger.info(f"🟢 [Workflow] Entering VALIDATE node (Iteration {state['iteration_count']})")
    from app.agents.critic_agent import CriticAgent

    changes = state.get("proposed_changes") or []

    # Objective-coverage pre-check: a rename must actually rename.
    # Deterministic — does not depend on the LLM claiming success.
    coverage_error = check_objective_coverage(state, changes)
    if coverage_error:
        report = {
            "overall": "FAIL",
            "coverage": coverage_error,
            "gates": {
                "syntax": {"status": "SKIP", "details": ""},
                "imports": {"status": "SKIP", "details": ""},
                "types": {"status": "SKIP", "details": ""},
                "tests": {"status": "SKIP", "details": ""},
                "security": {"status": "SKIP", "details": ""},
                "graph": {"status": "FAIL", "details": f"Objective not achieved: {coverage_error}"},
            },
        }
        logger.warning(f"🚫 Objective coverage check failed: {coverage_error}")
        return {
            "validation_report": report,
            "validation_passed": False,
            "iteration_count": state["iteration_count"] + 1
        }

    agent = CriticAgent(state["project_root"])
    report = agent.validate(changes)
    
    return {
        "validation_report": report,
        "validation_passed": report.get("overall") == "PASS",
        "iteration_count": state["iteration_count"] + 1
    }


def check_objective_coverage(state: RefactorState, changes: List[Dict[str, str]]) -> str:
    """
    Verify the refactor objective was actually achieved on the trigger file:
    the trigger file must be changed, and the target symbol must be gone from
    its definitions (a rename that removed nothing is a no-op).
    """
    import os
    trigger = state.get("file_name") or ""
    symbol = state.get("function_name") or ""

    if not changes:
        return "No changes produced."

    if trigger:
        trigger_changes = [c for c in changes if c["file_path"] == trigger]
        if not trigger_changes:
            return f"Trigger file '{trigger}' was not changed."
        new_content = trigger_changes[0]["content"]

        if symbol:
            old_path = os.path.join(state["project_root"], trigger)
            try:
                with open(old_path, "r", encoding="utf-8", errors="replace") as fh:
                    old_content = fh.read()
            except OSError:
                old_content = ""
            if old_content.strip() == new_content.strip():
                return f"Trigger file '{trigger}' content is unchanged (no-op)."

            from app.core.rename_propagation import defined_symbols
            old_defs = defined_symbols(old_content)
            new_defs = defined_symbols(new_content)
            if symbol in old_defs and symbol in new_defs:
                return f"Symbol '{symbol}' still defined in '{trigger}' (rename not applied)."
    return ""


async def repair_node(state: RefactorState) -> Dict[str, Any]:
    """Fix issues identified by the validation pipeline."""
    logger.info("🟢 [Workflow] Entering REPAIR node")
    from app.agents.repair_agent import RepairAgent
    from app.core.rename_propagation import propagate_renames
    
    agent = RepairAgent(state["project_root"])
    try:
        changes = await agent.run(
            state["proposed_changes"],
            state["validation_report"],
            state["affected_files"]
        )
    except Exception as e:
        logger.warning(f"⚠️ Repair agent failed ({e}); falling back to deterministic rename")
        changes = []

    # If the repair LLM also failed to touch the trigger file, fall back to
    # the deterministic objective-driven rename — the graph guarantees the
    # outcome even when the LLM cannot.
    changes = _apply_deterministic_rename(state, changes)
    changes = _apply_deterministic_removal(state, changes)

    # Keep the graph-native guarantee through the repair loop as well
    if changes:
        changes = propagate_renames(changes, state["project_root"], state["affected_files"])
    
    return {"proposed_changes": changes}


async def commit_node(state: RefactorState) -> Dict[str, Any]:
    """Apply the changes and update the graph."""
    logger.info("🟢 [Workflow] Entering COMMIT node")
    from app.core.transaction import commit_refactor
    from app.core.flight_recorder import FlightRecorder
    
    before_stats = _graph_stats()
    commit_refactor(state["proposed_changes"], state["project_root"])
    after_stats = _graph_stats()

    graph_delta = {
        "before": before_stats,
        "after": after_stats,
    }
    if before_stats and after_stats:
        delta = {}
        for key in before_stats:
            if isinstance(before_stats[key], int) and isinstance(after_stats.get(key), int):
                delta[key] = after_stats[key] - before_stats[key]
        graph_delta["delta"] = delta

    recorder = FlightRecorder()
    record_id = recorder.record(
        state,
        outcome="committed",
        graph_stats=graph_delta,
        ticket_id=state.get("ticket_id"),
    )
    
    return {"flight_record_id": record_id}

async def abort_node(state: RefactorState) -> Dict[str, Any]:
    """Record a refused change (audit trail for why nothing was committed)."""
    logger.info("🚫 [Workflow] Entering ABORT node")
    from app.core.flight_recorder import FlightRecorder
    record_id = FlightRecorder().record(
        state,
        outcome="aborted",
        graph_stats=_graph_stats(),
        ticket_id=state.get("ticket_id"),
    )
    return {"flight_record_id": record_id}


def _graph_stats() -> Optional[Dict[str, Any]]:
    """Best-effort snapshot of graph statistics."""
    try:
        from app.services.neo4j_service import Neo4jService
        neo4j = Neo4jService()
        try:
            return neo4j.get_stats()
        finally:
            neo4j.close()
    except Exception:
        logger.debug("Graph stats unavailable for flight record", exc_info=True)
        return None


# ─────────────────────────────────────────────
# Edge Logic
# ─────────────────────────────────────────────

def validation_gate(state: RefactorState) -> str:
    """Decide what to do after validation."""
    if state["validation_passed"]:
        logger.info("✅ Validation Passed. Proceeding to COMMIT.")
        return "commit"
        
    if state["iteration_count"] >= state["max_iterations"]:
        logger.error(f"❌ Validation Failed. Max iterations ({state['max_iterations']}) reached. Aborting.")
        return "abort"
        
    logger.warning("⚠️ Validation Failed. Proceeding to REPAIR.")
    return "repair"


# ─────────────────────────────────────────────
# Graph Construction
# ─────────────────────────────────────────────

def build_workflow():
    """Build and compile the LangGraph workflow."""
    workflow = StateGraph(RefactorState)

    # Add nodes
    workflow.add_node("PLAN", plan_node)
    workflow.add_node("DISCOVER", discover_node)
    workflow.add_node("GENERATE", generate_node)
    workflow.add_node("VALIDATE", validate_node)
    workflow.add_node("REPAIR", repair_node)
    workflow.add_node("COMMIT", commit_node)
    workflow.add_node("ABORT", abort_node)

    # Add edges
    workflow.add_edge(START, "PLAN")
    workflow.add_edge("PLAN", "DISCOVER")
    workflow.add_edge("DISCOVER", "GENERATE")
    workflow.add_edge("GENERATE", "VALIDATE")
    
    # Conditional edge from VALIDATE
    workflow.add_conditional_edges(
        "VALIDATE",
        validation_gate,
        {
            "commit": "COMMIT",
            "repair": "REPAIR",
            "abort": "ABORT"
        }
    )
    
    workflow.add_edge("REPAIR", "VALIDATE")
    workflow.add_edge("COMMIT", END)
    workflow.add_edge("ABORT", END)

    return workflow.compile()
