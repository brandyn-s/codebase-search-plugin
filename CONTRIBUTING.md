# Contributing

## Running the checks

```bash
python3 scripts/validate_plugin.py          # static contract: BOM, snapshots, skills, installers
python3 -m unittest discover -s tests       # unit and contract tests
shellcheck install.sh bin/run-code-search bin/code-graph bin/_bootstrap.sh
```

`validate.yml` runs the same commands plus the recorded-trace routing harness
(`bench/e2e`) and the comparison harness fixtures (`bench/compare`). Pull
requests must keep all of them green; the merge gate has no other dependencies
and never reads a component token.

## How component versions change

The plugin pins exact releases of code-search and code-graph in
`component-bom.json`. Tool schema snapshots under `compatibility/` are bound to
a SHA-256 of that install descriptor, so a pin change is a reviewed promotion,
not an edit: run `scripts/capture_component_contracts.py` against the newly
installed components, commit the BOM, snapshots, and readiness record together,
and let the trusted post-merge workflow
(`.github/workflows/trusted-component-promotion.yml`) install the exact pins and
regenerate the readiness evidence. Rollback reverts that one commit.

## Scope

Keep changes to the plugin layer: skills, installer, launchers, validators,
harnesses, documentation. Retrieval or graph behaviour belongs in the component
repositories.
