# Codebase Search Plugin

Index a codebase, then ask natural language questions. The plugin routes between semantic search (code-search) and structural graph analysis (code-graph) automatically.

## Quick Start

```bash
# 1. Install prerequisites (see below)
# 2. Set environment variables
export EMBEDDING_PROVIDER="voyage-context"  # or "jina" for local
export VOYAGE_API_KEY="your-key"            # only needed for voyage providers
export CODE_SEARCH_PATH="/path/to/code-search"
export CODE_GRAPH_PATH="/path/to/code-graph"

# 3. Install the plugin in Claude Code
/install-plugin /path/to/codebase-search-plugin

# 4. Index your repo
/index-repo /path/to/your/repo

# 5. Ask questions naturally
# "How does authentication work?"
# "What calls processOrder?"
# "Find dead code"
```

## What It Does

| Skill | Purpose |
|-------|---------|
| **`/index-repo`** | Index a repository for both semantic embeddings and structural graph analysis |
| **`/code-explore`** | Ask natural language questions — auto-routes to the right search tool |

**How routing works:**
- *Conceptual queries* ("how does X work?", "find the Y implementation") → **code-search** (semantic vector + BM25 hybrid)
- *Structural queries* ("what calls X?", "blast radius of changing Y") → **code-graph** (AST-based knowledge graph, Cypher queries)
- *Mixed queries* ("understand the auth system") → both tools chained automatically

## Prerequisites

Two MCP servers must be installed locally:

### code-search (semantic search)

Python-based. Hybrid BM25 + vector search using FAISS.

```bash
git clone https://github.com/redacted-org/code-search.git
cd code-search
python -m venv .venv
.venv/bin/pip install -r requirements.txt  # Linux/Mac
# or: .venv\Scripts\pip install -r requirements.txt  # Windows
```

### code-graph (structural analysis)

Go-based. Tree-sitter AST parsing, builds a knowledge graph with call chains, imports, and routes.

```bash
git clone https://github.com/redacted-org/code-graph.git
cd code-graph
go build -o codebase-memory-mcp ./cmd/codebase-memory-mcp
```

## Environment Variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `EMBEDDING_PROVIDER` | No | `voyage-context` if `VOYAGE_API_KEY` set, else `local` | Which embedding model to use (see Model Comparison below) |
| `VOYAGE_API_KEY` | Only for `voyage-context` / `voyage` | - | Voyage AI API key ([get one here](https://dash.voyageai.com)) |
| `CODE_SEARCH_PATH` | Yes | - | Path to your local code-search clone |
| `CODE_GRAPH_PATH` | Yes | - | Path to your local code-graph clone |
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

## Notes

- **Nix-based repos**: code-graph must use `mode: "full"` — the `/index-repo` skill handles this automatically.
- **Incremental updates**: Re-running `/index-repo` only processes changed files. No need to reindex from scratch.
- **Index storage**: Indexes are stored in `~/.claude_code_search/` by default. Set `CODE_SEARCH_STORAGE` to change.
- **code-graph is fully local**: No API calls, no data leaves the machine. Only code-search uses external APIs (when using Voyage providers).
