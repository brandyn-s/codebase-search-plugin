# Embedding providers: offline and online

"Embedding" means converting code into numeric vectors for similarity search.
code-search supports both online (cloud API) and offline (local) models. The
provider is selected at runtime via environment variable, not at install time.
Switching providers requires re-indexing because indexes are provider-specific.

## Defaults

If nothing is set, code-search auto-selects `voyage` for code when
`VOYAGE_API_KEY` exists and `local` otherwise. Documentation indexing selects
`voyage-context` when the Voyage key exists. `RERANKER=sonnet` is used only
when `ANTHROPIC_API_KEY` is present; otherwise reranking is off.

## Online: Voyage AI

Source chunks and search queries are sent to Voyage AI over HTTPS and returned
as vectors. Three selectors exist:

| Provider | Model | Role |
|----------|-------|------|
| **`voyage`** | **`voyage-4-large`** | Default for code when `VOYAGE_API_KEY` is present |
| `voyage-context` | `voyage-context-3` | Contextualized provider; auto-selected for documentation mode |
| `voyage-code-3` | `voyage-code-3` | Legacy, non-default provider retained for older and TypeScript-specific workflows |

`voyage` is not an alias for `voyage-code-3`. All three send code and query
text off-device.

- Requires internet and a Voyage API key
- About $0.06 per 1M tokens (roughly $2-5 to index a large monorepo)
- Indexing speed limited by API rate limits (about 5-10 minutes per 3K chunks)

Code-graph does not use `EMBEDDING_PROVIDER`, but it independently uses
`VOYAGE_API_KEY` for optional node embeddings during indexing and for
embedding-backed graph queries. `CODE_GRAPH_SKIP_EMBEDDINGS=1` prevents graph
node-embedding generation even if the key is present; remove the key from the
MCP environment to prevent all graph Voyage calls, including query embedding.

Use cloud embeddings only when your code and query text are permitted to leave
the machine.

## Offline: Jina code embeddings (`jina`)

A 494M-parameter model runs code-search embeddings on your CPU. Weights
(about 1 GB) are downloaded once from Hugging Face on first use; afterwards
embedding and query operations are local.

- No API key, no internet after the first download, no cost
- Indexing is CPU-bound (about 50 minutes per 3K chunks without a GPU)
- Query latency about 5 s versus about 1 s for Voyage
- Historical measurements put Jina near or above the older `voyage-code-3`
  on the measured subprojects (see `MEASUREMENTS.md`); they do not compare
  Jina with `voyage-4-large`

Use it when code cannot leave the machine, you do not want API keys, or you are
evaluating the plugin before committing to a paid provider. For a fully
on-device run, also remove `VOYAGE_API_KEY` from code-graph's environment and
set `CODE_GRAPH_SKIP_EMBEDDINGS=1`.

| Model | Params | Dim | RAM | CPU index (3K chunks) |
|-------|--------|-----|-----|----------------------|
| `jinaai/jina-code-embeddings-0.5b` (default) | 494M | 896 | ~2.3 GB | ~50 min |
| `jinaai/jina-code-embeddings-1.5b` | 1.54B | 1536 | ~4 GB | ~120 min |

Set via `LOCAL_EMBEDDING_MODEL=jinaai/jina-code-embeddings-1.5b`. Both support
Matryoshka dimension truncation via `JINA_TRUNCATE_DIM`.

## Offline: small model (`local`)

`all-MiniLM-L6-v2`: about 2-5 minutes per 3K chunks, lowest quality. Useful
for a quick look or the smallest possible index.

## Which should I use?

| Situation | Provider |
|-----------|----------|
| Cloud default for code | `voyage` (`voyage-4-large`) |
| Contextualized documentation indexing | `voyage-context` |
| Older `voyage-code-3`-specific workflow | `voyage-code-3` explicitly |
| Code must stay on-device | `jina`, remove the graph Voyage key, set `CODE_GRAPH_SKIP_EMBEDDINGS=1` |
| Quick evaluation, no keys | `jina` |
| Smallest index, lowest resources | `local` |

## Environment variables

Set them in your shell profile or before launching Claude Code; they are read
at runtime.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EMBEDDING_PROVIDER` | auto-detect | code-search provider: `voyage`, `voyage-context`, `voyage-code-3`, `jina`, or `local` |
| `VOYAGE_API_KEY` | unset | Enables Voyage in code-search and optional Voyage embeddings in code-graph |
| `ANTHROPIC_API_KEY` | unset | Enables the Sonnet reranker in code-search |
| `CODE_GRAPH_SKIP_EMBEDDINGS` | unset | `1` or `true` prevents code-graph from generating Voyage node embeddings |
| `LOCAL_EMBEDDING_MODEL` | provider-specific | Hugging Face model for `jina` or `local` |
| `JINA_TRUNCATE_DIM` | unset | Matryoshka dimension truncation |
| `QUANTIZATION` | `int8` | FAISS index type: `int8`, `float32`, `binary` |
| `CODE_SEARCH_STORAGE` | `~/.claude_code_search/` | Index storage location |

## Indexing time reference (3,000 chunks)

| Provider | Time | Notes |
|----------|------|-------|
| `voyage-context` | ~5-10 min | API calls, rate-limited |
| `voyage` | ~5-10 min | API calls, rate-limited |
| `jina` | ~50 min (CPU) | Local; first run downloads ~1 GB model |
| `local` | ~2-5 min | Local, small model, lower quality |

Code-graph's AST extraction and SQLite graph construction typically complete
in 30-60 seconds. With a Voyage key and embeddings enabled, the optional
node-embedding pass adds API-dependent time and cost.

## Supported languages

**code-search** (semantic): any text file. AST-aware chunking for Rust,
Python, TypeScript, JavaScript, Go, Java, C, C++, C#, Nix, HCL (Terraform),
TOML, YAML, and Markdown.

**code-graph** (structural): Rust, Python, TypeScript, JavaScript, Go, Java,
C, C++, C#, Nix, HCL, Ruby, Swift, Kotlin, Scala, and more.
