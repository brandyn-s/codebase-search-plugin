# Capability state — 2026-08-12

## Decision

The product remains **B+ overall** and **A- for verifiable code
intelligence**. The evidence is broader and the architecture is more honest,
but general search superiority is not established. The defensible position is
still verifiable code intelligence: useful retrieval and structural context
whose checkout identity, precision tier, exact evidence, route, and terminal
claims can be checked mechanically.

This iteration improved the product, not merely the release proof. It made
SCIP precision a persistent per-project contract, separated graph reachability
from variable-level taint assurance, added isolated cross-project discovery
and immutable index comparison, expanded the public comparison from 4 to 20
cases, and measured current relationship accuracy and million-line scale.

## Gradecard

| Capability | Grade | Current evidence and limit |
|---|---:|---|
| Verifiable code intelligence | A- | The sealed five-route/two-repetition Stage-4 successor completed 10/10 units with 1.0 evidence precision, recall, adjudication, routing, and routing-contract accuracy; zero unsupported claims, errors, or host canary violations. Backends issue typed immutable evidence IDs, and one host state machine owns route, evidence, trace, and terminal enforcement. This is a bounded holdout, not broad field evidence. |
| General code localization | B | On the balanced public LocBench n=20 comparison, code-search reached Acc@1 0.40, Acc@10 0.85, and MRR@10 0.534. Sourcegraph reached 0.20/0.25/0.225, with three request failures counted as misses. The paired Acc@1 test favored code-search 4-0 with 16 ties, but p=0.125; superiority is not established. Cursor, Augment, and Greptile remain ungraded. |
| Relationship graph | B+ | On a current pinned Go fixture, heuristic CALLS achieved scope-aligned precision 0.953, recall 1.000, and F1 0.976. Raw unscoped precision was 0.540, Go IMPORTS lacks a current oracle, and this does not generalize automatically to every language or relationship. SCIP can improve covered calls, but coverage and drift remain explicit. |
| Graph-only conceptual localization | C | On the n=20 issue-localization comparison, graph-only Acc@1 was 0.10 and MRR@10 was 0.117. This route is not used as the conceptual-discovery primary; code-search remains primary and graph is invoked for explicit relationships, traces, dependencies, and proof. |
| Operational scale | B+ | Direct measurement reached 2.39 million UTF-8 lines and 6,842 tracked files across separate single-repository cases. This establishes million-line operation, not a very large monorepo, unified multi-repository query plane, or distributed organizational fleet. |
| Resource efficiency | B- | On the 2.39-million-line Moto checkout, indexes totaled 566 MB and warm p50 was 632 ms search / 737 ms graph. The largest persisted pair was 1.148 GB on Transformers, with 675 ms / 1.408 s warm p50 and 1.04 GB / 1.72 GB peak RSS. The immutable-generation plus writable-compatibility design deliberately duplicates persisted search artifacts; safely removing that cost requires a persistence redesign. |
| Cross-project operations | B- | Search and graph now support bounded project-balanced discovery across up to 25 isolated indexes without mutating active state. Graph can compare immutable indexes by file content and declaration deltas. Scores remain per-index and non-comparable; there is no organization ACL model, index-to-index semantic score federation, or continuously managed indexing fleet. |
| Product surface | B- | The plugin offers FIND / UNDERSTAND / PROVE, coherent indexing, cross-project discovery, structural evidence, and deterministic verification. It does not provide Sourcegraph's search language/history/ACL surface, Cursor's editor experience, Augment's multi-source context fabric, or Greptile's complete review-feedback loop. Those are deliberate scope limits, not hidden parity claims. |
| Release and evidence integrity | A- | Contract snapshots, exact installed readiness, typed evidence IDs, component identity, explicit artifact roles, and sealed results are verified. Broad deterministic CI remains; expensive empirical validation is limited to one bounded holdout when behavior materially changes. |

## Public retrieval comparison

The checked-in instrument uses the first five eligible cases in each of four
categories from an independently pinned LocBench n=200 selection. Ground truth
requires agreement between LocBench and the corresponding merged GitHub pull
request at the exact historical revision. All arms receive the oracle-blind
issue text, failures remain misses, and no language model is called.

| Retrieval arm | Acc@1 | 95% interval | Acc@3 | Acc@10 | MRR@10 | Warm p50 | Warm p95 |
|---|---:|---:|---:|---:|---:|---:|---:|
| code-search candidate | 0.40 | 0.219–0.613 | 0.65 | 0.85 | 0.534 | 608 ms | 2,143 ms |
| Route-aware composition | 0.35 | 0.181–0.567 | 0.55 | 0.75 | 0.461 | 1,027 ms | 2,754 ms |
| Sourcegraph public search | 0.20 | 0.081–0.416 | 0.25 | 0.25 | 0.225 | 3,930 ms | 35,906 ms |
| code-graph candidate | 0.10 | 0.028–0.301 | 0.15 | 0.15 | 0.117 | 368 ms | 1,408 ms |
| Native lexical | 0.20 | 0.081–0.416 | 0.30 | 0.45 | 0.267 | 128 ms | 447 ms |

The code-search/Sourcegraph Acc@1 comparison produced four wins, zero losses,
and sixteen ties; the exact two-sided paired sign test yielded p=0.125. Three
Sourcegraph request timeouts are included as misses under the frozen contract.
Sourcegraph latency includes public network time and is not directly
comparable with local process latency. The compact record is
[bench/public_measure/results/2026-08-12-n20-summary.json](../bench/public_measure/results/2026-08-12-n20-summary.json).

The old n=4 result remains regression evidence, but it is no longer the
comparative headline. Cursor, Augment, and Greptile remain ungraded because no
callable, revision-pinned interface was available; that absence is a
measurement limitation, not evidence they perform worse.

## Graph precision and assurance contracts

The default graph tier is `heuristic`: tree-sitter plus static resolution
heuristics. A caller can request `precision_tier="scip"` and an optional
`scip_index_path` per project. That choice persists for watcher and incremental
runs. Index and status responses report requested/effective tier, SCIP digest,
document/function coverage, drift, heuristic replacements, inserted edges,
and degradation. Missing or stale SCIP cannot silently present as
compiler-grade success.

SCIP remains an optional coverage layer, not an automatic organization-wide
compiler pipeline. The product does not yet operate a Sourcegraph-scale SCIP
indexing fleet across languages and repositories.

`trace_data_flow` follows CALLS, READS, WRITES, and USAGE graph connectivity.
It does not model variables, value propagation, sanitizers, source-to-sink
semantics, or path feasibility. Requests declaring
`required_assurance="variable_level_taint"` fail closed with a structured
CodeQL handoff. CodeQL remains the appropriate tool for vulnerability-grade
taint and path analysis.

## Relationship accuracy

The current pinned Go CALLS measurement used a deterministic `go/ast` oracle
over five production subsets:

| Operating point | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| Scope-aligned heuristic CALLS | 2,869 | 141 | 0 | 0.953 | 1.000 | 0.976 |
| Raw exact, including oracle-external callers | 2,869 | 2,441 | 0 | 0.540 | 1.000 | 0.702 |

This closes the claim that relationship precision/recall was wholly
unmeasured, but only for this route, language, fixture, and oracle scope. Go
IMPORTS remains explicitly unmeasured by the current oracle, and per-subset
scope-aligned F1 ranges from 0.614 to 1.000. The full report is in the
code-graph repository under
`bench/accuracy/baselines/2026-08-12-code-graph-go-report.md`.

## Scale and resource profile

| Repository | Files | Lines | Search cold | Graph cold | Search index | Graph index | Search warm p50 | Graph warm p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| getmoto/moto | 2,770 | 2,385,397 | 24.54 s | 13.16 s | 267.9 MB | 298.1 MB | 632 ms | 737 ms |
| django/django | 6,842 | 1,047,067 | 44.72 s | 36.05 s | 287.6 MB | 386.2 MB | 892 ms | 1,061 ms |
| huggingface/transformers | 3,167 | 1,192,807 | 64.64 s | 76.88 s | 554.3 MB | 593.5 MB | 675 ms | 1,408 ms |
| PrefectHQ/prefect | 2,351 | 664,120 | 25.11 s | 17.50 s | 307.8 MB | 306.8 MB | 534 ms | 498 ms |

The Prefect one-file update took 2.32 s for search and 5.05 s for graph and
found the injected probe. These figures demonstrate bounded single-repository
operation and make the storage/latency cost visible. They do not demonstrate
huge-monorepo, multi-repository, distributed-fleet, or class-leading resource
efficiency.

## What changed

- Backends issue typed immutable `ev:v1` evidence IDs with exact coordinates
  and index generation; the model selects evidence instead of manufacturing
  ranges.
- One host-owned state machine enforces route capability, evidence reads,
  directed traces, selected IDs, and terminal output.
- Code-search isolates cross-project discovery from active project/provider
  state, caps it at 25 indexes, and interleaves per-project rankings without
  pretending scores are globally comparable.
- Code-graph persists a per-project heuristic/SCIP precision choice and makes
  coverage, drift, and effective tier observable.
- Code-graph distinguishes reachability from variable-level taint assurance
  and hands the latter to CodeQL.
- Code-graph supports project-balanced cross-index localization and immutable
  file/declaration index comparison.
- The public comparison expanded from 4 to 20 balanced cases and added Wilson
  intervals, an exact paired test, category breakdowns, resource-per-line
  metrics, and explicit claim gating.
- A current edge-level Go CALLS measurement was generated; unmeasured
  relationship types remain labeled as such.

## Next grade increase

Do not add another canary or proof layer. The next bounded work is:

1. Expand the public comparison only when a preregistered sample is large
   enough to narrow the Acc@1 interval and reduce live-comparator failures.
2. Add one independent current oracle for a non-Go language and one additional
   relationship type; prioritize IMPORTS and dynamic-dispatch-heavy CALLS.
3. Profile the immutable-generation/writable-mirror storage design and choose
   a safe copy-on-write or direct-generation reader architecture before trying
   to remove duplication.
4. Measure one genuinely large monorepo and one multi-repository workflow,
   including steady-state storage, repeated incremental updates, and failure
   recovery.

Product-surface expansion into a general developer platform, editor, review
bot, or organization indexing service remains outside the present thesis.
