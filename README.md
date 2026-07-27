# Codebase Search Plugin

**INTEGRATED READINESS: BLOCKED**

The exact components in the current BOM install and pass input-schema
validation, but they cannot yet prove a coherent dual index. `/index-repo`
therefore stops before either engine starts. Do not treat this revision as
ready for integrated indexing or search.

## Quick Start

```bash
# 1. Clone the plugin
git clone https://github.com/redacted-org/codebase-search-plugin.git
cd codebase-search-plugin

# 2. Run the install script (downloads both MCP servers)
bash install.sh            # Linux/Mac
# pwsh install.ps1         # Windows (PowerShell)

# 3. Set your embedding provider
export EMBEDDING_PROVIDER="jina"            # local, free, no data leaves machine
# OR
export EMBEDDING_PROVIDER="voyage-context"  # best quality (needs API key)
export VOYAGE_API_KEY="your-key"

# 4. Install the plugin in Claude Code
/install-plugin /path/to/codebase-search-plugin

# Integrated indexing is BLOCKED for this BOM. Do not run /index-repo yet.
```

The install script:
- Creates a Python venv inside the plugin directory and pip-installs code-search from GitHub
- Downloads the pre-built code-graph binary for your platform from GitHub releases
- Reads exact tested versions from `component-bom.json` instead of selecting a moving latest release
- Starts both installed stdio MCPs and rejects missing or schema-drifted tools before reporting success
- No manual cloning, building, or path configuration needed

> **Current compatibility block:** both pinned snapshots lack attested,
> complete v1 `index_identity` outputs, and code-search lacks an attested
> semantic `index_ready` output. The pinned graph release exposes
> `skip_report`, but its runtime behavior and unchanged-checkout guarantee
> still require separate readiness evidence. See `compatibility/README.md`.

## How It Works

The intended ready-state design combines two different search technologies.
The current BOM remains blocked as described above.

### Semantic Search (code-search)

**What it does:** Finds code by *meaning*, not just keywords. When you search "authentication middleware," it returns functions related to auth even if they're named `verify_jwt_token` or `check_session`.

**How:** Your code is split into chunks (functions, classes, modules) using tree-sitter AST parsing. Each chunk is converted into a numeric vector (embedding) that captures its meaning. Search queries are also converted to vectors, and the closest vectors are returned. A keyword index (BM25) runs in parallel and results are fused together.

**When to use:**
- "How does X work?" — conceptual understanding
- "Find the rate limiting implementation" — discovery by meaning
- "Where is the config for Y?" — locating code you know exists but can't grep for

### Structural Graph (code-graph)

**What it does:** Maps the *structure* of your code — which functions call which, what imports what, how modules connect. Answers questions about relationships and impact, not content.

**How:** Tree-sitter parses your code into an AST, extracts symbols (functions, classes, routes, imports), and builds a knowledge graph stored in SQLite. Queries use a Cypher-like language to traverse call chains, find dead code, and calculate blast radius.

**When to use:**
- "What calls processOrder?" — tracing call chains
- "Blast radius of changing UserService?" — impact analysis
- "Find dead code" — functions with zero callers
- "Show all API endpoints" — structural inventory

### How They Work Together

`/code-explore` automatically routes your question to the right tool:

| Question type | Tool used | Example |
|--------------|-----------|---------|
| Conceptual | code-search | "How does authentication work?" |
| Structural | code-graph | "What calls processOrder?" |
| Mixed | Both (chained) | "Understand the auth system" → finds auth code, then traces its callers |

You don't need to know which tool to use — the plugin decides based on your question.

### Skills

| Skill | Purpose |
|-------|---------|
| **`/index-repo`** | Fail closed for the current blocked BOM; future ready BOMs index both engines |
| **`/code-explore`** | Ask natural language questions — auto-routes to the right search tool |

## Offline vs Online Embedding Models

"Embedding" means converting code into numeric vectors for similarity search. The plugin supports both **online** (cloud API) and **offline** (local) models.

### Online: Voyage AI (`voyage-context`)

Your source code chunks are sent to Voyage AI's API (`api.voyageai.com`) over HTTPS. Voyage converts them to vectors and returns the vectors. Voyage does not store your code (per their data policy), but **your code does leave your machine** during indexing and every search query.

- Best quality (+24% on declarative config languages like Nix)
- Requires internet connection and API key
- ~$0.06 per 1M tokens (~$2-5 to index a large monorepo)
- Indexing speed limited by API rate limits (~5-10 min per 3K chunks)

**Use when:** Quality matters most and your code is not restricted from leaving the network.

### Offline: Jina Code Embeddings (`jina`)

A 494M parameter model runs entirely on your CPU. **No data leaves your machine** — not during indexing, not during search. The model weights are downloaded once (~1GB) from HuggingFace on first use, then everything is offline.

- Matches Voyage's non-contextualized model on all languages tested
- No API key, no internet (after first download), no cost
- Indexing is CPU-bound (~50 min per 3K chunks without GPU)
- Query latency ~5s vs ~1s for Voyage

**Use when:** Code cannot leave the machine (security/compliance), you don't want API keys, or you're evaluating the plugin before committing to a paid provider.

### Switching Between Providers

The provider is selected at **runtime** via environment variable — not at install time. You can switch freely:

```bash
# Switch to local
export EMBEDDING_PROVIDER="jina"

# Switch to cloud
export EMBEDDING_PROVIDER="voyage-context"
export VOYAGE_API_KEY="pa-..."
```

**Important:** On a future readiness-approved BOM, switching providers requires
re-indexing because each provider produces incompatible vector dimensions.

## Prerequisites

- **Python 3.12+** (for code-search)
- **Linux/Mac**: `curl` and `tar` (for downloading code-graph binary)
- **Windows**: PowerShell 5.1+ (built-in) — use `install.ps1` instead of `install.sh`

The `install.sh` script handles everything else — no need to manually clone or build anything.

### Manual install (alternative)

If you prefer not to use the install script:

```bash
# Read the tested repository and full commit from the BOM.
CODE_SEARCH_REPOSITORY="$(python3 -c \
  'import json; print(json.load(open("component-bom.json"))["components"]["code-search"]["install"]["repository"])')"
CODE_SEARCH_REF="$(python3 -c \
  'import json; print(json.load(open("component-bom.json"))["components"]["code-search"]["install"]["revision"])')"

# Install that exact commit and verify pip's PEP 610 provenance.
python3 -m venv .venv-code-search
.venv-code-search/bin/python -m pip install \
  "redacted-code-search @ git+${CODE_SEARCH_REPOSITORY}@${CODE_SEARCH_REF}"
.venv-code-search/bin/python scripts/verify_code_search_revision.py \
  "${CODE_SEARCH_REF}" \
  --repository "${CODE_SEARCH_REPOSITORY}"
```

For code-graph, download only the tag and platform asset declared in
`component-bom.json`, then verify the archive against that asset's BOM
`sha256` before extracting it. Configure the two verified MCP server paths
manually in `.mcp.json`.

Manual installs must match `component-bom.json`. Run the same fail-closed
contract check used by the installers before enabling the plugin:

```bash
python3 scripts/validate_installed.py \
  --server code-search=/path/to/code-search-mcp \
  --server code-graph=/path/to/codebase-memory-mcp
```

## Routing and Evidence Evaluation

`bench/e2e/` contains a deterministic standard-library harness for recorded
host-model traces. It scores routing accuracy, evidence precision/recall,
unsupported claims, tool calls, latency, and stale/mismatched-index handling.
The bundled runs validate the fixture and CI gate only; they are explicitly
not live performance results or comparative grades. See
`bench/e2e/README.md` for the JSONL contract and live-run workflow.

## Trusted component validation

The `validate-installed-components` job installs both private repositories
from the exact refs in `component-bom.json` and validates their real
`tools/list` responses. It runs only from a trusted `main` push or a manual
default-branch dispatch, never from pull-request-controlled code.
`CODE_INTEL_COMPONENT_TOKEN` is a required post-merge validation secret:
configure a fine-grained token with read access to
`redacted-org/code-search` and `redacted-org/code-graph`.
The validator exposes it only to authenticated `gh` clone/download commands
and removes it before package builds or MCP processes start.

There is currently no repository secret fallback. If the secret is absent,
the trusted job intentionally fails; do not skip or weaken this validation.
If a future BOM is promoted to `status: ready`, the same job invokes the
readiness smoke generator against the just-installed MCP executables. The
generated file stays under the isolated runner directory and must pass the
full version, readiness, identity-shape, generation, and unchanged-checkout
validator; neither an environment-supplied nor committed evidence fixture is
accepted as the live CI attestation.

## Environment Variables

These control which embedding model code-search uses. Set them in your shell profile (`.bashrc`, `.zshrc`) or before launching Claude Code. They're read at runtime — you can switch providers without reinstalling.

**For Jina (local, free):**
```bash
export EMBEDDING_PROVIDER="jina"
```

**For Voyage AI (cloud, best quality):**
```bash
export EMBEDDING_PROVIDER="voyage-context"
export VOYAGE_API_KEY="pa-..."  # Get a key at https://dash.voyageai.com
```

**If neither is set**, code-search auto-selects: `voyage-context` if `VOYAGE_API_KEY` exists, otherwise `local` (basic quality).

**All variables:**

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `EMBEDDING_PROVIDER` | No | Auto-detect | `voyage-context`, `jina`, `local` — see Model Comparison below |
| `VOYAGE_API_KEY` | Only for Voyage | - | Voyage AI API key |
| `LOCAL_EMBEDDING_MODEL` | No | `jinaai/jina-code-embeddings-0.5b` | HuggingFace model for `jina` provider |
| `JINA_TRUNCATE_DIM` | No | - | Matryoshka dim truncation (0.5b: 64-896, 1.5b: 128-1536) |
| `QUANTIZATION` | No | `int8` | FAISS index type: `int8` (4x smaller), `float32`, `binary` (32x smaller) |

## Usage

### Step 1: Integrated indexing (currently blocked)

```
/index-repo /path/to/your/monorepo
```

With the current BOM this command must return a blocked/incompatible result
before either engine starts. It becomes an indexing workflow only after the
BOM is promoted to `status: ready` with validated capability attestations and
version-matched readiness evidence.

**Component timing reference for a future ready BOM** (3,000 chunks, typical
single crate/package):

| Provider | Time | Notes |
|----------|------|-------|
| `voyage-context` | ~5-10 min | API calls, rate-limited |
| `voyage` | ~5-10 min | API calls, rate-limited |
| `jina` | ~50 min (CPU) | Local, no API. First run downloads ~1GB model |
| `local` | ~2-5 min | Local, small model, lower quality |

Structural indexing (code-graph) is always local and completes in ~30-60 seconds regardless of provider.

### Step 2: Search (after a future verified dual index)

The routing below describes the intended behavior once integrated readiness is
unblocked; it is not a claim that the current BOM produced a usable dual index.

| Question type | Example | What happens |
|--------------|---------|-------------|
| **Conceptual** | "How does authentication work?" | Semantic search finds auth code, graph traces the call chain |
| **Structural** | "What calls processOrder?" | Graph traces inbound call path with risk classification |
| **Discovery** | "Find the rate limiting implementation" | Semantic search by meaning, not just keywords |
| **Architecture** | "Show all API endpoints" | Graph queries for Route/Handler nodes |
| **Impact** | "Blast radius of changing UserService?" | Graph traces all dependents with hop-distance risk |
| **Quality** | "Find dead code" | Graph finds functions with zero inbound calls |
| **Security** | "Where are the input entry points / auth boundaries?" | Graph queries security-tagged surfaces (auth/crypto/input/sink) |
| **Security** | "Does user input reach a sensitive sink?" | Graph traces source→sink taint paths |
| **Compliance** | "What code satisfies STIG control X?" | Graph maps the control ID to code evidence |
| **Localization** | "Where would I fix \<issue\>?" | Semantic chunk evidence is aggregated into a file-level ranking |

### Step 3: Multi-repo (future ready BOM only)

This workflow is unavailable with the current blocked BOM. Once a later BOM
passes every readiness gate, each verified repo can be activated independently.

```
/index-repo /path/to/repo-a
/index-repo /path/to/repo-b
# Now asking about repo-a's code auto-switches back to repo-a
```

## Historical component-only measurements

The table below predates the provenance-bound routing/evidence harness. It
compares embedding providers inside code-search on 102 historical queries.
This is not an integrated E2E comparative grade, does not attest the current
blocked BOM, and must not be presented as a current live plugin result.

| Provider | Model | MRR (Nix) | MRR (Rust svc) | MRR (Rust lib) | MRR (TypeScript) | Data leaves machine? | Cost |
|----------|-------|-----------|----------------|----------------|------------------|---------------------|------|
| **`voyage-context`** | voyage-context-3 | **0.723** | **0.783** | **0.861** | **0.677** | Yes | ~$0.06/1M tokens |
| `voyage` | voyage-code-3 | 0.584 | 0.742 | 0.861 | 0.642 | Yes | ~$0.06/1M tokens |
| **`jina` (enriched)** | jina-code-embeddings-0.5b | **0.638** | 0.742 | ~0.86 | **0.660** | **No** | **Free** |
| `jina` (baseline) | jina-code-embeddings-0.5b | 0.582 | 0.742 | ~0.86 | 0.660 | No | Free |
| `local` | all-MiniLM-L6-v2 | ~0.35 | ~0.45 | ~0.50 | ~0.40 | No | Free |

*Jina "enriched" = default mode. Prepends sibling chunk names to each chunk's header, approximating Voyage's contextualized embeddings. Enabled automatically for Jina and local providers.*

### Key findings

- **`voyage-context-3` is the best model** across all languages tested. Its advantage comes from embedding chunks with awareness of their file context (sibling chunks). The advantage is largest for declarative configuration languages (+24% on Nix) and smallest for self-contained libraries (0% on Rust libs).

- **`jina-code-0.5b` with enriched headers closes 40% of the gap to Voyage** on Nix (0.582 → 0.638, reference 0.723). Enriched context is on by default — no configuration needed. It runs entirely on-device with no API calls.

- **`jina` now beats `voyage-code-3`** on Nix (0.638 vs 0.584, +9.2%) and TypeScript (0.660 vs 0.642, +2.8%), while staying fully local and free.

- **`voyage-code-3` has no advantage over Jina** and requires an API key + sends code to Voyage. There is no reason to use it.

### Which should I use?

| Situation | Recommended provider |
|-----------|---------------------|
| Best quality, code can be sent to Voyage AI | `voyage-context` |
| Code must stay on-device (security/compliance) | `jina` (enriched headers close 40% of gap) |
| Quick evaluation, don't want to set up API keys | `jina` |
| Smallest possible index, lowest resource usage | `local` (lower quality) |

### Jina model variants

| Model | Params | Dim | RAM | CPU Index (3K chunks) |
|-------|--------|-----|-----|----------------------|
| `jinaai/jina-code-embeddings-0.5b` (default) | 494M | 896 | ~2.3 GB | ~50 min |
| `jinaai/jina-code-embeddings-1.5b` | 1.54B | 1536 | ~4 GB | ~120 min |

Set via `LOCAL_EMBEDDING_MODEL=jinaai/jina-code-embeddings-1.5b`. Both support Matryoshka dimension truncation via `JINA_TRUNCATE_DIM`.

## Supported Languages

**code-search** (semantic): Any text file. AST-aware chunking for Rust, Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Nix, HCL (Terraform), TOML, YAML, and Markdown.

**code-graph** (structural): Rust, Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Nix, HCL, Ruby, Swift, Kotlin, Scala, and more.

## Lessons Learned: prototype-software-merry

Practical advice from running this plugin on the Corsair monorepo (~4,800 files, 34K chunks, Rust/Nix/TypeScript/Python/HCL).
These are historical component observations and future-workflow guidance, not
evidence that the current BOM is integrated-ready.

### Index by concern, not the entire repo

Indexing the entire monorepo at once produces 34K chunks. Queries compete against everything — a search for "firewall config" matches Nix modules, Rust network code, TypeScript UI components, and Terraform security groups. The results are diluted.

**Future ready workflow:** Index sub-projects separately based on what you're
working on only after the BOM block is lifted:

```
/index-repo /path/to/monorepo/nix           # NixOS system config
/index-repo /path/to/monorepo/assetman      # Asset management service (Rust)
/index-repo /path/to/monorepo/libnet        # Networking library (Rust)
/index-repo /path/to/monorepo/mithrandir    # Web UI (TypeScript)
```

The active project auto-switches when you ask questions. If you're working on the Rust networking library, the results come from `libnet` — not the entire monorepo.

### Quality varies by language — context matters more for config languages

Our eval (102 queries, 4 languages) showed that embedding quality depends on the language:

| Language type | Example | voyage-context-3 advantage | Why |
|---|---|---|---|
| **Declarative config** | Nix, HCL | **+24%** over baseline | `allowedTCPPorts = [ 80 443 ]` is meaningless without file context |
| **Application services** | Rust services, TypeScript | **+5.5%** | Typed functions carry meaning in signatures, but service context helps |
| **Self-contained libraries** | Rust libraries | **0%** (tie) | `fn login(path: &Path) -> Result<()>` is fully self-descriptive |

**Practical implication:** If you work primarily in Rust libraries, Jina (free, local) gives you the same quality as Voyage. If you work in NixOS config or large service codebases, Voyage's contextualized embeddings are worth the API cost.

### Nix-specific: use full mode for code-graph

On a future ready BOM, the `/index-repo` skill handles this automatically.
When testing code-graph directly, Nix repos require `mode: "full"`.

### First-time indexing is slow with Jina — incremental is fast

The Jina 0.5b model takes ~50 minutes to index 3K chunks on CPU (no GPU). This is a one-time cost. After the initial index:
- **Incremental re-index**: Only changed files are re-embedded. A 10-file change takes seconds.
- **Project switching**: Instant — just loads the existing index from disk.
- **Query latency**: ~5 seconds per query on CPU.

If the initial indexing time is a blocker, index during lunch or overnight. Or use Voyage for the initial index (`EMBEDDING_PROVIDER=voyage-context`), then switch to Jina for daily use once you're willing to re-index.

### The graph and semantic tools complement each other — don't use just one

Common mistake: using only semantic search ("find the auth code") and ignoring the graph. The graph answers questions that semantic search fundamentally cannot:

- **"What happens if I change this function?"** → Graph traces all callers (blast radius)
- **"Is this function dead code?"** → Graph checks for zero inbound calls
- **"What's the dependency chain from main() to this handler?"** → Graph traces the call path

Conversely, the graph can't answer "where is the code that handles rate limiting?" — that requires understanding *meaning*, which is what semantic search does.

**Best workflow:** Start with semantic search to *find* relevant code, then use graph queries to *understand* how it connects.

### Versioned indexes for docs and release notes

After a future BOM is promoted to ready, version-specific workflows can use
isolated Git worktrees:

```bash
# Create worktrees for each version you want to index
git worktree add ../myrepo-v1 v1.0.0
git worktree add ../myrepo-v2 v2.0.0

# Index each version separately — different paths = different project IDs
/index-repo ../myrepo-v1
/index-repo ../myrepo-v2
```

Each version gets its own isolated index. Use `switch_project` (or just ask about code in a specific version) to query one or the other. The plugin auto-switches based on which project your question targets.

**Use cases:**
- **Release notes**: Index v1 and v2, search each for "what changed in authentication" — compare results
- **Migration guides**: Index old and new versions, find functions that moved or were renamed
- **Version-specific docs**: Index the exact code a version ships, generate docs from that snapshot — no hallucinations from newer code

**Tip for doc generation:** If your docs are well-structured (MDX, Markdown with clear sections), the local Jina model works well — structured text is self-descriptive, like typed code. You don't need Voyage's contextualized embeddings for docs that already have good headings and organization.

**Current limitations:**
- No cross-project search (can't query both versions in one call)
- No diff between indexes ("show what changed between v1 and v2")
- Worktrees use disk space for each checked-out version

## Troubleshooting

**"Search returns irrelevant results from wrong files"**
- On a readiness-approved BOM, check which verified project is active. With
  the current BOM, `/index-repo` must remain blocked.

**"Indexing seems stuck"**
- Jina CPU indexing is genuinely slow (~1 chunk/second on CPU). Check `~/.claude_code_search/` for growing index files.
- For Voyage, check your API key is valid and you haven't hit rate limits.

**"All search results come from one large file"**
- Some repos have very large files (generated code, lockfiles) that dominate results. Add them to `.gitignore` or index a sub-directory instead.

**"code-graph returns 0 nodes"**
- For Nix repos: ensure `mode: "full"` is used (the `/index-repo` skill does this automatically).
- Check that the target directory has files in supported languages.

**"Vector search results seem random"**
- If you indexed before 2026-04-05: the FAISS int8 quantizer had a bug that returned zero similarities. Re-index to fix.

## Notes

- **Incremental updates**: This is a future-ready workflow; it is unavailable
  while the BOM status is blocked.
- **Index storage**: Indexes are stored in `~/.claude_code_search/` by default. Set `CODE_SEARCH_STORAGE` to change.
- **code-graph is fully local**: No API calls, no data leaves the machine. Only code-search uses external APIs (when using Voyage providers).
