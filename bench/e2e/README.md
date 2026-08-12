# Routing and Evidence Benchmark

This harness scores recorded `/code-explore` traces before anyone assigns
comparative grades to the integrated search experience. The scorer uses only
the Python standard library. A separate trusted host session must perform the
actual MCP calls and preserve the raw artifacts described below.

The bundled `fixture-good` and `fixture-bad` runs validate the scoring
pipeline. They are synthetic fixtures, **not live benchmark results** and not
evidence that this plugin outperforms another tool. Merely changing
`"run_mode"` to `"live"` is rejected.

## Bounded operator pilot

`pilot/run.py` executes eight committed content cases across native,
code-search, code-graph, and composed arms. Read
`pilot/preregistration-v2.json` before running it: the cases, two repetitions,
directed-trace contract, scoring rules, model alias, and activation bar are
fixed there. The set includes one false candidate assertion so correct
rejection is measured rather than inferred. The runner requires exact local
component executables, an existing code-search storage directory, and an
existing local embedding model. It denies mutation and network tools inside the
evaluated sessions.

Every run writes raw JSONL transcripts per repetition, objective case records,
a summary, the selected cases, the preregistration, and the component BOM.
`manifest.json` binds all of them—including every raw transcript—with SHA-256. Operator runs
are intentionally not bundled in this repository because they contain
host-specific paths and full model/tool traces. Preserve the complete output
directory when citing a result.

Example:

```bash
python3 bench/e2e/pilot/run.py \
  --arms native,code-search,code-graph,composed \
  --repetitions 2 \
  --max-budget-usd 1.0 \
  --output-dir /isolated/pilot-run \
  --code-search /verified/bin/code-search-mcp \
  --code-graph /verified/bin/codebase-memory-mcp \
  --code-search-storage /isolated/search-storage \
  --local-model /verified/local-model
```

Historical preregistrations retain their original $1.00 per-case ceiling. Fresh
deployment holdouts use a $2.50 per-case hard ceiling after calibration showed
that valid eight-round-trip mixed-route work could require $1.30 and that
repeated-case cost varied materially. This is a ceiling, not an expected spend;
the unchanged eight-round-trip and 180-second limits remain the primary
execution bounds. Two repetitions over this small fixture improve stability
evidence and expose route variance. They are still not enough for statistical
rankings or a comparative accuracy grade.
The measurements and retained constraints are recorded in
[`BUDGET_CALIBRATION.md`](BUDGET_CALIBRATION.md).

Run broad deterministic checks in CI. For an empirical release decision, run
one fresh five-route, two-repetition holdout exactly once; do not add canary
holdouts or tune against a consumed bank.

After a completed primary run identifies a bounded failure cell, use the
separate targeted registration without changing the adjudicated cases:

```bash
python3 bench/e2e/pilot/run.py \
  --arms composed \
  --repetitions 2 \
  --preregistration bench/e2e/pilot/preregistration-v3.json \
  --output-dir /isolated/targeted-confirmation \
  --code-search /verified/bin/code-search-mcp \
  --code-graph /verified/bin/codebase-memory-mcp \
  --code-search-storage /isolated/search-storage \
  --local-model /verified/local-model
```

The v3 file binds the primary run and its failure mass. Its composed-only result
is a post-primary remediation confirmation, not a replacement primary run or a
superiority claim.

Wave 4.2 and the intervening successor failures are retained historical
results. They exposed model-authored ranges, incomplete route enforcement,
manifest-discovery conventions, budget exhaustion, and backend/runner evidence
registry mismatches. The current consolidated contract uses backend-issued
typed evidence IDs, a unified host state machine, explicit artifact roles, and
canonical case bindings.

The latest sealed Stage-4 successor completed 10/10 units and passed every
fixed gate: evidence precision 1.0, recall 1.0, adjudication 1.0, routing and
routing-contract accuracy 1.0, unsupported asserted-claim rate 0.0, errors 0,
and host canary violations 0. Its retained manifest SHA-256 is
`92d290ef23811c00dd80b6545030a2896f54ff897c1b4f27b2db66f6b73121ac`.
The raw operator output remains external and sealed; it is not committed here.
See [`../../docs/CAPABILITY_STATE.md`](../../docs/CAPABILITY_STATE.md).

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

No raw live benchmark result is bundled. The latest external sealed Stage-4
result is summarized above. With the current readiness-approved BOM, a trusted
host runner can execute every case against `target-repo/` and must save all of
these artifacts in one isolated recording directory:

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
