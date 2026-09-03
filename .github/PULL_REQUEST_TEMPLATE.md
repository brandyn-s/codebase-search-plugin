## What changed

## Why

## Checks

- [ ] `python3 scripts/validate_plugin.py`
- [ ] `python3 -m unittest discover -s tests`
- [ ] `shellcheck install.sh bin/run-code-search bin/code-graph bin/_bootstrap.sh`
- [ ] Component pins unchanged, or this is a reviewed promotion (BOM, snapshots, readiness record in one commit)
- [ ] CHANGELOG.md updated
