import logging
import os
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.models.schemas import (
    RefactorRequest, RefactorResponse, GraphQueryRequest,
    ImpactRequest, PlanRequest, FileUpdate, ValidationReport
)
from app.core.workspace import get_project_root
from app.services.rag_services import HybridRAG
from app.services.neo4j_service import Neo4jService
from app.core.graph_workflow import build_workflow

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Code-Graph API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In production, restrict this
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Build LangGraph workflow once
workflow = build_workflow()


@app.get("/")
def read_root():
    return {"status": "Active", "system": "Graph-Augmented Autonomous Refactoring Engine"}


@app.post("/refactor", response_model=RefactorResponse)
async def refactor_endpoint(request: RefactorRequest):
    """
    Execute a full refactoring workflow.
    """
    try:
        project_root = get_project_root()
        logger.info(f"📥 Refactor Request: {request.objective}")

        # Initialize state
        state = {
            "objective": request.objective,
            "file_name": request.file_name,
            "function_name": request.function_name,
            "permission_mode": request.permission_mode,
            "project_root": project_root,
            "plan": None,
            "affected_files": {},
            "graph_context": {},
            "proposed_changes": [],
            "validation_report": None,
            "validation_passed": False,
            "iteration_count": 0,
            "max_iterations": 3,
            "history": []
        }

        # Run workflow
        final_state = await workflow.ainvoke(state)
        
        # Check if validation ultimately passed
        if not final_state.get("validation_passed"):
            raise HTTPException(
                status_code=400, 
                detail={
                    "message": "Refactoring failed validation after max iterations.",
                    "report": final_state.get("validation_report")
                }
            )
            
        changes = final_state.get("proposed_changes", [])
        
        report = final_state.get("validation_report") or {}
        gates = [
            ValidationGate(
                gate_name=name,
                status=g.get("status", "SKIPPED"),
                details=g.get("details", ""),
                duration_ms=g.get("duration_ms"),
            )
            for name, g in (report.get("gates") or {}).items()
        ]
        
        return RefactorResponse(
            changes=[
                FileUpdate(file_name=c.get("file_path", request.file_name),
                           new_code=c.get("content", ""))
                for c in changes
            ],
            explanation=f"Refactored {request.function_name} in {request.file_name}",
            impact_analysis=str(final_state.get("graph_context", {})),
            validation_report=ValidationReport(
                overall=report.get("overall", "FAIL"),
                gates=gates,
                iteration=report.get("iteration", 0),
                total_duration_ms=report.get("total_duration_ms"),
            ),
            turns=final_state.get("iteration_count", 0)
        )

    except Exception as e:
        logger.error(f"❌ Refactor failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/plan")
async def plan_endpoint(request: PlanRequest):
    """
    Generate a refactoring plan (read-only mode).
    """
    try:
        project_root = get_project_root()
        from app.agents.planner_agent import PlannerAgent
        agent = PlannerAgent(project_root)
        plan = await agent.run(request.objective, request.file_name, request.function_name)
        return {"plan": plan}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/impact")
async def impact_endpoint(request: ImpactRequest):
    """
    Analyze blast radius for a symbol.
    """
    try:
        project_root = get_project_root()
        from app.agents.planner_agent import PlannerAgent
        agent = PlannerAgent(project_root)
        impact = agent.analyze_impact(request.symbol)
        return impact
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/ask-graph")
def ask_graph_endpoint(request: GraphQueryRequest):
    """
    Chat with the codebase (Hybrid RAG: Vector + Graph).
    """
    try:
        rag = HybridRAG()
        answer = rag.answer_question(request.question)
        return {"response": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class IngestRequest(BaseModel):
    folder_path: str = ""
    mode: str = "cold" # "cold" or "warm"

@app.post("/ingest")
def ingest_endpoint(request: IngestRequest):
    """
    Trigger ingestion from the API.
    """
    from ingest import ingest_cold
    from app.core.transaction import ingest_warm
    
    path = request.folder_path or get_project_root()
    
    if request.mode == "cold":
        ingest_cold(path)
        return {"message": "Cold ingestion complete"}
    else:
        # For warm, we'd normally pass specific files. 
        # Doing a full walk for warm here is just an example.
        import glob
        files = glob.glob(os.path.join(path, "**/*.py"), recursive=True)
        rel_files = [os.path.relpath(f, path) for f in files]
        ingest_warm(path, rel_files)
        return {"message": "Warm ingestion complete"}


@app.get("/graph")
def get_graph_data():
    """
    Returns nodes and edges for Frontend D3.js Visualization.
    """
    try:
        neo4j = Neo4jService()
        data = neo4j.get_graph_data(limit=300)
        neo4j.close()
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/graph/stats")
def graph_stats():
    """
    Graph statistics + digital twin freshness (last ingest).
    """
    try:
        neo4j = Neo4jService()
        stats = neo4j.get_stats()
        freshness = neo4j.get_freshness()
        neo4j.close()
        return {"stats": stats, "freshness": freshness.get("last_ingested_at"),
                "ingest_mode": freshness.get("mode")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/symbol/{symbol}")
def analyze_symbol(symbol: str):
    """
    Full subgraph neighborhood around a symbol: blast radius, direct callers,
    callees, mutators, readers, edges. Falls back to grep when Neo4j is down.
    """
    try:
        project_root = get_project_root()
        from app.tools.graph_tools import impact_analysis
        from app.services.neo4j_service import Neo4jService
        import json

        neo4j = None
        try:
            neo4j = Neo4jService()
            subgraph = neo4j.get_subgraph(symbol)
            subgraph["source"] = "graph"
        except Exception:
            subgraph = json.loads(impact_analysis(symbol, project_root))
            subgraph.setdefault("edges", [])
        finally:
            try:
                if neo4j:
                    neo4j.close()
            except Exception:
                pass
        return subgraph
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/blast/{symbol}")
def analyze_blast(symbol: str):
    """
    Blast radius of changing a symbol (graph-first, grep fallback).
    """
    try:
        project_root = get_project_root()
        from app.agents.planner_agent import PlannerAgent
        agent = PlannerAgent(project_root)
        return agent.analyze_impact(symbol)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/changes")
def list_changes(limit: int = 50, outcome: str = None):
    """
    Flight recorder: recent change records (metadata, newest first).
    """
    try:
        from app.core.flight_recorder import FlightRecorder
        return {"records": FlightRecorder().list_records(limit=limit, outcome=outcome)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/changes/{record_id}")
def get_change(record_id: str):
    """
    Flight recorder: full audit record (objective -> plan -> diffs -> gates -> graph delta).
    """
    try:
        from app.core.flight_recorder import FlightRecorder
        record = FlightRecorder().get_record(record_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"No flight record {record_id}")
        return record
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class GardenerScanRequest(BaseModel):
    project_root: str = ""
    complexity_threshold: int = 10
    force: bool = False

class GardenerRunRequest(BaseModel):
    project_root: str = ""
    max_tickets: int = 5


@app.post("/gardener/scan")
def gardener_scan(request: GardenerScanRequest):
    """
    Autonomous gardener: scan the digital twin for improvement tickets
    (dead code, high complexity). Graph-evidence-first.
    """
    try:
        from app.core.gardener import Gardener
        project_root = request.project_root or get_project_root()
        result = Gardener().scan(
            project_root,
            complexity_threshold=request.complexity_threshold,
            force=request.force,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/gardener/run")
async def gardener_run(request: GardenerRunRequest):
    """
    Autonomous gardener: auto-execute pending low-risk tickets through the
    full 6-gate workflow. Every execution writes a flight record.
    """
    try:
        from app.core.gardener import Gardener
        project_root = request.project_root or get_project_root()
        return await Gardener().run_pending(
            project_root, max_tickets=request.max_tickets
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/gardener/tickets")
def gardener_tickets(status: str = None):
    """
    Autonomous gardener: list all tickets (optionally filtered by status).
    """
    try:
        from app.core.gardener import Gardener
        return {"tickets": Gardener().list_tickets(status=status)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health_check():
    """
    Service health: Neo4j connectivity + graph freshness.
    """
    from datetime import datetime, timezone
    health = {
        "status": "ok",
        "neo4j": "down",
        "last_ingested_at": None,
        "nodes": 0,
        "relationships": 0,
    }
    try:
        neo4j = Neo4jService()
        stats = neo4j.get_stats()
        freshness = neo4j.get_freshness()
        neo4j.close()
        health["neo4j"] = "up"
        health["nodes"] = (
            stats.get("modules", 0) + stats.get("classes", 0)
            + stats.get("functions", 0) + stats.get("variables", 0)
            + stats.get("files", 0)
        )
        health["relationships"] = stats.get("edges", 0)
        health["last_ingested_at"] = freshness.get("last_ingested_at")
    except Exception as e:
        health["status"] = "degraded"
        health["neo4j"] = f"down ({e})"
    health["timestamp"] = datetime.now(timezone.utc).isoformat()
    return health


# WebSocket for streaming progress (MVP placeholder)
@app.websocket("/ws/refactor")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"Message text was: {data}")
    except WebSocketDisconnect:
        pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)