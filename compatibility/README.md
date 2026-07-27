# Tested Component Compatibility

`component-bom.json` is the install source of truth. The JSON files in this
directory are snapshots of the supported MCP input schemas observed from
those exact components; do not add a tool or parameter until it exists in a
published component and has been captured from `tools/list`.

## Current readiness evidence

The current BOM's integrated readiness status is `ready`. Its
`tested_capabilities` section declares complete version-1 `index_identity`
outputs, semantic `index_ready`, graph `status: ready`, and the optional
boolean `skip_report` input exposed by code-graph `v0.7.0-redacted.2`. The
committed schema snapshots cover MCP input surfaces; they do not by themselves
prove output behavior. `/index-repo` revalidates every live input-schema
fingerprint before either index starts.

`readiness-evidence.json` is the committed `promotion-candidate` record. It
declares producer v2, a blocked candidate BOM, the expected component
versions, and a smoke result covering both engines. The v2 generator emits a
record only after calling `skip_report=true`, binding final status responses
to the same project and checkout root, observing semantic and graph readiness,
and preserving `checkout_unchanged: true`. Static validation checks the
producer and mode labels, declared component versions, ready outcomes,
identity shape and equality, UTC capture times, and unchanged-checkout flag.

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

A blocked BOM remains fail-closed. The smoke generator refuses to start either
server for a blocked BOM unless a reviewer explicitly supplies
`--candidate-evidence`, and candidate mode rejects an already-ready BOM.

## Code-search release promotion

The current production BOM remains on its full Git revision until an actual
code-search release exists and every exact release fact is known. Contract
capture and validation support a GitHub Release wheel candidate without
weakening readiness: capture preserves the complete release descriptor,
records the tag as the component version, and resets behavioral claims to a
blocked candidate that must be re-observed.

A release candidate must pin both the wheel and its JSONL attestation bundle
by filename and SHA-256. Installers download both through authenticated `gh`,
after resolving and peeling the exact release tag and requiring that the tag
resolves to the pinned source commit. The downloaded wheel is an attested build
artifact from the pinned release; its two BOM digests and offline
`gh attestation verify` result are the artifact checks. The attestation policy
fixes the release workflow, source digest, `refs/heads/main`, and
`--deny-self-hosted-runners`. Only then may pip install the local wheel; the
post-install verifier binds its distribution version, asset name, digest, and
PEP 610 record back to the BOM.

Promotion is one reviewed change: flip the production BOM descriptor, capture
fresh schema contracts from the installed candidate, regenerate candidate
readiness evidence, and run trusted live validation. A tag, wheel, checksum,
bundle, or snapshot update on its own is not a valid promotion.
