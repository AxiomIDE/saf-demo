# axiom-official/saf-demo — Self-Assembling Agent Demo (EPIC-SAF-003)

This package is the canonical demonstration of Axiom's mid-execution
flow mutation (ADR-051) and live-lineage canvas (ADR-052) features.
It ships three nodes — `LLMReasoner`, `SearchTools`, `AddToFlow` —
wired into a single seed flow whose graph grows itself in response
to a goal.

This is also a worked example for anyone building agentic flows on
Axiom: a tiny, readable package that hits every moving part of the
platform (`ax.secrets`, `ax.reflection.flow.*`, `ax.mutation.flow.*`).

> **Companion CI fixture** lives in the axiom platform repo at
> `internal/durability/mutationfork/saf_demo_lineage_test.go`. It
> mirrors this package's `testdata/golden-trace.json` inline and
> exercises the worker-side fork primitive against a 3-mutation
> lineage. Update both when you change the reference goal.

## What it does

The seed flow is three nodes and one loop edge:

```
                            ┌──────────────┐
       ┌───────────────────►│ LLMReasoner  │
       │  loop (ok==true)   │   (in: Reas) │
       │                    │   (out:ReasO)│
       │                    └──────┬───────┘
       │                           │ action_type == "add_tool"
       │                           ▼
       │                    ┌──────────────┐
       │                    │ SearchTools  │
       │                    │   (registry  │
       │                    │    lookup)   │
       │                    └──────┬───────┘
       │                           ▼
       │                    ┌──────────────┐
       │                    │  AddToFlow   │   ← mutation_capable=true
       └────────────────────┤  (emits      │
                            │   Mutation   │
                            │   Batch)     │
                            └──────────────┘
```

On every loop iteration:

1. **`LLMReasoner`** reads the goal, the current flow topology
   (`ax.reflection.flow.*`), and the mutation history so far. It
   prompts Claude (or replays from a transcript) and outputs either
   `action_type="add_tool"` (with a `ToolSpec`) or
   `action_type="terminate"` (with a final answer).

2. **`SearchTools`** queries the marketplace registry for nodes
   matching the reasoner's `need` and returns the top candidates.

3. **`AddToFlow`** picks the top candidate and calls
   `ax.mutation.flow.add_node` + `ax.mutation.flow.add_edge`. The
   buffered batch attaches to the node's `NodeResponse.mutation_batch`;
   the worker compiles a new `OptimizedGraph` that includes the new
   node, forks the execution onto a child graph (parent transitions
   to `FORKED`), and resumes from there. Live-lineage canvas mode
   materializes the new node into the running flow in real time.

When the reasoner says `terminate`, no outgoing edge condition matches
on its branch, so the run ends naturally. The SPA renders the entire
chain as one growing canvas thanks to ADR-052.

## File layout

```
saf-demo/                           # this repo (axiom_repos/test/saf-demo)
├── axiom.yaml                       # package manifest (3 nodes)
├── messages/messages.proto          # ReasonerIn/Out, ToolSpec/Candidates, MutationAck
├── nodes/
│   ├── llm_reasoner.py              # Claude-or-stub reasoning loop
│   ├── search_tools.py              # registry-or-stub marketplace search
│   ├── add_to_flow.py               # mutation emitter (ax.mutation.flow.*)
│   ├── test_llm_reasoner.py         # pytest unit tests
│   ├── test_search_tools.py
│   └── test_add_to_flow.py
├── seed-flow.json                   # SourceGraph for /graphs/compile
├── testdata/
│   ├── golden-trace.json            # recorded LLM transcript for CI
│   └── search-trace.json            # matching search-result transcript
└── README.md                        # this file

axiom/internal/durability/mutationfork/   # platform repo
└── saf_demo_lineage_test.go             # Go CI fixture (golden trace inlined)
```

## Running it

### Mode 1 — CI fixture (deterministic, no LLM calls)

The Go integration test lives in the platform repo so it can drive
the worker's fork-on-mutation primitive directly. It mirrors this
package's `testdata/golden-trace.json` inline and asserts:

- mutation_seq advances `0 → 1 → 2 → 3` on the lineage root
- three `GRAPH_MUTATED` events land in `execution_debug_events`
- each parent transitions to `FORKED`; the terminal child stays
  `RUNNING` (so the normal dispatch loop carries it to `SUCCEEDED`)
- three outbox rows enqueue, one per child

Run from the axiom repo root with `./dev forward` running:

```bash
TEST_DATABASE_URL="postgres://axiom:axiom@localhost:5433/axiom?sslmode=disable" \
  go test -run TestSAFDemo_GoldenTrace -v ./internal/durability/mutationfork/...
```

The Python node unit tests are self-contained — no DB, no LLM, no
generated proto bindings required:

```bash
cd axiom_repos/test/saf-demo && python3 -m pytest nodes/ -v
```

### Mode 2 — local KIND cluster, stubbed LLM + search

Same nodes, real worker, stubbed brains. Useful for filming the
canvas growing live without paying for tokens.

1. **Publish the package** from this directory:

   ```bash
   cd axiom_repos/test/saf-demo
   AXIOM_API_URL=http://localhost:8082 \
   AXIOM_INGRESS_URL=http://localhost:8080 \
     axiom push
   ```

   `axiom push` reads `axiom.yaml`, builds the container, uploads to
   the registry, and prints the three node ULIDs.

2. **Set the stub env vars** on the deployed Knative service. The
   stub paths must point at files mounted inside the node container;
   for the local KIND demo we bake them in via a tenant config
   override (see `./dev` documentation).

   ```bash
   export AXIOM_LLM_STUB_PATH=/workspace/testdata/golden-trace.json
   export AXIOM_SEARCH_STUB_PATH=/workspace/testdata/search-trace.json
   ```

3. **Compile the seed flow**:

   Substitute the three ULIDs printed by `axiom push` into
   `seed-flow.json`'s `{{ LLM_REASONER_ULID }}` /
   `{{ SEARCH_TOOLS_ULID }}` / `{{ ADD_TO_FLOW_ULID }}` placeholders,
   then `POST` it to `/api/graphs/compile`:

   ```bash
   sed -e "s/{{ LLM_REASONER_ULID }}/$LLM_REASONER_ULID/" \
       -e "s/{{ SEARCH_TOOLS_ULID }}/$SEARCH_TOOLS_ULID/" \
       -e "s/{{ ADD_TO_FLOW_ULID }}/$ADD_TO_FLOW_ULID/" \
       seed-flow.json > /tmp/compiled-seed.json

   curl -X POST http://localhost:8090/api/graphs/compile \
     -H "Authorization: Bearer $AXIOM_API_KEY" \
     -H "Content-Type: application/json" \
     --data-binary @/tmp/compiled-seed.json
   ```

   The response carries the artifact id of the compiled
   `OptimizedGraph`.

4. **Invoke it**:

   ```bash
   curl -X POST http://localhost:8080/v1/flows/invoke \
     -H "Authorization: Bearer $AXIOM_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "artifact_id": "<from step 3>",
       "input": {
         "goal": "fetch a URL, extract its main text, and summarize it",
         "iteration": 1
       }
     }'
   ```

5. **Watch it grow.** Open the execution in the SPA. Because
   `mutation_enabled=true` on the seed flow, the canvas switches into
   `live-lineage` mode and materializes nodes as they're added.
   The `MutationSidebar` logs each step.

### Mode 3 — live Claude, real screencast

Same as Mode 2, except:

- Don't set `AXIOM_LLM_STUB_PATH` — the reasoner will call Claude.
- Register your Anthropic key as a tenant secret:

  ```bash
  axiom secrets put ANTHROPIC_API_KEY sk-ant-...
  ```

- Either set `AXIOM_SEARCH_STUB_PATH` (recommended — keeps the
  candidate set bounded so the screencast is reproducible) or point
  `REGISTRY_URL` at the local registry to use real marketplace
  search.

The reasoner prompts Claude (default `claude-sonnet-4-5`, 512
max_tokens) once per loop iteration. Expected cost on the reference
goal: ~$0.01 / run (3 reasoner calls + 1 terminate call, each ~600
tokens in + ~200 out). Latency: ~1–2 s per LLM hop. Tune in
`nodes/llm_reasoner.py` (constants `MODEL` and `MAX_TOKENS`).

#### Failure modes & policy

The reasoner collapses every provider error into a `terminate`
decision rather than letting an exception escape — this keeps the
demo cleanly exiting on quota/network/parse failures instead of
landing in the retry path:

| failure                       | reasoner response                                                                        |
|-------------------------------|------------------------------------------------------------------------------------------|
| `ANTHROPIC_API_KEY` missing   | `terminate` with explanatory `terminal_answer`                                           |
| Anthropic SDK not installed   | `terminate` ("`anthropic SDK not installed.`")                                           |
| HTTP / quota error            | `terminate` ("`LLM call failed: <provider error>`")                                      |
| LLM returns unparseable JSON  | `terminate` ("`LLM output was not parseable JSON: ...`")                                 |
| LLM proposes a `node_ulid` that SearchTools cannot resolve | `AddToFlow` returns `ok=false`; loop edge doesn't fire; lineage ends. |
| Hard cap (`MAX_ITERATIONS=8`) | `terminate` ("`Reached the 8-iteration cap before settling on a terminal action.`")      |

Each of these paths is exercised by the unit tests in
`nodes/test_llm_reasoner.py`.

## Why this is a useful demo

It exercises every interesting platform surface in one place:

- **`ax.secrets`** — `LLMReasoner` reads `ANTHROPIC_API_KEY` from the
  tenant secret store, never from env.
- **`ax.reflection.flow.*`** (ADR-050) — the reasoner reads its own
  position in the running flow + the full topology to make decisions.
- **`ax.mutation.flow.*`** (ADR-051) — `AddToFlow` is the canonical
  example of a `mutation_capable` node. The platform forks on every
  emit.
- **Live-lineage canvas** (ADR-052) — every fork emits a
  `GRAPH_MUTATED` event; the SPA materializes the new node onto a
  root-anchored canvas in real time.
- **Tenant-scoped marketplace search** — `SearchTools` queries
  `/packages/search` for candidates.

## Limits & known gaps

- The seed flow is depth-0 only (ADR-051 invariant). Sub-flow mutation
  is a deferred follow-up.
- `MutationLineageCap` defaults to 256 per lineage root; the demo
  caps itself at 8 reasoner iterations so the cap never fires in
  normal use.
- `SearchTools` in live mode goes through outbound HTTP to the
  registry rather than through a `ax.*` surface. A sidecar-mediated
  marketplace API is on the open-items list (see `EPIC-SAF-003 §Open
  Items` in `backlog/self-assembling-flows-epics.md`).
- Screencast not included in this PR — capture manually after
  publishing locally; link it from
  `knowledge/vision/agentic-harness-assessment.md` §15 when recorded.
