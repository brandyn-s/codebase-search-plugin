# Changelog

## 0.5.0 - Unreleased

First public release line. Versions 0.4.x were internal.

- Marketplace renamed to `code-intelligence`; install id is now
  `codebase-search@code-intelligence`.
- Self-bootstrapping launchers: `bin/run-code-search` and `bin/code-graph` are
  committed, referenced by `.mcp.json`, and run `install.sh` on first launch.
  Installed components moved from `bin/` to `.runtime/bin/` (plus `.venv/`).
- Installer downloads public release assets directly with curl or
  Invoke-WebRequest; the GitHub CLI is only a fallback for private releases and
  is required only for build-provenance attestation. SHA-256 verification
  against the BOM is unchanged and mandatory.
- The installed code-graph binary is always named `code-graph` regardless of
  the archive contents.
- README restructured around install and use; detailed material moved to
  `docs/INSTALL.md`, `docs/EVIDENCE.md`, `docs/MEASUREMENTS.md`,
  `docs/EMBEDDINGS.md`, and `docs/LARGE_MONOREPOS.md`.
- Added CONTRIBUTING.md, issue and pull request templates.
- `SECURITY.md` with private reporting and the plugin's threat model.
- Windows launchers `bin/code-graph.cmd`, `bin/run-code-search.cmd`, and
  `bin/_bootstrap.cmd` are committed and self-bootstrap through `install.ps1`;
  `install.ps1` no longer generates them. A `windows-launchers` CI job drives
  them on `windows-latest`. Registration on Windows is documented in
  `docs/INSTALL.md`.
- Documented that code-graph runs with its default `core` toolset under the
  plugin and how to select `CODE_GRAPH_TOOLSET=full`.
- Trusted Component Promotion no longer needs a repository secret. A gate step
  (`scripts/promotion_gate.py`) checks that every BOM pin is a publicly
  reachable `brandyn-s/` release and exits successfully with a notice until
  the pins are promoted.
