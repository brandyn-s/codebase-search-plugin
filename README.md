# Codebase Search Plugin

Index a codebase, then ask natural language questions. The plugin routes between semantic search (code-search) and structural graph analysis (code-graph) automatically.

## Quick Start

```bash
# 1. Clone the plugin
git clone https://github.com/redacted-org/codebase-search-plugin.git
cd codebase-search-plugin

# 2. Run the install script (downloads both MCP servers)
bash install.sh

# 3. Set your embedding provider
export EMBEDDING_PROVIDER="jina"            # local, free, no data leaves machine
# OR
export EMBEDDING_PROVIDER="voyage-context"  # best quality (needs API key)
export VOYAGE_API_KEY="your-key"

# 4. Install the plugin in Claude Code
/install-plugin /path/to/codebase-search-plugin

# 5. Index your repo
/index-repo /path/to/your/repo

# 6. Ask questions naturally
# "How does authentication work?"
# "What calls processOrder?"
# "Find dead code"
```

The install script:
- Creates a Python venv inside the plugin directory and pip-installs code-search from GitHub
- Downloads the pre-built code-graph binary for your platform from GitHub releases
- No manual cloning, building, or path configuration needed

## How It Works

This plugin combines two different search technologies. Each solves a different problem.

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
| **`/index-repo`** | Index a repository for both semantic embeddings and structural graph analysis |
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

**Important:** Switching providers requires re-indexing your repos. Each provider produces different-dimensional vectors (Voyage: 1024, Jina: 896) that are incompatible. Just run `/index-repo` again after switching.

## Prerequisites

- **Python 3.12+** (for code-search)
- **`gh` CLI** (optional — install script uses it to find latest code-graph release)
- **`curl`** and `tar`/`unzip` (for downloading code-graph binary)

The `install.sh` script handles everything else — no need to manually clone or build anything.

### Manual install (alternative)

If you prefer not to use the install script:

```bash
# code-search: install from GitHub
pip install "redacted-code-search @ git+https://github.com/redacted-org/code-search.git"

# code-graph: download binary from releases
# https://github.com/redacted-org/code-graph/releases
```

Then configure the MCP server paths manually in `.mcp.json`.

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

### Step 1: Index

```
/index-repo /path/to/your/monorepo
```

This runs both semantic and structural indexing. You only need to do this once per repo — re-running only processes changed files (incremental).

**Indexing time** (3,000 chunks, typical single crate/package):

| Provider | Time | Notes |
|----------|------|-------|
| `voyage-context` | ~5-10 min | API calls, rate-limited |
| `voyage` | ~5-10 min | API calls, rate-limited |
| `jina` | ~50 min (CPU) | Local, no API. First run downloads ~1GB model |
| `local` | ~2-5 min | Local, small model, lower quality |

Structural indexing (code-graph) is always local and completes in ~30-60 seconds regardless of provider.

### Step 2: Search

After indexing, just ask naturally. No special syntax needed.

| Question type | Example | What happens |
|--------------|---------|-------------|
| **Conceptual** | "How does authentication work?" | Semantic search finds auth code, graph traces the call chain |
| **Structural** | "What calls processOrder?" | Graph traces inbound call path with risk classification |
| **Discovery** | "Find the rate limiting implementation" | Semantic search by meaning, not just keywords |
| **Architecture** | "Show all API endpoints" | Graph queries for Route/Handler nodes |
| **Impact** | "Blast radius of changing UserService?" | Graph traces all dependents with hop-distance risk |
| **Quality** | "Find dead code" | Graph finds functions with zero inbound calls |

### Step 3: Multi-repo

You can index multiple repos. The last-indexed repo becomes the active project. When you ask a question, `/code-explore` checks which project your query targets and auto-switches if needed.

```
/index-repo /path/to/repo-a
/index-repo /path/to/repo-b
# Now asking about repo-a's code auto-switches back to repo-a
```

## Model Comparison

Measured on 102 queries across 4 language sub-projects (Nix, Rust service, Rust library, TypeScript) from a production monorepo:

| Provider | Model | MRR (Nix) | MRR (Rust svc) | MRR (Rust lib) | MRR (TypeScript) | Data leaves machine? | Cost |
|----------|-------|-----------|----------------|----------------|------------------|---------------------|------|
| **`voyage-context`** | voyage-context-3 | **0.723** | **0.783** | **0.861** | **0.677** | Yes | ~$0.06/1M tokens |
| `voyage` | voyage-code-3 | 0.584 | 0.742 | 0.861 | 0.642 | Yes | ~$0.06/1M tokens |
| **`jina`** | jina-code-embeddings-0.5b | 0.582 | 0.742 | ~0.86 | **0.660** | **No** | **Free** |
| `local` | all-MiniLM-L6-v2 | ~0.35 | ~0.45 | ~0.50 | ~0.40 | No | Free |

### Key findings

- **`voyage-context-3` is the best model** across all languages tested. Its advantage comes from embedding chunks with awareness of their file context (sibling chunks). The advantage is largest for declarative configuration languages (+24% on Nix) and smallest for self-contained libraries (0% on Rust libs).

- **`jina-code-0.5b` matches `voyage-code-3`** on every language — and beats it on TypeScript (+2.8%). It runs entirely on-device with no API calls. This makes it the recommended choice when code cannot leave the machine.

- **`voyage-code-3` has no advantage over Jina** and requires an API key + sends code to Voyage. There is no reason to use it.

### Which should I use?

| Situation | Recommended provider |
|-----------|---------------------|
| Best quality, code can be sent to Voyage AI | `voyage-context` |
| Code must stay on-device (security/compliance) | `jina` |
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

### Index by concern, not the entire repo

Indexing the entire monorepo at once produces 34K chunks. Queries compete against everything — a search for "firewall config" matches Nix modules, Rust network code, TypeScript UI components, and Terraform security groups. The results are diluted.

**Better approach:** Index sub-projects separately based on what you're working on:

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

The `/index-repo` skill handles this automatically, but if you're calling code-graph directly: Nix repos must use `mode: "full"`. The `fast` mode returns zero results on Nix because tree-sitter Nix grammar produces different AST shapes than code-graph's fast parser expects.

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

## Troubleshooting

**"Search returns irrelevant results from wrong files"**
- Check which project is active: the last-indexed repo wins. Run `/index-repo` on the repo you're working in to make it active.

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

- **Incremental updates**: Re-running `/index-repo` only processes changed files. No need to reindex from scratch.
- **Index storage**: Indexes are stored in `~/.claude_code_search/` by default. Set `CODE_SEARCH_STORAGE` to change.
- **code-graph is fully local**: No API calls, no data leaves the machine. Only code-search uses external APIs (when using Voyage providers).
