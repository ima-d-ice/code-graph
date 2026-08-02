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
    """Find all affected files using the graph."""
    logger.info("🟢 [Workflow] Entering DISCOVER node")
    from app.agents.planner_agent import PlannerAgent
    from app.tools.file_tools import read_file
    
    # Use planner agent's discovery tools
    agent = PlannerAgent(state["project_root"])
    impact = agent.analyze_impact(state["function_name"])
    
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
                
    return {"affected_files": affected_files, "graph_context": impact}


async def generate_node(state: RefactorState) -> Dict[str, Any]:
    """Generate AST-aware edits based on the plan and affected files."""
    logger.info("🟢 [Workflow] Entering GENERATE node")
    from app.agents.executor_agent import ExecutorAgent
    
    agent = ExecutorAgent(state["project_root"])
    changes = await agent.run(
        state["objective"], 
        state["plan"], 
        state["affected_files"], 
        state["graph_context"]
    )
    
    return {"proposed_changes": changes}


async def validate_node(state: RefactorState) -> Dict[str, Any]:
    """Run the 5-gate validation pipeline."""
    logger.info(f"🟢 [Workflow] Entering VALIDATE node (Iteration {state['iteration_count']})")
    from app.agents.critic_agent import CriticAgent
    
    agent = CriticAgent(state["project_root"])
    report = agent.validate(state["proposed_changes"])
    
    return {
        "validation_report": report,
        "validation_passed": report.get("overall") == "PASS",
        "iteration_count": state["iteration_count"] + 1
    }


async def repair_node(state: RefactorState) -> Dict[str, Any]:
    """Fix issues identified by the validation pipeline."""
    logger.info("🟢 [Workflow] Entering REPAIR node")
    from app.agents.repair_agent import RepairAgent
    
    agent = RepairAgent(state["project_root"])
    changes = await agent.run(
        state["proposed_changes"],
        state["validation_report"],
        state["affected_files"]
    )
    
    return {"proposed_changes": changes}


async def commit_node(state: RefactorState) -> Dict[str, Any]:
    """Apply the changes and update the graph."""
    logger.info("🟢 [Workflow] Entering COMMIT node")
    from app.core.transaction import commit_refactor
    
    commit_refactor(state["proposed_changes"], state["project_root"])
    
    return {}


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
            "abort": END
        }
    )
    
    workflow.add_edge("REPAIR", "VALIDATE")
    workflow.add_edge("COMMIT", END)

    return workflow.compile()
