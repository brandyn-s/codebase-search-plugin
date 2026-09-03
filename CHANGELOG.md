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
