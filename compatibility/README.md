# Tested Component Compatibility

`component-bom.json` is the install source of truth. The JSON files in this
directory are snapshots of the supported MCP input schemas observed from
those exact components; do not add a tool or parameter until it exists in a
published component and has been captured from `tools/list`.

## Current rollout block

Both pinned component snapshots lack an attested complete version-1
`index_identity` output. The pinned code-search snapshot also lacks an
attested semantic `index_ready` output. Those missing output contracts are
the primary rollout block, so this BOM **cannot satisfy identity readiness**
for coherent dual indexing.

The pinned code-graph release `v0.7.0-redacted.2` exposes an optional boolean
`skip_report` input on `index_repository`. This offline schema capture proves
the input surface, not runtime behavior. A readiness run must call
`skip_report=true` and prove `checkout_unchanged: true` before the BOM can be
promoted.

`/index-repo` intentionally stops before either engine starts. Lifting the
block requires updated real-schema snapshots, matching tested capability
attestations in the BOM, and version-matched readiness evidence proving
semantic `index_ready`, graph `status: ready`, complete equal identities, and
`checkout_unchanged: true`. No readiness-evidence file is required while the
BOM remains blocked. A future ready BOM also makes real-component CI require
a separate runner-generated live smoke evidence file; the committed promotion
record cannot substitute for that run-specific attestation.
