# Routing, evidence, and proofs

## How the two engines work together

`/code-intel` presents three stable public primitives while preserving the
same automatic backend routing and cross-engine coherence checks:

| Public primitive | Routed capability | Example |
|------------------|-------------------|---------|
| FIND | Semantic or lexical code-search | "Find the authentication middleware" |
| UNDERSTAND | Structural code-graph, optionally chained from FIND | "What calls processOrder?" |
| PROVE | Coherent evidence from both engines plus deterministic contradiction and coverage evaluation | "Prove every request path passes through authorization" |

You do not select a backend. Conceptual discovery keeps code-search primary;
explicit caller, callee, trace, dependency, relationship, and impact questions
keep code-graph primary. A complete primary result is not diluted by the other
backend; the secondary engine fills only missing paths when composition is
useful. `/code-explore` remains available as the compact natural-language
workflow and preserves canonical evidence when the installed components expose
it.

### Semantic search (code-search)

Finds code by meaning. Your code is split into chunks (functions, classes,
modules) using tree-sitter AST parsing. Each chunk is embedded; a keyword index
(BM25) runs in parallel. Hybrid search detects explicit code signals such as
identifiers, qualified names, and file paths, widens the candidate pool,
reranks each arm, then applies bounded fusion and signal boosts.

### Structural graph (code-graph)

Maps structure: which functions call which, what imports what, how modules
connect. Tree-sitter parses code into an AST, extracts symbols (functions,
classes, routes, imports), and builds a knowledge graph in SQLite. Extraction
is local. When `VOYAGE_API_KEY` is present, code-graph also sends selected node
text to Voyage during indexing and query text for embedding-backed graph
searches; `CODE_GRAPH_SKIP_EMBEDDINGS=1` disables graph embedding generation.
Seed truncation, adjacency traversal, and equal-score result ordering are
canonicalized so repeated queries do not depend on iteration order.

The default graph tier is tree-sitter plus heuristic static resolution. It is
not compiler-grade. Projects with a current SCIP index can select the
persistent `scip` precision tier; clean Go and TypeScript repositories can opt
into the BOM-pinned generators with `--graph-precision auto`. Index status
reports effective tier, artifact digest, coverage, drift, replacements, and
insertions. A relationship satisfies compiler assurance only when its evidence
reference carries `resolution_source=scip-ingest` and the exact SCIP artifact
digest; enabling the tier does not upgrade every edge.

## Verification boundary

Evidence-capable backend tools issue typed, immutable `ev:v1` identifiers that
bind exact source coordinates and index generation. The model selects those
identifiers; it does not create or edit line ranges. One host-owned state
machine enforces the required route, directed trace, observed evidence IDs, and
terminal-output contract before an answer can complete.

Deployment verification follows one canonical, hash-bound receipt containing
explicit runtime and holdout manifest paths. Holdout manifests declare artifact
roles directly, so verification does not infer control files from directory or
filename patterns. Broad deterministic tests run in CI; a release uses one
bounded five-route, two-repetition empirical holdout after installation.

## Recorded-trace harness

`bench/e2e/` contains a deterministic standard-library harness for recorded
host-model traces. It scores routing accuracy, evidence precision/recall,
unsupported claims, tool calls, latency, and stale/mismatched-index handling.
The bundled runs validate the fixture and CI gate only; they are not live
performance results or comparative grades. See `bench/e2e/README.md` for the
JSONL contract and live-run workflow.

`bench/e2e/pilot/run.py` is the bounded operator runner used for a real
four-arm smoke: native tools, code-search, code-graph, and the composed
workflow. Its preregistration fixes cases, repetitions, scoring rules, a
directed-trace efficiency contract, the model alias, and the activation bar
before execution. Each run preserves raw model transcripts by repetition,
scored projections, the exact component BOM, and SHA-256 bindings for every
artifact. This small fixed fixture improves stability and falsification
evidence; it is not a statistical superiority claim.

## Portable proof packets

Proof bundles can declare an optional capability-level assurance requirement.
The deterministic evaluator preserves an evidence lattice across source
coordinates, lexical/semantic retrieval, structural relationships, compiler
resolution, runtime observation, and variable-level taint. It does not flatten
those sources into one confidence score: a claim remains unresolved when its
support or counterexample does not carry every requested capability.

The first external-analysis adapter projects one selected CodeQL SARIF code
flow into canonical `analysis:v1`, `ev:v1`, and `obs:v1` references:

```bash
python3 scripts/codeql_evidence.py ingest results.sarif \
  --database-manifest codeql-database-manifest.json \
  --query-pack-manifest query-pack-lock.json \
  --repository-id <repository-id> \
  --source-revision <source-revision> \
  --index-generation <index-generation> \
  --output codeql-observation.json
```

The reference binds the exact repository revision and index generation;
CodeQL CLI, extractor, and database-content identity; a passing extraction
quality receipt; query-pack manifest and SARIF digests; query/result/path
selection; and every ordered source/intermediate/sink coordinate. This is an
ingest boundary, not an embedded CodeQL runner. Graph reachability remains
discovery context and never becomes taint evidence.

After `proof_evaluator.py` accepts a proof bundle, export a deterministic
packet containing the canonical bundle, evaluator result, concise Markdown
report, and content-addressed manifest:

```bash
python3 scripts/export_proof.py export proof-bundle.json \
  --output-dir proof-packet
python3 scripts/export_proof.py verify proof-packet
```

Verification recomputes every artifact digest and reruns the deterministic
evaluator. A changed bundle, result, report, manifest, or evaluator outcome is
rejected. The validation workflow exports and verifies the committed proof
fixture before publishing the packet as a retained CI artifact.
