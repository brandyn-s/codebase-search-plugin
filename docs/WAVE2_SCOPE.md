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
