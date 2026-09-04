#!/usr/bin/env python3
"""Verify that pip installed the exact checksum-pinned code-search wheel."""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import PurePosixPath
import re
import sys
from urllib.parse import unquote, urlparse


class WheelVerificationError(RuntimeError):
    """Installed wheel provenance does not match the component BOM."""


def verify_wheel_install(
    document: dict,
    *,
    installed_version: str,
    expected_tag: str,
    expected_asset_name: str,
    expected_sha256: str,
) -> None:
    if (
        not isinstance(expected_tag, str)
        or not expected_tag.startswith("v")
        or len(expected_tag) == 1
    ):
        raise WheelVerificationError("expected release tag must start with v")
    if installed_version != expected_tag[1:]:
        raise WheelVerificationError(
            "installed distribution version does not match the release tag"
        )
    if (
        not isinstance(expected_asset_name, str)
        or "/" in expected_asset_name
        or "\\" in expected_asset_name
        or not expected_asset_name.endswith(".whl")
    ):
        raise WheelVerificationError("expected wheel asset name is invalid")
    if not isinstance(expected_sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", expected_sha256
    ) is None:
        raise WheelVerificationError(
            "expected wheel SHA-256 must be 64 lowercase hex characters"
        )
    if not isinstance(document, dict):
        raise WheelVerificationError("direct_url.json is not an object")

    raw_url = document.get("url")
    if not isinstance(raw_url, str):
        raise WheelVerificationError("installation provenance has no archive URL")
    parsed_url = urlparse(raw_url)
    if parsed_url.scheme != "file":
        raise WheelVerificationError("installed wheel was not loaded from a local file")
    installed_name = PurePosixPath(
        unquote(parsed_url.path).replace("\\", "/")
    ).name
    if installed_name != expected_asset_name:
        raise WheelVerificationError(
            "installed wheel asset name does not match the component BOM"
        )

    archive_info = document.get("archive_info")
    if not isinstance(archive_info, dict):
        raise WheelVerificationError(
            "installation provenance has no archive information"
        )
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, dict) or hashes.get("sha256") != expected_sha256:
        raise WheelVerificationError(
            "installed wheel SHA-256 does not match the component BOM"
        )
    legacy_hash = archive_info.get("hash")
    if legacy_hash is not None and legacy_hash != f"sha256={expected_sha256}":
        raise WheelVerificationError(
            "installed wheel provenance contains conflicting SHA-256 values"
        )


def installed_wheel_provenance(distribution_name: str) -> tuple[str, dict]:
    try:
        distribution = metadata.distribution(distribution_name)
    except metadata.PackageNotFoundError as exc:
        raise WheelVerificationError(
            f"installed distribution not found: {distribution_name}"
        ) from exc
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        raise WheelVerificationError(
            f"{distribution_name} has no PEP 610 direct_url.json provenance"
        )
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WheelVerificationError(
            f"{distribution_name} direct_url.json is malformed"
        ) from exc
    return distribution.version, document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify installed code-search wheel provenance"
    )
    parser.add_argument("expected_tag")
    parser.add_argument("--asset-name", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument(
        "--distribution",
        default="code-search-mcp",
        help="installed Python distribution name",
    )
    args = parser.parse_args()
    try:
        installed_version, document = installed_wheel_provenance(
            args.distribution
        )
        verify_wheel_install(
            document,
            installed_version=installed_version,
            expected_tag=args.expected_tag,
            expected_asset_name=args.asset_name,
            expected_sha256=args.sha256,
        )
    except WheelVerificationError as exc:
        print(f"Code-search wheel verification FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "Code-search wheel verification passed "
        f"({args.expected_tag}, {args.sha256})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
