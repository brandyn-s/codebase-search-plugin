#!/usr/bin/env python3
"""Decide whether trusted post-merge component validation can run.

Trusted validation installs the exact component releases pinned in
``component-bom.json``. It runs only when every pinned release lives in a
publicly reachable ``brandyn-s/`` repository; no credential is ever used to
reach a pre-promotion release. Until the pins move, the gate reports
``run=false`` and the workflow exits successfully with a notice.

Usage:
    python3 scripts/promotion_gate.py --component-bom component-bom.json
Prints one JSON object: {"run": bool, "pinned": {component: repository}}.
When ``GITHUB_OUTPUT`` is set, also appends ``run=<true|false>`` to it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PUBLIC_OWNER_PREFIX = "brandyn-s/"


def evaluate(bom: dict) -> dict:
    components = bom.get("components")
    if not isinstance(components, dict) or not components:
        raise ValueError("component-bom.json has no components")
    pinned: dict[str, str] = {}
    for name in sorted(components):
        install = components[name].get("install") if isinstance(components[name], dict) else None
        repository = install.get("repository") if isinstance(install, dict) else None
        if not isinstance(repository, str) or not repository:
            raise ValueError(f"component {name!r} has no install.repository")
        pinned[name] = repository
    run = all(repo.startswith(PUBLIC_OWNER_PREFIX) for repo in pinned.values())
    return {"run": run, "pinned": pinned}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component-bom", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        bom = json.loads(args.component_bom.read_text(encoding="utf-8"))
        result = evaluate(bom)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"::error::promotion gate cannot read the BOM: {exc}", file=sys.stderr)
        return 1
    if not result["run"]:
        for name, repository in result["pinned"].items():
            if not repository.startswith(PUBLIC_OWNER_PREFIX):
                print(
                    f"::notice::component-bom.json still pins the pre-promotion "
                    f"{name} release at {repository}; trusted post-merge "
                    f"validation resumes once the pins move to "
                    f"{PUBLIC_OWNER_PREFIX}* releases.",
                    file=sys.stderr,
                )
    print(json.dumps(result, sort_keys=True))
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"run={'true' if result['run'] else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
