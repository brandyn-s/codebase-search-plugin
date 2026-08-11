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

## Route selection

Classify the question before calling tools. Use the first matching route:

1. An explicit source-to-sink, trust-boundary, or security-path question uses
   graph security/relationship tools. Security vocabulary alone does not make
   a question a security-path question.
2. A question combining conceptual explanation with callers or relationships
   uses code-search semantic/default retrieval first, then exactly one directed
   graph relationship query. An explicit symbol does not waive this mixed route
   when explanation and relationship are both requested.
3. A callers-only or relationship-only question with an explicit symbol uses
   the narrowest graph tool directly.
4. Pure literal or location lookup for an exact identifier or config key uses
   `mcp__code-search__search_code` with `search_mode="keyword"`.
5. Conceptual how, why, or whether behavior uses code-search semantic/default
   retrieval, even when it names an exact symbol or discusses security.

Do not call graph security tools for conceptual behavior unless the question
explicitly requests a path, sink reachability, trust boundary, or
security-surface enumeration.
Do not substitute graph text search for a required code-search semantic or
keyword FIND step. Additional tools may corroborate the required route.

## FIND

Use for localization and discovery: where code lives, exact identifiers,
conceptual implementations, similar code, or files relevant to an issue.

- Exact identifier/config literal: `mcp__code-search__search_code` with
  `search_mode="keyword"`.
- Conceptual behavior: `mcp__code-search__search_code` with hybrid/default
  retrieval.
- Evidence-backed conceptual search: prefer the additive
  `search_code_evidence` tool when the installed code-search component exposes
  it. It uses the same retrieval pipeline and emits generation-bound
  `evidence_ref` and `observation_ref` objects only when the semantic index
  identity is unchanged across the search. It emits `symbol_ref` only when a
  canonical qualified name is available; never convert a short or merged chunk
  name into a graph identity. Until the tested component BOM includes that
  tool, use the ordinary search tool and do not manufacture references.
- Issue-to-file localization: `mcp__code-search__code_localize`.

Return the smallest sufficient set of file/line evidence. Shrink every
`path:start-end` range to only the lines needed to prove the answer; omit
imports, blank lines, or surrounding context unless they are necessary for an
atomic claim or named relationship endpoint. Apply a deletion test to every
location: remove it unless its deletion would leave an atomic clause or named
endpoint unsupported. Do not return discovery, contextual, or duplicate
corroborating locations. Evidence is claim-scoped, not flow-scoped. For a
direct relationship, imports or aliases are discovery context, not evidence;
cite the direct call site and named endpoint definitions only. Do not cite
extra upstream or downstream endpoints, call sites, or relationships not named
by the candidate unless an unnamed helper is the only direct implementation of
an atomic clause.

## UNDERSTAND

Use for relationships and consequences: callers, dependencies, architecture,
change blast radius, affected tests, service interactions, or why a symbol is
important.

Prefer the narrowest graph tool that directly answers the relationship. Chain
from FIND only when the structural target was not already explicit. Preserve
canonical references through the chain; do not downgrade an exact `symbol_ref`
to fuzzy short-name matching. For `search_graph`, set `include_source=true`
when generation-bound graph evidence is required; ordinary searches retain the
fast metadata-only path.

Callers of an exact function use `mcp__code-graph__trace_call_path` with
`direction="inbound"`; callees use the same tool with `direction="outbound"`.
Call the directed trace once. Do not add `search_graph` before or after it when
the exact symbol resolves; use `search_graph` only when the exact function name
is unresolved. Use `Read` only to corroborate the returned relationship and pin
source lines. For a relationship claim, cite every named relationship endpoint,
both caller and callee or source and target, even when the trace already shows
one endpoint. Do not replace those narrow operations with ad hoc `query_graph`
queries. The installed query evaluator accepts the documented Cypher subset
and does not support the full Cypher function surface (for example, `type(r)`).

When the installed code-graph component exposes `get_relationship_evidence`,
use it after resolving an exact qualified symbol whenever the answer depends on
an edge rather than only a source location. Preserve its `relationship_ref`,
resolver source, confidence tier/band, and runtime-observation fields. A
runtime-confirmed edge corroborates a static relationship; it does not erase
coverage gaps elsewhere in the graph.

## PROVE

Use when the user asks for a security/compliance assertion, exhaustive
relationship, or a claim where missing evidence matters: trust boundaries,
source-to-sink paths, policy/control evidence, or whether all relevant paths
satisfy a property.

A PROVE answer is a deterministic proof workflow, not ordinary retrieval:

1. Pass the coherence gate below before gathering mixed evidence.
2. Create one canonical claim record with a stable `claim:v1` identity.
3. Gather supporting observations. Preserve backend-emitted `observation_ref`
   objects without editing their IDs or nested evidence. For relationship
   claims, prefer canonical relationship observations over prose assembled from
   separate source and target results.
4. Run an explicit contradiction search designed to falsify the claim. Record
   the strategy and the number of counterexample candidates examined.
5. For exhaustive assertions, enumerate the complete subject set. Classify each
   subject as `pass`, `fail`, or `unresolved`, then run:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/invariant_evaluator.py" \
     <invariant-bundle.json> --output <invariant-result.json>
   ```

6. Assemble the claim, coherent index state, observations, contradiction-search
   record, coverage, and optional invariant result into a proof bundle matching
   `compatibility/proof-schema-v1.json`.
7. Run:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/proof_evaluator.py" \
     <proof-bundle.json> --output <proof-result.json>
   ```

8. Use the word **verified** only when the evaluator returns
   `verdict="verified"`. Preserve the exact evaluator verdict otherwise:
   `contradicted`, `unresolved`, or `blocked`.
9. When the proof must leave the session, export and verify a portable packet:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/export_proof.py" export \
     <proof-bundle.json> --output-dir <proof-packet-directory>
   python "${CLAUDE_PLUGIN_ROOT}/scripts/export_proof.py" verify \
     <proof-packet-directory>
   ```

   The packet binds the canonical bundle, deterministic evaluator result, and
   concise Markdown report with SHA-256. Verification rejects tampering and
   evaluator-result drift.

### Contradiction rules

A contradiction pass is mandatory for verification. Search for plausible
bypasses, alternate implementations, direct sink calls, dynamically registered
surfaces, and relationships excluded by the primary query. A result with no
counterexample is not verified when coverage is partial, an index is stale,
subjects are unresolved, or the contradiction pass was skipped.

For relationship claims, the contradiction pass must not inspect only the
selected edge type or resolver tier. Search alternate direct edges, unresolved
call sites, ambiguous/fuzzy relationships, runtime-only service paths, and
subjects omitted by the primary direction/filter.

### Confidence rules

Do not infer confidence from a semantic score alone. The proof evaluator derives
confidence from index coherence, completeness, contradiction outcomes,
independent engines or derivations, resolver provenance, and runtime
corroboration. Runtime-confirmed static relationships may provide independent
corroboration, but low/speculative-only support cannot verify a claim. Report
the evaluator rationale rather than replacing it with a subjective estimate.

## Coherence gate

Before mixed or chained retrieval, read both engines' status envelopes. Require
matching ready v1 `repository_id`, `checkout_id`, `source_revision`,
`dirty_fingerprint`, and `index_generation`. If they differ, do not combine
results. A single-engine answer is allowed only when that engine alone can
answer the question; label it as not cross-engine coherent. A PROVE workflow
using mixed evidence must return `blocked` when this gate fails.

## Evidence contract

When a backend returns canonical `symbol_ref`, `relationship_ref`,
`evidence_ref`, or `observation_ref` fields, preserve them through chaining
rather than re-resolving by short symbol name. References are content-addressed;
changing a field without recomputing the canonical ID invalidates the proof
bundle. Relationship references also bind source and target symbols, edge type,
resolver source, confidence, runtime confirmation, observation count, and index
generation.

## Output

Give the verdict or direct answer first, then the minimum supporting evidence.
When evaluating a supplied candidate assertion, reproduce it byte-for-byte,
including terminal punctuation, or mark it unsupported; do not silently rewrite
the claim identity.
Use `not_supported` only when cited code directly contradicts at least one
atomic clause. If direct evidence supports every atomic clause, every named
endpoint is resolved, and no cited code contradicts the candidate, the
disposition must be supported. Implementation-quality, naming, persistence, or
style caveats do not refute a literal claim unless the claim requires that
property.
For PROVE, include:

- proof ID and verdict;
- confidence band and rationale;
- supporting and contradicting evidence locations;
- relationship resolver sources and runtime-confirmed count when present;
- coverage counts and unresolved subjects;
- contradiction-search strategy;
- material freshness, fallback, or index-coherence caveats.

Avoid listing every tool call unless the user asks for the trace.
