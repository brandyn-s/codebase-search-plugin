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
- Evidence-backed conceptual search: prefer the additive
  `search_code_evidence` tool when the installed code-search component exposes
  it. It uses the same retrieval pipeline and emits generation-bound
  `evidence_ref`, `symbol_ref`, and `observation_ref` objects only when the
  semantic index identity is ready. Until the tested component BOM includes
  that tool, use the ordinary search tool and do not manufacture references.
- Issue-to-file localization: `mcp__code-search__code_localize`.

Return the smallest sufficient set of file/line evidence.

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

## PROVE

Use when the user asks for a security/compliance assertion, exhaustive
relationship, or a claim where missing evidence matters: trust boundaries,
source-to-sink paths, policy/control evidence, or whether all relevant paths
satisfy a property.

A PROVE answer is a deterministic proof workflow, not ordinary retrieval:

1. Pass the coherence gate below before gathering mixed evidence.
2. Create one canonical claim record with a stable `claim:v1` identity.
3. Gather supporting observations. Preserve backend-emitted `observation_ref`
   objects without editing their IDs or nested evidence.
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

### Contradiction rules

A contradiction pass is mandatory for verification. Search for plausible
bypasses, alternate implementations, direct sink calls, dynamically registered
surfaces, and relationships excluded by the primary query. A result with no
counterexample is not verified when coverage is partial, an index is stale,
subjects are unresolved, or the contradiction pass was skipped.

### Confidence rules

Do not infer confidence from a semantic score alone. The proof evaluator derives
confidence from index coherence, completeness, contradiction outcomes, and
independent engines or derivations. Report its rationale rather than replacing
it with a subjective estimate.

## Coherence gate

Before mixed or chained retrieval, read both engines' status envelopes. Require
matching ready v1 `repository_id`, `checkout_id`, `source_revision`,
`dirty_fingerprint`, and `index_generation`. If they differ, do not combine
results. A single-engine answer is allowed only when that engine alone can
answer the question; label it as not cross-engine coherent. A PROVE workflow
using mixed evidence must return `blocked` when this gate fails.

## Evidence contract

When a backend returns canonical `symbol_ref`, `evidence_ref`, or
`observation_ref` fields, preserve them through chaining rather than
re-resolving by short symbol name. References are content-addressed; changing a
field without recomputing the canonical ID invalidates the proof bundle.

## Output

Give the verdict or direct answer first, then the minimum supporting evidence.
For PROVE, include:

- proof ID and verdict;
- confidence band and rationale;
- supporting and contradicting evidence locations;
- coverage counts and unresolved subjects;
- contradiction-search strategy;
- material freshness, fallback, or index-coherence caveats.

Avoid listing every tool call unless the user asks for the trace.
