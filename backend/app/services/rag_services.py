"""
Hybrid RAG service with local embeddings + graph enrichment.

Uses sentence-transformers (all-MiniLM-L6-v2) for free, local, offline embeddings.
ChromaDB for vector storage. Neo4j for graph context enrichment.
LLM Router for answer synthesis.
"""

import os
import shutil
import logging
from typing import List, Dict, Any, Optional

from langchain_core.documents import Document

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class HybridRAG:
    """
    Hybrid vector + graph RAG pipeline.
    
    Search flow:
    1. Embed query locally (sentence-transformers)
    2. Vector similarity search in ChromaDB
    3. Enrich results with Neo4j graph context (dependencies, callers)
    4. Synthesize answer via LLM (Groq)
    
    Ingestion:
    - Function/class-level chunking (not whole files)
    - Each chunk stores source file metadata
    - Graph enrichment happens at query time, not ingestion
    """

    def __init__(self, llm_router=None):
        from app.services.neo4j_service import Neo4jService
        self.neo4j = Neo4jService()

        # Local embeddings — zero API cost, no rate limits
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
            self.embeddings = HuggingFaceEmbeddings(
                model_name="all-MiniLM-L6-v2",
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            )
            logger.info("🧠 Local embeddings loaded (all-MiniLM-L6-v2)")
        except ImportError:
            logger.error(
                "❌ langchain-huggingface not installed. "
                "Run: pip install langchain-huggingface sentence-transformers"
            )
            raise

        # Vector store
        from chromadb import Client as ChromaClient
        from chromadb.config import Settings
        import chromadb

        self.vector_db_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "chroma_db"
        )

        self._chroma_client = chromadb.PersistentClient(path=self.vector_db_path)

        from langchain_chroma import Chroma
        self.vector_store = Chroma(
            client=self._chroma_client,
            collection_name="code_snippets",
            embedding_function=self.embeddings,
        )

        # LLM for synthesis
        self._llm_router = llm_router

        logger.info(f"💾 ChromaDB initialized at {self.vector_db_path}")

    def _get_llm_router(self):
        """Lazy-load LLM router to avoid circular imports."""
        if self._llm_router is None:
            from app.core.llm_router import LLMRouter
            self._llm_router = LLMRouter()
        return self._llm_router

    # ─────────────────────────────────────────
    # Ingestion
    # ─────────────────────────────────────────

    def ingest_code_text(self, file_name: str, code_content: str):
        """
        Ingest a code file into the vector store.
        Chunks by function/class for better retrieval granularity.
        """
        chunks = self._chunk_code(file_name, code_content)

        if not chunks:
            # Fallback: store whole file as one chunk
            chunks = [Document(
                page_content=code_content,
                metadata={"source": file_name, "chunk_type": "file"}
            )]

        # Remove old chunks for this file before adding new ones
        self._delete_file_chunks(file_name)

        self.vector_store.add_documents(chunks)
        logger.info(f"💾 Vectorized {file_name} ({len(chunks)} chunks)")

    def _chunk_code(self, file_name: str, code: str) -> List[Document]:
        """
        Split code into function/class-level chunks.
        Each chunk is a self-contained unit for better retrieval.
        """
        import ast

        chunks = []
        try:
            tree = ast.parse(code)
        except SyntaxError:
            # Can't parse — store as single chunk
            return [Document(
                page_content=code,
                metadata={"source": file_name, "chunk_type": "file"}
            )]

        lines = code.split("\n")

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                start = node.lineno - 1
                end = node.end_lineno or start + 1
                chunk_code = "\n".join(lines[start:end])
                chunks.append(Document(
                    page_content=chunk_code,
                    metadata={
                        "source": file_name,
                        "chunk_type": "class",
                        "name": node.name,
                        "start_line": node.lineno,
                        "end_line": end,
                    }
                ))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = node.lineno - 1
                end = node.end_lineno or start + 1
                chunk_code = "\n".join(lines[start:end])
                chunks.append(Document(
                    page_content=chunk_code,
                    metadata={
                        "source": file_name,
                        "chunk_type": "function",
                        "name": node.name,
                        "start_line": node.lineno,
                        "end_line": end,
                    }
                ))

        # Also store imports block as a chunk
        import_lines = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                start = node.lineno - 1
                end = node.end_lineno or start + 1
                import_lines.extend(lines[start:end])

        if import_lines:
            chunks.append(Document(
                page_content="\n".join(import_lines),
                metadata={
                    "source": file_name,
                    "chunk_type": "imports",
                    "name": "imports",
                }
            ))

        return chunks

    def _delete_file_chunks(self, file_name: str):
        """Remove all existing chunks for a file before re-ingesting."""
        try:
            collection = self._chroma_client.get_collection("code_snippets")
            # Get IDs of existing chunks for this file
            results = collection.get(
                where={"source": file_name},
            )
            if results and results["ids"]:
                collection.delete(ids=results["ids"])
                logger.debug(f"  Deleted {len(results['ids'])} old chunks for {file_name}")
        except Exception as e:
            logger.debug(f"  Chunk cleanup note: {e}")

    def wipe_vector_store(self):
        """Delete all data from the vector store."""
        if os.path.exists(self.vector_db_path):
            shutil.rmtree(self.vector_db_path)
            logger.info("🗑️ ChromaDB wiped")

        # Re-initialize
        import chromadb
        self._chroma_client = chromadb.PersistentClient(path=self.vector_db_path)
        from langchain_chroma import Chroma
        self.vector_store = Chroma(
            client=self._chroma_client,
            collection_name="code_snippets",
            embedding_function=self.embeddings,
        )

    # ─────────────────────────────────────────
    # Search
    # ─────────────────────────────────────────

    def hybrid_search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
        """
        Hybrid vector + graph search.
        
        1. Vector similarity search → relevant code chunks
        2. Graph enrichment → structural context for each result
        """
        logger.info(f"🔍 Hybrid search: '{query[:80]}...'")

        # Vector search
        docs = self.vector_store.similarity_search(query, k=k)
        seen_files = set()
        context_data = []

        for doc in docs:
            file_path = doc.metadata.get("source", "unknown")
            chunk_type = doc.metadata.get("chunk_type", "file")
            chunk_name = doc.metadata.get("name", "")

            # Avoid duplicate file-level entries
            file_key = f"{file_path}:{chunk_name}"
            if file_key in seen_files:
                continue
            seen_files.add(file_key)

            # Graph enrichment
            graph_data = []
            try:
                graph_data = self.neo4j.get_file_dependencies(file_path)
            except Exception as e:
                logger.debug(f"  Graph enrichment failed for {file_path}: {e}")

            context_data.append({
                "file": file_path,
                "chunk_type": chunk_type,
                "chunk_name": chunk_name,
                "code": doc.page_content,
                "structure": graph_data,
            })

        logger.info(f"  Found {len(context_data)} relevant chunks")
        return context_data

    def answer_question(self, user_question: str) -> str:
        """
        Answer a question about the codebase using hybrid RAG.
        
        Flow: search → build context → LLM synthesis
        """
        context = self.hybrid_search(user_question, k=5)

        # Build context string
        context_str = ""
        for item in context:
            context_str += f"\n--- {item['file']} ({item['chunk_type']}: {item['chunk_name']}) ---\n"
            context_str += item["code"][:2000]  # Limit per chunk
            if item["structure"]:
                context_str += f"\nGraph dependencies: {item['structure']}\n"

        prompt = f"""You are a Senior Developer Assistant analyzing a codebase.

User Question: {user_question}

Here is the relevant code context found via Hybrid RAG (Vector + Graph search):

{context_str}

Instructions:
- Answer the question technically and precisely
- Reference specific files, functions, and classes by name
- Use the graph dependency data to explain relationships (e.g., "X calls Y", "A inherits from B")
- If the context doesn't contain enough information, say so clearly
"""

        router = self._get_llm_router()
        response = router.route_sync(
            task_type="summarize",
            prompt=prompt,
        )
        return response

    # ─────────────────────────────────────────
    # Cleanup
    # ─────────────────────────────────────────

    def close(self):
        """Close connections."""
        try:
            self.neo4j.close()
        except Exception:
            pass