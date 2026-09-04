# Security

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's advisory form:
<https://github.com/brandyn-s/codebase-search-plugin/security/advisories/new>.
Do not open a public issue for a suspected vulnerability.

You will get an acknowledgement within seven days. We aim to publish a fix and
an advisory within 90 days of the report, sooner when a fix is straightforward.
If a report concerns one of the bundled components rather than the plugin,
we will forward it to that component's repository
([code-search](https://github.com/brandyn-s/code-search/security/advisories/new),
[code-graph](https://github.com/brandyn-s/code-graph/security/advisories/new))
and tell you where it went.

## Threat model

The plugin is a thin installer and launcher. On first start it downloads two
components from the GitHub releases pinned in `component-bom.json`, verifies
each archive and binary against the SHA-256 digests recorded in the BOM, and
verifies GitHub build provenance with `gh attestation verify` when the GitHub
CLI is available (otherwise it prints one line saying provenance was not
checked). Downloads use HTTPS to `github.com` and `objects.githubusercontent.com`
only. Installed components live under the plugin directory (`.venv/` for
code-search, `.runtime/` for code-graph and its optional SCIP indexers); the
plugin does not write anywhere else and does not modify Claude Code settings.

Both components run as local stdio MCP servers spawned by Claude Code. They
read the source trees you point them at and write indexes to the user's home
directory (`~/.claude_code_search/` and `~/.cache/code-graph/`). The plugin
itself sends nothing over the network after install. The components send
query text and source-derived chunks to Voyage AI when `VOYAGE_API_KEY` is set
and to Anthropic when `ANTHROPIC_API_KEY` is set; with no keys configured
everything stays on the machine. The launchers write only to stderr before
handing off to the server, so the MCP protocol channel on stdout is never
polluted by installer output. Neither component authenticates its optional
HTTP transport; keep them on stdio or bind to localhost only.
