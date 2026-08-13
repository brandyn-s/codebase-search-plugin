# Bounded index lifecycle and resource baseline — 2026-08-13

This zero-LLM run measures one clean index, one true no-op reindex, and one
comment-only single-file update for code-search and code-graph. It then measures
20 warm queries after clean indexing and after the update. The source is public
Chainlit at revision `8b2d4bacfd4fa2c8af72e2d140d527d20125b07b`: 513 tracked
files and 71,204 UTF-8 text lines. Both servers are released artifacts:
code-search v0.3.6 and code-graph v0.8.0-redacted.9.

| Component / phase | Index time | Peak RSS | Allocated index | Warm p95 |
|---|---:|---:|---:|---:|
| code-search clean | 8.760 s | 679.6 MiB | 15.82 MiB | 383.5 ms |
| code-search no-op | 0.410 s | 722.6 MiB | 15.82 MiB | — |
| code-search one-file update | 0.812 s | 726.5 MiB | 23.76 MiB | 467.2 ms |
| code-graph clean | 0.704 s | 130.9 MiB | 16.40 MiB | 5.3 ms |
| code-graph no-op | 0.261 s | 132.8 MiB | 16.39 MiB | — |
| code-graph one-file update | 0.655 s | 139.2 MiB | 16.86 MiB | 5.6 ms |

Backend-reported deltas confirm the intended lifecycle cells. Search reported
460 added files on clean, no source changes on no-op, and exactly one modified
file plus two added/one removed chunks on update. Graph reported `full`, `noop`,
and `incremental` respectively, with exactly one changed file in the last cell.

Most importantly, the comment-only graph update preserved the exact canonical
semantic fingerprint: 4,355 nodes, 10,479 edges, and SHA-256
`80b95c74ce21ae924db8d2054c68ffa6641accfc363ed735b2518601bb13e25b`
before and after. Earlier diagnostic runs exposed incremental deletion,
relationship rehydration, community-order, and ambiguous-decorator resolution
defects; regression-first fixes closed those defects before this final run.

The dominant measured time cell is code-search clean indexing; the dominant
maintenance-time cell is its one-file update. Search's allocated index grows by
7.94 MiB after the update because publication retains immutable generation
data; graph grows by 0.46 MiB. Neither resource cell violated a declared
threshold, so no resource optimization was made. The only optimized failure
cell was graph semantic equivalence, which had a direct correctness oracle.

This is one machine, one medium public repository, one comment-only mutation,
one query, and 20 warm repetitions. It supports capacity planning and lifecycle
correctness for this bounded case. It does not establish class-leading
efficiency, write-heavy behavior, non-APFS behavior, fleet operation, or a
general latency distribution. The compact machine record is
[`2026-08-13-index-lifecycle-resource-baseline.json`](2026-08-13-index-lifecycle-resource-baseline.json).

Reproduce with a fresh workspace and output path:

```bash
python3 bench/lifecycle_measure.py \
  --repository /absolute/path/to/pinned/chainlit \
  --mutation-file frontend/src/components/atoms/icons/Descope.tsx \
  --search-server /absolute/path/to/run-code-search \
  --graph-server /absolute/path/to/codebase-memory-mcp \
  --local-model /absolute/path/to/pinned/all-MiniLM-L6-v2 \
  --workspace /absolute/fresh/workspace \
  --output /absolute/fresh/result.json \
  --query 'parse source file diagnostics' \
  --warm-repetitions 20
```
