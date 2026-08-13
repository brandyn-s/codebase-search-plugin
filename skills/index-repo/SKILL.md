---
name: index-repo
description: >
  Index a repository for both semantic search (code-search) and structural
  graph analysis (code-graph) in one command. Trigger phrases: "index repo",
  "index this repo", "index-repo", "set up search for".
  Do NOT use for searching (use code-explore).
argument-hint: "[repo-path]"
---

# Index Repo

Index a repository for both code-search (semantic embeddings) and code-graph (AST-based structural analysis) in a single invocation.

## Usage

`/index-repo /path/to/my-repo [--graph-precision heuristic|scip|auto] [--scip-policy preferred|required] [--scip-index /path/to/index.scip]`

If no path is provided, ask the user which repo to index.

Default `<graph-precision>` to `heuristic` and `<scip-policy>` to `preferred`.
Automatic SCIP generation is explicit opt-in: select `auto` only when the user
requests it. Select `scip` only when the user supplies an index. Do not treat
the mere presence of an `index.scip` file as consent. Resolve a supplied SCIP
path to an absolute regular file. `--scip-index` is invalid with the heuristic
or auto tier, and `--scip-policy` has no effect outside auto mode.

## Steps

1. Resolve the candidate path to Git's canonical worktree root:
   ```
   git -C <candidate-path> rev-parse --show-toplevel
   ```
   The command must succeed and return one existing directory. Trim only the
   trailing newline and record its exact output as `<resolved-root>`; record
   the basename as `<resolved-project-name>`. This is the resolved repository
   root used by every later call and comparison. Do not infer repository
   membership from the presence of a `.git` directory: linked worktrees and
   subdirectories must resolve through Git.

2. **Before starting either index**, require `component-bom.json` to declare
   integrated readiness `status: "ready"`, then inspect the installed live
   host tool metadata for both components. A tested BOM snapshot alone is
   insufficient.

   - Compute each installed tool's exact canonical input-schema fingerprint
     (SHA-256 of its `inputSchema` serialized as sorted, compact JSON) and
     require it to equal that tool's `input_schema_sha256` in the BOM-linked
     compatibility snapshot. A missing live schema or any fingerprint
     mismatch is an **incompatible component** result.
   - After that exact match, require the live code-graph `index_repository`
     schema to expose boolean `skip_report`, string/enum `precision_tier`, and
     optional string `scip_index_path` properties.
   - Also require the live code-search `get_index_status` schema to expose an
     optional string `project_path` property. It must not appear in the
     schema's `required` array. This binds final semantic verification to the
     canonical checkout without changing the active project first.
   - The current BOM attests complete v1 identity outputs, semantic
     `index_ready`, graph `status: ready`, optional search `project_path`, and
     optional graph `skip_report`. Live schema fingerprint checks remain
     mandatory because a configured host can still drift from the BOM.
   - The pinned graph release can write `ARCHITECTURE_REPORT.md` by default.
     Always pass `skip_report=true`; the v2 readiness gate requires its live
     integrated run to leave the checkout unchanged.
   - If any gate fails, stop before either index starts. Do not start code-search
     or code-graph. Do not allow a graph build to write
     `ARCHITECTURE_REPORT.md`: that would change the checkout after semantic
     indexing and invalidate the shared dirty fingerprint/generation.

3. Start **code-search** indexing:
   ```
   mcp__code-search__index_directory(directory_path=<resolved-root>)
   ```
   Accept only the pinned new-job response envelope:

   - `status == "indexing"`
   - a non-empty `job_id`, recorded as `<semantic-job-id>`
   - `directory == <resolved-root>`
   - `project_name == <resolved-project-name>`
   - `index_ready == false`
   - the exact message `Indexing started in background. Use
     get_indexing_progress to check status.`

   Do not adopt any pre-existing job, even when it appears to target the same
   path or provider. A response containing `requested_directory`,
   `indexing_conflict` (whether true or false), an "already indexing" or
   "reusing" message, a missing new-job marker, or any unknown-origin job is
   an incompatible start response. Stop without polling or starting
   code-graph. Starting the background job is not success and does not make
   the semantic index ready.

4. Wait for **code-search** to reach an explicit terminal state. Poll:
   ```
   mcp__code-search__get_indexing_progress()
   ```
   Poll every 15-30 seconds for at most two hours (or an earlier
   user-supplied deadline), and show a concise progress update at least every
   five minutes.

   Every polling response, including a terminal response, must preserve the
   recorded binding before its status is interpreted:

   | Response field | Required value |
   | --- | --- |
   | `job_id` | `job_id == <semantic-job-id>` |
   | `directory` | `directory == <resolved-root>` |
   | `project_name` | `project_name == <resolved-project-name>` |

   A missing field or literal value difference is a **semantic job binding
   mismatch**. Stop, show the expected and returned envelope, and do not start
   code-graph or switch projects. Never adopt a replacement or unknown job.

   - `status: "indexing"`: continue polling.
   - `status == "completed"`: continue only when `result` is an object,
     `result.success == true`, top-level `index_ready == true`,
     `result.index_ready == true`, and `result.error is absent, null, or empty`.
     Any top-level error must also be absent, null, or empty. Missing
     success/readiness fields fail closed; do not infer success from
     `status` alone.
   - `status: "failed"` or `status: "cancelled"`: stop and report the
     returned error/result.
   - Any missing, malformed, `idle`, or otherwise unknown status: stop as an
     incompatible response instead of guessing.
   - Reaching the deadline is a timeout failure.

   Retain the completed result's backend-issued lifecycle counts when present:
   `files_added`, `files_modified`, `files_removed`, `chunks_added`, and
   `chunks_removed`. Report the values verbatim; code-search does not issue an
   operation-mode label, so do not manufacture `full`, `noop`, or
   `incremental` from those counts.

   **Do not run code-graph indexing. Do not start code-graph** and do not call
   `switch_project` unless the bound semantic job satisfied every
   completed-result gate above.

4a. For requested `auto`, prepare a trusted Go or TypeScript SCIP index only
    after semantic completion and before code-graph starts. Select the generator
    from the canonical root: root `go.mod` uses `scip-go`; root `tsconfig.json`
    uses the isolated `scip-typescript` runtime. If both exist, require the user
    to select `--language` rather than guessing.

    Go:
    ```
    python "${CLAUDE_PLUGIN_ROOT}/scripts/prepare_scip_index.py" prepare \
      <resolved-root> \
      --generator "${CLAUDE_PLUGIN_ROOT}/bin/scip-go" \
      --component-bom "${CLAUDE_PLUGIN_ROOT}/component-bom.json" \
      --cache-root "$HOME/.cache/redacted-code-intel/scip"
    ```

    TypeScript:

    ```
    python "${CLAUDE_PLUGIN_ROOT}/scripts/prepare_scip_index.py" prepare \
      <resolved-root> \
      --language typescript \
      --runtime "${CLAUDE_PLUGIN_ROOT}/bin/scip-typescript-runtime/node/bin/node" \
      --generator "${CLAUDE_PLUGIN_ROOT}/bin/scip-typescript-runtime/package/node_modules/@sourcegraph/scip-typescript/dist/src/main.js" \
      --component-bom "${CLAUDE_PLUGIN_ROOT}/component-bom.json" \
      --cache-root "$HOME/.cache/redacted-code-intel/scip"
    ```

    On Windows, use the corresponding `node/node.exe` runtime path. The helper
    requires a clean Git checkout and the exact BOM-pinned generator/runtime
    digests. Go requires a root `go.mod`. TypeScript requires a root
    `tsconfig.json` and an already-present `node_modules`; it never installs
    dependencies in the target. Both write only to an out-of-tree cache bound
    to `index_generation`, verify that the checkout is unchanged, and emit a
    receipt with the absolute index path and digest.

    - On `status == "ready"`, set `<graph-precision>` to `scip` and
      `<absolute-scip-path-if-supplied>` to the receipt's index path.
    - Any unsupported language/layout, missing generator, digest/version
      mismatch, generator failure, empty output, timeout, or checkout mutation
      is an automatic-precision failure. A `required` policy stops before
      code-graph. A `preferred` policy may continue only as requested/effective
      `heuristic`, with the preparation failure reported verbatim.
    - Never describe that fallback as compiler-grade. Never install or upgrade
      a generator during indexing; installation is a separate BOM-governed
      release action.

5. Run **code-graph** indexing without mutating the checkout:
   ```
   mcp__code-graph__index_repository(
     repo_path=<resolved-root>,
     skip_report=true,
     precision_tier=<graph-precision>,
     scip_index_path=<absolute-scip-path-if-supplied>
   )
   ```
   For Nix-based repos (presence of `flake.nix`, `Cargo.nix`), use `mode: "full"` — fast mode returns 0 results on Nix repos.
   Require that MCP `isError` is absent or false, the payload
   error is absent, null, or empty, `status` is absent or exactly `"ready"`
   (explicit null, `"failed"`, `"degraded"`, or unknown values fail closed),
   `identity_status == "captured"`, and `index_identity` is a complete v1
   identity. Capture the
   non-empty `project` field for the status call; this response contract does
   not contain a `success` field or `project_name`. A graph error, explicit
   non-ready status, degraded identity, or missing project is a partial-index
   failure; report semantic success and graph failure, but do not claim the
   repository is ready.

   Retain `index_delta` from the graph completion response when present. Report
   `index_delta.mode` (`full`, `noop`, or `incremental`) plus
   `files_discovered`, `files_changed`, and `files_unchanged` verbatim. These
   fields are non-gating lifecycle telemetry: an absent or malformed delta is
   reported as unavailable but does not override the readiness and identity
   gates above. A `noop` delta means the backend observed no changed source
   files; do not infer semantic equivalence from it. Semantic-equivalence and
   resource measurements remain the bounded lifecycle harness's job.

6. Verify both engines independently after indexing:
   ```
   mcp__code-search__get_index_status(project_path=<resolved-root>)
   mcp__code-graph__index_status(project=<graph-project>)
   ```
   The semantic response must satisfy every exact gate:
   `project_path == <resolved-root>`, `index_ready == true`,
   `index_identity_status == "ready"`, and the semantic error is absent, null, or empty.
   The graph response must report
   `project == <graph-project>`, `root_path == <resolved-root>`,
   `status == "ready"`, `identity_status == "captured"`, and an error that is
   absent, null, or empty. Missing or different binding fields fail closed;
   do not infer readiness from chunks, files, provider state, or a nonempty
   identity. Both responses must contain an
   `index_identity` object with `schema_version: 1` and these fields:
   `repository_id`, `checkout_id`, `source_revision`, `dirty_fingerprint`,
   `index_generation`, and `captured_at`.

   Both graph completion and status responses must also contain
   `graph_precision` with `requested_tier == <graph-precision>` after any auto
   preparation and an
   `effective_tier` of `heuristic` or `scip`:

   - For requested `heuristic`, require effective `heuristic`.
   - For requested `scip`, require effective `scip`, status `ready`, the
     expected SCIP path/digest, nonzero covered documents/functions, and
     nonzero SCIP insertions. In auto mode, require
     `scip_status.index_sha256` to equal the preparation receipt's
     `index.sha256` in both graph completion and final status. A missing or
     invalid SCIP artifact, zero compiler coverage, or zero compiler edge
     insertion is a visible **partial index**, even when the heuristic graph
     remains usable. Nonzero `drifted_documents` is coverage telemetry, not a
     global failure: those documents remain heuristic and cannot satisfy
     compiler assurance.
   - Preserve coverage, drift, heuristic replacements, and SCIP insertions in
     the final report. Never call the whole graph compiler-grade. Only a
     relationship evidence reference carrying `resolution_source=scip-ingest`
     and the exact `resolution_artifact_sha256` may satisfy the
     `compiler_resolution` lattice capability.

   First compare the graph completion identity with the final graph identity.
   `repository_id`, `checkout_id`, `source_revision`, `dirty_fingerprint`, and `index_generation`
   must match exactly. Only after that comparison passes, compare the final semantic and graph identities.
   Require the same five stable fields to match exactly because both engines
   indexed the same resolved local checkout.

   - `captured_at` must be a valid UTC RFC3339 timestamp in every identity
     envelope but need not match; each engine and response captures at its own
     moment.
   - Legacy or missing identity fields are incompatible and therefore
     not-ready, never a successful verification.

   The identity contract is deterministic: `repository_id` is SHA-256 of
   `remote:` plus the normalized origin URL, falling back to `path:` plus the
   resolved root; `checkout_id` is SHA-256 of `path:` plus the resolved root;
   `source_revision` is git `HEAD` or `unborn`; `dirty_fingerprint` is
   `clean` or a deterministic SHA-256 over status, diffs, and untracked
   contents; and `index_generation` is SHA-256 of
   `repository_id + NUL + source_revision + NUL + dirty_fingerprint`.

   On status or identity mismatch, report both status/identity envelopes and
   the exact differing fields. Do not switch projects or claim readiness.

7. Only after both indexes and identities verify, **set the active project**
   for code-search:
   ```
   mcp__code-search__switch_project(project_path=<resolved-root>)
   ```
   Require an explicit success response. A switch failure means indexing
   succeeded but the repository is not ready for immediate queries.

8. Summarize semantic chunks/files/time and file/chunk deltas; graph
   nodes/edges/time and its reported lifecycle mode plus discovered/changed/
   unchanged counts; requested and effective graph precision with SCIP
   coverage/drift; the shared `index_generation`; and the active project.
   Confirm readiness only when every prior gate succeeded. Otherwise use the
   phrase **partial index** and state the failed gate and safe retry action.

## Notes

- **code-search** embeds code for semantic similarity search. The embedding provider is chosen at runtime via `EMBEDDING_PROVIDER` — no key is required for local use:
  - `jina` (recommended high-quality local option) — runs on-device, no API key, no data leaves the machine. The first index of ~3K chunks takes ~50min on CPU; incremental re-indexing is fast.
  - `voyage` — current default cloud route for code indexing; uses Voyage 4
    Large, sends code to Voyage AI, and requires `VOYAGE_API_KEY`.
  - `voyage-context` — contextual cloud route; sends code to Voyage AI and
    requires `VOYAGE_API_KEY`. `voyage-code-3` is a separate legacy route.
  - If neither is set, code indexing auto-selects `voyage` when
    `VOYAGE_API_KEY` is present and `local` otherwise. Documentation indexing
    selects `voyage-context` when the key is present.
  - See the plugin README for the full provider comparison.
- **code-graph** performs tree-sitter AST extraction locally (~30-60s). When
  `VOYAGE_API_KEY` is configured, its optional natural-language graph search
  also sends graph-node text to Voyage for embedding.
  - The normal graph is the `heuristic` tier. The optional `scip` tier replaces
    covered heuristic CALLS with compiler-derived relationships and persists
    per project for watcher/incremental runs. It is not an automatic
    organization-wide SCIP indexing service.
- Both support incremental indexing — re-running only processes changed files.
- After indexing, use natural language queries. The code-explore skill handles routing.

## Examples

**Index a repo for the first time:**
```
/index-repo /home/user/projects/my-monorepo
```
Runs code-search and code-graph indexing, reports chunk count and node/edge counts, sets the repo as the active project.

**Re-index after major changes:**
```
/index-repo /home/user/projects/my-monorepo
```
Runs incremental indexing on both tools. Only changed files are reprocessed.

## Success Criteria

- Installed code-graph schema supports `skip_report`, and graph indexing used `skip_report=true`
- Installed code-graph schema supports explicit persistent graph precision;
  requested/effective tier and SCIP coverage/drift were verified
- Auto precision used only the pinned Go generator, preserved checkout
  identity, matched the preparation/indexed artifact digests, and either
  produced verified SCIP or reported an explicit
  heuristic fallback under `preferred`
- `get_indexing_progress` explicitly returned `completed` before graph indexing began
- Both code-search and code-graph indexes verified without errors
- Both engines reported the same complete `index_identity` envelope
- Chunk count (code-search) and node/edge counts (code-graph) reported
- Repo set as active project via `switch_project`
- Repo is immediately searchable via code-explore queries
