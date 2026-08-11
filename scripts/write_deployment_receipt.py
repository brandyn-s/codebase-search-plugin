#!/usr/bin/env python3
"""Write the single explicit runtime/holdout contract used by deployment verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import sys
from typing import Any


class ReceiptError(ValueError):
    """The requested deployment receipt is unsafe or invalid."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _binding(evidence_root: Path, manifest: Path, *, label: str) -> dict[str, str]:
    if manifest.name != "manifest.json" or manifest.is_symlink() or not manifest.is_file():
        raise ReceiptError(f"{label} must be a real manifest.json file")
    try:
        relative = manifest.relative_to(evidence_root)
    except ValueError as exc:
        raise ReceiptError(f"{label} must be inside the evidence root") from exc
    parsed = PurePosixPath(relative.as_posix())
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ReceiptError(f"{label} path is unsafe")
    current = evidence_root
    for part in parsed.parts:
        current = current / part
        if current.is_symlink():
            raise ReceiptError(f"{label} traverses a symlink")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"{label} is invalid JSON") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("artifacts"), dict)
    ):
        raise ReceiptError(f"{label} has an unsupported artifact-manifest shape")
    return {"path": parsed.as_posix(), "sha256": _sha256(manifest)}


def write_receipt(
    *,
    evidence_root: Path,
    plugin_version: str,
    runtime_manifest: Path,
    holdout_manifest: Path | None,
) -> Path:
    reported_destination = evidence_root.absolute() / "deployment-receipt.json"
    root = evidence_root.resolve(strict=True)
    if evidence_root.is_symlink() or not root.is_dir():
        raise ReceiptError("evidence root must be a real directory")
    if re.fullmatch(r"\d+\.\d+\.\d+", plugin_version) is None:
        raise ReceiptError("plugin version must be semantic x.y.z")
    runtime = _binding(root, runtime_manifest.resolve(strict=True), label="runtime manifest")
    holdout = (
        _binding(root, holdout_manifest.resolve(strict=True), label="holdout manifest")
        if holdout_manifest is not None
        else None
    )
    value: dict[str, Any] = {
        "schema_version": 1,
        "receipt_type": "code-intelligence-deployment",
        "plugin_version": plugin_version,
        "runtime_manifest": runtime,
        "holdout_manifest": holdout,
    }
    destination = root / "deployment-receipt.json"
    temporary = root / f".deployment-receipt.{os.getpid()}.tmp"
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, destination)
    return reported_destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--plugin-version", required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--holdout-manifest", type=Path)
    args = parser.parse_args()
    try:
        receipt = write_receipt(
            evidence_root=args.evidence_root,
            plugin_version=args.plugin_version,
            runtime_manifest=args.runtime_manifest,
            holdout_manifest=args.holdout_manifest,
        )
    except (OSError, ReceiptError) as exc:
        print(f"Deployment receipt FAILED: {exc}", file=sys.stderr)
        return 2
    print(receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
