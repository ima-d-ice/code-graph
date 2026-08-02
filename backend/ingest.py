import os
import json
import logging

from app.services.parser_service import SemanticParser
from app.services.neo4j_service import Neo4jService
from app.services.rag_services import HybridRAG

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

WORKSPACE_FILE = "workspace.json"


def ingest_cold(folder_path: str):
    """
    Cold ingestion. Wipes Neo4j and ChromaDB completely.
    Extracts rich semantics using tree-sitter.
    """
    logger.info(f"❄️ Cold ingest started for: {folder_path}")

    neo4j = Neo4jService()
    rag = HybridRAG()
    parser = SemanticParser()

    # Reset databases
    rag.wipe_vector_store()
    neo4j.wipe_database()
    neo4j.ensure_indexes()

    # Parse all files
    parse_results = parser.parse_directory(folder_path)

    # Ingest to Neo4j
    logger.info(f"🕸️ Ingesting {len(parse_results)} files into Neo4j...")
    for res in parse_results:
        if res.errors:
            logger.warning(f"  Parse errors in {res.file_path}: {res.errors}")
            continue

        rel_path = os.path.relpath(res.file_path, folder_path)
        
        # Convert models to dicts for merge
        nodes = [
            {"node_type": n.node_type.value, "name": n.name, "metadata": n.metadata}
            for n in res.nodes
        ]
        edges = [
            {"edge_type": e.edge_type.value, "source_name": e.source_name, "target_name": e.target_name}
            for e in res.edges
        ]
        
        neo4j.merge_file_subgraph(rel_path, nodes, edges)
        
        # Also add to vector DB
        try:
            with open(res.file_path, "r", encoding="utf-8") as f:
                code = f.read()
            rag.ingest_code_text(rel_path, code)
        except Exception as e:
            logger.error(f"  Failed to read {res.file_path} for vectorization: {e}")

    # Print stats
    stats = neo4j.get_stats()
    logger.info(f"📊 Graph Stats: {stats}")

    neo4j.record_ingest(mode="cold")
    neo4j.close()
    logger.info("✅ Cold ingestion complete")


if __name__ == "__main__":
    target_folder = input("Enter the full path to your Python project: ").strip()

    if not os.path.exists(target_folder):
        print("❌ Folder not found")
        exit(1)

    ingest_cold(target_folder)

    # Persist workspace
    with open(WORKSPACE_FILE, "w") as f:
        json.dump({"project_root": target_folder}, f, indent=2)

    print(f"📌 Workspace set to: {target_folder}")
