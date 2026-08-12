# Bounded public retrieval and scale measurement

This directory contains a small, direct measurement of the installed code
intelligence backends. It intentionally does not invoke a language model or
the Stage-4 release harness.

The latest checked-in compact results are:

- `results/2026-08-12-n80-summary.json`: balanced 80-case public
  file-localization comparison, including the bounded paired test;
- `results/2026-08-12-multirepo-summary.json`: direct querying across three
  isolated, revision-pinned repositories; and
- `results/2026-08-12-llvm-scale-summary.json`: both released backends on a
  39,222,246-line LLVM checkout, including cold time, peak RSS, persisted
  bytes, bytes per line, and warm latency.

The measurement answers two narrow questions:

1. On a balanced 20-case public LocBench sample, how do code-search,
   code-graph, their deterministic composition, a local lexical baseline, and
   Sourcegraph public code search rank the oracle files at the same historical
   revision?
2. On those real repositories, what are cold indexing time, peak resident
   memory, index size, and warm-query latency for the installed local
   backends? One predeclared repository also receives a one-file incremental
   update.

This is a bounded comparison, not a market ranking. It reports confidence
intervals and an exact paired test, and it refuses a superiority claim when
that test does not pass. It does not evaluate Cursor, Augment, or Greptile
because no callable, revision-
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

The current sample contains the first five eligible Bug, Feature, Performance,
and Security cases in the previously recorded 200-case pin. Eligibility
requires the complete LocBench label to be corroborated by the merged GitHub
patch before retrieval. One case was excluded before execution because its
live patch did not expose two labeled function headers. Selection therefore
predates the run and is not based on observed performance.

The query text is independently available in the public GitHub issues named by
each checked-in case. The checked-in files do not redistribute the external
LocBench Parquet or its complete case set.

## Frozen measurement contract

- File localization is the primary endpoint: Acc@1, Acc@3, Acc@10, and
  MRR@10 over ten distinct ranked files.
- Acc@1 includes a Wilson 95% interval. The code-search/Sourcegraph Acc@1
  comparison uses an exact two-sided paired sign test. A point estimate alone
  cannot authorize a superiority statement.
- The issue text is the normalized first paragraph recorded by the existing
  200-case pin.
- Code-search receives the issue text in hybrid mode. The released v0.3.5
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

The balanced n=20 run is summarized in
[`results/2026-08-12-n20-summary.json`](results/2026-08-12-n20-summary.json).
Code-search reached Acc@1 0.40 (Wilson 95% interval 0.219–0.613), Acc@10 0.85,
and MRR@10 0.534. Sourcegraph reached 0.20, 0.25, and 0.225 respectively, with
three request timeouts counted as misses. The paired Acc@1 result was four
code-search wins, zero losses, and sixteen ties, but the exact two-sided
p-value was 0.125. The point estimate favors code-search; statistical
superiority is not established.

Graph-only issue localization reached Acc@1 0.10 and MRR@10 0.117. This is a
measured routing boundary: conceptual discovery should remain search-primary;
the graph is for explicit relationships, traces, dependencies, and evidence.
Route-aware composition reached 0.35/0.461 and therefore did not improve the
search-primary result on this sample.

The previous balanced n=4 result remains in
[`results/2026-08-12-summary.json`](results/2026-08-12-summary.json), and the
pre-improvement n=4 baseline remains in
[`results/2026-08-11-summary.json`](results/2026-08-11-summary.json). They are
historical regression evidence, not the current comparative headline.

Single-repository scale now reaches 2,385,397 UTF-8 lines and 6,842 tracked
files across different cases. On the 2.39-million-line Moto checkout, cold
indexing took 24.54 s for search and 13.16 s for graph; warm p50 was 632 ms and
737 ms; the indexes totaled 566 MB. The largest persisted pair was 1.148 GB on
the 1.19-million-line Transformers checkout, with search/graph warm p50 of
675 ms/1.408 s and peak RSS of 1.04 GB/1.72 GB. This demonstrates million-line
single-repository operation, not giant-monorepo, multi-repository fleet, or
distributed organizational scale.

The separate direct multi-repository run queried three isolated, pinned
checkouts totaling 283,785 UTF-8 lines through each released backend. Search
returned the correct project at ranks 2, 1, and 3 and the oracle file within
that project at ranks 1, 1, and 2, with no project errors. Graph returned the
correct project at ranks 1, 1, and 3 but did not return the oracle file in its
top results. Combined indexes occupied 67,542,627 bytes, or 238.0 bytes per
source line. One of three search cases changed ranked results across warm
repetitions; all graph cases were stable. The compact record is
[`results/2026-08-12-multirepo-summary.json`](results/2026-08-12-multirepo-summary.json).
This demonstrates bounded direct cross-project operation, not an organization
indexing fleet, unified ACL model, or globally comparable per-project scores.

The current public headline is the frozen balanced n=80 run in
[`results/2026-08-12-n80-summary.json`](results/2026-08-12-n80-summary.json).
Code-search measured Acc@1 0.375, Acc@10 0.788, and MRR@10 0.503 versus the
Sourcegraph public endpoint at 0.150/0.188/0.165. The exact paired Acc@1 test
recorded 22 wins, 4 losses, and 54 ties (p=0.00053), with zero Sourcegraph
request failures. This supports only the preregistered narrow
file-localization claim; general platform superiority remains prohibited.

The Sourcegraph adapter uses the documented
[V3 streaming endpoint](https://sourcegraph.com/docs/api/stream-api) and
[repository/revision query syntax](https://sourcegraph.com/docs/code-search/queries).
Sourcegraph latency includes public network time and should not be compared
directly with the local process timings.
