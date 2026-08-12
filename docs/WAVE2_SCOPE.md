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
product layer without expanding that harness: query-aware hybrid ranking,
deterministic graph ordering, and route-aware composition reached 1.00 Acc@1
and MRR@10 on the unchanged directional n=4 public pilot. The remaining grade
gap is public retrieval breadth, cross-platform graph reproducibility, and
larger-repository performance—not more canaries or proof-harness layers. See
[`CAPABILITY_STATE.md`](CAPABILITY_STATE.md).
