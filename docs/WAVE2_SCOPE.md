# Wave 2 implementation scope

This stacked change set builds one complete `PROVE` vertical slice on top of Wave 1:

1. both engines emit canonical generation-bound evidence references;
2. orchestration composes evidence into claims using deterministic confidence rules;
3. a contradiction pass is mandatory before a security or exhaustive claim can be marked verified;
4. executable invariant specs and fixture tests pin pass, fail, unresolved, and incoherent-index behavior.

This was the original Wave 2 slice boundary. Wave 3 subsequently added
generation-bound relationship evidence with resolver provenance and optional
runtime corroboration. Those signals strengthen a static relationship; they
do not waive coherence, coverage, contradiction, or unresolved-subject rules.

Wave 4 consolidated the verification boundary rather than adding another
backend. Evidence-capable tools now issue immutable typed evidence IDs at exact
source coordinates; a single host-owned state machine enforces route, trace,
evidence, and terminal completion; and schema-v2 manifests bind canonical cases
and artifact roles explicitly. The latest five-route/two-repetition Stage-4
successor passed all fixed gates. Plugin 0.4.21 subsequently improved the
product layer without expanding that harness. Plugin 0.4.30 consolidates
persistent per-project SCIP precision, a fail-closed reachability-versus-taint
contract, bounded cross-project discovery, immutable graph-index comparison,
automatic pinned Go and TypeScript SCIP preparation, independent Go SSA/RTA and
TypeScript compiler-tier CALLS and IMPORTS oracles, a measured lower-memory LLVM
localization path, an independent TypeScript declared-relationship oracle,
code-aware source-role ranking, copy-on-write search publication on supported
filesystems, and bounded lifecycle/resource measurement. Direct
three-repository measurement now exercises the released cross-project
interfaces and records both successful
search localization and the graph's weaker file-localization boundary. The
public comparison is frozen at 80 balanced cases and the
very-large-repository runner records storage, memory, cold indexing, and
warm-query cost. These replace the former n=20 and 2.39-million-line ceilings
as the current evidence once their checked-in results are named in
[`CAPABILITY_STATE.md`](CAPABILITY_STATE.md).

The remaining grade gap is broader relationship-oracle coverage beyond the
measured Go/TypeScript cells, additional callable public comparators,
graph-only conceptual ranking, fleet/ACL operations when demanded, and further
indexing/resource efficiency—not more canaries or proof-harness layers. See
[`CAPABILITY_STATE.md`](CAPABILITY_STATE.md).

The evidence lattice, canonical CodeQL-path ingestion, and pinned SCIP
preparation paths stay inside that boundary. Compiler assurance is edge-scoped
and requires the exact ingested artifact digest; graph-wide tier labels or
legacy SCIP provenance are insufficient. These are consolidation changes, not
another holdout layer or an organization-wide indexing service.
