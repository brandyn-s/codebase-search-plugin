# Holdout Budget Calibration

Calibration date: 2026-08-11

This calibration sets an operational ceiling for one fresh empirical release
holdout. It does not change the evidence, adjudication, routing, error, or
canary gates.

## Measurements

The sealed historical pilot set contained 50 terminal units across eight
exactly-once runs:

- 45 successful terminals: median cost $0.7527, p95 $0.9453, maximum $0.9747.
- 5 budget terminals: minimum cost $1.0057, maximum $1.1544.
- Maximum observed duration was 128.448 seconds.
- Across 15 same-case repetition pairs, the median cost ratio was 1.277, the
  p90 ratio was 1.356, and the largest absolute spread was $0.278.

Three disposable public-fixture probes exercised the installed 0.4.16 runtime
without consuming a private bank:

| Route | Ceiling | Result | Cost | Reported turns | Duration |
| --- | ---: | --- | ---: | ---: | ---: |
| mixed | $1.50 | success | $1.2994 | 8 | 85.111 s |
| security | $1.50 | success | $0.9556 | 7 | 83.137 s |
| mixed repetition | $2.00 | success | $1.0870 | 7 | 68.380 s |

These probes measure execution cost and termination behavior only. They are not
holdout accuracy evidence and are not included in deployment scoring.

## Policy

- Per-case hard ceiling: **$2.50**. This is an operator-authorized ceiling, not
  an expected spend.
- Agentic tool round trips: **8**, unchanged.
- Per-case timeout: **180 seconds**, unchanged.
- Repetitions: **2**, unchanged.
- Correctness and safety gates: unchanged.
- Empirical release validation: one fresh five-route holdout, executed exactly
  once. Broad deterministic checks remain in CI.

The higher dollar ceiling prevents a valid final tool round trip from being
cut off, while the independent turn and time limits continue to bound work.
Historical preregistrations remain immutable at their original ceilings.
