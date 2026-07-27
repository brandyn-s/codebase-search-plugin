#!/usr/bin/env python3
"""Generate or verify the seven-case offline five-arm instrument fixtures."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _reject_package_initializer() -> None:
    initializer = Path(__file__).resolve().parent / "__init__.py"
    if initializer.exists() or initializer.is_symlink():
        print(
            "ERROR: bench/compare must remain a namespace package; "
            "refusing executable package initializer",
            file=sys.stderr,
        )
        raise SystemExit(1)


_reject_package_initializer()

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.compare.provenance import atomic_write_json  # noqa: E402
from bench.compare.run import load_case_pin  # noqa: E402
from bench.compare.schema import (  # noqa: E402
    canonical_json,
    component_identity,
    component_identity_sha256,
)


class FixtureBuildError(ValueError):
    """The checked-in fixture differs from its deterministic construction."""


def build_fixture(pin_path: Path, *, bad: bool, root: Path) -> dict:
    _pin, cases, cases_sha256, _source_pin_bytes = load_case_pin(
        pin_path,
        allow_instrument_fixture=True,
        root=root,
    )
    if len(cases) != 7:
        raise FixtureBuildError("instrument fixture must contain exactly seven cases")
    faults = (
        [
            {
                "unit_key": f"{cases[0]['case_id']}|r1|code-graph",
                "error_class": "timeout",
            }
        ]
        if bad
        else []
    )
    return {
        "schema_version": 1,
        "kind": "deterministic_executor_fault_plan_v1",
        "cases_sha256": cases_sha256,
        "component_identity_sha256": component_identity_sha256(
            component_identity(root)
        ),
        "faults": faults,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--good", type=Path, required=True)
    parser.add_argument("--bad", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    try:
        expected = {
            arguments.good: build_fixture(arguments.pin, bad=False, root=root),
            arguments.bad: build_fixture(arguments.pin, bad=True, root=root),
        }
        if arguments.check:
            for path, value in expected.items():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise FixtureBuildError(f"cannot read {path}: {exc}") from exc
                if canonical_json(current) != canonical_json(value):
                    raise FixtureBuildError(
                        f"{path}: checked-in fixture differs from deterministic build"
                    )
            print('{"status":"verified","cases":7,"units":35}')
            return 0
        for path, value in expected.items():
            atomic_write_json(path, value)
        print('{"status":"written","cases":7,"units":35}')
        return 0
    except FixtureBuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
