# Bounded public retrieval and scale measurement

This directory contains a small, direct measurement of the installed code
intelligence backends. It intentionally does not invoke a language model or
the Stage-4 release harness.

The measurement answers two narrow questions:

1. On a balanced four-case public LocBench pilot, how do code-search,
   code-graph, their deterministic composition, a local lexical baseline, and
   Sourcegraph public code search rank the oracle files at the same historical
   revision?
2. On those real repositories, what are cold indexing time, peak resident
   memory, index size, and warm-query latency for the installed local
   backends? One predeclared repository also receives a one-file incremental
   update.

This is a directional pilot, not a statistically powered market ranking. It
does not evaluate Cursor, Augment, or Greptile because no callable, revision-
pinned interface for those products is available in this environment. It also
does not evaluate answer quality or agent behavior: all arms are direct,
zero-LLM retrieval calls.

## Ground truth

`cases.json` contains only public issue text and immutable repository pins.
`oracle.json` contains the labels and is loaded only by the scorer. The labels
are accepted only where two public sources agree:

- LocBench V1 at its pinned dataset revision and Parquet SHA-256; and
- the merged GitHub pull request, whose base revision, changed file, and diff
  hunk function header agree with the LocBench label.

The four cases are the first Bug, Feature, Performance, and Security cases in
the previously recorded 200-case pin. Selection therefore predates this run
and is not based on observed performance.

The query text is independently available in public GitHub issues:
[UXARRAY #1116](https://github.com/UXARRAY/uxarray/issues/1116),
[Chainlit #1359](https://github.com/Chainlit/chainlit/issues/1359),
[vLLM #3127](https://github.com/vllm-project/vllm/issues/3127), and
[Prefect #16105](https://github.com/PrefectHQ/prefect/issues/16105). The
checked-in files do not redistribute the external LocBench Parquet or its
complete case set.

## Frozen measurement contract

- File localization is the primary endpoint: Acc@1, Acc@3, Acc@10, and
  MRR@10 over ten distinct ranked files.
- The issue text is the normalized first paragraph recorded by the existing
  200-case pin.
- Code-search receives the issue text in hybrid mode. The released v0.3.4
  adapter extracts explicit symbols, qualified names, and GitHub blob paths,
  widens the candidate pool when those signals are present, reranks each
  lexical/vector arm before fusion, and applies a bounded post-fusion boost.
- Code-graph receives deterministic identifier/title anchors through
  `code_localize` with substring seeds and depth 3.
- Sourcegraph receives the same anchors through its documented V3 streaming
  search API, scoped to the exact repository and revision.
- `composed` is a deterministic route-aware cascade. Conceptual localization
  keeps code-search primary; explicit caller, callee, trace, dependency,
  relationship, or impact questions keep code-graph primary. The secondary
  ranking only fills unseen paths, so fusion cannot dilute the chosen route.
  It has no model or tuning step.
- All failures remain misses. No arm falls back to another arm.
- Cold indexes use the installed plugin binaries and the pinned local
  `sentence-transformers/all-MiniLM-L6-v2` model revision recorded in
  `contract.json`.
- Warm latency uses five calls per local backend and repository. The first call
  supplies the scored ranking; later calls measure warm behavior and cannot
  change the score.

## Run

The runner needs public network access for GitHub checkout and Sourcegraph,
plus the installed plugin binaries. Use a fresh output directory:

```bash
python3 bench/public_measure/run.py \
  --contract bench/public_measure/contract.json \
  --cases bench/public_measure/cases.json \
  --oracle bench/public_measure/oracle.json \
  --selection-pin /absolute/operator-only/locbench-n200-pin.json \
  --dataset-parquet /absolute/operator-only/test-00000-of-00001.parquet \
  --output /absolute/fresh/output.json \
  --workspace /absolute/fresh/workspace \
  --search-server /absolute/path/to/run-code-search \
  --graph-server /absolute/path/to/codebase-memory-mcp \
  --local-model /absolute/path/to/pinned/all-MiniLM-L6-v2
```

The runner first executes one tiny known-truth fixture check. It then writes a
single JSON result with raw ranked paths, per-case metrics, aggregate metrics,
scale observations, component hashes, and run provenance. It never writes to
the pinned source checkout or calls a model API.

## Latest measured result

The 2026-08-12 run is summarized in
[`results/2026-08-12-summary.json`](results/2026-08-12-summary.json). On the
unchanged directional n=4 pilot, released code-search v0.3.4 and route-aware
composition each reached Acc@1 1.00 and MRR@10 1.00. Sourcegraph reached 0.75
and 0.875, code-graph 0.50 and 0.583, and native lexical 0.50 and 0.50. The
preceding frozen run is retained in
[`results/2026-08-11-summary.json`](results/2026-08-11-summary.json): its
code-search arm measured 0.00/0.119 and symmetric composition 0.25/0.438.
This is a direct before/after result on the same cases, but four cases do not
establish statistical superiority.

The largest measured checkout contained 2,351 tracked files and 664,120 UTF-8
lines. In the latest run, cold indexing took 18.82 s for code-search and 18.27 s
for code-graph; warm p50 query latency was 501 ms and 496 ms respectively. The
one-file Prefect update took 2.31 s for search and 5.06 s for graph. The
persisted-index size defect was fixed in code-search v0.3.3 and the regression
remains covered in v0.3.4.

Code-graph v0.8.0-redacted.3 canonicalizes truncated seeds, adjacency traversal,
and equal-score result ordering. All four graph rankings were stable across
five warm calls in the latest fresh build. The prior cross-build variance is
retained as historical evidence; a larger cross-platform repeat is still
needed before claiming universal reproducibility.

The Sourcegraph adapter uses the documented
[V3 streaming endpoint](https://sourcegraph.com/docs/api/stream-api) and
[repository/revision query syntax](https://sourcegraph.com/docs/code-search/queries).
Sourcegraph latency includes public network time and should not be compared
directly with the local process timings.
