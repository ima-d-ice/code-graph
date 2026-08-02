# Metrics & Evidence

How this project proves it works. Every metric below maps to an industry
standard so the claims survive review — no vibes, just numbers.

| Metric | Source | Why it matters |
|---|---|---|
| Resolution rate | RefactorBench gold checks | Can the system actually complete a refactor correctly? |
| Blast radius accuracy | RefactorBench (expected vs found) | Did it find *exactly* the right files — no misses, no collateral? |
| Graph vs grep (moat A/B) | RefactorBench `--modes graph,grep` | Is the Neo4j graph adding value over a plain-text search baseline? |
| Per-run token count & cost | Telemetry (model-reported usage) | Unit economics per refactor; price per merged change. |
| Node timings | `state["node_timings"]` | Where time actually goes (plan vs discover vs generate...). |
| Repair iterations | Flight recorder | How often the gates catch LLM mistakes. |
| Deterministic fallback rate | `fallback_used` flag | How often hard rules had to override the LLM (safety net usage). |
| Health score (0–10) | CodeHealth | Composite of complexity + dead-code + tech-debt — what the gardener moves. |
| Tech-debt ratio & A–E rating | CodeHealth (SonarQube model) | Same scale as SonarQube's maintainability rating. |
| Hotspots | CodeHealth (CodeScene model) | complexity × fan-in — where refactoring pays the most. |
| Dead-code density | CodeHealth orphan analysis | Fuel for gardener tickets. |
| Health trend | health_snapshots | "Trends over absolute values" (CodeScene) — do consecutive gardener runs improve the codebase? |

## Industry mapping

- **SonarQube**: maintainability rating (A–E debt bands), debt ratio
  (debt / cost-to-redevelop), quality gates. We emit our own A–E bands from
  the same formula shape.
- **CodeScene**: hotspots = complexity × fan-in; the priority rule
  "hotspots over absolute complexity". We rank by the same product.
- **CodeRabbit** (AI code review): acceptance rate, avg iterations per PR,
  time-to-merge. Our analog: resolution rate, repair iterations per run,
  wall-clock per refactor.
- **SWE-bench / Terminal-Bench**: the discipline "public benchmarks are
  filters, internal evals are the verdict". RefactorBench is our internal
  eval — small, deterministic, LLM-free verification with gold checks.
- **DORA / WAVE**: deployment frequency, lead time, change failure rate.
  Our analog: per-run telemetry gives lead time (wall clock) and change
  failure (aborted runs / repair iterations).

## Endpoints

| Endpoint | Content |
|---|---|
| `GET /metrics` | Run count, resolution rate, median tokens/cost, avg timings, fallback rate |
| `GET /metrics/cost` | Per-model token totals + estimated USD (Groq pricing table) |
| `GET /metrics/prometheus` | Text-format (v0.0.4) scrape export — zero deps |
| `GET /metrics/health` | Latest code-health snapshot (score, debt, rating, hotspots) |
| `GET /metrics/trends?days=30` | Health snapshot series |
| `POST /metrics/health/snapshot` | Take a snapshot now |
| `GET /benchmarks` | Scoreboard, blast accuracy, moat verdict, recent runs |

## CLI

```bash
# Take a health snapshot of a repo (Neo4j-first, parser fallback)
python3 -m app.core.code_health snapshot --root /path/to/repo
# Trends for the last 30 days
python3 -m app.core.code_health trends --days 30
```

## Cost model (documented assumption)

Groq reports usage tokens; we assume a 3:1 input:output ratio for
unreported paths (`INPUT_TOKEN_FRACTION = 0.75`) and multiply against the
per-model pricing table in `app/core/telemetry.py`. This understates cost
for long-output agents — reported as an assumption, never as fact.
