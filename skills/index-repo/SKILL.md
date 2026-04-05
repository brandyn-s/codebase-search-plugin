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

`/index-repo /path/to/my-repo`

If no path is provided, ask the user which repo to index.

## Steps

1. Resolve the repo path. Accept absolute paths or short names. Verify the path exists and is a git repo (has `.git/` directory).

2. Run **code-search** indexing:
   ```
   mcp__code-search__index_directory(directory_path=<path>)
   ```
   Report: chunks indexed, files, time taken.

3. Run **code-graph** indexing:
   ```
   mcp__code-graph__index_repository(repo_path=<path>)
   ```
   For Nix-based repos (presence of `flake.nix`, `Cargo.nix`), use `mode: "full"` — fast mode returns 0 results on Nix repos.
   Report: nodes, edges, time taken.

4. **Set active project** for code-search so queries work immediately:
   ```
   mcp__code-search__switch_project(project_path=<path>)
   ```

5. Summarize both indexes and confirm the repo is ready for search. Include which project is now active.

## Notes

- **code-search** uses Voyage AI embeddings for semantic similarity search. Takes 30-90min for large repos (35K+ chunks) due to API rate limits. Requires `VOYAGE_API_KEY`.
- **code-graph** uses local tree-sitter AST parsing (~30-60s). When `VOYAGE_API_KEY` is set, it also generates embeddings for natural language search over graph nodes.
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

- Both code-search and code-graph indexes populated without errors
- Chunk count (code-search) and node/edge counts (code-graph) reported
- Repo set as active project via `switch_project`
- Repo is immediately searchable via code-explore queries
