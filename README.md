# codebase-search plugin for Claude Code

A Claude Code plugin that bundles [code-search](https://github.com/brandyn-s/code-search)
and [code-graph](https://github.com/brandyn-s/code-graph) so an agent can
**find**, **understand**, and **prove** claims about a codebase, with evidence
it can show you.

Most code-intelligence tools hand the model a ranked list and hope. This plugin
adds an evidence contract: both indexes are bound to one exact checkout, every
citation carries an immutable generation-bound reference, security and
"every path" claims must pass contradiction and coverage checks, and the result
is a deterministic verdict you can export and re-verify. See
[docs/EVIDENCE.md](docs/EVIDENCE.md).

## Install

```bash
claude plugin marketplace add brandyn-s/codebase-search-plugin
claude plugin install codebase-search@code-intelligence --scope user
```

That is all. The two MCP servers install themselves the first time Claude Code
launches them: the launchers in `bin/` fetch the exact component releases
pinned in `component-bom.json`, verify SHA-256 checksums, and start the
servers. The first launch downloads a Python environment and a Go binary, so it
can take a few minutes; if Claude Code reports the servers as failed on that
first start, wait for `.runtime/bootstrap.log` in the plugin directory to
finish and reconnect with `/mcp`. Windows users run `install.ps1` from the
installed plugin directory once instead.

No API keys are required. Without keys, code-search embeds locally and skips
LLM reranking, and code-graph stays fully on-device.

| Optional variable | Effect |
|---|---|
| `VOYAGE_API_KEY` | Cloud embeddings for code-search (`voyage` maps to `voyage-4-large`); code-graph also uses it for optional node embeddings unless `CODE_GRAPH_SKIP_EMBEDDINGS=1` |
| `ANTHROPIC_API_KEY` | Enables the Sonnet reranker in code-search (`RERANKER=sonnet`) |
| `EMBEDDING_PROVIDER` | Force `voyage`, `voyage-context`, `jina` (local, best offline), or `local` (small, fast) |

Switching embedding providers requires a re-index. Details, model choices, and
cost/latency numbers are in [docs/EMBEDDINGS.md](docs/EMBEDDINGS.md).

## Use

```text
/index-repo /path/to/your/repo
/code-intel Find the request authentication entry points
/code-intel What calls processOrder, and what breaks if its signature changes?
/code-intel Prove every HTTP route passes through the authorization middleware
```

`/index-repo` runs both engines and checks that they indexed the same
unchanged checkout. This runs both semantic and structural indexing; the
graph side suppresses report writes. `/code-intel` routes each question to the
right engine and applies the evidence rules. `/code-explore` is the compact
natural-language variant.

## What you get

- **`/index-repo`**: index both engines and verify readiness and checkout identity.
- **`/code-intel`**: FIND / UNDERSTAND / PROVE facade with coherence, contradiction, and coverage rules.
- **`/code-explore`**: natural-language questions, auto-routed.
- **code-search MCP server**: hybrid semantic + lexical retrieval with persistent per-project indexes.
- **code-graph MCP server**: callers, callees, implementations, routes, change impact, dead code, security surfaces.

For clean Go or TypeScript repositories, `/index-repo ... --graph-precision auto`
adds compiler-grade CALLS and IMPORTS relationships through pinned SCIP
generators. Everything else is tree-sitter plus heuristic resolution and is
labeled as such.

## Verified install

Checksums are always verified against the BOM. When the GitHub CLI is
installed and authenticated, the installer also verifies GitHub build
provenance for both components with `gh attestation verify`; without it, the
installer says so once and continues. Manual installs, token scopes, the
trusted post-merge validation workflow, and the readiness record are described
in [docs/INSTALL.md](docs/INSTALL.md).

## Using the components without Claude Code

Both servers are ordinary stdio MCP servers and work with Cursor, Codex, Gemini
CLI, Windsurf, and any other MCP client. See each component's client guide:
[code-search docs/clients.md](https://github.com/brandyn-s/code-search/blob/main/docs/clients.md)
and
[code-graph docs/clients.md](https://github.com/brandyn-s/code-graph/blob/main/docs/clients.md).

## Measurements and benchmarks

Bounded measurements (public LocBench localization, LLVM-scale indexing,
lifecycle timings) are recorded in [docs/MEASUREMENTS.md](docs/MEASUREMENTS.md).
The reproducible comparison harness lives in
[bench/compare/README.md](bench/compare/README.md) and the recorded-trace
routing harness in [bench/e2e/README.md](bench/e2e/README.md). Guidance for
very large repositories is in [docs/LARGE_MONOREPOS.md](docs/LARGE_MONOREPOS.md).

## Troubleshooting

- **Servers fail on the very first launch**: the bootstrap is still installing. Watch `.runtime/bootstrap.log` under the plugin directory, then reconnect with `/mcp`. Set `CODE_INTEL_NO_BOOTSTRAP=1` to disable auto-install and run `install.sh` yourself.
- **Indexing seems stuck**: local Jina indexing is CPU-bound (about one chunk per second). Check `~/.claude_code_search/` for growing files. With Voyage, check the key and rate limits.
- **Results come from the wrong project**: rerun `/index-repo` on the checkout you mean; the active project follows the verified index.
- **code-graph returns 0 nodes on a Nix repo**: `/index-repo` already uses `mode: "full"`; when calling the server directly, pass it explicitly.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CHANGELOG.md](CHANGELOG.md).

## License

The plugin itself (this repository: skills, installer, validators, benchmark
harness) is licensed under the MIT License; see `LICENSE`.

### Third-party components

The installer downloads two separately licensed components that remain under
their own licenses:

- **code-search** ([brandyn-s/code-search](https://github.com/brandyn-s/code-search))
  is licensed under the GNU General Public License v3.0. It is derived from
  [FarhanAliRaza/claude-context-local](https://github.com/FarhanAliRaza/claude-context-local).
- **code-graph** ([brandyn-s/code-graph](https://github.com/brandyn-s/code-graph))
  is licensed under the MIT License. It is derived from
  [DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)
  and bundles tree-sitter grammars under their respective licenses.
