# Executable engineering invariants

Wave 2 treats exhaustive engineering assertions as data rather than prose.
A PROVE workflow enumerates the complete subject set, preserves canonical
`observation_ref` IDs for each determination, and evaluates the normalized
bundle with `scripts/invariant_evaluator.py`.

## Subject states

- `pass`: the subject satisfies the invariant and cites supporting observations.
- `fail`: a counterexample exists and cites contradicting observations.
- `unresolved`: the subject could not be resolved completely. This prevents a
  passing invariant; unresolved is never interpreted as absence.

An empty subject set is unresolved, not vacuously true.

## Evaluate

```bash
python scripts/invariant_evaluator.py \
  invariants/examples/admin-routes-require-auth.json \
  --output /tmp/admin-auth-result.json
```

Copy the resulting top-level `invariant` object into a proof bundle. Attach the
supporting and contradicting observation records themselves to that proof, not
only the IDs reported in `details`.

## Included examples

- `admin-routes-require-auth.json`: every administrative route must pass through
  an authorization boundary.
- `no-unsanitized-input-to-sql.json`: no external input path may reach an SQL
  execution sink without a sanitizer or authorization boundary.

The checked-in observation IDs are illustrative placeholders. Real runs must
preserve IDs emitted by the indexed engines and must use one coherent current
index generation.
