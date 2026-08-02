# ARCHITECTURE

Code-Graph is a layered system. Each layer has a single responsibility; layers communicate only
through well-defined interfaces.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PRESENTATION (Phase 4)                              │
│  FastAPI REST/WS  ·  D3 graph visualizer  ·  Gardener dashboard              │
│  /refactor /plan /impact /ask-graph /graph /metrics /gardener/*              │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │ HTTP / WebSocket
┌───────────────────────────────▼─────────────────────────────────────────────┐
│                          ORCHESTRATION (LangGraph)                           │
│                                                                              │
│    START → PLAN → DISCOVER → GENERATE → VALIDATE → COMMIT → END              │
│                        ↑____________ REPAIR ←_______________↓                │
│    (max 3 repair iterations; conditional gate: commit|repair|abort)          │
│                                                                              │
│    Agent layer:  PlannerAgent → ExecutorAgent → CriticAgent → RepairAgent    │
└───────────────┬───────────────────────────┬──────────────────────────────────┘
                │                           │
┌───────────────▼──────────────┐ ┌──────────▼─────────────────────────────────┐
│       AGENT CORE             │ │           KNOWLEDGE LAYER                   │
│  AgentLoop (tool loop)       │ │                                             │
│  ToolRegistry (permissions)  │ │  SemanticParser (tree-sitter + ast fallback)│
│  LLMRouter (12 keys × 8      │ │      nodes: Module/Class/Function/Variable  │
│    models, RPM/RPD/TPM/TPD,  │ │      edges: CALLS/IMPORTS/INHERITS_FROM/... │
│    circuit breaker, guard)   │ │                                             │
│  DiffEngine (libcst rename)  │ │  Neo4jService (knowledge graph,             │
│  ExecutionSandbox (temp copy)│ │      blast radius, graph data)              │
└───────────────┬──────────────┘ │  HybridRAG (ChromaDB + local embeddings     │
                │                │      + graph enrichment)                    │
                │                └─────────────────────────────────────────────┘
┌───────────────▼─────────────────────────────────────────────────────────────┐
│                         TRANSACTION & INGESTION                              │
│  apply_changes (atomic writes) → commit_refactor → ingest_warm (self-updating│
│  digital twin: graph + vectors re-indexed per committed change)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Layer responsibilities

### 1. Knowledge layer (the IR)
- **`app/services/parser_service.py`** — tree-sitter semantic parsing with AST fallback.
  Extracts Module/Class/Function/Variable nodes and CALLS, READS, MUTATES, RETURNS,
  INHERITS_FROM, IMPORTS, CONTAINS, DEFINED_IN edges, plus cyclomatic complexity.
- **`app/services/neo4j_service.py`** — the knowledge graph. `get_blast_radius` computes exact
  transitive callers (depth ≤5), inheritors, importers — the graph-exact impact analysis.
- **`app/services/rag_services.py`** — hybrid retrieval: function/class-level ChromaDB chunks,
  local embeddings (all-MiniLM-L6-v2), enriched with graph dependencies at query time.

### 2. Agent core
- **`app/core/agent_loop.py`** — the tool-using loop with provider fallback chains and a
  prompt-injection guard (llama-prompt-guard-2-86m).
- **`app/core/tool_registry.py`** — tool registration with permission gating (PLAN/EXECUTE/AUTO).
- **`app/core/llm_router.py`** — 12 Groq keys × 8 models (96 profiles), per-profile
  RPM/RPD/TPM/TPD tracking, circuit breaker, task-based routing, real token accounting via
  `usage_metadata`.
- **`app/core/diff_engine.py`** — deterministic libcst AST transforms (rename_symbol),
  ast+tokenize fallback, naive last resort. Machine applies, model decides.
- **`app/core/sandbox.py`** — isolated validation environment (temp copy of project).

### 3. Orchestration (LangGraph)
- **`app/core/graph_workflow.py`** — the state machine. DISCOVER consumes the subgraph,
  GENERATE emits multi-file changes, VALIDATE runs the gate pipeline, REPAIR fixes on failure,
  COMMIT applies and re-ingests atomically.

### 4. Verification engine (Phase 3 target)
- 5 gates today: syntax → imports → types (mypy) → tests (pytest) → security (bandit).
- Phase 1 adds the **graph integrity gate** (no dangling references post-refactor).
- Phase 3 adds behavior preservation (differential tests) and semantic-diff verification
  (intent equals exact graph delta), plus the per-change flight recorder.

### 5. Transaction & ingestion
- **`app/core/transaction.py`** — atomic multi-file apply + warm ingest: the graph and vector
  store are updated per committed change (the self-updating digital twin).

## Data flow for a refactor

1. `POST /refactor` → state initialized with objective, file, symbol, permission mode
2. PLAN — planner agent explores (graph tools + files), returns JSON plan
3. DISCOVER — `impact_analysis(symbol)` via Neo4j (or grep fallback) → affected files read
4. GENERATE — executor agent rewrites affected files (multi-file `<changes>` XML)
5. VALIDATE — 6-gate pipeline in sandbox; FAIL → REPAIR (max 3 iterations) → re-VALIDATE
6. COMMIT — `commit_refactor`: atomic writes + warm ingest (graph + vectors updated)

## Key invariants

- **The graph is the source of truth** for structure; the LLM is the worker.
- **No commit without gates**: validation failure after max iterations aborts the workflow.
- **The digital twin never goes stale**: every commit re-indexes the graph.
- **Fail-open safety**: guard and graph checks degrade gracefully (grep fallback) so the
  pipeline stays usable when infrastructure is down.
- **No change without evidence**: every COMMIT/ABORT writes a flight record.
- **Improvements are measurable**: every run writes telemetry; health snapshots are
  persisted so trends prove the gardener moves the codebase, not vibes.

## Observability & evidence layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        EVIDENCE LAYER (Phase 2)                           │
│  FlightRecorder (SQLite audit)  ·  Telemetry (tokens/cost/timings)       │
│  CodeHealth (score, debt, hotspots, A–E rating) · health_snapshots       │
│  RefactorBench scoreboard (resolution rate, blast accuracy, moat A/B)    │
└──────────────────────────────────────────────────────────────────────────┘
```

- **`app/core/flight_recorder.py`** — append-only audit: objective → blast radius →
  plan → diffs → gates → graph delta. `GET /changes`, `GET /changes/{id}`.
- **`app/core/telemetry.py`** — per-run metrics: node timings, per-model token
  counts (model-reported usage), estimated cost (Groq pricing table), repair
  iterations, deterministic-fallback usage. `GET /metrics*`.
- **`app/core/code_health.py`** — SonarQube/CodeScene metric family: complexity
  stats, hotspots (complexity × fan-in), dead-code density, tech-debt ratio,
  maintainability rating A–E, composite health score 0–10. Snapshot + trend.
- **`app/core/benchmark.py`** — RefactorBench store: task defs (rename, dead-code
  removal), gold verification, scoreboard + graph-vs-grep moat A/B.
- **`app/core/progress.py`** — real-time workflow progress to `/ws/refactor`.

## Production surface

- Dockerfile + docker-compose (api + Neo4j, healthchecks, volumes).
- Structured JSON logging (one object per line).
- Optional `X-API-Key` auth + per-IP rate limiting (env-driven).
- Prometheus text-format export on `/metrics/prometheus` (zero deps).

## Roadmap mapping

| Phase | Architectural change |
|---|---|
| 0 | Vision docs, CI, tests (this document) |
| 1 | Graph-first DISCOVER, graph integrity gate, semantic query API, freshness endpoint |
| 2 | Gardener (dead-code tickets + auto-execution), flight recorder, metrics, health, RefactorBench |
| 3 | Behavior-preservation gate, semantic diff verification, convention learning |
| 4 | Policy engine, cross-repo migration, frontend/dashboard |
