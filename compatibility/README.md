# Tested Component Compatibility

`component-bom.json` is the install source of truth. The JSON files in this
directory are snapshots of the supported MCP input schemas observed from
those exact components; do not add a tool or parameter until it exists in a
published component and has been captured from `tools/list`.

## Current readiness evidence

The current BOM's integrated readiness status is `ready`. Its
`tested_capabilities` section declares complete version-1 `index_identity`
outputs, semantic `index_ready`, graph `status: ready`, and the optional
boolean `skip_report` input exposed by code-graph `v0.8.0-redacted.6`. The
committed schema snapshots cover MCP input surfaces; they do not by themselves
prove output behavior. `/index-repo` revalidates every live input-schema
fingerprint before either index starts.

`readiness-evidence.json` is the committed `promotion-candidate` record. It
declares producer v3, a blocked candidate BOM, the expected component
install-descriptor hashes, and a smoke result covering both engines. The v3
generator emits a record only after calling `skip_report=true`, binding final
status responses to the same project and checkout root, observing semantic and
graph readiness, requiring an exact backend-issued atomic evidence candidate
instead of a retrieval-context range, and preserving `checkout_unchanged:
true`. Static validation checks the producer and mode labels, canonical
component install descriptors, ready outcomes, identity shape and equality,
UTC capture times, and unchanged-checkout flag.

Because this file is stored in the same repository as the BOM and validator,
those internal consistency checks do not independently prove which
executables produced it. Treat it as supporting promotion-review evidence,
not as the trusted runtime attestation for a `main` revision.

On every trusted `main` push or manual default-branch run, real-component CI
installs the exact code-search descriptor and checksum-verified graph release,
then generates a fresh `ready-validation` record under the isolated runner
directory. The validator requires that live override to report
`bom_readiness_status: ready`; it cannot be replaced by the committed
`promotion-candidate` record. That trusted, run-specific `ready-validation`
artifact is the authoritative runtime check for the revision.

## Optional Go and TypeScript compiler precision

`precision_generators.go-scip` pins the public `scip-code/scip-go` release,
source revision, reported version, and per-platform archive and extracted
binary digests. `precision_generators.typescript-scip` pins the official
`@sourcegraph/scip-typescript` package, npm integrity value, lockfile, and an
isolated Node 22 runtime with per-platform archive and executable digests.
Interactive install supports Go on macOS arm64 and Linux amd64/arm64, and
TypeScript on macOS, Linux, and Windows amd64. Unsupported platforms retain
heuristic and user-supplied SCIP modes rather than selecting an unpinned
generator.

Automatic use remains explicit through `/index-repo --graph-precision auto`.
The preparation boundary requires a clean canonical Git root and exactly one
supported project shape: root `go.mod` for Go, or root `package.json` plus
`tsconfig.json`/`jsconfig.json` for TypeScript. It verifies the selected
generator and runtime, rejects checkout mutation, and stores the result outside
the repository under an index-generation-bound cache key. Mixed Go/TypeScript
roots require an explicit language instead of guessing. Code-graph must report
the same SCIP digest it prepared. Drift remains visible coverage telemetry:
only emitted relationship references bound to that digest can satisfy
`compiler_resolution`; uncovered or drifted relationships remain heuristic.
This is bounded per-repository automation, not a general SCIP indexing fleet.

The released Go compiler tier has an independent SSA/RTA CALLS oracle. The
TypeScript path has deterministic generation and ingestion coverage but does
not yet have a comparably independent compiler-tier accuracy oracle, so its
presence must not be interpreted as a language-wide accuracy score.

A blocked BOM remains fail-closed. The smoke generator refuses to start either
server for a blocked BOM unless a reviewer explicitly supplies
`--candidate-evidence`, and candidate mode rejects an already-ready BOM.

## Code-search release promotion

The production BOM pins code-search release
[`v0.3.5`](https://github.com/redacted-org/code-search/releases/tag/v0.3.5),
its exact source commit, wheel, and offline attestation bundle. Contract
capture and validation support future GitHub Release wheel candidates without
weakening readiness: capture preserves the complete release descriptor,
records the tag as the component version, and resets behavioral claims to a
blocked candidate that must be re-observed.

A release candidate must pin the wheel, `SHA256SUMS`, and its JSONL
attestation bundle by filename and SHA-256. Installers download all three
through authenticated `gh`, after resolving and peeling the exact release tag
and requiring that the tag resolves to the pinned source commit. The exact
manifest entry and all three BOM digests must match before offline
`gh attestation verify`. The attestation policy fixes the release workflow,
source digest, `refs/heads/main`, and `--deny-self-hosted-runners`. Only then
may pip install the local wheel; the post-install verifier binds its
distribution version, asset name, digest, and PEP 610 record back to the BOM.

Promotion is one reviewed change: flip the production BOM descriptor, capture
fresh schema contracts from the installed candidate, regenerate candidate
readiness evidence, and run trusted live validation. A tag, wheel, checksum,
bundle, or snapshot update on its own is not a valid promotion.

## Code-graph release provenance

The code-graph descriptor pins every platform archive, `checksums.txt`, and an
operator-fetched provenance bundle vendored at a release-specific repository
path. That bundle is itself SHA-256-bound by the complete install descriptor
and contains the release and build-provenance statements covering all five
immutable archives. Static validation rejects a missing, modified, symlinked,
or release-mismatched bundle. Installers and trusted validation pass it to
`gh attestation verify --bundle` without an online Attestations API lookup,
then extract only after the exact repository, signer workflow, source commit,
`refs/heads/main`, and GitHub-hosted-runner policy all verify.
