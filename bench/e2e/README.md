# Routing and Evidence Benchmark

This harness scores recorded `/code-explore` traces before anyone assigns
comparative grades to the integrated search experience. The scorer uses only
the Python standard library. A separate trusted host session must perform the
actual MCP calls and preserve the raw artifacts described below.

The bundled `fixture-good` and `fixture-bad` runs validate the scoring
pipeline. They are synthetic fixtures, **not live benchmark results** and not
evidence that this plugin outperforms another tool. Merely changing
`"run_mode"` to `"live"` is rejected.

## Run it

```bash
python3 bench/e2e/score.py \
  --cases bench/e2e/cases.jsonl \
  --runs bench/e2e/runs/fixture-good.jsonl \
  --thresholds bench/e2e/thresholds.json
```

The good fixture exits 0. The intentionally bad fixture must exit 1:

```bash
python3 bench/e2e/score.py \
  --cases bench/e2e/cases.jsonl \
  --runs bench/e2e/runs/fixture-bad.jsonl \
  --thresholds bench/e2e/thresholds.json
```

## Concrete target fixture

`target-repo/` is the source tree for every benchmark case.
`target-repo-manifest.json` binds every file SHA-256 and derives a deterministic
fixture revision from the sorted `path + NUL + SHA-256` entries. Case evidence
must name a manifested path and an existing line range. Adding, deleting, or
changing any target file invalidates live provenance.

## Live recording workflow

No live result is bundled, and the current blocked component BOM cannot
produce one. After a future BOM is readiness-approved, a trusted host runner
must execute every case against `target-repo/` and save all of these artifacts
in one isolated recording directory:

- the exact cases, thresholds, and readiness-approved component BOM;
- version-matched component evidence with equal ready index identities;
- the target fixture plus `target-repo-manifest.json`;
- a raw MCP transcript containing every tool request, response, evidence ID,
  index error, and observed latency;
- the literal final answers returned by the host;
- an independent claim extraction whose answer SHA-256 and claims bind back to
  those final answers; and
- the projected scoring run JSONL.

Bind those files with the recorder:

```bash
python3 bench/e2e/record_live.py \
  --cases /recording/cases.jsonl \
  --runs /recording/runs.jsonl \
  --thresholds /recording/thresholds.json \
  --component-bom /recording/component-bom.json \
  --component-evidence /recording/component-evidence.json \
  --target-manifest /recording/target-repo-manifest.json \
  --raw-transcript /recording/raw-mcp-transcript.jsonl \
  --final-answers /recording/final-answers.jsonl \
  --claim-extraction /recording/claim-extraction.jsonl \
  --output /recording/provenance.json
```

Then score exactly those bound paths:

```bash
python3 bench/e2e/score.py \
  --cases /recording/cases.jsonl \
  --runs /recording/runs.jsonl \
  --thresholds /recording/thresholds.json \
  --bom /recording/component-bom.json \
  --provenance /recording/provenance.json
```

Every artifact path must remain under the provenance directory. Missing files,
changed SHA-256 values, a changed target tree, version or identity mismatches,
and differences between raw calls, final answers, claim extraction, and the
scored projection all fail closed. Even a passing live run is labeled
`PROVENANCED LIVE MEASUREMENT — NO COMPARATIVE GRADE`; comparative claims
require separately reviewed repeated measurements and a declared baseline.

## JSONL case schema

Each line in `cases.jsonl` is one object:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Must be `1` |
| `case_id` | string | Stable unique identifier |
| `category` | string | Coverage label such as semantic, graph, lexical, mixed, security, stale, or mismatch |
| `query` | string | User request supplied to the host |
| `expected_route` | string | Derived route: `semantic`, `graph`, `lexical`, `mixed`, `security`, or `block_index` |
| `expected_evidence` | string array | Ground-truth evidence IDs, normally `path:start-end` |
| `expected_claims` | non-empty object array | Adjudicated contracts with stable `claim_id`, exact canonical `text`, and non-empty `required_evidence_ids` |
| `expected_index_error` | string | `none`, `stale`, or `identity_mismatch` |

## JSONL recorded-run schema

Each line in a run file is one object:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Must be `1` |
| `run_id` | string | One stable ID shared by all records in a run |
| `run_mode` | string | `fixture` or `live`; modes cannot be mixed |
| `case_id` | string | References exactly one benchmark case |
| `tool_calls` | object array | Ordered calls with `tool`, `arguments`, and optional per-call `latency_ms` |
| `evidence` | string array | Evidence IDs returned to the user |
| `claims` | object array | One adjudication record per expected claim, with the same `claim_id`, normalized assertion `text`, and cited `evidence_ids` |
| `index_error` | string | Observed `none`, `stale`, or `identity_mismatch` state |
| `latency_ms` | number | End-to-end case latency from request to final answer |

The runner derives routing from tool names and arguments rather than trusting
a self-reported route. `claims` is not arbitrary final-answer prose and it is
not a self-reported unsupported count. It is a normalized adjudication
inventory: whitespace is canonicalized, then each stable claim ID must match
the case's exact canonical assertion and cite every required evidence ID.
Missing expected claims, unknown or duplicate IDs, substituted text, and
under-citation are each counted as unsupported. This includes diagnostic
stale/mismatch cases, which must record the adjudicated block claim.

A stale/mismatch case is also an error if the recorded trace continues into
any retrieval tool instead of stopping. The scorer uses one canonical
retrieval set for both route derivation and this block, including graph
lexical `mcp__code-graph__search_code` as well as semantic, graph traversal,
and security tools.

Metrics are micro evidence precision/recall, per-case routing accuracy,
unsupported-claim rate, total/mean tool calls, mean/p95 latency, and
stale-index errors. `thresholds.json` is the CI gate.
