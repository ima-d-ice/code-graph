"""
Pydantic models for API requests/responses.
Updated for the goated architecture.
"""
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Literal


class RefactorRequest(BaseModel):
    file_name: str
    function_name: str
    objective: str
    permission_mode: Literal["plan", "execute", "auto"] = "execute"
    current_code: Optional[str] = None


class PlanRequest(BaseModel):
    objective: str
    file_name: str
    function_name: str


class ImpactRequest(BaseModel):
    symbol: str


class FileUpdate(BaseModel):
    file_name: str
    new_code: str
    diff: Optional[str] = None  # Unified diff string
    explanation: Optional[str] = None


class ValidationGate(BaseModel):
    gate_name: str  # syntax | imports | types | tests | security
    status: Literal["PASS", "FAIL", "SKIPPED"]
    details: str
    duration_ms: Optional[int] = None


class ValidationReport(BaseModel):
    overall: Literal["PASS", "FAIL"]
    gates: List[ValidationGate]
    iteration: int = 0
    total_duration_ms: Optional[int] = None


class ImpactAnalysisResult(BaseModel):
    symbol: str
    direct_callers: List[Dict[str, str]]
    inheritors: List[Dict[str, str]]
    variable_users: List[Dict[str, str]]
    importers: List[Dict[str, str]]
    total_affected: int
    risk_score: float = Field(..., ge=0.0, le=1.0)
    recommendation: Literal["PROCEED", "REVIEW", "BLOCK"]
    graph_paths: Optional[List[List[str]]] = None


class PlanResult(BaseModel):
    objective: str
    steps: List[str]
    estimated_files: int
    estimated_risk: float
    reasoning: str


class RefactorResponse(BaseModel):
    changes: List[FileUpdate]
    explanation: str
    impact_analysis: str
    validation_report: ValidationReport
    plan: Optional[PlanResult] = None
    cost: float = 0.0  # Estimated API cost
    turns: int = 0
    duration_ms: Optional[int] = None


class GraphQueryRequest(BaseModel):
    question: str


class GraphData(BaseModel):
    nodes: List[Dict[str, Any]]
    links: List[Dict[str, Any]]


class IngestRequest(BaseModel):
    folder_path: str
    mode: Literal["cold", "warm"] = "cold"


class RouterStats(BaseModel):
    session_requests: int
    session_estimated_cost_usd: float
    providers_total: int
    providers_healthy: int
    providers_available_now: int
    provider_details: List[Dict[str, Any]]
