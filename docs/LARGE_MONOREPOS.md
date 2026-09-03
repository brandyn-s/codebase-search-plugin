# Working with large monorepos

Practical advice from running the plugin on a large private monorepo (about
4,800 files, 34K chunks, Rust/Nix/TypeScript/Python/HCL). These are workflow
observations, not readiness evidence.

## Index by concern, not the entire repo

Indexing the entire monorepo at once produces 34K chunks and queries compete
against everything: a search for "firewall config" matches Nix modules, Rust
network code, TypeScript UI components, and Terraform security groups. Index
sub-projects separately based on what you are working on:

```
/index-repo /path/to/monorepo/nix           # NixOS system config
/index-repo /path/to/monorepo/asset-service # Rust service
/index-repo /path/to/monorepo/libnet        # Rust library
/index-repo /path/to/monorepo/web-ui        # TypeScript
```

The active project auto-switches when you ask questions.

## Quality varies by language

| Language type | Example | Contextualized-embedding advantage | Why |
|---|---|---|---|
| Declarative config | Nix, HCL | +24% | `allowedTCPPorts = [ 80 443 ]` is meaningless without file context |
| Application services | Rust services, TypeScript | +5.5% | Typed functions carry meaning, but service context helps |
| Self-contained libraries | Rust libraries | 0% | `fn login(path: &Path) -> Result<()>` is self-descriptive |

If you work mostly in self-contained libraries, the local Jina model gives you
the same quality as Voyage. In NixOS config or large service codebases,
contextualized cloud embeddings are worth the API cost.

## Nix: use full mode for code-graph

`/index-repo` handles this automatically. When testing code-graph directly,
Nix repos require `mode: "full"`.

## First-time indexing is slow with Jina; incremental is fast

The Jina 0.5b model takes about 50 minutes to index 3K chunks on CPU. After the
initial index, incremental re-index re-embeds only changed files (seconds for a
10-file change), project switching is instant, and query latency is about 5 s.
If the initial time is a blocker, index with Voyage first and switch to Jina
later when you are willing to re-index.

## Use both engines

Semantic search cannot answer "what happens if I change this function", "is
this dead code", or "what is the call path from main() to this handler"; the
graph can. The graph cannot find "the code that handles rate limiting" from a
description; semantic search can. Start with semantic search when the target
is not yet known, then use graph queries to understand how it connects. If you
already know the exact symbol, go straight to the narrowest graph relationship
tool.

## Versioned indexes for docs and release notes

```bash
git worktree add ../myrepo-v1 v1.0.0
git worktree add ../myrepo-v2 v2.0.0
/index-repo ../myrepo-v1
/index-repo ../myrepo-v2
```

Each version gets its own isolated index. Use bounded cross-project discovery
to locate candidates, then select one project for exact follow-up evidence.
Code-graph's immutable index comparison reports file-content and declaration
deltas without treating retrieval scores as comparable.

Limitations: cross-project results are project-balanced discovery only; index
comparison covers file content and declarations, not a semantic diff; there is
no organization ACL model or managed indexing fleet; worktrees use disk space.

## Multi-repo

Each verified repo can be activated independently. Bounded cross-project
discovery can query up to 25 isolated indexes without changing the active
project; rankings remain per-project and scores are not compared globally.
