# Graph Query Quick Reference

## Structure Exploration

```
get_graph_schema          # Node/edge counts, relationship patterns
search_graph(label="Module")  # List top-level modules
search_graph(label="Route")   # List all REST routes
search_graph(label="Function", name_pattern=".*Handler.*")  # Find by name
get_code_snippet(qualified_name="project.path.FunctionName")  # Read source
```

Scope to a directory with `qn_pattern=".*services\\.order\\..*"`.

## Dead Code & Quality Analysis

```
# Dead code: functions with zero inbound calls (excluding entry points)
search_graph(label="Function", relationship="CALLS", direction="inbound", max_degree=0, exclude_entry_points=true)

# High fan-out (calling 10+ others — refactor candidates)
search_graph(label="Function", relationship="CALLS", direction="outbound", min_degree=10)

# High fan-in (called by 10+ others — critical functions)
search_graph(label="Function", relationship="CALLS", direction="inbound", min_degree=10)

# Files that change together (hidden coupling)
query_graph(query="MATCH (a)-[r:FILE_CHANGES_WITH]->(b) WHERE r.coupling_score >= 0.5 RETURN a.name, b.name, r.coupling_score ORDER BY r.coupling_score DESC LIMIT 20")
```

Before deleting dead code candidates, verify with `trace_call_path(direction="inbound", depth=1)` and check for USAGE edges.

## Call Chain Tracing

`trace_call_path` requires an **exact** name. Discover it first:
```
search_graph(name_pattern=".*Order.*", label="Function")
```

Then trace:
```
trace_call_path(function_name="ProcessOrder", direction="both", depth=3)

# Risk-classified impact analysis
trace_call_path(function_name="ProcessOrder", direction="inbound", depth=3, risk_labels=true)
# Returns CRITICAL (hop 1), HIGH (hop 2), MEDIUM (hop 3), LOW (hop 4+)

# Git diff blast radius
detect_changes()                    # All uncommitted changes
detect_changes(scope="branch", base_branch="main")  # Branch delta
```

## Cross-Service & Async

```
# HTTP calls between services
query_graph(query="MATCH (a)-[r:HTTP_CALLS]->(b) RETURN a.name, b.name, r.url_path, r.confidence LIMIT 20")

# Interface implementations
query_graph(query="MATCH (s)-[r:OVERRIDE]->(i) WHERE i.name = 'Read' RETURN s.name, i.name LIMIT 20")

# Read references (callbacks, variable assignments)
query_graph(query="MATCH (a)-[r:USAGE]->(b) WHERE b.name = 'ProcessOrder' RETURN a.name, a.file_path LIMIT 20")
```

## Key Pitfalls

1. `search_graph(relationship="HTTP_CALLS")` filters nodes by degree — does NOT return edges. Use `query_graph` with Cypher to see actual edges.
2. `query_graph` caps at 200 rows — COUNT queries silently undercount. Use `search_graph` with degree filters for counting.
3. `trace_call_path` needs exact names — use `search_graph(name_pattern=...)` first.
4. `search_graph` with degree filters has no row cap (unlike `query_graph`).
