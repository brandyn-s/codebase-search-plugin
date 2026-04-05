# Codebase Search Plugin

A Claude Code plugin that provides semantic and structural code search across any codebase. Index once, then ask natural language questions — the plugin automatically routes to the right search tool.

## What It Does

- **`/index-repo`** — Index a repository for both semantic embeddings and structural graph analysis in one command
- **`/code-explore`** — Ask natural language questions about your codebase; automatically routes between semantic search and structural graph queries

## Prerequisites

This plugin requires two MCP servers to be installed and accessible:

### 1. code-search (semantic search)

Python-based MCP server using Voyage AI embeddings for semantic code search.

- **Repo**: [redacted-org/code-search](https://github.com/redacted-org/code-search)
- **Requirements**: Python 3.12+, Voyage API key

```bash
git clone https://github.com/redacted-org/code-search.git
cd code-search
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. code-graph (structural analysis)

Go-based MCP server using tree-sitter AST parsing for structural code analysis.

- **Repo**: [redacted-org/code-graph](https://github.com/redacted-org/code-graph)
- **Requirements**: Pre-built binary or Go 1.21+

```bash
git clone https://github.com/redacted-org/code-graph.git
cd code-graph
go build -o codebase-memory-mcp ./cmd/codebase-memory-mcp
```

## Setup

### 1. Set environment variables

```bash
export VOYAGE_API_KEY="your-voyage-api-key"
export CODE_SEARCH_PATH="/path/to/code-search"
export CODE_GRAPH_PATH="/path/to/code-graph"
```

### 2. Install the plugin

In Claude Code:
```
/install-plugin /path/to/codebase-search-plugin
```

Or copy the plugin directory to `~/.claude/plugins/`.

## Usage

### Index a repo

```
/index-repo /path/to/your/monorepo
```

This runs both semantic and structural indexing. For large repos (35K+ files), semantic indexing may take 30-90 minutes due to API rate limits. Structural indexing is local and completes in ~60 seconds.

### Ask questions

After indexing, just ask naturally:

| Question | What happens |
|----------|-------------|
| "How does authentication work?" | Semantic search finds auth code, graph traces the call chain |
| "What calls processOrder?" | Graph traces inbound call path |
| "Find the rate limiting implementation" | Semantic search by concept |
| "Show all API endpoints" | Graph queries for Route nodes |
| "What's the blast radius of changing UserService?" | Graph traces all dependents |
| "Find dead code" | Graph finds functions with zero inbound calls |

The plugin handles tool selection, project switching, and result chaining automatically.

## Supported Languages

code-search supports any text file. code-graph has AST support for:

Rust, Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Nix, HCL (Terraform), Ruby, Swift, Kotlin, Scala, and more.

## Notes

- **Nix-based repos**: code-graph must use `mode: "full"` (the index-repo skill handles this automatically)
- **Multiple repos**: After indexing multiple repos, the last-indexed repo becomes the active project. The code-explore skill auto-switches if your query context doesn't match the active project.
- **Incremental updates**: Re-running `/index-repo` only processes changed files.
