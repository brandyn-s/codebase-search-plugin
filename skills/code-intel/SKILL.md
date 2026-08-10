---
name: code-intel
description: >
  Unified code-intelligence entry point. Use for code discovery, structural
  understanding, change impact, security analysis, and evidence-backed code
  questions. Presents FIND, UNDERSTAND, and PROVE as the stable public
  primitives while routing to the existing code-search and code-graph tools.
argument-hint: "[engineering question]"
---

# Code Intelligence

Use three public primitives. Do not expose backend selection as a user concern.
Preserve the existing cross-engine index-identity checks before combining
results from code-search and code-graph.

## FIND

Use for localization and discovery: where code lives, exact identifiers,
conceptual implementations, similar code, or files relevant to an issue.

- Exact identifier/config literal: `mcp__code-search__search_code` with
  `search_mode="keyword"`.
- Conceptual behavior: `mcp__code-search__search_code` with hybrid/default
  retrieval.
- Issue-to-file localization: `mcp__code-search__code_localize`.

Return the smallest sufficient set of file/line evidence.

## UNDERSTAND

Use for relationships and consequences: callers, dependencies, architecture,
change blast radius, affected tests, service interactions, or why a symbol is
important.

Prefer the narrowest graph tool that directly answers the relationship. Chain
from FIND only when the structural target was not already explicit.

## PROVE

Use when the user asks for a security/compliance assertion, exhaustive
relationship, or a claim where missing evidence matters: trust boundaries,
source-to-sink paths, policy/control evidence, or whether all relevant paths
satisfy a property.

Use graph security/compliance tools and explicitly report unresolved coverage,
index freshness, confidence/provenance metadata, and counterexamples. Do not
turn absence of a returned match into proof of absence when graph coverage is
partial or unresolved.

## Coherence gate

Before mixed or chained retrieval, read both engines' status envelopes. Require
matching ready v1 `repository_id`, `checkout_id`, `source_revision`,
`dirty_fingerprint`, and `index_generation`. If they differ, do not combine
results. A single-engine answer is allowed only when that engine alone can
answer the question; label it as not cross-engine coherent.

## Evidence contract

When a backend returns canonical `symbol_ref` or `evidence_ref` fields, preserve
them through chaining rather than re-resolving by short symbol name. Prefer
exact generation-bound references over fuzzy name matching.

## Output

Give the answer first, then the minimum supporting evidence. Surface material
freshness, fallback, confidence, unresolved-edge, or coverage caveats. Avoid
listing every tool call unless the user asks for the trace.
