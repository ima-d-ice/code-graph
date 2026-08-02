"""
LangGraph state definition for the refactoring workflow.
Replaces the old AgentState (which was unused).
"""
from typing import TypedDict, Optional, List, Dict, Any
from app.models.schemas import FileUpdate, ValidationReport, PlanResult


class RefactorState(TypedDict):
    """
    The shared state object passed between LangGraph nodes.
    
    Flow:
    START → plan → discover → generate → validate → (repair if fail) → commit → END
    """
    # Input
    objective: str
    trigger_file: str
    trigger_function: str
    permission_mode: str  # plan | execute | auto
    
    # Planning
    plan: Optional[PlanResult]
    
    # Discovery
    affected_files: Dict[str, str]  # file_path -> original_content
    graph_context: Dict[str, Any]  # Neo4j query results
    blast_radius: Optional[Dict[str, Any]]
    
    # Generation
    proposed_changes: List[FileUpdate]
    
    # Validation
    validation_report: Optional[ValidationReport]
    
    # Repair loop
    iteration_count: int
    max_iterations: int
    critique_feedback: List[str]
    
    # Execution tracking
    agent_history: List[Dict[str, Any]]  # Tool calls and results
    cost_accumulated: float
    
    # Output
    final_result: Optional[Dict[str, Any]]
    error: Optional[str]
