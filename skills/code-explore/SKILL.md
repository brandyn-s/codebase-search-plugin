---
name: code-explore
description: >
  Find code by meaning, understand how something works, or explore code
  needing both semantic search and structural analysis. Routes to code-search
  for conceptual queries and adds code-graph only for requested relationships.
  Trigger phrases: "find code", "where is", "how does", "show me the",
  "find the implementation", "understand this codebase", "what calls".
  Do NOT use for file reading (use Read), simple grep (use Grep), or
  non-code questions.
argument-hint: "[natural language code query]"
---

# Code Explore

Route code exploration queries to the narrowest tool that can answer them.

## Tool Inventory

### code-search (semantic + keyword hybrid)

| Tool | Use for |
|------|---------|
| `mcp__code-search__search_code` | Find code by meaning/keywords |
| `mcp__code-search__find_similar_code` | Find similar chunks to a result |
| `mcp__code-search__code_localize` | Rank files for a natural-language issue |
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

### code-graph (security & localization — redacted extensions)

| Tool | Use for |
|------|---------|
| `mcp__code-graph__query_security_surfaces` | Enumerate security-tagged surfaces by `role` (`auth_boundary`, `input_entry_point`, `sensitive_sink`, `crypto_operation`, `privilege_escalation`, `session_management`, `audit_logging`, `sanitizer`). Pass `mode="tainted_paths"` to return source→sink taint paths instead of a flat surface list |
| `mcp__code-graph__trace_data_flow` | Trace propagation from a `source` function through the graph (env-var aware) — "where does this sensitive data end up?" |
| `mcp__code-graph__query_stig_evidence` | Map a STIG/NIST `control_id` to the code that provides evidence for it |
Use code-search's `code_localize` for natural-language issue localization.
Code-graph's structural localizer remains available for explicitly structural
localization, but it does not replace hybrid semantic/lexical retrieval.

## Pre-flight Check

Before routing, verify the target repo is indexed and active:

1. **Check active project**: `mcp__code-search__list_projects` — look at the `current_project` field.
   - If `current_project` is `null`: infer the target repo from the user's query context (CWD, mentioned crate/service names, or explicit repo reference) and run `mcp__code-search__switch_project(project_path=<path>)`.
   - If `current_project` points to a different repo than the query targets: run `switch_project` to the correct one. Tell the user which project you switched to.
   - If `current_project` matches the query context: proceed.
2. **Read and retain both status envelopes before retrieval**:
   - `mcp__code-search__get_index_status` — if empty or stale, suggest running `/index-repo`
   - `mcp__code-graph__index_status` — if not found or stale, suggest running `/index-repo`
3. **Verify cross-engine identity**. Each status must contain
   `index_identity.schema_version: 1` plus `repository_id`, `checkout_id`,
   `source_revision`, `dirty_fingerprint`, and `index_generation`.
   Compare every field exactly across the two envelopes. The `checkout_id`
   must match because both engines must describe the same local checkout.
   - If either status is stale, any identity field is missing, or any value
     differs, block mixed or chained retrieval.
   - Report the exact missing, stale, or mismatched fields and both observed
     values. Never describe a legacy/missing envelope as ready.
4. **For code-graph queries**, pass the `project` parameter explicitly (code-graph uses project name, not an active-project concept). Use `mcp__code-graph__list_projects` to find the correct project name if unsure.

If only one index is usable, or both are individually usable but their
identities do not match, a single-engine fallback is allowed only when that
engine can answer the query. State explicitly that the result is **not cross-engine coherent**
and name the engine used. Do not combine evidence
from the other engine, and do not auto-chain into it.

## Routing Decision Tree

### Step 1: Classify the query

Apply this precedence before the examples in the table. An explicit
source-to-sink, trust-boundary, or security-path request is structural security
work. Security vocabulary alone does not make a question a security-path
question. A request combining conceptual explanation with callers or other
relationships uses code-search semantic/default retrieval first, followed by
exactly one directed graph relationship query. Conceptual how, why, or whether
behavior remains semantic/default retrieval even when it names an exact symbol
or discusses security. Do not call graph security tools for conceptual behavior
unless the question explicitly requests a path, sink reachability, trust
boundary, or security-surface enumeration. Use keyword retrieval only for pure
literal or location lookup, and graph-only routing only for an explicit
relationship without a conceptual explanation.

Keep semantic and lexical work within the selected route. Do not add graph text
search as corroboration unless the question is graph, mixed, or security work.

| Query pattern | Type | Primary tool |
|--------------|------|-------------|
| "Where is the X code?" | Conceptual | code-search |
| "Find the X implementation" | Conceptual | code-search |
| "How does X work?" | Conceptual | code-search |
| "Show me X patterns" | Conceptual | code-search |
| "What calls X?" | Structural | graph: trace_call_path inbound |
| "Who uses X?" | Structural | graph: trace_call_path inbound when X is exact |
| "Blast radius of changing X" | Structural | graph: detect_changes |
| "Find dead code" | Structural | graph: search_graph max_degree=0 |
| "Show all routes/endpoints" | Structural | graph: get_architecture routes |
| "Trace from X to Y" | Structural | graph: trace_call_path |
| "What depends on X?" | Structural | graph: query_graph IMPORTS inbound |
| "Understand this codebase" | Overview | graph: get_architecture, then code-search |
| "Where are the auth/input/crypto surfaces?" | Security | graph: query_security_surfaces (by `role`) |
| "Does any user input reach a sink?" | Security | graph: query_security_surfaces mode="tainted_paths" |
| "Trace how X (secret/PII/token) flows" | Security | graph: trace_data_flow(source=X) |
| "What code satisfies STIG/NIST <control>?" | Compliance | graph: query_stig_evidence(control_id=...) |
| "Where's the code I'd change for <issue>?" | Localization | code-search: code_localize (issue text) |

### Step 2: Execute primary tool

Run the tool identified in Step 1.

For an exact function relationship, make one `trace_call_path` call with
`direction="inbound"` for callers or `direction="outbound"` for callees.
Do not add `search_graph` before or after a trace that resolves the exact
symbol; use it only when the symbol is unresolved. Use `Read` to corroborate returned
relationships and pin source lines without repeating graph discovery. Resolve
every named relationship endpoint before asserting the edge. Cite the direct
call site as edge evidence and minimal source evidence for every candidate-named
endpoint. One location may satisfy both the edge and endpoint roles.

### Step 3: Chain only when the question needs the other tool

Do not auto-chain a complete conceptual result into code-graph. Add the other
engine only when the question explicitly asks for a relationship, caller,
callee, dependency, impact, architecture, or source-to-sink path, or when the
primary engine cannot resolve the target needed for that requested operation.
Keep the selected route first and append secondary context; do not use symmetric
rank fusion that can demote a correct primary result.

| After this result... | Follow up with... |
|---------------------|-------------------|
| User asked who calls a function found by code-search | Graph: one inbound `trace_call_path` |
| User asked what a function calls | Graph: one outbound `trace_call_path` |
| code-search result is truncated | Graph: `get_code_snippet` by qualified name |
| Graph found callers/callees and explanation was requested | code-search: semantic/default evidence retrieval |
| Graph found a node | Read tool with file:line for exact implementation |
| "How does X work?" partially answered | code-search: `find_similar_code` with chunk_id |

### Step 4: Evidence Closure Gate

Before treating a relationship as verified, decompose the candidate into
atomic relationships and make an inspection ledger for every named endpoint.
Read or retrieve the definition of each caller and callee, or each source and
target. Endpoint resolution is an adjudication check, but inspected locations
do not automatically become answer evidence. Include minimal source evidence
for every candidate-named endpoint. Apply a deletion test to the remaining
answer evidence: omit unnamed definitions, helpers, and context unless removing
the location would leave an atomic claim unsupported. If any endpoint is missing,
retrieve it before answering; if it cannot be resolved, do not present the
relationship as supported and state that it remains unresolved.

After the deletion test, pin every final `path:start-end` with one successful
exact `Read` using `offset=start` and `limit=end-start+1`. Whole-file or
unbounded Reads remain inspection-only. Cite every final evidence ID verbatim
in the answer; an uncited location is not answer evidence.

When pinning coordinates, do not copy a synthetic terminal line from `Read`.
`Read` can display one extra numbered empty line after a file-ending newline;
the evidence range must stop at the final physical source line.

### Step 5: Present combined answer

1. Direct answer to the question
2. Primary result (file, function, line numbers)
3. Chained context (callers, dependencies, similar code)

## Graph Query Quick Reference

Detailed Cypher patterns and pitfalls are in `references/graph-queries.md`.

## Examples

**"Where's the rate limiting code?"**
1. Conceptual -> code-search: `search_code("rate limiting code")`
2. Answer with the backend-issued source evidence. Do not add a graph query
   unless the user also asks for callers, dependencies, or impact.

**"What calls processOrder?"**
1. Structural -> graph: `trace_call_path(function_name="processOrder", direction="inbound")`
2. Chain -> code-search for implementation details
3. Answer with call chain and file locations

**"Understand the authentication system"**
1. Overview -> graph: `get_architecture(aspects=["routes", "services"])`
2. Conceptual -> code-search: `search_code("authentication logic")`
3. Structural -> graph: trace auth call chain
4. Answer with combined narrative

**"Where are the input entry points, and does any reach a sensitive sink?"**
1. Security -> graph: `query_security_surfaces(role="input_entry_point", mode="tainted_paths")`
2. Chain -> graph: `trace_data_flow(source=<entry function>)` to follow propagation
3. Answer with the surfaces, any source→sink paths, and file:line for each

## Success Criteria

- Query routed to the correct tool (code-search for conceptual, code-graph for structural)
- Results include file paths and line numbers for navigation
- Cross-engine chaining applied only when the requested answer needs it
- Active project verified before any search
