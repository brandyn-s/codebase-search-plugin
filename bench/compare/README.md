# Five-arm code-localization comparison

This directory contains the offline-first instrument for comparing five
retrieval conditions under one frozen host-model contract:

- `corpus`: no tools; a deterministic whole-repository or explicitly labeled
  query-conditioned pack.
- `native`: `Glob`, `Grep`, and `Read`.
- `code-search`: `Read` and a reviewed read-only code-search tool subset.
- `code-graph`: `Read` and a reviewed read-only code-graph tool subset.
- `composed`: both reviewed MCP subsets under the checked-in plugin routing
  policy.

The checked-in seven-case fixture is instrument validation, not effectiveness evidence.
It proves that good data exits zero, bad data exits one, failures remain
intent-to-treat misses, mismatched identities fail closed, and interrupted
runs resume without duplicate stable keys. It does not show that any arm is
better.

The current public result does not use this model-driven harness. The bounded
zero-LLM comparison in [`../public_measure/`](../public_measure/) calls each
retrieval backend directly. Its 2026-08-12 directional n=4 result gives
code-search and route-aware composition 1.00 Acc@1 and MRR@10 and records
real-repository scale; the preceding run is retained as the before state. This
five-arm harness remains available for a future agent-level study, but its
disabled live executor is not a plugin release blocker.

## Frozen contract

Every arm uses the same provider, exact model and Claude CLI versions, prompt,
system prompt, response schema, temperature, query, pinned checkout, fresh
session, and no memory. The fixed limits are:

- `top_k=10`
- `max_discovery_tool_calls=20`
- `64,000` novel repository-evidence tokens under the frozen tokenizer
- `128,000` total context tokens
- `600` seconds wall time
- plan/read-only permission mode

Arm-specific tool exposure is hashed separately from the common control hash.
The five arms execute in deterministic Latin-square order. Each observation
must disclose requested K, candidate count, effective K, and truncation. A
component, schema snapshot, routing policy, prompt, model, cost, or context
identity mismatch stops the run; no arm silently falls back.

Repository text is untrusted data. It cannot authorize a tool, write, network
request, secret read, or policy change. `system.md` states that boundary, and
the fixture repository itself includes a content-addressed injection canary.
For every measured unit, the offline runner copies the exact manifested
repository into a read-only sandbox and launches the oracle-blind deterministic
arm executor in a fresh process. The executor must return a challenge-response
proof that it read the canary. The host excludes the secret from the child,
observes a write sentinel and a fresh loopback listener, snapshots the sandbox
before and after execution, and scans captured output and public artifacts for
the secret. It does not trust executor-supplied side-effect claims.
`Bash`, `Edit`, `Write`, Web tools,
indexing, deletion, and report-writing MCP tools are not in any measured arm.

## Corpus pack

`build_corpus_pack.py` uses only `git ls-files` from an exact clean revision.
It rejects binary/non-UTF-8 files and symlinks, splits text into 200-line
blocks with 20-line overlap, then ranks by deterministic query-token overlap
with shorter-path, path, and start-line tie breakers. Oracle labels, model
responses, and MCP results are not accepted by its API.

The pack prepends the repository tree and records its SHA-256, eligible and
included bytes and tokens, line coverage, candidate count, effective K, and
truncation. `code_intel_ascii_lexeme_unicode_scalar_v1` is the exact,
content-addressed tokenizer shared by every arm; the repository-evidence
ceiling is 64,000 novel tokens and the total context ceiling is 128,000
tokens. A pack is called `whole_repository` only when every
eligible block fits; otherwise it is `query_conditioned_pack`. Whether the
target happened to appear is computed only after construction.

## Durable artifacts and privacy

A run has four append-only, stable-key, per-record-hashed and fsynced ledgers:

- `cases.jsonl`
- `setup.jsonl`
- `observations.jsonl`
- `errors.jsonl`

`manifest.json` binds the cases, controls, component descriptors, tool-schema
snapshots, routing policy, execution order, privacy policy, and expected keys.
The scorer requires the exact union of observations and errors, so provider
errors, timeouts, invalid JSON, and tool-limit outcomes stay in the
denominator. It reports file Acc@1/3/10, MRR@10, eligible class/function
Acc@10, failures, tokens, cost, latency, setup cost, and amortized
`Q=1,5,20,100` cost. Primary composed-vs-corpus and composed-vs-native
contrasts use paired exact McNemar and case-clustered bootstrap evidence. The
manifest freezes 10,000 resamples, seed 42, the two contrasts, and Holm
correction. McNemar counts each case once; when repeats exist, an arm is a
case-level success only when every repeat succeeds, so correlated repeats
cannot inflate the exact-test sample size.

Only exact coverage produces `summary.json`, `provenance.json`, and `.done`.
The production manifest names `bench.compare.score:score_bundle_v1` as its
authoritative consumer. Finalization and every finalized reopen replay that
consumer over the raw ledgers and require the stored summary to match. The
configuration `run_id` binds the embedded source-pin bytes; the separate
`result_id` binds the run ID plus the final cases, setup, observations, errors,
manifest, and summary artifact descriptors. Rehashing changed final content
therefore cannot preserve a semantically valid finalized bundle.
The result ID is self-verifying, not an external trust anchor; third-party
provenance requires publishing or signing that ID in an independent immutable
system.
An incomplete run has no `.done` and resumes missing keys without rewriting
prior units. Raw model responses are not stored in the public bundle; live
responses belong in separate short-retention encrypted storage, with only
fingerprints and usage metadata in public artifacts. Inputs must be public
pinned repositories and labels.

## Offline fixture

CI runs these same boundaries. To reproduce the good fixture:

```bash
run_dir="$(mktemp -d)"
export COMPARE_CANARY_WRITE_PATH="${run_dir}.host-write-canary"
export COMPARE_SECRET_CANARY="fixture-host-secret-canary-do-not-emit"
python3 bench/compare/run.py \
  --cases bench/compare/pins/fixture-public-n7.json \
  --arms corpus,native,code-search,code-graph,composed \
  --replicates 1 \
  --top-k 10 \
  --max-tool-calls 20 \
  --evidence-token-budget 64000 \
  --context-token-budget 128000 \
  --wall-timeout 600 \
  --run-dir "$run_dir" \
  --mode fixture \
  --fixture-results bench/compare/fixtures/five-arm-good.json
python3 bench/compare/score.py \
  --run-dir "$run_dir" \
  --intent-to-treat \
  --bootstrap 10000 \
  --seed 42 \
  --holm-primary composed-corpus,composed-native \
  --thresholds bench/compare/thresholds.json
```

The checked-in JSON files are narrow fault plans, not prerecorded results.
The good plan injects no faults. Replace it with `five-arm-bad.json` to inject
one measured timeout only after that unit has executed; the scorer must exit
one. Fatal budget errors are forbidden from becoming fixture misses. To
exercise resume, add `--fixture-stop-after 1`, observe exit three and no
`.done`, then rerun the identical command without that option.

## Calibration and June references

LocBench is retrospective calibration and regression only, not final decision
evidence. Because it is a published benchmark, possible training overlap must
be assumed for any model that may have seen its cases or derivatives. Its
license is unspecified; public download access does not grant redistribution
rights. The source parquet, pins, queries, patches, PR-response cache, and
repository cache therefore remain local and uncommitted.

`build_pin.py build-n40` accepts separate content-addressed public label and
audit-evidence sources plus a local cache of the pinned public Git
repositories. It requires the base and head commits to exist, derives the
binary patch and changed paths directly from those objects, loads oracle
snapshots and blob IDs from Git, verifies oracle symbols, discloses quarantined
disputes, and selects exactly ten verified Bug, Feature, Performance, and
Security cases. Selection uses cross-version
`sha256_priority_v1` with seed 42, not runtime-dependent sampling.

No real `n=40` pin is checked in yet: the required public label source,
base/head object evidence, and repository cache are absent. The builder and
negative tests are ready, but manufacturing that evidence from colocated
oracle labels would be circular and is forbidden.

The exact June `n=200` cases remain external rather than copying query or case
data here. `pins/locbench-june-n200.external.json` fixes the repository,
commit, path, SHA-256, count, depth, and recorded-order hash.
The address was published at code-graph merge
`d7b93959dace3215cd096a13c1a27e259063dc95`. The external pin identifies the
exact recorded order but does not contain runnable queries, oracles, and label
audits. Verification therefore confirms the address and recomputed order, then
returns `address_verified_not_runnable` with exit two. Verify a local copy
without copying it into this repository:

```bash
python3 bench/compare/build_pin.py verify-june \
  --reference bench/compare/pins/locbench-june-n200.external.json \
  --external-pin /path/to/locbench-n200-pin.json
```

To prepare the external June cases, keep every mutable input, cache, and output
outside this plugin checkout. `--output` is the complete pin only when all 200
cases verify; `--quarantine-report` is the content-free failure artifact:

```bash
python3 bench/compare/build_pin.py prepare-june \
  --reference bench/compare/pins/locbench-june-n200.external.json \
  --external-pin /absolute/operator-only/locbench-n200-pin.json \
  --parquet /absolute/operator-only/test-00000-of-00001.parquet \
  --github-pr-cache /absolute/operator-only/github-pr-cache \
  --repository-root /absolute/operator-only/repositories \
  --output /absolute/operator-only/locbench-june-n200.prepared.json \
  --quarantine-report /absolute/operator-only/locbench-june-n200.quarantine.json
```

The future confirmatory decision set is a separate 40-case sample: 10 Bug, 10
Feature, 10 Performance, and 10 Security cases drawn only from post-development
public merged pull requests. Each case binds an immutable base, head, and unique
merge-base, requires two independent reviewers, and the oracle remains hidden
during retrieval. No live decision run is valid until that confirmatory pin and
the published external `n=200` calibration address both verify.

## Dormant five-arm live path

Live mode is intentionally fail-closed in this zero-cost build. A managed
Claude.ai or keychain OAuth session does not satisfy `--bare`; compatible
headless authentication must be proven by a signed, operation-bound authority
and a configured trusted verifier. The default CLI has no such verifier and
does not interpret supplied authority claims. Consequently
`bare_incompatible_authentication` can be reported only by a future boundary
where a genuinely trusted verifier has accepted the signature and rejected the
credential mode.

The diagnostic always reports these unresolved requirements:

- `bare_compatible_authentication_not_verified`
- `missing_trusted_signature_verifier`
- `missing_transactional_or_provider_cost_enforcement`
- `missing_authorized_numeric_cap`
- `missing_real_executor`
- `live_executor_not_enabled_in_zero_cost_build`

`--max-budget-usd` is defense in depth only; it is a Claude CLI stop and not
transactional or provider enforcement. Likewise, caller-supplied
`--max-total-usd` and `--max-unit-usd` strings cannot create signed numeric
authority. There is no production trusted signature verifier, transactional
broker, provider hard limit, reviewed real executor, or encrypted response
store in this zero-cost build.

The command exits two, records `status: not_evaluated` and
`spent_usd: 0.000000`, and writes only `diagnostic.json`: no `.done`, manifest,
observation, error, attempt journal, or model response artifact. Enabling model
calls belongs to a separately authorized phase after every blocker is removed
by reviewed production dependencies.

These are blockers only for this future five-arm model experiment. They do not
invalidate the direct public retrieval measurement, the deterministic CI suite,
or the completed bounded Stage-4 release holdout.

`GH_TOKEN`, when an operator supplies one, is only a fetch credential for
installing exact release artifacts. It must never enter a child MCP or model process.
Model and embedding credentials likewise stay outside MCP child environments,
public logs, and public artifacts. The diagnostic records content hashes only;
credentials and authority claims are never printed or forwarded.

The checked-in `merge-gate` depends on both deterministic workflow jobs.
Repository branch protection is an external GitHub setting and must require
the stable `merge-gate` check.

This harness changes no production routing, indexes, or doctor behavior.
