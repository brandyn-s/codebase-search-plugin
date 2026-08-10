#!/usr/bin/env python3
"""Evaluate normalized engineering-invariant subject checks.

The evaluator is deliberately independent of the retrieval engines. Callers
enumerate the invariant's complete subject set, classify each subject as pass,
fail, or unresolved, and attach canonical observation IDs. This script reduces
those checks to the strict invariant result consumed by ``proof_evaluator.py``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ASSERTIONS = {"all_subjects_satisfy", "no_counterexamples"}
SUBJECT_STATES = {"pass", "fail", "unresolved"}


class InvariantInputError(ValueError):
    """Raised when an invariant bundle is malformed."""


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InvariantInputError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvariantInputError(f"{field} must be a non-empty string")
    return value.strip()


def evaluate(bundle: object) -> dict[str, Any]:
    value = _object(bundle, "bundle")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InvariantInputError("schema_version must equal 1")

    invariant = _object(value.get("invariant"), "invariant")
    invariant_id = _string(invariant.get("id"), "invariant.id")
    description = _string(invariant.get("description"), "invariant.description")
    assertion = _string(invariant.get("assertion"), "invariant.assertion")
    if assertion not in ASSERTIONS:
        raise InvariantInputError(f"unsupported assertion: {assertion}")

    subjects = value.get("subjects")
    if not isinstance(subjects, list):
        raise InvariantInputError("subjects must be an array")

    subject_ids: set[str] = set()
    violating: list[str] = []
    unresolved_subjects: list[str] = []
    supporting_observations: set[str] = set()
    contradicting_observations: set[str] = set()

    for position, subject_value in enumerate(subjects):
        subject = _object(subject_value, f"subjects[{position}]")
        subject_id = _string(subject.get("id"), f"subjects[{position}].id")
        if subject_id in subject_ids:
            raise InvariantInputError(f"duplicate subject id: {subject_id}")
        subject_ids.add(subject_id)

        status = _string(subject.get("status"), f"subjects[{position}].status")
        if status not in SUBJECT_STATES:
            raise InvariantInputError(
                f"subjects[{position}].status must be pass, fail, or unresolved"
            )
        observation_ids = subject.get("observation_ids", [])
        if not isinstance(observation_ids, list):
            raise InvariantInputError(
                f"subjects[{position}].observation_ids must be an array"
            )
        normalized_observations: list[str] = []
        for observation_position, observation_id in enumerate(observation_ids):
            observation_id = _string(
                observation_id,
                (
                    f"subjects[{position}].observation_ids"
                    f"[{observation_position}]"
                ),
            )
            if not observation_id.startswith("obs:v1:"):
                raise InvariantInputError(
                    f"observation id must be an obs:v1 reference: {observation_id}"
                )
            normalized_observations.append(observation_id)

        if status in {"pass", "fail"} and not normalized_observations:
            raise InvariantInputError(
                f"{status} subject requires at least one observation"
            )

        if status == "pass":
            supporting_observations.update(normalized_observations)
        elif status == "fail":
            violating.append(subject_id)
            contradicting_observations.update(normalized_observations)
        else:
            unresolved_subjects.append(subject_id)

    if violating:
        status = "fail"
    elif not subjects or unresolved_subjects:
        status = "unresolved"
    else:
        status = "pass"

    core_result = {
        "id": invariant_id,
        "status": status,
        "checked": len(subjects),
        "violations": len(violating),
        "unresolved": len(unresolved_subjects),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "invariant": core_result,
        "details": {
            "description": description,
            "assertion": assertion,
            "violating_subject_ids": sorted(violating),
            "unresolved_subject_ids": sorted(unresolved_subjects),
            "supporting_observation_ids": sorted(supporting_observations),
            "contradicting_observation_ids": sorted(contradicting_observations),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = evaluate(bundle)
    except (OSError, json.JSONDecodeError, InvariantInputError) as exc:
        rendered = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": "invalid_invariant_bundle",
                    "message": str(exc),
                },
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 2

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
