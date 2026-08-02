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


def semantic_search(query: str, k: int = 5) -> str:
    """Hybrid vector + graph search."""
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


def impact_analysis(symbol: str) -> str:
    """Predict the blast radius of changing a symbol."""
    from app.services.neo4j_service import Neo4jService
    
    neo4j = Neo4jService()
    try:
        radius = neo4j.get_blast_radius(symbol)
        
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
            
        return json.dumps(radius, indent=2)
    except Exception as e:
        return f"Error analyzing impact: {e}"
    finally:
        neo4j.close()


def register_graph_tools(registry):
    """Register graph tools with the tool registry."""
    from app.core.tool_registry import PermissionMode
    
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
        handler=impact_analysis
    )
