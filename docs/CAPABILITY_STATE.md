# Capability state — 2026-08-11

## Decision

The current product grade is **B+ overall** and **A- for verifiable code
intelligence**. The supergoal materially improved the product: it converted
retrieved context into typed, generation-bound evidence; made routing, trace,
evidence, and terminal enforcement host-owned; consolidated deployment
verification around explicit artifact roles; fixed a real incremental-indexing
defect; and produced a sealed ten-unit Stage-4 pass.

This is not evidence that the plugin is the strongest general code-search
platform. A balanced four-case public localization pilot favored Sourcegraph.
The defensible position is narrower and more valuable: the plugin is a credible,
unusually auditable implementation of **verifiable code intelligence**.

## Gradecard

| Capability | Grade | Evidence and limit |
|---|---:|---|
| Verifiable code intelligence | A- | The latest sealed Stage-4 successor completed 10/10 units with 1.0 evidence precision, recall, adjudication, routing, and routing-contract accuracy; zero unsupported asserted claims, errors, or host canary violations. The retained manifest SHA-256 is `92d290ef23811c00dd80b6545030a2896f54ff897c1b4f27b2db66f6b73121ac`. This is a bounded five-route, two-repetition holdout, not a broad market benchmark. |
| General code localization | B- | On the public LocBench n=4 pilot, Sourcegraph led at Acc@1 0.75 and MRR@10 0.875. Code-graph reached Acc@1 0.50 and MRR@10 0.625–0.661 across two fresh builds. The local MiniLM semantic arm reached Acc@1 0.00 and MRR@10 0.119. Four cases are directional only. |
| Structural retrieval | B+ | Code-graph reached Acc@10 0.75–1.00 and was the strongest local arm in the pilot. One case moved from outside the top ten to rank seven across fresh builds, so reproducibility caps the grade. |
| Operational scale | B+ | Measured successfully through 2,351 tracked files and 664,120 UTF-8 lines. At that point, cold indexing was 22.30 s for search and 22.06 s for graph; warm p50 query latency was 537 ms for each; combined index storage was about 649 MB. This is not yet a giant-monorepo result. |
| Release and evidence integrity | A- | Exact release assets, checksums, provenance, component identities, canonical cases, immutable evidence IDs, explicit manifest roles, and read-only seals are verified. The release process is still more elaborate than the product requires and should now be simplified, not expanded. |

Cursor, Augment, and Greptile are **ungraded** because no revision-pinned,
callable interface was available. Assigning them inferred scores would be less
honest than leaving the cells blank.

## Public head-to-head

The checked-in measurement uses the first recorded Bug, Feature, Performance,
and Security case from an independently pinned LocBench selection. Ground truth
requires agreement between LocBench and the corresponding merged GitHub pull
request. Every arm receives an oracle-blind query at the exact historical
repository revision, failures remain misses, and no language model is called.

| Retrieval arm | Acc@1 | Acc@3 | Acc@10 | MRR@10 | Warm p50 | Warm p95 |
|---|---:|---:|---:|---:|---:|---:|
| Sourcegraph public search | 0.75 | 1.00 | 1.00 | 0.875 | 469 ms | 1,285 ms |
| code-graph | 0.50 | 0.75 | 1.00 | 0.661 | 73 ms | 550 ms |
| Native lexical | 0.50 | 0.50 | 0.50 | 0.500 | 53 ms | 131 ms |
| Deterministic composition | 0.25 | 0.50 | 0.75 | 0.438 | 603 ms | 1,100 ms |
| code-search, local MiniLM | 0.00 | 0.25 | 0.50 | 0.119 | 464 ms | 623 ms |

Sourcegraph latency includes a public network request and is not directly
comparable with local process latency. The corrected run is bound by file
SHA-256 `592fcb92283446fe4a60bf0f1c58d2aa9313f40d4c67a78689ba1c9f51d4e858`
and embedded result hash
`f965544b1710cf3be6d76ce13cb432ebf2e3dfcbd413666a77ea3445010ac7c9`.
The compact checked-in record is
[`bench/public_measure/results/2026-08-11-summary.json`](../bench/public_measure/results/2026-08-11-summary.json).

The graph arm was stable across five warm calls within each run, but its fresh
build ranking changed on one case. Across the two builds its Acc@10 ranged from
0.75 to 1.00 and MRR@10 from 0.625 to 0.661. That variance is a product finding,
not noise to hide.

## Real-repository scale

| Repository | Files | Lines | Search cold | Graph cold | Search index | Graph index | Search warm p50 | Graph warm p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| UXARRAY/uxarray | 309 | 210,499 | 3.95 s | 0.60 s | 11.1 MB | 16.8 MB | 401 ms | 52 ms |
| Chainlit/chainlit | 513 | 71,204 | 4.63 s | 0.86 s | 18.5 MB | 22.6 MB | 437 ms | 73 ms |
| vllm-project/vllm | 1,263 | 269,055 | 10.97 s | 6.26 s | 104.5 MB | 159.4 MB | 466 ms | 406 ms |
| PrefectHQ/prefect | 2,351 | 664,120 | 22.30 s | 22.06 s | 308.2 MB | 341.0 MB | 537 ms | 537 ms |

The Prefect one-file update exposed a code-search defect: a fresh manager read
the persisted index size as zero and forced a full rebuild. Code-search v0.3.3
loads the persisted index before applying that guard. After the fix, the same
update reported zero added files and one modified file, finishing search refresh
in 2.57 s instead of the 22.30 s cold build (8.7x faster). Graph updated in
5.83 s instead of 22.06 s (3.8x faster).

## What changed during the supergoal

- Backend tools now issue typed immutable `ev:v1` evidence IDs with exact
  source coordinates and index generation. The model selects evidence rather
  than manufacturing ranges.
- A unified host-owned state machine enforces route capability, evidence reads,
  directed traces, selected evidence IDs, and terminal output.
- Canonical response and manifest contracts separate host-observed canaries
  from model declarations and bind artifact roles explicitly.
- The release budget was calibrated to a $2.50 per-case ceiling while retaining
  eight-round-trip and 180-second bounds.
- Code-search v0.3.3 fixes incremental refresh from a fresh manager and is pinned
  as an attested, checksum-bound release in plugin 0.4.20.
- One bounded public comparison and real-repository scale measurement now place
  an external limit around the product claims.

## Next grade increase

Do not add another proof layer. The next work should be product-facing:

1. Expand the public localization comparison to a preregistered 40–100 cases
   with repository/language strata and confidence intervals.
2. Diagnose and eliminate fresh-build graph ranking variance.
3. Measure at least one multi-language repository above one million lines,
   including steady-state memory and repeated incremental updates.
4. Improve semantic retrieval and fusion; the measured MiniLM arm is currently
   the weakest retrieval component. Re-evaluate the production Jina and Voyage
   providers separately rather than generalizing from MiniLM.

Those measurements can raise the general-search and scale grades. More canaries,
sealed-bank variants, or filename-discovery conventions cannot.
