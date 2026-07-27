#!/usr/bin/env python3
"""Verify that pip installed code-search from the exact BOM Git commit."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import re
import sys


class RevisionError(RuntimeError):
    """Installed distribution provenance does not match the pinned revision."""


def verify_direct_url(
    document: dict,
    expected_revision: str,
    expected_repository: str,
) -> None:
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_revision) is None:
        raise RevisionError("expected revision is not a full lowercase Git object ID")
    if not isinstance(expected_repository, str) or not expected_repository:
        raise RevisionError("expected repository URL is missing")
    if not isinstance(document, dict):
        raise RevisionError("direct_url.json is not an object")
    installed_repository = document.get("url")
    if not isinstance(installed_repository, str):
        raise RevisionError("installation provenance has no repository URL")
    if installed_repository.removeprefix("git+").rstrip("/") != (
        expected_repository.removeprefix("git+").rstrip("/")
    ):
        raise RevisionError("installed Git repository does not match the BOM")
    vcs_info = document.get("vcs_info")
    if not isinstance(vcs_info, dict) or vcs_info.get("vcs") != "git":
        raise RevisionError("installation provenance is not Git")
    if vcs_info.get("requested_revision") != expected_revision:
        raise RevisionError("requested Git revision does not match the BOM")
    if vcs_info.get("commit_id") != expected_revision:
        raise RevisionError("resolved Git commit does not match the BOM")


def installed_direct_url(distribution_name: str) -> dict:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise RevisionError(
            f"installed distribution not found: {distribution_name}"
        ) from exc
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        raise RevisionError(
            f"{distribution_name} has no PEP 610 direct_url.json provenance"
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RevisionError(
            f"{distribution_name} direct_url.json is malformed"
        ) from exc
    if not isinstance(document, dict):
        raise RevisionError(
            f"{distribution_name} direct_url.json is not an object"
        )
    return document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify installed code-search Git provenance"
    )
    parser.add_argument("expected_revision")
    parser.add_argument(
        "--repository",
        required=True,
        help="Git repository URL declared by the component BOM",
    )
    parser.add_argument(
        "--distribution",
        default="redacted-code-search",
        help="installed Python distribution name",
    )
    args = parser.parse_args()
    try:
        verify_direct_url(
            installed_direct_url(args.distribution),
            args.expected_revision,
            args.repository,
        )
    except RevisionError as exc:
        print(f"Code-search revision verification FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "Code-search revision verification passed "
        f"({args.expected_revision})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
