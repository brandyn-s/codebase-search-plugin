# Changelog

## 0.5.0 - Unreleased

First public release line. Versions 0.4.x were internal.

- Promoted the component pins to the first public releases: code-graph
  v0.9.0 (release commit b655cec) and code-search v0.4.0 (release commit
  7fee121), both immutable GitHub releases with build attestations. The BOM
  records every asset digest, the code-graph attestation bundle is vendored
  under `compatibility/attestations/`, both tool snapshots were captured from
  the installed components (code-graph now exposes its 26-tool default `core`
  toolset; set `CODE_GRAPH_TOOLSET=full` for all 40), and
  `compatibility/readiness-evidence.json` is the promotion-candidate record
  with integrated readiness `ready`.
- The installers install the code-search wheel with its `[local]` extra so the
  documented on-device embedding path works without any API key.
- Fixed the installer's GitHub tag parser, which failed under Python 3.12+
  because of escaped quotes inside an f-string.
- Removed all references to the originating organization. License and
  manifests now name the codebase-search-plugin contributors.

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
