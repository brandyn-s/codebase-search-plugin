# Capability state — 2026-08-13

## Decision

The focused product is **A- overall** and **A for verifiable code
intelligence**. It is not an A+ general code-search platform. The strongest
claim is narrower and more defensible: code-search and code-graph can discover,
relate, and prove code facts against an exact checkout while preserving the
origin and assurance tier of every consequential observation.

This grade is based on released and installed behavior, not development plans.
Plugin 0.4.30 pins code-search v0.3.6 and code-graph
v0.8.0-redacted.9. The graph release was built from commit
`b110e6ac2e54682436b3776d90e93d606dcf06f1` with per-platform checksums and
GitHub-hosted build provenance. The user-scoped plugin installation uses those
exact component identities and both installed MCPs pass live connection and
contract checks.

The product improved materially during this effort:

- backends issue typed immutable evidence IDs with exact source coordinates;
- one host-owned state machine enforces route, evidence, trace, and terminal
  completion;
- Go and TypeScript repositories can automatically prepare pinned SCIP
  artifacts outside a clean checkout;
- compiler-derived relationships bind the exact SCIP digest rather than
  relabeling an entire graph as compiler-grade;
- independent Go SSA/RTA and TypeScript compiler-API oracles now measure
  released CALLS, TypeScript IMPORTS, and TypeScript declared
  `INHERITS`/`IMPLEMENTS` relationships;
- a frozen public comparison is expanded from 20 to 80 balanced cases;
- a paired n=80 replay measures a code-aware source-role prior and file
  diversification without re-indexing or oracle access;
- released backends are measured on three repositories at once and on a
  39.2-million-line LLVM checkout; and
- copy-on-write search publication avoids most initial compatibility-mirror
  allocation on APFS while preserving distinct inodes;
- a bounded zero-LLM lifecycle instrument measures clean, no-op, and one-file
  updates with physical storage, process-tree peak RSS, warm p95, backend
  deltas, and exact graph semantic equivalence; and
- storage, peak memory, cold indexing, warm latency, instability, and misses
  remain visible even when they are unfavorable.

## Gradecard

| Capability | Grade | Current evidence and limit |
|---|---:|---|
| Verifiable code intelligence | A | A sealed five-route/two-repetition Stage-4 successor completed 10/10 with 1.0 evidence precision, recall, adjudication, routing, and routing-contract accuracy; zero unsupported claims, errors, or host canary violations. Typed backend IDs and one host state machine make the proof boundary mechanical. This remains one bounded holdout, not a field reliability rate. |
| General code localization | B+ | On the frozen balanced public LocBench n=80 endpoint, code-search v0.3.5 reached Acc@1 0.375, Acc@10 0.788, and MRR@10 0.503. Sourcegraph reached 0.150/0.188/0.165 with zero request failures. A separate same-index paired replay for v0.3.6 improved its immediate baseline from 0.3625 to 0.3875 Acc@1 and from 0.491 to 0.516 MRR@10, with 13 improved cases and no regressions. These establish bounded progress and narrow superiority on one file-localization endpoint—not general platform superiority. Cursor, Augment, and Greptile remain ungraded because no callable revision-pinned interface was available. |
| Relationship graph | A- | The normal tier remains tree-sitter plus static heuristics. The released Go compiler tier has independent aggregate precision 0.969, recall 0.932, and F1 0.950 across code-graph and Cobra; TypeScript reached 138/138 compiler-tier CALLS on Ky, 456/456 project-local static IMPORTS/re-exports across Ky and Chainlit's frontend, and 13/13 normal-tier declared `INHERITS`/`IMPLEMENTS` relationships across three public projects. Other relationships and languages still lack comparable independent oracles, and not every edge is compiler-derived. |
| Graph-only conceptual localization | C+ | On the same n=80 issue-localization endpoint, graph-only reached Acc@1 0.175, Acc@10 0.350, and MRR@10 0.219. That is better than the old n=20 result but remains materially weaker than search. Conceptual discovery remains search-primary; graph is primary for explicit relationships, traces, dependencies, and proof. |
| Operational scale | A- | Both released backends completed a 39,222,246-line, 160,123-file LLVM checkout, and direct querying across three isolated repositories also completed. This is direct single-host evidence, not a distributed indexing fleet or failure-recovery study. |
| Resource efficiency | B | The historical LLVM indexing run persisted 4.98 GB for search and 2.89 GB for graph—7.87 GB combined—and graph indexing peaked at 9.43 GB RSS. The optimized graph localization path separately improved median latency from 12.95 s to 3.02 s with identical output and reduced a fresh-process sample from 1.78 GB to 627 MB. Search v0.3.6 publishes compatibility mirrors with APFS copy-on-write. A new released-component Chainlit lifecycle trial measures clean/no-op/one-file update, allocated storage, process-tree peak RSS, and warm p95; it identifies search clean indexing as the dominant local resource cell. Non-APFS, write-heavy, and fleet behavior remain unmeasured, so efficiency is not class-leading. |
| Cross-project operations | B | Direct zero-LLM querying across three pinned repositories and 283,785 lines completed without project errors. Search found every oracle file at within-project ranks 1, 1, and 2; graph identified the correct projects but missed the oracle files in its top results. This is not organization-wide ACL, fleet, or globally calibrated ranking. |
| Product surface | B- | FIND / UNDERSTAND / PROVE, coherent indexing, compiler-tier provenance, cross-project discovery, and portable proofs are useful. The product does not replicate Sourcegraph's query language/history/ACL UX, Cursor's editor, Augment's context fabric, or Greptile's review loop. That breadth is outside the current thesis. |
| Release and evidence integrity | A | Source, release assets, runtime receipts, installed plugin/MCP identities, compiler artifacts, public inputs, and measurement results are hash-bound. Broad deterministic CI is retained; expensive model validation is limited to one bounded release holdout. |

## Public retrieval comparison

| Retrieval arm | Acc@1 | 95% interval | Acc@3 | Acc@10 | MRR@10 | Warm p50 | Warm p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| code-search v0.3.5 | 0.375 | 0.277–0.485 | 0.613 | 0.788 | 0.503 | 686 ms | 1,597 ms |
| Route-aware composition | 0.363 | 0.266–0.472 | 0.563 | 0.763 | 0.475 | 1,154 ms | 3,758 ms |
| Native lexical | 0.200 | 0.127–0.300 | 0.388 | 0.525 | 0.311 | 135 ms | 597 ms |
| code-graph | 0.175 | 0.107–0.273 | 0.250 | 0.350 | 0.219 | 459 ms | 2,091 ms |
| Sourcegraph public search | 0.150 | 0.088–0.244 | 0.175 | 0.188 | 0.165 | 5,274 ms | 60,200 ms |

The paired code-search/Sourcegraph Acc@1 comparison produced 22 wins, 4
losses, and 54 ties. The exact two-sided paired sign test yielded p=0.00053,
and all 80 Sourcegraph calls completed under the bounded retry policy. The
preregistered narrow claim therefore passes: code-search was superior to the
measured Sourcegraph public endpoint for this frozen, balanced file-localization
task. The general-platform claim remains prohibited.

All 80 search and graph rankings were stable across five warm repetitions.
The compact result is
[`bench/public_measure/results/2026-08-12-n80-summary.json`](../bench/public_measure/results/2026-08-12-n80-summary.json);
the full operator result is bound by SHA-256
`0d0983f3bf4e70cfc120b653be08736d51cae5bf2b187273824ec34fe6e8af5e`.

The instrument is zero-LLM and oracle-blind during retrieval. It uses the first
20 eligible cases in each of four LocBench categories from an independently
pinned n=200 selection. Each expected file must agree between LocBench and the
immutable merged GitHub pull-request record at the exact historical revision.
Twelve cases were excluded before retrieval because the second source did not
expose every required terminal symbol. Backend or comparator failures remain
misses. Sourcegraph is allowed at most three identical attempts; its public
network latency is not directly comparable with local process latency.

The comparison can support only its preregistered file-localization endpoint.
It cannot establish general platform superiority, editor/review quality, code
understanding, or performance against unavailable competitors.

Code-search v0.3.6 was also evaluated in a separate paired, same-index replay
against the immediate pre-change `main` revision. With the same 80 queries,
repositories, revisions, oracle, and stored index generations, its bounded
source-role prior and final-result diversification changed Acc@1 from 0.3625
to 0.3875, Acc@3 from 0.6125 to 0.6250, Acc@10 from 0.7625 to 0.7750, and
MRR@10 from 0.49147 to 0.51608. Thirteen cases improved and none regressed.
This replay did not re-run Sourcegraph and therefore does not replace or enlarge
the released public comparison above.

## Graph precision and the evidence lattice

The default graph tier is `heuristic`: tree-sitter plus static resolution
heuristics. It is broad and useful, but it is not compiler-grade. A project can
request `precision_tier="scip"` and supply a current SCIP index. Index and
status responses report requested/effective tier, artifact digest, coverage,
drift, replacements, insertions, and degradation. Missing or stale SCIP cannot
silently present as compiler-grade success.

Plugin 0.4.30 makes two automatic preparation paths operational:

- Go uses the BOM-pinned `scip-go` release on supported macOS arm64 and Linux
  amd64/arm64 hosts.
- TypeScript uses official `@sourcegraph/scip-typescript` 0.4.0 with a pinned
  npm integrity value, lockfile, and isolated Node 22 runtime on supported
  macOS, Linux, and Windows amd64 hosts.

Both paths require a clean canonical Git root, verify the generator/runtime,
write only to an out-of-tree generation-bound cache, reject checkout mutation,
and require code-graph to report the identical artifact digest. Auto-detection
does not guess on a mixed Go/TypeScript root. A real TypeScript fixture produced
and reused a 2,336-byte immutable index while the checkout remained clean.

Compiler assurance is edge-scoped. Only a canonical relationship whose
`resolution_source` is `scip-ingest` and whose
`resolution_artifact_sha256` matches the prepared artifact satisfies
`compiler_resolution`. Uncovered, drifted, legacy, and heuristic edges do not.
TypeScript accuracy is independently measured for compiler-derived CALLS,
normal-tier static IMPORTS, and normal-tier declared `INHERITS`/`IMPLEMENTS` as
described below. Other TypeScript relationship types remain ungraded.

The proof evaluator preserves an evidence lattice rather than flattening every
signal into one confidence score:

1. exact source coordinates;
2. lexical or semantic retrieval;
3. structural relationship;
4. compiler resolution;
5. runtime observation; and
6. variable-level taint.

A claim remains unresolved when its requested capability is missing. Graph
reachability never becomes taint evidence. `trace_data_flow` follows CALLS,
READS, WRITES, and USAGE connectivity; it does not model variables, values,
sanitizers, source/sink semantics, or path feasibility. Variable-level taint
requests fail closed to a CodeQL handoff. Selected CodeQL SARIF paths can be
ingested only with exact revision, database-quality, query-pack, and ordered
path provenance.

## Independent compiler-tier accuracy

The Go compiler behavior first released in `v0.8.0-redacted.5` was compared with
an independent Go SSA/RTA oracle and remains in `v0.8.0-redacted.9`. The oracle loads source with
`go/packages`, builds SSA, runs RTA with all source functions as roots, and
emits definition coordinates. It reads neither SCIP nor code-graph truth.
Candidate edges are limited to CALLS edges whose resolver is `scip-ingest` and
whose artifact digest equals the exact fixture index.

| Fixture | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| Hand-enumerated synthetic gate | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| code-graph at `41b8400` | 2,643 | 50 | 154 | 0.981 | 0.945 | 0.963 |
| spf13/cobra at `f2878ba` | 504 | 52 | 77 | 0.906 | 0.867 | 0.887 |
| Real-fixture aggregate | 3,147 | 102 | 231 | 0.969 | 0.932 | 0.950 |

Dynamic RTA edges are counted separately and excluded because the current SCIP
ingestion contract emits statically resolved call sites. The preregistered
synthetic and aggregate gates pass, but Cobra's lower recall prevents a
compiler-perfect or language-general claim. The full report is in the
code-graph repository at
`bench/accuracy/baselines/2026-08-12-compiler-tier-calls-report.md`.

The normal heuristic-tier Go CALLS operating point remains separate: precision
0.953, recall 1.000, and F1 0.976 under the scope-aligned go/ast oracle. Its raw
unscoped precision is 0.540 because it includes callers outside that oracle's
analyzed universe. Neither number may be substituted for the compiler-tier
result.

The TypeScript compiler tier has a separate oracle built with the TypeScript
5.9.3 compiler API; it reads neither SCIP nor code-graph output. On a hand
fixture it found all 6 expected calls. On Ky at revision `3419113`, the
scope-aligned named-endpoint comparison produced 138 TP, 0 FP, and 0 FN
(precision/recall/F1 1.000). The scope includes named declarations,
constructors, top-level variable arrows, private identifiers, and nested-arrow
calls attributed to the enclosing stored function. It excludes callable type
signatures, anonymous local functions without graph endpoints, and dynamic
dispatch beyond the current static SCIP contract. This establishes the tested
TypeScript CALLS tier, not compiler-perfect TypeScript graph accuracy.

TypeScript `IMPORTS` has a separate compiler-API oracle that resolves static
imports and re-exports to project-local source files without reading graph
output. In code-graph v0.8.0-redacted.9, Ky produced 83 TP, 0 FP, and 0 FN;
Chainlit's frontend produced 373 TP, 0 FP, and 0 FN. The aggregate 456/456
result covers the measured project scopes, including relative `.js` specifiers
that resolve to `.ts`/`.tsx`, re-export barrels, and unambiguous root-relative
modules. It does not establish arbitrary `paths` globs, package exports,
dynamic imports, JavaScript without TypeScript project configuration, or
language-general IMPORTS accuracy.

Normal-tier TypeScript declared relationships have a separate TypeScript 5.9.3
compiler-API oracle that reads source and compiler symbols but never graph
output. A hand-enumerated fixture passed 5/5. Across Ky, Chainlit's frontend,
and free-style at immutable revisions, the prior graph emitted none of the 13
expected relationships; v0.8.0-redacted.9 produced 10/10 `INHERITS` and 3/3
`IMPLEMENTS` edges with zero false positives. This establishes project-local
declared `extends`/`implements` in the measured scopes, including generic
targets and interfaces extending local type aliases. It does not establish
structural interface satisfaction, runtime prototype changes, or external-
package relationships.

## Very-large-repository and storage measurement

Both released backends completed a clean LLVM checkout pinned to
`2078da43e25a4623cab2d0d60decddf709aaea28`: 160,123 tracked files,
39,222,246 UTF-8 source lines, and 1.77 GB of UTF-8 source text. The measurement
invoked no language model.

| Backend | Cold indexing | Peak RSS | Persisted index | Bytes/source line | Warm p50 | Warm p95 |
|---|---:|---:|---:|---:|---:|---:|
| code-search v0.3.5 | 609.3 s | 3.65 GB | 4.98 GB | 126.9 | 3.77 s | 3.84 s |
| code-graph v0.8.0-redacted.6 | 2,198.0 s | 9.43 GB | 2.89 GB | 73.7 | 9.78 s | 10.50 s |

Graph produced 729,625 nodes and 2,302,869 edges; search produced 183,663
chunks. The combined persisted size was 7.87 GB, or 200.6 bytes per UTF-8
source line. The compact, hash-bound result is
[`bench/public_measure/results/2026-08-12-llvm-scale-summary.json`](../bench/public_measure/results/2026-08-12-llvm-scale-summary.json).

This closes the missing very-large-monorepo measurement on a single host. It
does not demonstrate distributed indexing, organization-wide operation,
failure recovery, or class-leading efficiency. The graph result in particular
shows a concrete optimization target in peak memory and broad warm-query latency.

A separate fixed replay on the preserved LLVM graph index measured the precise
`code_localize` path changed in `v0.8.0-redacted.7`. Five candidate repetitions
had a 3.02-second median versus 12.95 seconds across three released-binary
repetitions (4.29x). A fresh-process comparison measured 3.12 seconds and
627,032,064 bytes peak RSS versus 16.10 seconds and 1,784,184,832 bytes. The
ranked result payload SHA-256 remained
`f50060acf519a7096f8f8c91abc17657de39566eeb18ea3a7e4cd90321100d4b`.
This does not remeasure full indexing, database size, or every graph query.

Search v0.3.6 separately changes publication, not index contents. On APFS it
uses `clonefile(2)` to publish mutable root compatibility mirrors from immutable
generations with distinct inodes. For the same 282,106,413-byte index artifact,
an ordinary copy initially allocated 282,017,792 bytes while the clone allocated
446,464 bytes, a 99.84% reduction. Unsupported filesystems retain the portable
copy path, and unexpected clone failures fail closed. This does not reduce
logical size, retroactively compact old indexes, or guarantee the same saving
after extensive mirror mutation.

The bounded lifecycle instrument separately ran the released components on
Chainlit at revision `8b2d4bacfd4fa2c8af72e2d140d527d20125b07b`
(513 tracked files; 71,204 UTF-8 text lines). It invoked no language model.

| Component / phase | Index time | Peak RSS | Allocated index | Warm p95 |
|---|---:|---:|---:|---:|
| code-search clean / no-op / one-file update | 8.760 / 0.410 / 0.812 s | 679.6 / 722.6 / 726.5 MiB | 15.82 / 15.82 / 23.76 MiB | 383.5 / — / 467.2 ms |
| code-graph clean / no-op / one-file update | 0.704 / 0.261 / 0.655 s | 130.9 / 132.8 / 139.2 MiB | 16.40 / 16.39 / 16.86 MiB | 5.3 / — / 5.6 ms |

Search reported 460 added files on clean, zero changes on no-op, and exactly
one modified file on update. Graph reported `full`, `noop`, and `incremental`
with exactly one changed file. The graph held 4,355 nodes and 10,479 edges, and
its complete qualified-name/relationship fingerprint was byte-identical before
and after the comment-only update. The dominant measured resource cell is
search clean indexing. No preregistered resource threshold failed, so it was
not optimized speculatively; the observed graph equivalence defect was fixed
and regression-bound. This remains one machine, one medium repository, one
mutation, one query, and 20 warm repetitions.

## Direct multi-repository measurement

Three clean revision-pinned repositories were indexed together through the
released backends: UXARRAY, Chainlit, and Okta's Python JWT verifier. They total
852 tracked files and 283,785 UTF-8 lines.

| Backend | Correct project ranks | Oracle-file ranks within project | Warm p50 range | Warm p95 range | Errors |
|---|---|---|---:|---:|---:|
| code-search | 2, 1, 3 | 1, 1, 2 | 62–154 ms | 183–382 ms | 0 |
| code-graph | 1, 1, 3 | not in top results | 563–1,716 ms | 664–2,923 ms | 0 |

The combined indexes occupied 67,542,627 bytes, or 238.0 bytes per source
line. Search rankings changed across repetitions in one of three cases; graph
rankings were stable in all three. This demonstrates direct discovery across
isolated indexes and also shows why conceptual cross-project localization must
remain search-primary. Claims still require project-bound evidence.

The result does not demonstrate a continuously managed fleet, cross-project
semantic score calibration, index-to-index operational diff, organization-wide
ACL enforcement, or distributed failure recovery.

## What remains before A+

Do not add another canary, gate, receipt convention, or empirical holdout. The
next grade increase should come from product and measurement work:

1. Add another callable, revision-pinned public comparator if one becomes
   available; do not grade inaccessible Cursor, Augment, or Greptile surfaces.
2. Improve graph-only conceptual localization separately from relationship
   proof; compiler edges strengthen facts but do not make conceptual ranking
   competitive with semantic search.
3. Extend independent relationship oracles only where users rely on the edge
   kind next. TypeScript declared `INHERITS`/`IMPLEMENTS` is now measured in a
   released graph; choose the next cell from observed demand—for example Go or
   Rust IMPORTS—while retaining per-language and
   per-relationship scores.
4. Use the new bounded clean/no-op/one-file-update lifecycle baseline before
   making the next resource-efficiency change. It identifies search clean
   indexing as the largest local time/RSS cell, but no declared resource gate
   failed; profile that cell on another representative workload before
   optimizing it. Non-APFS publication and write-heavy update behavior remain
   unmeasured.
5. Add an organization-owned indexing service only if real users need fleet,
   ACL, freshness-SLO, and failure-recovery behavior. Do not broaden into a
   generic developer platform merely to imitate Sourcegraph.

The differentiation remains verifiable code intelligence: a smaller surface
whose claims carry exact source identity and explicit assurance. Sourcegraph or
an editor-native product remains preferable when broad search syntax, history,
organization operations, or integrated editing/review is the primary need.
