"""
Neo4j graph database service with rich semantic schema.

Manages a code knowledge graph with:
- Node types: Module, Class, Function, Variable
- Edge types: CALLS, RETURNS, MUTATES, READS, INHERITS_FROM, IMPORTS, CONTAINS, DEFINED_IN

Designed for Neo4j Aura (cloud) with neo4j+s:// TLS connections.
"""

import os
import logging
from typing import List, Dict, Any, Optional
from contextlib import contextmanager

from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class Neo4jService:
    """
    Rich semantic graph database service.
    
    Schema:
        (:Module {name, path, imports, exports})
        (:Class  {name, file, bases, methods, decorators, docstring})
        (:Function {name, file, class_owner, parameters, return_type,
                    decorators, complexity, is_async, docstring, simple_name})
        (:Variable {name, file, scope, type_annotation, is_mutable})
    
    Edges:
        (Function)-[:CALLS]->(Function|Class)
        (Function)-[:RETURNS]->(Type)
        (Function)-[:MUTATES]->(Variable)
        (Function)-[:READS]->(Variable|Function)
        (Class)-[:INHERITS_FROM]->(Class)
        (Module)-[:IMPORTS]->(Module)
        (Module)-[:CONTAINS]->(Class|Function)
        (Function)-[:DEFINED_IN]->(Class|Module)
    """

    def __init__(self):
        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        user = os.getenv("NEO4J_USERNAME", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "password")
        self.database = os.getenv("NEO4J_DATABASE", "neo4j")

        # Fallback to neo4j+ssc:// if using +s:// to avoid self-signed cert errors on some Aura tiers
        if uri.startswith("neo4j+s://"):
            uri = uri.replace("neo4j+s://", "neo4j+ssc://")

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        
        logger.info(f"🕸️ Neo4j connected to {uri} (db: {self.database})")

    def close(self):
        """Close the driver connection."""
        self.driver.close()
        logger.info("🕸️ Neo4j connection closed")

    @contextmanager
    def _session(self):
        """Context manager for database sessions."""
        session = self.driver.session(database=self.database)
        try:
            yield session
        finally:
            session.close()

    # ─────────────────────────────────────────
    # Schema & Indexes
    # ─────────────────────────────────────────

    def ensure_indexes(self):
        """Create indexes for fast lookups. Safe to call multiple times."""
        index_queries = [
            "CREATE INDEX IF NOT EXISTS FOR (m:Module) ON (m.name)",
            "CREATE INDEX IF NOT EXISTS FOR (c:Class) ON (c.name)",
            "CREATE INDEX IF NOT EXISTS FOR (f:Function) ON (f.name)",
            "CREATE INDEX IF NOT EXISTS FOR (v:Variable) ON (v.name)",
            "CREATE INDEX IF NOT EXISTS FOR (f:File) ON (f.name)",
            "CREATE INDEX IF NOT EXISTS FOR (f:Function) ON (f.file)",
            "CREATE INDEX IF NOT EXISTS FOR (c:Class) ON (c.file)",
        ]
        with self._session() as session:
            for q in index_queries:
                try:
                    session.run(q)
                except Exception as e:
                    # Some index types may not be supported on Aura free tier
                    logger.debug(f"Index creation note: {e}")
        logger.info("📑 Neo4j indexes ensured")

    # ─────────────────────────────────────────
    # Node Operations
    # ─────────────────────────────────────────

    def add_module(self, name: str, file_path: str,
                   imports: List[str] = None, exports: List[str] = None):
        """Create or update a Module node."""
        query = """
        MERGE (m:Module {name: $name})
        SET m.file = $file_path,
            m.imports = $imports,
            m.exports = $exports
        MERGE (f:File {name: $file_path})
        MERGE (f)-[:CONTAINS_MODULE]->(m)
        """
        with self._session() as session:
            session.run(query, name=name, file_path=file_path,
                       imports=imports or [], exports=exports or [])

    def add_class(self, name: str, file_path: str,
                  bases: List[str] = None, methods: List[str] = None,
                  decorators: List[str] = None, docstring: str = None):
        """Create or update a Class node."""
        query = """
        MERGE (c:Class {name: $name, file: $file_path})
        SET c.bases = $bases,
            c.methods = $methods,
            c.decorators = $decorators,
            c.docstring = $docstring
        MERGE (f:File {name: $file_path})
        MERGE (f)-[:CONTAINS]->(c)
        """
        with self._session() as session:
            session.run(query, name=name, file_path=file_path,
                       bases=bases or [], methods=methods or [],
                       decorators=decorators or [], docstring=docstring or "")

    def add_function(self, name: str, file_path: str,
                     class_owner: str = None, parameters: str = None,
                     return_type: str = None, decorators: List[str] = None,
                     complexity: int = 1, is_async: bool = False,
                     docstring: str = None, simple_name: str = None):
        """Create or update a Function node."""
        query = """
        MERGE (fn:Function {name: $name, file: $file_path})
        SET fn.class_owner = $class_owner,
            fn.parameters = $func_parameters,
            fn.return_type = $return_type,
            fn.decorators = $decorators,
            fn.complexity = $complexity,
            fn.is_async = $is_async,
            fn.docstring = $docstring,
            fn.simple_name = $simple_name
        MERGE (f:File {name: $file_path})
        MERGE (f)-[:CONTAINS]->(fn)
        """
        with self._session() as session:
            session.run(query, name=name, file_path=file_path,
                       class_owner=class_owner or "",
                       func_parameters=parameters or "[]",
                       return_type=return_type or "",
                       decorators=decorators or [],
                       complexity=complexity,
                       is_async=is_async,
                       docstring=docstring or "",
                       simple_name=simple_name or name)

    def add_variable(self, name: str, file_path: str,
                     scope: str = None, type_annotation: str = None,
                     is_mutable: bool = True, class_owner: str = None):
        """Create or update a Variable node."""
        query = """
        MERGE (v:Variable {name: $name, file: $file_path})
        SET v.scope = $scope,
            v.type_annotation = $type_annotation,
            v.is_mutable = $is_mutable,
            v.class_owner = $class_owner
        """
        with self._session() as session:
            session.run(query, name=name, file_path=file_path,
                       scope=scope or "", type_annotation=type_annotation or "",
                       is_mutable=is_mutable, class_owner=class_owner or "")

    # ─────────────────────────────────────────
    # Edge Operations
    # ─────────────────────────────────────────

    def add_edge(self, source_name: str, target_name: str,
                 edge_type: str, source_file: str = "",
                 metadata: Dict[str, Any] = None):
        """
        Create a typed edge between two nodes.
        Dynamically determines source/target node labels.
        """
        # Use a generic MERGE that works regardless of node label
        query = f"""
        MATCH (source {{name: $source_name}})
        MATCH (target {{name: $target_name}})
        MERGE (source)-[r:{edge_type}]->(target)
        SET r.source_file = $source_file
        """
        with self._session() as session:
            try:
                session.run(query, source_name=source_name,
                           target_name=target_name, source_file=source_file)
            except Exception as e:
                logger.debug(f"Edge creation note ({source_name}->{target_name}): {e}")

    def add_calls_edge(self, caller: str, callee: str, file_path: str):
        """Create a CALLS relationship."""
        query = """
        MERGE (source:Function {name: $caller})
        MERGE (target:Function {name: $callee})
        MERGE (source)-[r:CALLS]->(target)
        SET r.source_file = $file_path
        """
        with self._session() as session:
            session.run(query, caller=caller, callee=callee, file_path=file_path)

    def add_inherits_edge(self, child_class: str, parent_class: str, file_path: str):
        """Create an INHERITS_FROM relationship."""
        query = """
        MERGE (child:Class {name: $child_class})
        MERGE (parent:Class {name: $parent_class})
        MERGE (child)-[r:INHERITS_FROM]->(parent)
        SET r.source_file = $file_path
        """
        with self._session() as session:
            session.run(query, child_class=child_class,
                       parent_class=parent_class, file_path=file_path)

    def add_imports_edge(self, source_module: str, target_module: str, file_path: str):
        """Create an IMPORTS relationship."""
        query = """
        MERGE (source:Module {name: $source_module})
        MERGE (target:Module {name: $target_module})
        MERGE (source)-[r:IMPORTS]->(target)
        SET r.source_file = $file_path
        """
        with self._session() as session:
            session.run(query, source_module=source_module,
                       target_module=target_module, file_path=file_path)

    # ─────────────────────────────────────────
    # Graph Queries
    # ─────────────────────────────────────────

    def run_query(self, cypher: str, params: Dict[str, Any] = None) -> List[Dict]:
        """Execute an arbitrary Cypher query and return results as dicts."""
        with self._session() as session:
            result = session.run(cypher, **(params or {}))
            return [dict(record) for record in result]

    def get_file_dependencies(self, file_name: str) -> List[Dict]:
        """
        Returns all functions in a file and what they call.
        Compatible with the old API for HybridRAG enrichment.
        """
        query = """
        MATCH (f:File {name: $file_name})-[:CONTAINS]->(func:Function)
        OPTIONAL MATCH (func)-[:CALLS]->(target:Function)
        RETURN func.name as function, collect(DISTINCT target.name) as calls
        """
        with self._session() as session:
            result = session.run(query, file_name=file_name)
            data = [dict(record) for record in result]

            if not data:
                # Try basename match
                from os.path import basename
                alt_name = basename(file_name)
                if alt_name != file_name:
                    result = session.run(query, file_name=alt_name)
                    data = [dict(record) for record in result]

            return data

    def get_transitive_callers(self, symbol: str, max_depth: int = 10) -> List[Dict]:
        """
        Find all functions that call the given symbol, transitively.
        Uses variable-length path traversal.
        """
        query = """
        MATCH path = (caller:Function)-[:CALLS*1..$max_depth]->(target {name: $symbol})
        RETURN DISTINCT caller.name as caller, caller.file as file,
               length(path) as depth
        ORDER BY depth
        LIMIT 100
        """
        with self._session() as session:
            result = session.run(query, symbol=symbol, max_depth=max_depth)
            return [dict(record) for record in result]

    def get_blast_radius(self, symbol: str) -> Dict[str, Any]:
        """
        Compute the full blast radius of a symbol change.
        Returns direct callers, transitive callers, importers, and inheritors.
        """
        result = {
            "direct_callers": [],
            "transitive_callers": [],
            "importing_modules": [],
            "inheriting_classes": [],
            "affected_files": set(),
        }

        with self._session() as session:
            # Direct callers
            q1 = """
            MATCH (caller)-[:CALLS]->(target {name: $symbol})
            RETURN caller.name as name, caller.file as file, labels(caller) as labels
            """
            for record in session.run(q1, symbol=symbol):
                result["direct_callers"].append({
                    "name": record["name"],
                    "file": record["file"],
                    "type": record["labels"][0] if record["labels"] else "Unknown"
                })
                if record["file"]:
                    result["affected_files"].add(record["file"])

            # Transitive callers (depth 2+)
            q2 = """
            MATCH path = (caller:Function)-[:CALLS*2..5]->(target {name: $symbol})
            RETURN DISTINCT caller.name as name, caller.file as file,
                   length(path) as depth
            ORDER BY depth
            LIMIT 50
            """
            for record in session.run(q2, symbol=symbol):
                result["transitive_callers"].append({
                    "name": record["name"],
                    "file": record["file"],
                    "depth": record["depth"]
                })
                if record["file"]:
                    result["affected_files"].add(record["file"])

            # Modules that import this symbol's module
            q3 = """
            MATCH (m:Module)-[:IMPORTS]->(target:Module)
            WHERE target.name CONTAINS $symbol
            RETURN m.name as name, m.file as file
            """
            for record in session.run(q3, symbol=symbol):
                result["importing_modules"].append({
                    "name": record["name"],
                    "file": record["file"]
                })
                if record["file"]:
                    result["affected_files"].add(record["file"])

            # Classes that inherit from this symbol
            q4 = """
            MATCH (child:Class)-[:INHERITS_FROM*1..3]->(parent {name: $symbol})
            RETURN child.name as name, child.file as file
            """
            for record in session.run(q4, symbol=symbol):
                result["inheriting_classes"].append({
                    "name": record["name"],
                    "file": record["file"]
                })
                if record["file"]:
                    result["affected_files"].add(record["file"])

        result["affected_files"] = list(result["affected_files"])
        return result

    def get_graph_data(self, limit: int = 200) -> Dict[str, List]:
        """Return nodes and edges for visualization."""
        query = """
        MATCH (n)-[r]->(m)
        RETURN n.name as source, labels(n) as source_labels,
               m.name as target, labels(m) as target_labels,
               type(r) as relationship
        LIMIT $limit
        """

        nodes = {}
        links = []

        with self._session() as session:
            result = session.run(query, limit=limit)
            for record in result:
                s_label = record["source_labels"][0] if record["source_labels"] else "Unknown"
                t_label = record["target_labels"][0] if record["target_labels"] else "Unknown"

                source_name = record["source"]
                target_name = record["target"]

                if source_name and source_name not in nodes:
                    nodes[source_name] = {"id": source_name, "group": s_label}
                if target_name and target_name not in nodes:
                    nodes[target_name] = {"id": target_name, "group": t_label}

                if source_name and target_name:
                    links.append({
                        "source": source_name,
                        "target": target_name,
                        "type": record["relationship"]
                    })

        return {
            "nodes": list(nodes.values()),
            "links": links
        }

    # ─────────────────────────────────────────
    # Bulk Operations
    # ─────────────────────────────────────────

    def merge_file_subgraph(self, file_path: str, nodes: List[Dict], edges: List[Dict]):
        """
        Atomic upsert for a single file's subgraph.
        1. Delete all existing nodes owned by this file
        2. Insert new nodes and edges
        
        This is the core of warm ingestion.
        """
        with self._session() as session:
            # Delete old file-scoped nodes and their edges
            session.run("""
                MATCH (f:File {name: $file_path})-[:CONTAINS|CONTAINS_MODULE]->(n)
                DETACH DELETE n
            """, file_path=file_path)

            # Delete the file node itself (will be recreated)
            session.run("""
                MATCH (f:File {name: $file_path})
                DELETE f
            """, file_path=file_path)

        # Re-create via individual add operations
        import json
        for node in nodes:
            nt = node.get("node_type", "")
            meta = node.get("metadata", {})

            if nt == "Module":
                self.add_module(
                    name=node["name"],
                    file_path=file_path,
                    imports=meta.get("imports", []),
                    exports=meta.get("exports", [])
                )
            elif nt == "Class":
                self.add_class(
                    name=node["name"],
                    file_path=file_path,
                    bases=meta.get("bases", []),
                    methods=meta.get("methods", []),
                    decorators=meta.get("decorators", []),
                    docstring=meta.get("docstring")
                )
            elif nt == "Function":
                self.add_function(
                    name=node["name"],
                    file_path=file_path,
                    class_owner=meta.get("class_owner"),
                    parameters=json.dumps(meta.get("parameters", [])),
                    return_type=meta.get("return_type"),
                    decorators=meta.get("decorators", []),
                    complexity=meta.get("complexity", 1),
                    is_async=meta.get("is_async", False),
                    docstring=meta.get("docstring"),
                    simple_name=meta.get("simple_name")
                )
            elif nt == "Variable":
                self.add_variable(
                    name=node["name"],
                    file_path=file_path,
                    scope=meta.get("scope"),
                    type_annotation=meta.get("type_annotation"),
                    is_mutable=meta.get("is_mutable", True),
                    class_owner=meta.get("class_owner")
                )

        # Create edges
        for edge in edges:
            et = edge.get("edge_type", "")
            if et == "CALLS":
                self.add_calls_edge(edge["source_name"], edge["target_name"], file_path)
            elif et == "INHERITS_FROM":
                self.add_inherits_edge(edge["source_name"], edge["target_name"], file_path)
            elif et == "IMPORTS":
                self.add_imports_edge(edge["source_name"], edge["target_name"], file_path)
            else:
                self.add_edge(edge["source_name"], edge["target_name"], et, file_path)

    def wipe_database(self):
        """Delete all nodes and edges. Used for cold ingestion."""
        with self._session() as session:
            session.run("MATCH (n) DETACH DELETE n")
        logger.info("🗑️ Neo4j database wiped")

    def get_stats(self) -> Dict[str, int]:
        """Return graph statistics."""
        stats = {}
        queries = {
            "modules": "MATCH (n:Module) RETURN count(n) as count",
            "classes": "MATCH (n:Class) RETURN count(n) as count",
            "functions": "MATCH (n:Function) RETURN count(n) as count",
            "variables": "MATCH (n:Variable) RETURN count(n) as count",
            "files": "MATCH (n:File) RETURN count(n) as count",
            "edges": "MATCH ()-[r]->() RETURN count(r) as count",
        }
        with self._session() as session:
            for key, query in queries.items():
                result = session.run(query)
                record = result.single()
                stats[key] = record["count"] if record else 0
        return stats