# Measurements

Every number here is a bounded measurement of a specific pinned release on a
specific instrument. None of them is a general accuracy claim or a market-wide
superiority claim. Plugin 0.4.32 (the last internal release) pinned code-search
v0.3.6 and code-graph v0.8.0-redacted.11; the numbers below are from that pin.

## Public file localization (LocBench, n=80)

The direct public instrument lives under `bench/public_measure/`. The frozen
balanced run contains 80 two-source-corroborated cases across bug, feature,
performance, and security categories. Code-search reached Acc@1 0.375,
Acc@10 0.788, and MRR@10 0.503 versus Sourcegraph at 0.150/0.188/0.165 with
zero request failures. The paired Acc@1 result was 22 wins, 4 losses, and 54
ties (p=0.00053). This supports narrow file-localization superiority on this
frozen endpoint only.

A separate same-index replay isolated the v0.3.6 source-role prior and result
diversification: Acc@1 improved from 0.3625 to 0.3875 and MRR@10 from 0.49147
to 0.51608, with 13 improved cases and no regressions. It did not re-run the
public comparator.

Graph-only localization on the same n=80 replay improved from Acc@1 0.175 to
0.200, Acc@10 0.350 to 0.400, and MRR@10 0.219 to 0.260 after lexical seed
quality was preserved through graph expansion. The absolute graph-only result
remains below code-search, so conceptual discovery stays search-primary.

## Compiler-tier relationship oracles

Independent Go SSA/RTA and TypeScript compiler-API oracles measure released
CALLS and IMPORTS relationships. Code-graph v0.8.0-redacted.11 preserves 13/13
TypeScript declared type, 456/456 TypeScript IMPORTS, and direct-method
relationship results.

## Scale (LLVM)

Both released backends completed a clean, revision-pinned LLVM checkout
containing 39,222,246 UTF-8 source lines and 160,123 tracked files. Search
indexed it in 609.3 seconds with 3.65 GB peak RSS, a 4.98 GB index, and 3.77 s
warm-query p50. Graph indexed it in 2,198.0 seconds with 9.43 GB peak RSS, a
2.89 GB index, and 9.78 s warm-query p50. The combined 7.87 GB footprint is
direct very-large-single-host evidence, not a distributed-fleet claim.

The latest graph release replays one fixed LLVM `code_localize` query over
that preserved index. Median latency fell from 12.95 seconds to 3.02 seconds
(4.29x), and a fresh-process sample reduced peak RSS from 1.78 GB to 627 MB
while preserving the ranked-output SHA-256 exactly.

## Index lifecycle

A zero-LLM clean/no-op/one-file-update lifecycle instrument on pinned Chainlit
(71,204 text lines) measured code-search at 8.760 s / 0.410 s / 0.812 s and
code-graph at 0.704 s / 0.261 s / 0.655 s for those three phases. The graph's
canonical fingerprint remained identical after a comment-only update. See
[`../bench/baselines/2026-08-13-index-lifecycle-resource-baseline.md`](../bench/baselines/2026-08-13-index-lifecycle-resource-baseline.md).

Search v0.3.6 uses APFS copy-on-write clones for mutable compatibility mirrors
of immutable generations. In a controlled 282,106,413-byte artifact replay,
initial allocation fell from 282,017,792 bytes to 446,464 bytes while retaining
distinct inodes and independent writes. Portable copy remains the fallback on
unsupported filesystems.

## Routing and evidence holdout

The latest sealed Stage-4 successor completed all ten five-route,
two-repetition units and passed every fixed gate: 1.0 precision, recall,
adjudication, routing, and routing-contract accuracy, with zero unsupported
asserted claims, errors, or host canary violations. That single bounded result
is the current empirical release claim; it is not a statistical ranking. See
`bench/e2e/README.md`.

## Historical component-only measurements

The table below predates the provenance-bound routing/evidence harness. It
compares embedding providers inside code-search on 102 historical queries
across four private subprojects. This is
not an integrated E2E comparative grade and must not be presented as a
current live plugin result.

| Provider | Model | MRR (Nix) | MRR (Rust svc) | MRR (Rust lib) | MRR (TypeScript) | Data leaves machine? | Cost |
|----------|-------|-----------|----------------|----------------|------------------|---------------------|------|
| **`voyage-context`** | voyage-context-3 | **0.723** | **0.783** | **0.861** | **0.677** | Yes | ~$0.06/1M tokens |
| historical `voyage` selector | voyage-code-3 | 0.584 | 0.742 | 0.861 | 0.642 | Yes | ~$0.06/1M tokens |
| **`jina` (enriched)** | jina-code-embeddings-0.5b | **0.638** | 0.742 | ~0.86 | **0.660** | **No** | **Free** |
| `jina` (baseline) | jina-code-embeddings-0.5b | 0.582 | 0.742 | ~0.86 | 0.660 | No | Free |
| `local` | all-MiniLM-L6-v2 | ~0.35 | ~0.45 | ~0.50 | ~0.40 | No | Free |

*Jina "enriched" = default mode. Prepends sibling chunk names to each chunk's
header, approximating Voyage's contextualized embeddings.*

At the time of this measurement the `voyage` selector resolved to
`voyage-code-3`. At the pinned code-search release, `voyage` maps to
`voyage-4-large`, while `voyage-code-3` is a separately selected non-default
provider.

Key findings from that run:

- `voyage-context-3` led the tested models. Its advantage came from embedding
  chunks with awareness of their file context. The advantage was largest for
  declarative configuration languages (+24% on Nix) and smallest for
  self-contained libraries (0% on Rust libs).
- `jina-code-0.5b` with enriched headers closed 40% of the gap to Voyage on
  Nix (0.582 to 0.638, reference 0.723), entirely on-device.
- `jina` beat `voyage-code-3` on Nix (+9.2%) and TypeScript (+2.8%) while
  running locally.

The content-addressed five-arm localization instrument lives under
`bench/compare/`; see `bench/compare/README.md` for frozen controls, fixture
falsifiers, public-pin requirements, and privacy boundaries.
