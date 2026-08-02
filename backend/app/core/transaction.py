import os
import logging
from typing import List, Dict

from app.services.neo4j_service import Neo4jService
from app.services.rag_services import HybridRAG
from app.services.parser_service import SemanticParser

logger = logging.getLogger(__name__)

def apply_changes(changes: List[Dict[str, str]], project_root: str):
    """Writes refactored code back to disk."""
    for change in changes:
        file_path = change["file_path"]
        content = change["content"]
        
        full_path = os.path.abspath(os.path.join(project_root, file_path))
        if not full_path.startswith(os.path.abspath(project_root)):
            raise ValueError(f"Illegal write attempt: {file_path}")
            
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"💾 Applied changes to {file_path}")

def ingest_warm(project_root: str, changed_files: List[str]):
    """
    Runtime-safe ingestion using atomic subgraphs.
    NO database wipe.
    """
    logger.info("🔥 Warm ingest started")
    
    neo4j = Neo4jService()
    rag = HybridRAG()
    parser = SemanticParser()
    
    for file in changed_files:
        full_path = os.path.join(project_root, file)
        
        if not os.path.exists(full_path):
            continue
            
        with open(full_path, "r", encoding="utf-8") as f:
            code = f.read()
            
        # Update vector DB (automatically deletes old chunks for this file)
        rag.ingest_code_text(file, code)
        
        # Parse and update graph DB (atomic merge_file_subgraph)
        try:
            parse_result = parser.parse_file(full_path, code)
            
            # Convert to dicts for merge_file_subgraph
            nodes = [
                {
                    "node_type": n.node_type.value,
                    "name": n.name,
                    "metadata": n.metadata
                }
                for n in parse_result.nodes
            ]
            
            edges = [
                {
                    "edge_type": e.edge_type.value,
                    "source_name": e.source_name,
                    "target_name": e.target_name
                }
                for e in parse_result.edges
            ]
            
            neo4j.merge_file_subgraph(file, nodes, edges)
            logger.info(f"🕸️ Updated graph for {file}")
            
        except Exception as e:
            logger.error(f"Failed to update graph for {file}: {e}")

    neo4j.record_ingest(mode="warm")
    neo4j.close()
    logger.info("✅ Warm ingest complete")

def commit_refactor(changes: List[Dict[str, str]], project_root: str):
    """
    Atomic refactor commit:
    1. Write files
    2. Incrementally re-index affected files
    """
    if not changes:
        logger.warning("No changes to commit")
        return
        
    apply_changes(changes, project_root)
    
    changed_files = [c["file_path"] for c in changes]
    ingest_warm(project_root, changed_files)
