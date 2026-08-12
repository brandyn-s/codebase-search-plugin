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
product layer without expanding that harness. The current candidate adds
persistent per-project SCIP precision, a fail-closed reachability-versus-taint
contract, bounded cross-project discovery, and immutable graph-index
comparison. The direct public comparison now covers 20 balanced cases:
code-search measured 0.40 Acc@1 and 0.534 MRR@10 versus Sourcegraph at 0.20
and 0.225, but the paired test did not establish superiority (p=0.125).
Single-repository scale now reaches 2.39 million lines. The remaining grade
gap is statistically stronger public retrieval evidence, broader relationship
oracles, very-large-monorepo and multi-repository operation, and resource
efficiency—not more canaries or proof-harness layers. See
[`CAPABILITY_STATE.md`](CAPABILITY_STATE.md).

The next development slice stays inside that boundary. It adds an explicit
capability lattice, canonical CodeQL-path ingestion, and an opt-in pinned Go
SCIP preparation path. Compiler assurance is edge-scoped and requires the
exact ingested artifact digest; graph-wide tier labels or legacy SCIP
provenance are insufficient. These are consolidation changes, not another
holdout layer or an organization-wide indexing service.
