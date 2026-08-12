#!/usr/bin/env python3
"""Export and independently verify a portable code-intelligence proof packet."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import sys
from typing import Any

from proof_evaluator import ProofInputError, evaluate, validate_bundle


SCHEMA_VERSION = 1
ARTIFACT_MEDIA_TYPES = {
    "proof-bundle.json": "application/json",
    "proof-result.json": "application/json",
    "proof.md": "text/markdown",
}


class ProofExportError(ValueError):
    """Raised when a proof packet cannot be exported or verified."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _package_id(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "proof-package:v1:" + _sha256(encoded)


def _one_line(value: object) -> str:
    return html.escape(" ".join(str(value).split()))


def _inline_values(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def _markdown(bundle: dict[str, Any], result: dict[str, Any]) -> str:
    confidence = result["confidence"]
    coverage = result["coverage"]
    contradiction = result["contradiction_search"]
    lines = [
        "# Code intelligence proof",
        "",
        f"- Verdict: `{result['verdict']}`",
        f"- Proof ID: `{result['proof_id']}`",
        f"- Claim ID: `{result['claim_id']}`",
        f"- Index generation: `{result['index_generation']}`",
        f"- Confidence: `{confidence['band']}`",
        "",
        "## Claim",
        "",
        f"> {_one_line(bundle['claim']['claim_text'])}",
        "",
        "## Evidence",
        "",
        f"- Supporting observations: {len(result['supporting_observation_ids'])}",
        f"- Contradicting observations: {len(result['contradicting_observation_ids'])}",
        f"- Relationship observations: {result['relationship_evidence']['count']}",
        (
            "- Runtime-confirmed relationships: "
            f"{result['relationship_evidence']['runtime_confirmed']}"
        ),
        "",
        "## Completeness",
        "",
        f"- Coverage: `{coverage['state']}` ({coverage['examined']}/{coverage['expected']})",
        f"- Unresolved subjects: {coverage['unresolved']}",
        f"- Contradiction search performed: `{str(contradiction['performed']).lower()}`",
        f"- Counterexample candidates examined: {contradiction['candidate_count']}",
        f"- Strategy: `{_one_line(contradiction['strategy'])}`",
        "",
        "## Evaluation notes",
        "",
    ]
    notes = [
        *(f"Blocker: {item}" for item in result["blockers"]),
        *(f"Caveat: {item}" for item in result["caveats"]),
        *(f"Confidence: {_one_line(item)}" for item in confidence["rationale"]),
    ]
    lines.extend(f"- {item}" for item in notes or ["No blockers or caveats."])
    lattice = result.get("assurance_lattice")
    if lattice and lattice["required_capabilities"]:
        lines.extend(
            [
                "",
                "## Assurance lattice",
                "",
                (
                    "- Required capabilities: `"
                    f"{_inline_values(lattice['required_capabilities'])}`"
                ),
                (
                    "- Observed supporting capabilities: `"
                    f"{_inline_values(lattice['supporting_capabilities'])}`"
                ),
                (
                    "- Missing for support: `"
                    f"{_inline_values(lattice['missing_supporting_capabilities'])}`"
                ),
                f"- Satisfied by: `{lattice['satisfied_by'] or 'none'}`",
            ]
        )
    lines.extend(
        [
            "",
            "The JSON bundle is the canonical evidence record. Verify this packet with:",
            "",
            "```bash",
            "python scripts/export_proof.py verify <proof-packet-directory>",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def export_packet(bundle_path: Path, output_dir: Path) -> dict[str, Any]:
    if output_dir.exists():
        raise ProofExportError(f"output directory already exists: {output_dir}")

    raw_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle = validate_bundle(raw_bundle)
    result = evaluate(bundle)
    rendered = {
        "proof-bundle.json": _canonical_json(bundle).encode("utf-8"),
        "proof-result.json": _canonical_json(result).encode("utf-8"),
        "proof.md": _markdown(bundle, result).encode("utf-8"),
    }
    artifacts = [
        {
            "path": name,
            "media_type": ARTIFACT_MEDIA_TYPES[name],
            "bytes": len(rendered[name]),
            "sha256": _sha256(rendered[name]),
        }
        for name in sorted(rendered)
    ]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "proof_id": result["proof_id"],
        "claim_id": result["claim_id"],
        "verdict": result["verdict"],
        "artifacts": artifacts,
    }
    manifest = {"package_id": _package_id(payload), **payload}

    output_dir.mkdir(parents=True)
    for name, contents in rendered.items():
        (output_dir / name).write_bytes(contents)
    (output_dir / "manifest.json").write_text(
        _canonical_json(manifest),
        encoding="utf-8",
    )
    return manifest


def verify_packet(packet_dir: Path) -> dict[str, Any]:
    manifest_path = packet_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ProofExportError("manifest schema_version must equal 1")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ProofExportError("manifest artifacts must be an array")
    expected_names = set(ARTIFACT_MEDIA_TYPES)
    observed_names = {item.get("path") for item in artifacts if isinstance(item, dict)}
    if observed_names != expected_names or len(artifacts) != len(expected_names):
        raise ProofExportError("manifest must contain exactly the portable proof artifacts")

    for artifact in artifacts:
        name = artifact["path"]
        if Path(name).name != name:
            raise ProofExportError(f"unsafe artifact path: {name}")
        contents = (packet_dir / name).read_bytes()
        if _sha256(contents) != artifact.get("sha256"):
            raise ProofExportError(f"sha256 mismatch for {name}")
        if len(contents) != artifact.get("bytes"):
            raise ProofExportError(f"byte length mismatch for {name}")

    package_payload = {
        key: manifest.get(key)
        for key in ("schema_version", "proof_id", "claim_id", "verdict", "artifacts")
    }
    expected_package_id = _package_id(package_payload)
    if manifest.get("package_id") != expected_package_id:
        raise ProofExportError("package_id does not match manifest contents")

    bundle = json.loads((packet_dir / "proof-bundle.json").read_text(encoding="utf-8"))
    recorded_result = json.loads(
        (packet_dir / "proof-result.json").read_text(encoding="utf-8")
    )
    evaluated_result = evaluate(bundle)
    if recorded_result != evaluated_result:
        raise ProofExportError("proof result does not match deterministic evaluation")
    for key in ("proof_id", "claim_id", "verdict"):
        if manifest.get(key) != evaluated_result[key]:
            raise ProofExportError(f"manifest {key} does not match proof result")

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "verified",
        "package_id": expected_package_id,
        "proof_id": evaluated_result["proof_id"],
        "verdict": evaluated_result["verdict"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser("export", help="export a proof packet")
    export_parser.add_argument("bundle", type=Path)
    export_parser.add_argument("--output-dir", type=Path, required=True)
    verify_parser = commands.add_parser("verify", help="verify a proof packet")
    verify_parser.add_argument("packet_dir", type=Path)
    args = parser.parse_args()

    try:
        if args.command == "export":
            output = export_packet(args.bundle, args.output_dir)
        else:
            output = verify_packet(args.packet_dir)
    except (OSError, json.JSONDecodeError, ProofInputError, ProofExportError) as exc:
        print(
            _canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "invalid",
                    "error": str(exc),
                }
            ),
            end="",
            file=sys.stderr,
        )
        return 2

    print(_canonical_json(output), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
