# VISION — The Compiler for Software Evolution

> **Code enters as text. The graph is the intermediate representation. Every change exits
> graph-planned, graph-applied, graph-verified, and flight-recorded — and then the codebase
> improves itself without being asked.**

## 1. The one-sentence identity

**Code-Graph is a self-governing codebase intelligence platform: the first system that treats a
codebase as a living knowledge graph and makes every software change — planned, executed,
verified, and audited — against that graph, then continuously improves the codebase on its own.**

## 2. Why this has never existed

Every mainstream coding agent (Claude Code, Cursor, Devin) treats code as **text**:

- They approximate blast radius by prompting the model ("find everything that calls this")
- They apply edits as model-written text
- They validate opportunistically (run tests if the user asks)
- They act **on demand** — nothing changes until someone asks

Code-Graph inverts all four:

| Dimension | Text-based agents | Code-Graph |
|---|---|---|
| Understanding | Prompted approximation | Exact graph traversal (Neo4j) |
| Blast radius | LLM guess | Transitive callers, inheritors, importers — exact |
| Edit application | Model-written text | libcst AST transforms (deterministic) |
| Validation | Opportunistic | 6-gate sandbox pipeline, refuses unsafe commits |
| Autonomy | On-demand | Continuous: improves the codebase unasked |
| Evidence | None | Flight recorder per change |

The compiler analogy is exact: **a compiler takes source text and produces verified machine code.
Code-Graph takes a codebase and produces verified codebase evolution.** No one has built the
compiler for software change. This project is the attempt.

## 3. The moat is not the LLM

The LLM is the **worker** — swappable, commoditized. The moat is everything around it:

1. **The knowledge graph (IR)** — exact dependency semantics, persisted across sessions
2. **The verification engine** — gates that refuse unsafe changes, including graph-integrity checks no
   text-based agent can perform
3. **The self-updating digital twin** — every committed change re-ingests the graph (already
   implemented via warm ingest)
4. **The flight recorder** — per-change audit trail: objective → blast radius → plan → diffs →
   gates → graph delta → tests

## 4. The product pillars

### Pillar A — Graph-native understanding (Phase 1)
The graph is the operating surface. Planning consumes the subgraph, not raw files. Semantic
queries ("who mutates X", "who can break Y") are answered by traversal — exact and sub-second,
never guessed.

### Pillar B — Autonomous gardener (Phase 2)
The unprecedented leap: the system continuously analyzes the codebase, generates improvement
tickets with exact risk scores, and auto-executes low-risk ones (dead-code removal, cycle
breaking, complexity reduction) — with gates, evidence, and re-ingestion. The codebase gets
better **without being asked**.

### Pillar C — Verification engine (Phase 3)
Behavior-preservation gates (differential testing), semantic-diff verification (intent must equal
the exact graph delta), and per-change flight records. Trust is earned by evidence.

### Pillar D — Memory & governance (Phase 4)
The graph remembers every refactor, learns conventions, enforces org policy continuously, and
drives cross-repo migrations.

## 5. Competitive position

| Player | Strengths | Code-Graph's edge |
|---|---|---|
| Claude Code / Cursor | General coding, UX, huge context | No graph, no verification gates, no self-improvement |
| Devin | Long-horizon autonomy | Evidence-gated autonomy; graph-exact impact |
| Semgrep / CodeQL | Static analysis | Static, no agentic execution or verification loop |
| Code-Graph | Graph-native, verified, self-governing | The only one with all four pillars |

**We do not compete on "general coding agent." We compete on verified, self-governing,
graph-native change.** In that space there is no incumbent.

## 6. Honest risks

- **Ops dependency**: Neo4j + ChromaDB must run; grep fallback keeps demos alive offline.
- **LLM reliability**: multi-file edits occasionally miss sites — the repair loop absorbs this;
  measure, don't assume.
- **Scope discipline**: Phase 1 is the product, Phase 2 is the moonshot. Ship in order.
- **Benchmarks are the proof**: superiority claims require numbers (call-site coverage, gate pass
  rate, regression rate, cost per refactor) — not vibes.

## 7. Definition of done for "never existed"

A non-technical observer can watch the system: ingest a repo → ask "what can break if I change
X" (exact answer) → approve a ticket the system generated itself → see the change applied,
gated, evidenced, committed, and the graph updated — and ask a question that is answered from
the graph with sub-second exactness. No other tool can complete that loop.
