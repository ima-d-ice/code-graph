"""
Graph querying and semantic search tools for the agent.
Allows the agent to traverse Neo4j and search ChromaDB.
"""

import json
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


def graph_query(cypher: str, explanation: str) -> str:
    """Execute a Cypher query against the Neo4j database."""
    from app.services.neo4j_service import Neo4jService
    
    logger.info(f"🔍 [GraphQuery] {explanation}")
    logger.debug(f"Cypher: {cypher}")
    
    neo4j = Neo4jService()
    try:
        results = neo4j.run_query(cypher)
        # Format results nicely
        if not results:
            return "No results found."
            
        # Truncate if too large
        result_str = json.dumps(results, indent=2)
        if len(result_str) > 5000:
            return result_str[:5000] + "\n... (truncated, too large)"
        return result_str
    except Exception as e:
        return f"Error executing Cypher: {e}"
    finally:
        neo4j.close()


def semantic_search(query: str, k: int = 5, **kwargs: Any) -> str:
    """Hybrid vector + graph search. Unknown kwargs are tolerated (LLM may
    pass max_results/path etc. regardless of the declared schema)."""
    from app.services.rag_services import HybridRAG

    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 5

    rag = HybridRAG()
    try:
        results = rag.hybrid_search(query, k=k)
        
        if not results:
            return "No results found."
            
        formatted = []
        for r in results:
            formatted.append(f"--- File: {r['file']} (Type: {r['chunk_type']}, Name: {r['chunk_name']}) ---")
            formatted.append(r['code'])
            if r['structure']:
                formatted.append(f"Dependencies: {r['structure']}")
            formatted.append("")
            
        return "\n".join(formatted)
    except Exception as e:
        return f"Error in semantic search: {e}"


def impact_analysis(symbol: str, project_root: str = "") -> str:
    """Predict the blast radius of changing a symbol (graph-first, grep-second)."""
    from app.services.neo4j_service import Neo4jService

    neo4j = Neo4jService()
    try:
        radius = neo4j.get_blast_radius(symbol)
        neo4j.close()

        known = (
            radius["direct_callers"]
            or radius["transitive_callers"]
            or radius["importing_modules"]
            or radius["inheriting_classes"]
        )
        if not known and not radius["affected_files"]:
            # Graph is up but does not know this symbol (stale or never
            # ingested repo). Fall back to on-disk discovery — the graph
            # is only trusted when it has data.
            logger.warning(
                f"Symbol '{symbol}' unknown to graph; falling back to grep"
            )
            return grep_fallback_impact(symbol, project_root)

        # Calculate risk score (0.0 to 1.0)
        # Based on number of affected files and depth of transitive callers
        num_files = len(radius["affected_files"])
        num_transitive = len(radius["transitive_callers"])

        risk = min(1.0, (num_files * 0.1) + (num_transitive * 0.05))

        radius["risk_score"] = round(risk, 2)

        if risk < 0.3:
            radius["recommendation"] = "PROCEED - Low Impact"
        elif risk < 0.7:
            radius["recommendation"] = "REVIEW - Moderate Impact"
        else:
            radius["recommendation"] = "BLOCK/PLAN CAREFULLY - High Impact"

        radius["source"] = "graph"
        return json.dumps(radius, indent=2)
    except Exception as e:
        logger.warning(f"Neo4j impact analysis failed ({e}); using grep fallback")
        return grep_fallback_impact(symbol, project_root)
    finally:
        try:
            neo4j.close()
        except Exception:
            pass


def grep_fallback_impact(symbol: str, project_root: str = "") -> str:
    """
    Graph-first, grep-second: when Neo4j is unavailable, discover the blast
    radius by locating every file that references the symbol on disk.
    """
    if not project_root:
        from app.core.workspace import get_project_root
        try:
            project_root = get_project_root()
        except Exception:
            project_root = "."

    from app.tools.file_tools import grep_search

    raw = grep_search(pattern=symbol, path=".", project_root=project_root)

    affected_files = []
    if raw and not raw.startswith("Error") and raw != "No matches found.":
        seen = set()
        for line in raw.splitlines():
            fname = line.split(":", 1)[0]
            if fname and fname not in seen:
                seen.add(fname)
                affected_files.append(fname)

    return json.dumps({
        "direct_callers": [],
        "transitive_callers": [],
        "importing_modules": [],
        "inheriting_classes": [],
        "affected_files": affected_files,
        "risk_score": round(min(1.0, len(affected_files) * 0.1), 2),
        "recommendation": "REVIEW - grep-based fallback (Neo4j unavailable)",
        "source": "grep-fallback",
    }, indent=2)


def register_graph_tools(registry, project_root: str = ""):
    """Register graph tools with the tool registry."""
    from app.core.tool_registry import PermissionMode

    def impact_handler(symbol: str) -> str:
        from app.services.neo4j_service import Neo4jService
        neo4j = None
        try:
            neo4j = Neo4jService()
            res = impact_analysis(symbol)
            return res
        except Exception as e:
            logger.warning(f"Impact analysis fallback triggered: {e}")
            return grep_fallback_impact(symbol, project_root)
        finally:
            try:
                if neo4j:
                    neo4j.close()
            except Exception:
                pass

    registry.register(
        name="graph_query",
        description="Query the codebase dependency graph using Cypher. Find callers, imports, inheritance.",
        input_schema={
            "type": "object",
            "properties": {
                "cypher": {"type": "string", "description": "The Cypher query to run"},
                "explanation": {"type": "string", "description": "Why you are running this query"}
            },
            "required": ["cypher", "explanation"]
        },
        required_permission=PermissionMode.PLAN,
        handler=graph_query
    )
    
    registry.register(
        name="semantic_search",
        description="Search the codebase using natural language. Returns relevant code snippets with graph dependencies.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "k": {"type": ["integer", "string"], "description": "Number of results to return (default 5)"}
            },
            "required": ["query"]
        },
        required_permission=PermissionMode.PLAN,
        handler=semantic_search
    )
    
    registry.register(
        name="impact_analysis",
        description="Analyze the blast radius of a proposed refactoring on a specific symbol (function, class, variable).",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Name of the symbol to analyze"}
            },
            "required": ["symbol"]
        },
        required_permission=PermissionMode.PLAN,
        handler=impact_handler
    )
