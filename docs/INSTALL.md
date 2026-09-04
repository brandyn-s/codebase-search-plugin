# Installing and verifying the components

The plugin installs two separately released components pinned in
`component-bom.json`: the code-search Python wheel and the code-graph binary.
This document covers what the installer does, how to do it by hand, and how
the pins are promoted. The short version is in the README.

## Prerequisites

- **Python 3.12+** (for code-search)
- **`curl` and `tar`** on Linux and macOS; PowerShell 5.1+ on Windows (see [Windows](#windows))
- **GitHub CLI (`gh`)**, optional: needed only to download private releases and
  to verify GitHub build provenance. Unauthenticated REST calls honour
  `GH_TOKEN` when set. Without it the installer verifies SHA-256
  checksums against the BOM and prints one line saying provenance was not checked.

## What install.sh does

1. Validates the plugin contract and the committed readiness evidence
   (`scripts/validate_plugin.py`).
2. Creates `.venv/` and installs the exact code-search source declared by the
   BOM: a pinned Git commit, or a GitHub Release wheel with
   `install.kind: github-release`.
3. Downloads the code-graph release archive and `checksums.txt` for your
   platform, verifies both against the BOM, and installs the binary as
   `.runtime/bin/code-graph`.
4. Installs the optional, BOM-pinned `scip-go` generator and an isolated,
   lockfile-pinned `scip-typescript` plus Node runtime where the platform is
   supported. Unsupported platforms keep heuristic and user-supplied SCIP modes.
5. Starts both installed stdio servers and rejects missing or schema-drifted
   tools before promoting the staged install.

Downloads use the public release URL first and fall back to
`gh release download` only when the public URL is unavailable (private
releases). The previously installed `.venv` and `.runtime/bin` are kept until
the replacement passes its live MCP schema check; if any step fails, the
rollback handler restores the prior installation.

The launchers in `bin/` are committed and are what `.mcp.json` points at. On
first launch they run `install.sh` themselves (log: `.runtime/bootstrap.log`),
serialized with a lock so two servers starting at once share one install. Set
`CODE_INTEL_NO_BOOTSTRAP=1` to disable that and run the installer yourself;
`CODE_INTEL_BOOTSTRAP_WAIT_SECONDS` (default 1800) bounds how long a launcher
waits for an in-progress install. If the bootstrap fails, the launcher prints
the last lines of `.runtime/bootstrap.log` to stderr and the next launch
retries.

## code-graph toolset

code-graph advertises its `core` toolset by default (`CODE_GRAPH_TOOLSET=core`):
the tools the plugin skills, the benchmark arm contracts, and the indexing,
status, and evidence essentials use. Every tool stays registered and callable
by name; the toolset only controls what `tools/list` returns, which keeps the
schema small for the agent. To expose all tools, set `CODE_GRAPH_TOOLSET=full`
in the environment Claude Code launches the plugin with (for example in your
shell profile, or in a user-scope MCP registration's `env`), then restart the
server. The plugin does not override this variable.

## Windows

The plugin manifest format has no platform-specific command field, and Claude
Code spawns `.mcp.json` commands directly, so the bash launchers it references
(`bin/code-graph`, `bin/run-code-search`) do not start on Windows. The
repository ships equivalent shims, `bin\code-graph.cmd` and
`bin\run-code-search.cmd`, which bootstrap through `install.ps1` on first
launch (log: `.runtime\bootstrap.log`) and then run the installed component.
Register them once at user scope, pointing at the installed plugin directory
(`claude plugin list --json` shows `installPath`):

```powershell
$p = (claude plugin list --json | ConvertFrom-Json | Where-Object id -eq "codebase-search@code-intelligence").installPath
claude mcp add --scope user code-graph -- cmd /c "$p\bin\code-graph.cmd"
claude mcp add --scope user code-search -- cmd /c "$p\bin\run-code-search.cmd"
```

Alternatively run `install.ps1` from that directory once; the shims then find
the installed components immediately. `CODE_INTEL_NO_BOOTSTRAP=1` and
`CODE_INTEL_BOOTSTRAP_WAIT_SECONDS` apply to the shims as well. Unlike the bash
launchers, the shim runs the installer in the foreground of the first launch,
so an MCP client that kills a slow first start also stops the install; the next
launch resumes it. If Claude Code adds platform-specific manifest commands, the
registration step goes away.

## Upgrade

The installed components are runtime state and are not refreshed by
`claude plugin update` alone. Upgrade in this order so the plugin cache cannot
carry a schema-compatible but older binary forward:

```bash
claude plugin marketplace update code-intelligence
claude plugin update codebase-search@code-intelligence --scope user
```

Then either delete `.runtime/` and `.venv/` inside the installed plugin
directory so the launchers bootstrap the new pins on next start, or run
`install.sh` (or `install.ps1`) from that directory. The installer stages,
verifies, and atomically promotes the exact BOM components and keeps the
launchers self-contained. Restart Claude Code after the update so existing
sessions do not retain the prior MCP processes, then confirm both servers with
`claude mcp list`.

## Readiness record

`component-bom.json` carries an `integrated_readiness` status. It is `ready`
for the pinned code-graph v0.9.0 and code-search v0.4.0 releases. `pending`
would mean a `promotion_state: pending-first-release` BOM whose component
releases are not published yet, where installers and launchers refuse to run.
`ready` means the
committed `promotion-candidate` record satisfied static evidence-shape,
version, backend-issued evidence, and checkout-identity checks, and that
trusted post-merge CI installed the exact pins and generated a fresh
`ready-validation` record. The installers print the current status on
completion; a `blocked` BOM must not be used for `/index-repo`. See
`compatibility/README.md` for the evidence layout.

## Manual install (alternative)

The production BOM pins code-search release
[`v0.4.0`](https://github.com/brandyn-s/code-search/releases/tag/v0.4.0)
with `install.kind: github-release`. Its descriptor fixes the source commit,
wheel name and SHA-256, `SHA256SUMS` manifest name and SHA-256, JSONL
attestation bundle name and SHA-256, signer workflow, and `refs/heads/main`;
use those values directly rather than selecting a moving release.

Follow the same order as the installers:

1. Resolve the exact Git tag through the Git refs API, peel annotated tags,
   and require that the tag resolves to the pinned source commit.
2. Download exactly the wheel, checksum manifest, and attestation bundle named
   by the BOM and tag (public release URL, or `gh release download` for a
   private release).
3. Verify all three files against their separate BOM SHA-256 values, then
   require exactly one manifest entry matching the wheel name and digest.
4. If `gh` is available, run `gh attestation verify` with the offline
   `--bundle`, pinned repository, `--signer-workflow`, `--source-digest`,
   `--source-ref refs/heads/main`, and `--deny-self-hosted-runners`.
5. Only after those checks pass, pip-install the local wheel with
   `--force-reinstall` and run `scripts/verify_code_search_wheel.py` to verify
   its version, filename, checksum, and PEP 610 installation provenance.

For code-graph, use the release named by the BOM (currently
[`v0.9.0`](https://github.com/brandyn-s/code-graph/releases/tag/v0.9.0)).
Resolve its tag to the BOM's pinned source commit; download exactly the
platform archive and `checksums.txt`; verify both BOM digests and the exact
archive manifest entry; verify the operator-fetched, vendored JSONL bundle at
the path and SHA-256 pinned by the BOM; then, with `gh`, run secret-free
`gh attestation verify --bundle` with the pinned repository, release workflow,
source digest, `refs/heads/main`, and GitHub-hosted-runner policy. Extract only
after every check passes, install the binary as `code-graph`, and configure the
two server paths manually in `.mcp.json`.

Manual installs must match `component-bom.json`. Run the same fail-closed
contract check used by the installers before enabling the plugin:

```bash
python3 scripts/validate_installed.py \
  --server code-search=/path/to/code-search-mcp \
  --server code-graph=/path/to/code-graph
```

## Trusted component validation

The ordinary pull-request workflow has a stable, fail-closed `merge-gate`
whose only dependency is the deterministic `validate` job. It reads no
secrets. Trusted installation is isolated in
`.github/workflows/trusted-component-promotion.yml`; its
`validate-installed-components` job installs both component releases from the
exact descriptor path passed with `--component-bom` and validates their real
`tools/list` responses. The same job downloads the exact public `scip-go`
release asset, verifies the pinned release commit plus archive and binary
digests, and runs the generator verifier. It runs only from a trusted `main`
push or a manual default-branch dispatch, never on `pull_request`.

Post-merge trusted validation runs only when the BOM pins publicly reachable
brandyn-s releases; until then the workflow's first step
(`scripts/promotion_gate.py`) reports that it is gated and the job exits
successfully. No repository secret is used to reach a component release: in
CI the validator receives only the workflow's ephemeral read-only
`github.token`, and without any token it resolves tags and downloads assets
through the public GitHub REST API (limited to 60 requests per hour per
address). An operator running the validator by hand may set `GH_TOKEN` to
raise that limit; the validator passes it only to authenticated GitHub
fetch/tag-resolution commands and removes it before package builds or MCP
processes start.
Provenance verification (`gh attestation verify --bundle`) does not need a
token.

For the release-wheel path, repository `Contents: read` is sufficient to
resolve and peel the tag and download its assets. The wheel is treated as an
attested build artifact downloaded from that pinned release; the checks do not
cryptographically prove its placement there. Its separately checksum-pinned
offline attestation bundle is passed directly to `gh attestation verify`; no
online Attestations API lookup is used, so the search verification
does not need `Attestations: read`. Code-graph uses an operator-fetched canonical
bundle vendored under `compatibility/attestations/`; the graph descriptor pins
its repository-relative path and SHA-256, and static validation rejects a
missing, modified, or release-mismatched bundle. The bundle covers all
platform archives, so runtime verification is also offline. Both policies bind
the build to the pinned source commit, release workflow, `refs/heads/main`,
and GitHub-hosted runners.

There is no repository secret fallback. If the secret is absent while the
releases are private, the trusted job intentionally fails; do not skip or
weaken this validation. Because the current BOM is `status: ready`, the same
job invokes the readiness smoke generator against the just-installed MCP
executables on every trusted `main` push. The committed `promotion-candidate`
record cannot substitute for that run-specific attestation. The uploaded
trusted artifact includes freshly captured schema contracts and readiness
evidence, each bound to the canonical SHA-256 of the complete install
descriptor.

Rollback is descriptor-atomic: revert the reviewed component-promotion commit
that changed the BOM, snapshots, and readiness record together, then rerun the
deterministic validation gate and trusted workflow. Never roll back only a tag,
digest, manifest, or evidence file.
