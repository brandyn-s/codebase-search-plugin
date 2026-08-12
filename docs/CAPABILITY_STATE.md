# Capability state — 2026-08-12

## Decision

The product remains **B+ overall** and **A- for verifiable code intelligence**.
The grade is now better supported, and general localization moves from B- to
B+, but the four-case public comparison is too small to justify an overall A
or a claim that this is the strongest general code-search platform.

The supergoal did result in a better product. It first consolidated the
verification boundary, then exposed and fixed concrete product defects:
manufactured evidence ranges, route drift, nondeterministic graph ordering,
weak hybrid ranking for explicit code signals, symmetric fusion that diluted
the right backend, and stale incremental-index state. The latest sealed
Stage-4 holdout still provides the behavioral proof, while the latest direct
public measurement shows that the retrieval changes materially improved
localization without adding another proof layer.

The defensible product position is **verifiable code intelligence**: useful
retrieval and structural context whose source identity, exact evidence,
routing, and terminal claims can be checked mechanically.

## Gradecard

| Capability | Grade | Current evidence and limit |
|---|---:|---|
| Verifiable code intelligence | A- | The sealed five-route/two-repetition Stage-4 successor completed 10/10 units with 1.0 evidence precision, recall, adjudication, routing, and routing-contract accuracy; zero unsupported claims, errors, or host canary violations. Manifest SHA-256: `92d290ef23811c00dd80b6545030a2896f54ff897c1b4f27b2db66f6b73121ac`. This is a bounded holdout, not broad field evidence. |
| General code localization | B+ | On the unchanged public LocBench n=4 pilot, code-search v0.3.4 and route-aware composition each reached Acc@1 and MRR@10 of 1.00, versus Sourcegraph at 0.75 and 0.875. The earlier code-search result was 0.00/0.119 and composition was 0.25/0.438. Four cases are directional only. |
| Structural retrieval | B+ | Code-graph v0.8.0-redacted.3 made seed truncation, edge traversal, and equal-score ordering deterministic. The latest run was stable across five warm calls per case, with Acc@1 0.50, Acc@3 0.75, and MRR@10 0.583. Broader cross-build and cross-platform repeats remain. |
| Operational scale | B+ | Measured through 2,351 tracked files and 664,120 UTF-8 lines. On that checkout, cold indexing was 18.82 s for search and 18.27 s for graph; warm p50 query latency was 501 ms and 496 ms; combined index storage was about 578 MB. This is not yet a giant-monorepo result. |
| Release and evidence integrity | A- | Plugin 0.4.21 pins checksum- and provenance-bound code-search v0.3.4 and code-graph v0.8.0-redacted.3. Contract snapshots, exact installed readiness, typed evidence IDs, component identity, explicit artifact roles, and sealed results are verified. The release path should now be simplified, not expanded. |

Cursor, Augment, and Greptile remain **ungraded** because no callable,
revision-pinned interface was available. Their exclusion is a measurement
limit, not evidence that they perform worse.

## Public head-to-head

The checked-in instrument uses the first recorded Bug, Feature, Performance,
and Security cases from an independently pinned LocBench selection. Ground
truth requires agreement between LocBench and the corresponding merged GitHub
pull request. Every arm receives an oracle-blind query at the exact historical
revision; failures remain misses; no language model is called.

| Retrieval arm | Acc@1 | Acc@3 | Acc@10 | MRR@10 | Warm p50 | Warm p95 |
|---|---:|---:|---:|---:|---:|---:|
| code-search v0.3.4 | 1.00 | 1.00 | 1.00 | 1.000 | 493 ms | 565 ms |
| Route-aware composition | 1.00 | 1.00 | 1.00 | 1.000 | 592 ms | 1,017 ms |
| Sourcegraph public search | 0.75 | 1.00 | 1.00 | 0.875 | 309 ms | 38,028 ms |
| code-graph v0.8.0-redacted.3 | 0.50 | 0.75 | 0.75 | 0.583 | 67 ms | 497 ms |
| Native lexical | 0.50 | 0.50 | 0.50 | 0.500 | 54 ms | 120 ms |

Sourcegraph latency includes public network requests and is not directly
comparable with local process latency. The latest result file SHA-256 is
`710a2ac9325435643879bc1be017d435158cd8d3e6d77056f8a29e071e34c862`;
its embedded result hash is
`92a230fd173b4d54989c1e43a343f6ae4530b4859bea66be69068c19287bd8a5`.
The compact record is
[bench/public_measure/results/2026-08-12-summary.json](../bench/public_measure/results/2026-08-12-summary.json).

This result is a strong directional signal, not a market ranking. The next
comparative claim requires a preregistered, stratified sample with confidence
intervals.

## Real-repository scale

| Repository | Files | Lines | Search cold | Graph cold | Search index | Graph index | Search warm p50 | Graph warm p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| UXARRAY/uxarray | 309 | 210,499 | 3.79 s | 0.57 s | 11.1 MB | 13.2 MB | 466 ms | 46 ms |
| Chainlit/chainlit | 513 | 71,204 | 4.24 s | 0.83 s | 18.5 MB | 17.9 MB | 422 ms | 63 ms |
| vllm-project/vllm | 1,263 | 269,055 | 9.72 s | 6.13 s | 104.3 MB | 126.3 MB | 511 ms | 360 ms |
| PrefectHQ/prefect | 2,351 | 664,120 | 18.82 s | 18.27 s | 307.3 MB | 270.3 MB | 501 ms | 496 ms |

The Prefect one-file update reported zero added files and one modified file.
Search refreshed in 2.31 s instead of the 18.82 s cold build (8.13x faster);
graph updated in 5.06 s instead of 18.27 s (3.61x faster). The persisted-size
defect was fixed in code-search v0.3.3 and remains covered in v0.3.4.

## What changed

- Backends issue typed, immutable `ev:v1` evidence IDs with exact source
  coordinates and index generation. The model selects evidence rather than
  manufacturing ranges.
- One host-owned state machine enforces route capability, evidence reads,
  directed traces, selected IDs, and terminal output.
- Code-search recognizes explicit identifiers, qualified names, acronyms,
  camel/snake tokens, and GitHub blob paths; it widens and reranks hybrid
  candidates without abandoning semantic retrieval.
- Code-graph canonicalizes seeds before truncation, orders adjacency before
  traversal, and applies a deterministic tie comparator at the final ranking
  boundary.
- Composition preserves route intent: conceptual localization is search
  primary; explicit relationships, traces, dependencies, and impact are graph
  primary. The secondary backend fills gaps instead of diluting the primary.
- Canonical response and manifest contracts distinguish host observations from
  model declarations and bind artifact roles explicitly.
- The empirical runner uses a $2.50 per-case ceiling with eight-round-trip and
  180-second bounds. One bounded successful holdout is retained; further
  release validation is deterministic unless behavior materially changes.
- Plugin 0.4.21 binds the attested backend releases and the latest readiness
  evidence.

## Next grade increase

Do not add another canary or proof layer. Use the existing harness for:

1. A preregistered 40–100 case public comparison stratified by repository,
   language, and query type, with confidence intervals.
2. One multi-language repository above one million lines, including peak
   memory, steady-state storage, and repeated incremental updates.
3. Cross-platform graph reproducibility and graph-specific relationship
   accuracy, not only file localization.
4. Separate measurements for the production Jina and Voyage embedding
   providers; the current public result uses pinned local MiniLM.

Those results can raise the overall and scale grades. More release-harness
machinery cannot.
