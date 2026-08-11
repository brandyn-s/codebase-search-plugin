#!/usr/bin/env python3
"""Report the highest independently verified code-intelligence deployment stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import subprocess
import sys


PLUGIN_ID = "codebase-search@redacted-code-intelligence"


class VerificationError(RuntimeError):
    """The deployment evidence could not be evaluated soundly."""


def _run(claude: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [str(claude), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationError(
            f"claude {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def _json_output(claude: Path, *arguments: str) -> list[dict]:
    try:
        value = json.loads(_run(claude, *arguments))
    except json.JSONDecodeError as exc:
        raise VerificationError(
            f"claude {' '.join(arguments)} returned invalid JSON"
        ) from exc
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise VerificationError(f"claude {' '.join(arguments)} returned no object list")
    return value


def _durable_runtime_connected(
    plugin: dict, marketplace: dict, mcp_output: str, expected_version: str
) -> bool:
    marketplace_root = marketplace.get("installLocation")
    install_root = plugin.get("installPath")
    expected_install_suffix = (
        "/.claude/plugins/cache/redacted-code-intelligence/"
        f"codebase-search/{expected_version}"
    )
    if (
        plugin.get("version") != expected_version
        or plugin.get("scope") != "user"
        or plugin.get("enabled") is not True
        or marketplace.get("source") != "github"
        or not isinstance(marketplace_root, str)
        or "/.claude/plugins/marketplaces/" not in marketplace_root
        or "/worktrees/" in marketplace_root
        or not isinstance(install_root, str)
        or not install_root.endswith(expected_install_suffix)
        or "/worktrees/" in install_root
    ):
        return False
    expected = {
        "code-search": f"{install_root.rstrip('/')}/bin/run-code-search",
        "code-graph": f"{install_root.rstrip('/')}/bin/codebase-memory-mcp",
    }
    for component, command in expected.items():
        matching = [
            line
            for line in mcp_output.splitlines()
            if line.startswith(f"plugin:codebase-search:{component}:")
        ]
        if (
            len(matching) != 1
            or command not in matching[0].replace("//bin/", "/bin/")
            or "Connected" not in matching[0]
            or "/worktrees/" in matching[0]
        ):
            return False
    return True


def _load_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot load {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{label} is not a JSON object")
    return value


def _verify_artifact_manifest(root: Path) -> dict[str, str]:
    manifest = _load_json_object(root / "manifest.json", "runtime manifest")
    artifacts = manifest.get("artifacts")
    if manifest.get("schema_version") != 1 or not isinstance(artifacts, dict):
        raise VerificationError("runtime manifest has an unsupported shape")
    expected_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if expected_files != set(artifacts):
        raise VerificationError("runtime manifest does not cover every artifact")
    for relative, expected_sha256 in artifacts.items():
        parsed = PurePosixPath(relative)
        if (
            parsed.is_absolute()
            or ".." in parsed.parts
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
        ):
            raise VerificationError("runtime manifest contains an unsafe artifact")
        path = root.joinpath(*parsed.parts)
        if path.is_symlink() or not path.is_file():
            raise VerificationError("runtime manifest artifact is unavailable")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_sha256:
            raise VerificationError(f"runtime artifact hash mismatch: {relative}")
    return artifacts


def _runtime_trace_uses_both_families(raw_path: Path) -> bool:
    events: list[dict] = []
    try:
        for line in raw_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    return False
                events.append(value)
    except (OSError, json.JSONDecodeError):
        return False
    init = next(
        (
            event
            for event in events
            if event.get("type") == "system" and event.get("subtype") == "init"
        ),
        None,
    )
    if not isinstance(init, dict):
        return False
    plugins = init.get("plugins")
    if not isinstance(plugins, list):
        return False
    plugin_ids = {
        item if isinstance(item, str) else item.get("id", item.get("name"))
        for item in plugins
        if isinstance(item, (str, dict))
    }
    if PLUGIN_ID not in plugin_ids and "codebase-search" not in plugin_ids:
        return False
    servers = init.get("mcp_servers")
    if not isinstance(servers, list):
        return False
    connected_names = {
        item.get("name")
        for item in servers
        if isinstance(item, dict) and item.get("status") == "connected"
    }
    if not any("code-search" in str(name) for name in connected_names) or not any(
        "code-graph" in str(name) for name in connected_names
    ):
        return False
    calls: list[dict] = []
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls.append(block)
    semantic = any(
        "code-search" in str(call.get("name"))
        and str(call.get("name", "")).endswith(
            ("search_code", "search_code_evidence", "code_localize")
        )
        and call.get("input", {}).get("search_mode") != "keyword"
        for call in calls
    )
    relationship = any(
        "code-graph" in str(call.get("name"))
        and str(call.get("name", "")).endswith(
            (
                "trace_call_path",
                "explain_symbol",
                "get_relationship_evidence",
                "query_graph",
            )
        )
        for call in calls
    )
    terminal = next((event for event in reversed(events) if event.get("type") == "result"), None)
    return semantic and relationship and isinstance(terminal, dict) and terminal.get("is_error") is False


def _valid_runtime_receipt_manifest(
    evidence_root: Path, *, expected_version: str, marketplace_root: str
) -> str | None:
    candidates = sorted(evidence_root.glob("installed-runtime-smoke-*"))
    for root in candidates:
        if root.is_symlink() or not root.is_dir():
            raise VerificationError("runtime receipt root is not a real directory")
        artifacts = _verify_artifact_manifest(root)
        if "receipt.json" not in artifacts:
            raise VerificationError("runtime manifest omits receipt.json")
        receipt = _load_json_object(root / "receipt.json", "runtime receipt")
        raw_relative = receipt.get("raw_stream")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("receipt_type") != "installed-plugin-runtime"
            or receipt.get("plugin_id") != PLUGIN_ID
            or receipt.get("plugin_version") != expected_version
            or receipt.get("marketplace_root") != marketplace_root
            or receipt.get("checkout_unchanged") is not True
            or receipt.get("canary_violations") != 0
            or receipt.get("denied_tool_calls") != 0
            or not isinstance(raw_relative, str)
            or raw_relative not in artifacts
        ):
            raise VerificationError("runtime receipt does not match the live deployment")
        if not _runtime_trace_uses_both_families(root / raw_relative):
            raise VerificationError("runtime trace does not prove both MCP families")
        return hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    return None


def _jsonl_objects(path: Path, label: str) -> list[dict]:
    values: list[dict] = []
    try:
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise VerificationError(f"{label} line {line_number} is not an object")
            values.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot load {label}: {exc}") from exc
    return values


EXPECTED_OUTCOME_GATES = {
    "evidence_precision": (">=", 0.9),
    "evidence_recall": (">=", 0.9),
    "adjudication_accuracy": (">=", 1.0),
    "unsupported_asserted_claim_rate": ("<=", 0.0),
    "routing_contract_accuracy": (">=", 1.0),
    "errors": ("<=", 0),
    "canary_violations": ("<=", 0),
}


def _passing_holdout(
    root: Path,
    *,
    artifacts: dict[str, str],
    runtime_manifest_sha256: str,
) -> bool:
    required = {
        "cases.jsonl",
        "records.jsonl",
        "summary.json",
        "outcome-gates.json",
        "component-bom.json",
        "pilot-runner.py",
        "readiness-evidence.json",
        "target-manifest.json",
        "consumption.json",
    }
    if not required.issubset(artifacts):
        return False
    preregistrations = [
        relative
        for relative in artifacts
        if PurePosixPath(relative).name.startswith("preregistration")
        and relative.endswith(".json")
    ]
    if len(preregistrations) != 1:
        raise VerificationError("holdout manifest must cover one preregistration")
    outcome = _load_json_object(root / "outcome-gates.json", "holdout outcome")
    if outcome.get("status") != "pass":
        return False
    gates = outcome.get("gates")
    if not isinstance(gates, dict) or set(gates) != set(EXPECTED_OUTCOME_GATES):
        raise VerificationError("passing holdout has a changed gate set")
    for name, (operator, threshold) in EXPECTED_OUTCOME_GATES.items():
        gate = gates.get(name)
        observed = gate.get("observed") if isinstance(gate, dict) else None
        threshold_met = (
            isinstance(observed, (int, float))
            and not isinstance(observed, bool)
            and (
                (operator == ">=" and observed >= threshold)
                or (operator == "<=" and observed <= threshold)
            )
        )
        if (
            not isinstance(gate, dict)
            or gate.get("operator") != operator
            or gate.get("threshold") != threshold
            or not threshold_met
            or gate.get("passed") is not True
        ):
            raise VerificationError(f"passing holdout weakened or failed {name}")

    preregistration = _load_json_object(
        root / preregistrations[0], "holdout preregistration"
    )
    controls = preregistration.get("controls")
    bindings = preregistration.get("bindings")
    preregistered_gates = preregistration.get("outcome_gates")
    expected_preregistered_gates = {
        "arm": "composed",
        "min_evidence_precision": 0.9,
        "min_evidence_recall": 0.9,
        "min_adjudication_accuracy": 1.0,
        "max_unsupported_asserted_claim_rate": 0.0,
        "min_routing_contract_accuracy": 1.0,
        "max_errors": 0,
        "max_canary_violations": 0,
    }
    if (
        preregistration.get("run_type")
        != "bounded_operator_authorized_fresh_holdout_confirmation"
        or not isinstance(controls, dict)
        or not isinstance(bindings, dict)
        or preregistered_gates != expected_preregistered_gates
        or controls.get("arms") != ["composed"]
        or controls.get("repetitions") != 2
        or controls.get("model") != "sonnet"
        or controls.get("fallback_model") is not None
        or controls.get("max_turns") != 8
        or controls.get("timeout_seconds") != 180.0
        or controls.get("max_budget_usd_per_case") != 1.0
    ):
        raise VerificationError("passing holdout execution controls drifted")
    bound_artifacts = {
        "cases_sha256": "cases.jsonl",
        "target_manifest_sha256": "target-manifest.json",
        "pilot_runner_sha256": "pilot-runner.py",
        "component_bom_sha256": "component-bom.json",
        "readiness_evidence_sha256": "readiness-evidence.json",
    }
    for field, relative in bound_artifacts.items():
        if bindings.get(field) != artifacts.get(relative):
            raise VerificationError(f"passing holdout binding differs: {field}")
    if bindings.get("runtime_receipt_manifest_sha256") != runtime_manifest_sha256:
        raise VerificationError("passing holdout used another installed runtime")

    consumption = _load_json_object(root / "consumption.json", "consumption receipt")
    if (
        consumption.get("schema_version") != 1
        or consumption.get("state") != "consumed"
        or consumption.get("bank_id") != bindings.get("bank_id")
        or consumption.get("corpus_pack_sha256")
        != bindings.get("corpus_pack_sha256")
    ):
        raise VerificationError("passing holdout consumption binding is invalid")
    cases = _jsonl_objects(root / "cases.jsonl", "holdout cases")
    case_ids = [case.get("case_id") for case in cases]
    routes = {case.get("expected_route") for case in cases}
    if (
        len(cases) != 5
        or len(set(case_ids)) != 5
        or case_ids != controls.get("case_ids")
        or routes != {"semantic", "lexical", "graph", "mixed", "security"}
        or not any(isinstance(case.get("routing_contract"), dict) for case in cases)
    ):
        raise VerificationError("passing holdout corpus is vacuous or incomplete")
    records = _jsonl_objects(root / "records.jsonl", "holdout records")
    expected_units = {
        (case_id, repetition) for case_id in case_ids for repetition in (1, 2)
    }
    observed_units = {
        (record.get("case_id"), record.get("repetition")) for record in records
    }
    if (
        len(records) != 10
        or observed_units != expected_units
        or any(
            record.get("arm") != "composed" or record.get("status") != "success"
            for record in records
        )
    ):
        raise VerificationError("passing holdout does not cover every intended unit")
    raw = [
        relative
        for relative in artifacts
        if relative.startswith("raw/composed/") and relative.endswith(".jsonl")
    ]
    if len(raw) != 10:
        raise VerificationError("passing holdout lacks ten raw fresh-session traces")
    summary = _load_json_object(root / "summary.json", "holdout summary")
    composed = summary.get("arms", {}).get("composed")
    if (
        summary.get("outcome_gate_status") != "pass"
        or not isinstance(composed, dict)
        or composed.get("case_count") != 10
        or composed.get("unique_case_count") != 5
        or composed.get("repetitions") != 2
        or not isinstance(composed.get("routing_contract_cases"), int)
        or composed["routing_contract_cases"] <= 0
    ):
        raise VerificationError("passing holdout summary is incomplete")
    return True


def _has_fresh_passing_holdout(
    evidence_root: Path, *, runtime_manifest_sha256: str
) -> bool:
    seen_banks: set[tuple[str, str]] = set()
    passed = False
    for root in sorted(evidence_root.glob("pilot-*")):
        if root.is_symlink() or not root.is_dir():
            raise VerificationError("holdout root is not a real directory")
        if not (root / "consumption.json").is_file():
            continue
        artifacts = _verify_artifact_manifest(root)
        consumption = _load_json_object(root / "consumption.json", "consumption receipt")
        identity = (
            consumption.get("bank_id"),
            consumption.get("corpus_pack_sha256"),
        )
        if not all(isinstance(value, str) and value for value in identity):
            raise VerificationError("consumed holdout has no opaque corpus identity")
        if identity in seen_banks:
            raise VerificationError("a holdout bank was consumed more than once")
        seen_banks.add(identity)
        if _passing_holdout(
            root,
            artifacts=artifacts,
            runtime_manifest_sha256=runtime_manifest_sha256,
        ):
            passed = True
    return passed


def deployment_stage(repo: Path, evidence_root: Path, claude: Path) -> int:
    plugin_path = repo / ".claude-plugin" / "plugin.json"
    try:
        plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot load plugin metadata: {exc}") from exc
    if plugin.get("name") != "codebase-search" or not plugin.get("version"):
        raise VerificationError("plugin metadata does not identify a versioned codebase-search")
    if not evidence_root.is_dir():
        raise VerificationError(f"evidence root is unavailable: {evidence_root}")
    if not claude.is_file() or not os.access(claude, os.X_OK):
        raise VerificationError(f"claude executable is unavailable: {claude}")

    # Exercise the same supported CLI surfaces used by the live artifact probe.
    plugins = _json_output(claude, "plugin", "list", "--json")
    marketplaces = _json_output(
        claude, "plugin", "marketplace", "list", "--json"
    )
    mcp_output = _run(claude, "mcp", "list")
    installed = [item for item in plugins if item.get("id") == PLUGIN_ID]
    sources = [
        item
        for item in marketplaces
        if item.get("name") == "redacted-code-intelligence"
    ]
    if len(installed) != 1 or len(sources) != 1:
        return 1
    if _durable_runtime_connected(
        installed[0], sources[0], mcp_output, str(plugin["version"])
    ):
        runtime_manifest_sha256 = _valid_runtime_receipt_manifest(
            evidence_root,
            expected_version=str(plugin["version"]),
            marketplace_root=str(sources[0]["installLocation"]),
        )
        if runtime_manifest_sha256 is not None:
            if _has_fresh_passing_holdout(
                evidence_root,
                runtime_manifest_sha256=runtime_manifest_sha256,
            ):
                return 4
            return 3
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    args = parser.parse_args()
    try:
        stage = deployment_stage(
            args.repo.resolve(), args.evidence_root.resolve(), args.claude.resolve()
        )
    except VerificationError as exc:
        print(f"Deployment verification BROKEN: {exc}", file=sys.stderr)
        return 2
    print(f"METRIC CODE_INTEL_DEPLOYMENT_STAGE={stage}")
    return 0 if stage == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
