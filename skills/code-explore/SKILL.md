---
name: code-explore
description: >
  Find code by meaning, understand how something works, or explore code
  needing both semantic search and structural analysis. Routes to code-search
  for conceptual queries and auto-chains with code-graph for context.
  Trigger phrases: "find code", "where is", "how does", "show me the",
  "find the implementation", "understand this codebase", "what calls".
  Do NOT use for file reading (use Read), simple grep (use Grep), or
  non-code questions.
argument-hint: "[natural language code query]"
---

# Code Explore

Route code exploration queries to the right tool and chain results automatically.

## Tool Inventory

### code-search (semantic + keyword hybrid)

| Tool | Use for |
|------|---------|
| `mcp__code-search__search_code` | Find code by meaning/keywords |
| `mcp__code-search__find_similar_code` | Find similar chunks to a result |
| `mcp__code-search__get_index_status` | Check if repo is indexed |
| `mcp__code-search__switch_project` | Change active project context |

### code-graph (structural analysis)

| Tool | Use for |
|------|---------|
| `mcp__code-graph__search_graph` | Find nodes by name/pattern |
| `mcp__code-graph__query_graph` | Cypher relationship queries |
| `mcp__code-graph__trace_call_path` | Trace call chains between functions |
| `mcp__code-graph__get_code_snippet` | Get source + caller/callee metadata |
| `mcp__code-graph__get_architecture` | Codebase overview (routes, hotspots, layers) |
| `mcp__code-graph__detect_changes` | Blast radius of uncommitted changes |

## Pre-flight Check

Before routing, verify the target repo is indexed and active:

1. **Check active project**: `mcp__code-search__list_projects` — look at the `current_project` field.
   - If `current_project` is `null`: infer the target repo from the user's query context (CWD, mentioned crate/service names, or explicit repo reference) and run `mcp__code-search__switch_project(project_path=<path>)`.
   - If `current_project` points to a different repo than the query targets: run `switch_project` to the correct one. Tell the user which project you switched to.
   - If `current_project` matches the query context: proceed.
2. **Check indexes exist**:
   - `mcp__code-search__get_index_status` — if empty, suggest running `/index-repo`
   - `mcp__code-graph__index_status` — if not found, suggest running `/index-repo`
3. **For code-graph queries**, pass the `project` parameter explicitly (code-graph uses project name, not an active-project concept). Use `mcp__code-graph__list_projects` to find the correct project name if unsure.

If only one index exists, route to that tool only and note the limitation.

## Routing Decision Tree

### Step 1: Classify the query

| Query pattern | Type | Primary tool |
|--------------|------|-------------|
| "Where is the X code?" | Conceptual | code-search |
| "Find the X implementation" | Conceptual | code-search |
| "How does X work?" | Conceptual | code-search |
| "Show me X patterns" | Conceptual | code-search |
| "What calls X?" | Structural | graph: trace_call_path inbound |
| "Who uses X?" | Structural | graph: search_graph + trace |
| "Blast radius of changing X" | Structural | graph: detect_changes |
| "Find dead code" | Structural | graph: search_graph max_degree=0 |
| "Show all routes/endpoints" | Structural | graph: get_architecture routes |
| "Trace from X to Y" | Structural | graph: trace_call_path |
| "What depends on X?" | Structural | graph: query_graph IMPORTS inbound |
| "Understand this codebase" | Overview | graph: get_architecture, then code-search |

### Step 2: Execute primary tool

Run the tool identified in Step 1.

### Step 3: Auto-chain if the answer needs the other tool

| After this result... | Follow up with... |
|---------------------|-------------------|
| code-search found a function | Graph: `trace_call_path` to see who calls it |
| code-search found a function | Graph: `get_code_snippet` with include_neighbors=true |
| code-search result is truncated | Graph: `get_code_snippet` by qualified name |
| Graph found callers/callees | code-search: `search_code` to understand what they do |
| Graph found a node | Read tool with file:line for exact implementation |
| "How does X work?" partially answered | code-search: `find_similar_code` with chunk_id |

### Step 4: Present combined answer

1. Direct answer to the question
2. Primary result (file, function, line numbers)
3. Chained context (callers, dependencies, similar code)

## Graph Query Quick Reference

Detailed Cypher patterns and pitfalls are in `references/graph-queries.md`.

## Examples

**"Where's the rate limiting code?"**
1. Conceptual -> code-search: `search_code("rate limiting code")`
2. Chain -> graph: trace callers of the found function
3. Answer with file path, line number, and caller context

**"What calls processOrder?"**
1. Structural -> graph: `trace_call_path(function_name="processOrder", direction="inbound")`
2. Chain -> code-search for implementation details
3. Answer with call chain and file locations

**"Understand the authentication system"**
1. Overview -> graph: `get_architecture(aspects=["routes", "services"])`
2. Conceptual -> code-search: `search_code("authentication logic")`
3. Structural -> graph: trace auth call chain
4. Answer with combined narrative

## Success Criteria

- Query routed to the correct tool (code-search for conceptual, code-graph for structural)
- Results include file paths and line numbers for navigation
- Auto-chaining applied when the primary result needs context from the other tool
- Active project verified before any search
