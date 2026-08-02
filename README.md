# code-graph

Graph-Augmented Autonomous Refactoring Engine — an LLM coding agent that
doesn't guess. It plans refactors against a live Neo4j digital twin of the
codebase, executes with a governed multi-agent workflow, verifies every
change against 6 behavioral gates, audits everything into a flight
recorder, and proves its own value with benchmarks and metrics.

> "Public benchmarks are filters, internal evals are the verdict."

```
                  ┌────────────────────────────────────────────┐
                  │                 NEO4j DIGITAL TWIN          │
                  │  semantic AST → files · functions · calls  │
                  └──────────────┬─────────────────────────────┘
                                 │
  POST /refactor ─► PLAN ─► DISCOVER ─► GENERATE ─► VALIDATE ─► COMMIT
  objective         planner     graph impact   executor     6 gates    flight record
                                 │ fallback    (LLM)        + sandbox   + warm re-index
                                 ▼
                          Tree-sitter parser
                          (fail-open grep fallback)
```

## Why graph-first?

- **Precise blast radius** — DISCOVER reads the real call graph, not a guess.
- **Digital-twin queries** — semantic API: orphan functions, callers, cycles,
  god functions (all dead code below was found by the graph, not by grep).
- **The graph is the source of truth; the LLM is the worker** — LLM failures
  are caught by deterministic gates and hard fallbacks, not review.

## Features

- **Multi-agent workflow (LangGraph)**: planner, impact analyst, executor
  (tools + JSON `<changes>` output), 6-gate validator, repair loop (max 3).
- **Graph engine**: Tree-sitter semantic parser, Neo4j ingestion
  (files/functions/calls), warm incremental indexing, freshness tracking.
- **Astor + tree-sitter diff engine**: strict rename propagation and
  dead-code removal with AST-aware safety.
- **Deterministic fallbacks**: rename propagation, dead-code removal, and
  impact analysis all have LLM-free implementations — prompt the model,
  verify with rules.
- **Flight recorder**: append-only audit of every run — objective, blast
  radius, plan, diffs, gate results, graph delta. `GET /changes`.
- **Autonomous gardener**: scans the graph for dead code, files tickets,
  executes them under governance, and proves the codebase improves.
  `POST /gardener/scan`, `POST /gardener/run`.
- **RefactorBench**: internal eval with gold checks — resolution rate,
  blast-radius accuracy, and a graph-vs-grep A/B that demonstrates the
  moat. `GET /benchmarks`.
- **Telemetry + code health**: per-run tokens/cost/timings, SonarQube-style
  A–E maintainability rating, CodeScene hotspots, 0–10 health score with
  snapshots and trends. `GET /metrics*`.
- **Real-time progress**: `/ws/refactor` streams live workflow events.
- **Production surface**: Docker + compose, structured JSON logging,
  optional API-key auth + rate limiting, Prometheus export.

## Quickstart

```bash
# 1. Install (Python 3.11+, Neo4j 5.x, Groq API keys in backend/.env)
pip install -r requirements.txt -r backend/requirements.txt

# 2. Index a repo into the graph (cold ingest)
cd backend && python ingest.py --repo /path/to/repo

# 3. Run the API
uvicorn main:app --host 0.0.0.0 --port 8000
curl -X POST localhost:8000/refactor -d '{
  "project_root": "/path/to/repo",
  "objective": "Rename compute_sum to calculate_total across the codebase and update every call site",
  "file": "lib/utils.py",
  "symbol": "compute_sum"
}'
```

## Running the benchmarks (the moat, quantified)

```bash
cd backend
# Full A/B: graph mode vs plain-text grep mode, 2 tasks × 2 sizes
python ../examples/run_benchmark.py --tasks rename,remove_dead \
  --sizes 10,50 --modes graph,grep --trials 1
```

Expected output: scoreboard (resolution rate, cost), blast accuracy, and a
moat verdict (`GRAPH WINS` / `NO ADVANTAGE DETECTED`). See `METRICS.md`.

### First run (n=1, gpt-oss-120b pool, 2026-08-02)

| Task | Size | Mode | Resolution | Blast found/expected |
|---|---|---|---|---|
| rename | 10 | graph | 100% | 9/9 |
| rename | 10 | grep | 100% | 9/9 |
| rename | 50 | graph | 100% | 41/41 |
| rename | 50 | grep | **0%** | 34/41 |
| remove_dead | 10 | graph | 100% | 1/1 |
| remove_dead | 10 | grep | 100% | 1/1 |
| remove_dead | 50 | graph | 100% | 1/1 |
| remove_dead | 50 | grep | 100% | 1/1 |

**Moat verdict: GRAPH WINS** — resolution 100% vs 75%, blast accuracy 1.0 vs
0.957. At scale (50 call sites) the prompt-only path missed 7 call sites and
failed the gates; the graph-first path never missed. Median refactor cost
measured: ~$0.004 (remove) to ~$0.008 (rename@50).

> Run your own to keep this honest — `--trials 3` for confidence.

## Gardener demo (dead-code hunt, autonomous)

```bash
cd backend && python ../examples/run_gardener_demo.py
```

## API

| Endpoint | Description |
|---|---|
| `POST /refactor` | Run the refactor workflow (objective, file, symbol) |
| `GET /changes`, `GET /changes/{id}` | Flight recorder audit |
| `POST /gardener/scan`, `POST /gardener/run`, `GET /gardener/tickets` | Autonomous dead-code removal |
| `GET /metrics`, `/metrics/cost`, `/metrics/prometheus` | Run telemetry |
| `GET /metrics/health`, `/metrics/trends`, `POST /metrics/health/snapshot` | Code-health intelligence |
| `GET /benchmarks` | RefactorBench scoreboard + moat verdict |
| `WS /ws/refactor` | Live workflow progress |
| `GET /graph/semantic/query` | Digital-twin queries (orphans, callers, cycles, god functions) |
| `GET /health` | Liveness |

Auth: set `CODEGRAPH_API_KEY` to require `X-API-Key` on non-health requests
(rate limit via `CODEGRAPH_RATE_LIMIT`, default 120/min per IP).

## Docker

```bash
NEO4J_PASSWORD=yourpassword docker compose up --build
# api: http://localhost:8000  neo4j browser: http://localhost:7474
```

## Tests & CI

```bash
python -m pytest tests/ -q          # 78 tests, offline
python smoke_test.py                # end-to-end (needs GROQ keys, online)
```

CI: unit tests + smoke on push; nightly RefactorBench baseline (grep mode,
Neo4j-free) reports to the Actions summary. See `METRICS.md` for what each
number means and how it maps to SonarQube/CodeScene/CodeRabbit/DORA.

## Layout

```
backend/
  ingest.py                  cold/warm graph ingestion
  main.py                    FastAPI surface
  app/core/                  workflow, router, guards, gardener,
                             flight recorder, telemetry, health, benchmark, progress
  app/services/              neo4j, rag, parser, diff engines
  app/tools/                 graph tools (impact analysis, fallbacks)
examples/                    rename demo, gardener demo, RefactorBench harness
tests/                       78 offline tests
```

## Docs

- `ARCHITECTURE.md` — design, invariants, observability layers
- `METRICS.md` — every metric mapped to an industry standard
