#!/usr/bin/env python3
"""Validate the codebase-search plugin's manifest, MCP config, and skills.

Run locally with:  python3 scripts/validate_plugin.py

Exits non-zero (printing each problem) if anything is malformed. Uses only the
standard library so it runs anywhere without extra dependencies.
"""
import json
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []
TOOL_REFERENCE = re.compile(r"mcp__code-(search|graph)__([A-Za-z0-9_]+)")
COMPONENT_FOR_PREFIX = {"search": "code-search", "graph": "code-graph"}
GRAPH_ASSET_NAMES = {
    "darwin-amd64": "codebase-memory-mcp-darwin-amd64.tar.gz",
    "darwin-arm64": "codebase-memory-mcp-darwin-arm64.tar.gz",
    "linux-amd64": "codebase-memory-mcp-linux-amd64.tar.gz",
    "linux-arm64": "codebase-memory-mcp-linux-arm64.tar.gz",
    "windows-amd64": "codebase-memory-mcp-windows-amd64.zip",
}
REQUIRED_IDENTITY_FIELDS = [
    "repository_id",
    "checkout_id",
    "source_revision",
    "dirty_fingerprint",
    "index_generation",
    "captured_at",
]
EQUAL_IDENTITY_FIELDS = [
    "repository_id",
    "checkout_id",
    "source_revision",
    "dirty_fingerprint",
    "index_generation",
]
READINESS_REQUIREMENTS = {
    "index_identity": {
        "schema_version": 1,
        "required_fields": REQUIRED_IDENTITY_FIELDS,
        "equal_fields": EQUAL_IDENTITY_FIELDS,
    },
    "code-search": {
        "completion.success": True,
        "completion.error": "empty",
        "index_ready": True,
    },
    "code-graph": {
        "index_status.status": "ready",
        "index_repository.skip_report": True,
    },
    "readiness_evidence": {
        "schema_version": 1,
        "component_versions_match_bom": True,
        "checkout_unchanged": True,
    },
}


def load_json(rel: str, required_keys: tuple[str, ...]):
    path = ROOT / rel
    if not path.is_file():
        errors.append(f"{rel}: missing")
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        errors.append(f"{rel}: invalid JSON ({exc})")
        return None
    for key in required_keys:
        if key not in data:
            errors.append(f"{rel}: missing required key '{key}'")
    return data


# Plugin manifest
load_json(".claude-plugin/plugin.json", ("name", "version", "description"))

# MCP server config
mcp = load_json(".mcp.json", ("mcpServers",))
if mcp and isinstance(mcp.get("mcpServers"), dict):
    for name, cfg in mcp["mcpServers"].items():
        if not isinstance(cfg, dict) or "command" not in cfg:
            errors.append(f".mcp.json: server '{name}' missing 'command'")

# Skills: every skills/*/SKILL.md needs YAML frontmatter with name + description
skill_files = sorted((ROOT / "skills").glob("*/SKILL.md"))
if not skill_files:
    errors.append("skills/: no SKILL.md files found")
for skill in skill_files:
    rel = skill.relative_to(ROOT)
    text = skill.read_text()
    if not text.startswith("---"):
        errors.append(f"{rel}: missing YAML frontmatter (no leading '---')")
        continue
    end = text.find("\n---", 3)
    if end == -1:
        errors.append(f"{rel}: frontmatter not terminated with '---'")
        continue
    frontmatter = text[3:end]
    for key in ("name", "description"):
        if not any(line.strip().startswith(f"{key}:") for line in frontmatter.splitlines()):
            errors.append(f"{rel}: frontmatter missing '{key}'")

# Tested component BOM and tool-schema snapshots.
bom = load_json("component-bom.json", ("schema_version", "components"))
snapshots: dict[str, dict] = {}
if bom:
    if (
        isinstance(bom.get("schema_version"), bool)
        or bom.get("schema_version") != 1
    ):
        errors.append("component-bom.json: schema_version must be 1")
    components = bom.get("components")
    if not isinstance(components, dict):
        errors.append("component-bom.json: components must be an object")
    else:
        expected_components = {"code-search", "code-graph"}
        if set(components) != expected_components:
            errors.append(
                "component-bom.json: components must be exactly "
                + ", ".join(sorted(expected_components))
            )
        for component, details in components.items():
            if not isinstance(details, dict):
                errors.append(f"component-bom.json: {component} must be an object")
                continue
            install = details.get("install")
            snapshot_rel = details.get("schema_snapshot")
            if not isinstance(install, dict):
                errors.append(f"component-bom.json: {component}.install missing")
                continue
            if not isinstance(snapshot_rel, str):
                errors.append(
                    f"component-bom.json: {component}.schema_snapshot missing"
                )
                continue
            snapshot = load_json(
                snapshot_rel,
                (
                    "schema_version",
                    "component",
                    "source",
                    "fingerprint",
                    "tested_capabilities",
                    "tools",
                ),
            )
            if not isinstance(snapshot, dict):
                continue
            snapshots[component] = snapshot
            if (
                isinstance(snapshot.get("schema_version"), bool)
                or snapshot.get("schema_version") != 1
            ):
                errors.append(f"{snapshot_rel}: schema_version must be 1")
            if snapshot.get("component") != component:
                errors.append(f"{snapshot_rel}: component must be {component}")
            snapshot_capabilities = snapshot.get("tested_capabilities")
            bom_capabilities = details.get("tested_capabilities")
            if not isinstance(snapshot_capabilities, dict):
                errors.append(f"{snapshot_rel}: tested_capabilities must be an object")
            if not isinstance(bom_capabilities, dict):
                errors.append(
                    f"component-bom.json: {component}.tested_capabilities missing"
                )
            elif snapshot_capabilities != bom_capabilities:
                errors.append(
                    f"component-bom.json: {component}.tested_capabilities "
                    f"does not match {snapshot_rel}"
                )

            version_key = "revision" if component == "code-search" else "tag"
            install_version = install.get(version_key)
            source_version = snapshot.get("source", {}).get("version")
            if not isinstance(install_version, str) or not install_version:
                errors.append(
                    f"component-bom.json: {component}.install.{version_key} missing"
                )
            elif source_version != install_version:
                errors.append(
                    f"{snapshot_rel}: source version does not match BOM "
                    f"{component}.{version_key}"
                )

            if component == "code-graph":
                assets = install.get("assets")
                if not isinstance(assets, dict):
                    errors.append(
                        "component-bom.json: code-graph.install.assets missing"
                    )
                else:
                    if set(assets) != set(GRAPH_ASSET_NAMES):
                        errors.append(
                            "component-bom.json: code-graph.install.assets must "
                            "cover exactly " + ", ".join(sorted(GRAPH_ASSET_NAMES))
                        )
                    for platform_key, expected_name in GRAPH_ASSET_NAMES.items():
                        asset = assets.get(platform_key)
                        if not isinstance(asset, dict):
                            errors.append(
                                "component-bom.json: code-graph asset "
                                f"{platform_key} missing"
                            )
                            continue
                        if asset.get("name") != expected_name:
                            errors.append(
                                "component-bom.json: code-graph asset "
                                f"{platform_key}.name must be {expected_name}"
                            )
                        sha256 = asset.get("sha256")
                        if not isinstance(sha256, str) or re.fullmatch(
                            r"[0-9a-f]{64}", sha256
                        ) is None:
                            errors.append(
                                "component-bom.json: code-graph asset "
                                f"{platform_key}.sha256 must be 64 lowercase hex characters"
                            )

            tools = snapshot.get("tools")
            if not isinstance(tools, dict) or not tools:
                errors.append(f"{snapshot_rel}: tools must be a non-empty object")
                continue
            for tool_name, tool in tools.items():
                if not isinstance(tool, dict) or not isinstance(
                    tool.get("input_schema"), dict
                ):
                    errors.append(
                        f"{snapshot_rel}: tool '{tool_name}' missing input_schema"
                    )
                    continue
                canonical = json.dumps(
                    tool["input_schema"],
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                actual = hashlib.sha256(canonical).hexdigest()
                if tool.get("input_schema_sha256") != actual:
                    errors.append(
                        f"{snapshot_rel}: tool '{tool_name}' fingerprint mismatch"
                    )

if snapshots:
    references = {"code-search": set(), "code-graph": set()}
    for skill in skill_files:
        for prefix, tool_name in TOOL_REFERENCE.findall(
            skill.read_text(encoding="utf-8")
        ):
            references[COMPONENT_FOR_PREFIX[prefix]].add(tool_name)
    for component, referenced in references.items():
        supported = set(snapshots.get(component, {}).get("tools", {}))
        for missing in sorted(referenced - supported):
            errors.append(
                f"skills/: {component} tool '{missing}' is not in the tested snapshot"
            )

    graph_index_schema = (
        snapshots.get("code-graph", {})
        .get("tools", {})
        .get("index_repository", {})
        .get("input_schema", {})
    )
    graph_properties = graph_index_schema.get("properties", {})
    graph_supports_skip_report = isinstance(
        graph_properties, dict
    ) and "skip_report" in graph_properties
    readiness = bom.get("integrated_readiness", {}) if bom else {}
    if not isinstance(readiness, dict):
        errors.append("component-bom.json: integrated_readiness must be an object")
        readiness = {}
    readiness_status = readiness.get("status")
    if readiness_status not in {"blocked", "ready"}:
        errors.append(
            "component-bom.json: integrated_readiness.status must be blocked or ready"
        )
    if readiness.get("requires") != READINESS_REQUIREMENTS:
        errors.append(
            "component-bom.json: integrated_readiness.requires must preserve "
            "every identity, status, skip_report, and evidence gate"
        )
    required_identity_version = (
        readiness.get("requires", {})
        .get("index_identity", {})
        .get("schema_version")
        if isinstance(readiness.get("requires"), dict)
        else None
    )
    if isinstance(required_identity_version, bool):
        errors.append(
            "component-bom.json: readiness index_identity schema_version "
            "must be integer 1"
        )

    search_capabilities = snapshots.get("code-search", {}).get(
        "tested_capabilities", {}
    )
    graph_capabilities = snapshots.get("code-graph", {}).get(
        "tested_capabilities", {}
    )
    search_outputs = (
        search_capabilities.get("outputs", {})
        if isinstance(search_capabilities, dict)
        and isinstance(search_capabilities.get("outputs", {}), dict)
        else {}
    )
    graph_inputs = (
        graph_capabilities.get("inputs", {})
        if isinstance(graph_capabilities, dict)
        and isinstance(graph_capabilities.get("inputs", {}), dict)
        else {}
    )
    graph_outputs = (
        graph_capabilities.get("outputs", {})
        if isinstance(graph_capabilities, dict)
        and isinstance(graph_capabilities.get("outputs", {}), dict)
        else {}
    )
    graph_skip_report_attested = (
        graph_inputs.get("index_repository.skip_report") is True
    )
    if graph_skip_report_attested != graph_supports_skip_report:
        errors.append(
            "compatibility/code-graph-tools.json: tested skip_report capability "
            "does not match the index_repository input schema"
        )

    def complete_identity_capability(capability) -> bool:
        return (
            isinstance(capability, dict)
            and capability.get("supported") is True
            and not isinstance(capability.get("schema_version"), bool)
            and capability.get("schema_version") == 1
            and isinstance(capability.get("fields"), list)
            and len(capability["fields"]) == len(REQUIRED_IDENTITY_FIELDS)
            and set(capability["fields"]) == set(REQUIRED_IDENTITY_FIELDS)
        )

    if readiness_status == "ready":
        ready_capability_errors = []
        if not complete_identity_capability(search_outputs.get("index_identity")):
            ready_capability_errors.append(
                "code-search tested index_identity capability is not complete v1"
            )
        if not complete_identity_capability(graph_outputs.get("index_identity")):
            ready_capability_errors.append(
                "code-graph tested index_identity capability is not complete v1"
            )
        if search_outputs.get("semantic_index_ready") is not True:
            ready_capability_errors.append(
                "code-search tested semantic_index_ready capability is not true"
            )
        if graph_outputs.get("graph_status_ready") is not True:
            ready_capability_errors.append(
                "code-graph tested graph_status_ready capability is not true"
            )
        if not graph_skip_report_attested or not graph_supports_skip_report:
            ready_capability_errors.append(
                "code-graph tested skip_report capability and input schema "
                "must both be true"
            )
        for problem in ready_capability_errors:
            errors.append(
                "component-bom.json: integrated readiness must remain blocked: "
                + problem
            )

        evidence_rel = readiness.get("evidence")
        evidence = None
        evidence_label = str(evidence_rel)
        if not isinstance(evidence_rel, str) or not evidence_rel:
            errors.append(
                "component-bom.json: ready status requires a readiness evidence path"
            )
        else:
            evidence_override = os.environ.get(
                "CODE_INTEL_READINESS_EVIDENCE_OVERRIDE", ""
            ).strip()
            evidence_path_allowed = True
            if evidence_override:
                evidence_path = Path(evidence_override).resolve()
                evidence_label = str(evidence_path)
                runner_temp_raw = os.environ.get("RUNNER_TEMP", "").strip()
                if not runner_temp_raw:
                    errors.append(
                        "live readiness evidence override requires RUNNER_TEMP"
                    )
                    evidence_path_allowed = False
                else:
                    try:
                        evidence_path.relative_to(Path(runner_temp_raw).resolve())
                    except ValueError:
                        errors.append(
                            "live readiness evidence override must be under "
                            "RUNNER_TEMP"
                        )
                        evidence_path_allowed = False
            else:
                evidence_path = (ROOT / evidence_rel).resolve()
                try:
                    evidence_path.relative_to(ROOT.resolve())
                except ValueError:
                    errors.append(
                        "component-bom.json: readiness evidence path escapes "
                        "the checkout"
                    )
                    evidence_path_allowed = False
            if evidence_path_allowed:
                if not evidence_path.is_file():
                    errors.append(
                        f"{evidence_label}: readiness evidence file is missing"
                    )
                else:
                    try:
                        evidence = json.loads(
                            evidence_path.read_text(encoding="utf-8")
                        )
                    except (OSError, json.JSONDecodeError) as exc:
                        errors.append(
                            f"{evidence_label}: invalid readiness evidence ({exc})"
                        )

        if evidence is not None and not isinstance(evidence, dict):
            errors.append(
                f"{evidence_label}: readiness evidence must be a JSON object"
            )
        if isinstance(evidence, dict):
            if (
                isinstance(evidence.get("schema_version"), bool)
                or evidence.get("schema_version") != 1
            ):
                errors.append(
                    f"{evidence_label}: readiness evidence schema_version must be 1"
                )
            evidence_components = evidence.get("components")
            if not isinstance(evidence_components, dict):
                errors.append(
                    f"{evidence_label}: readiness evidence components missing"
                )
                evidence_components = {}

            expected_versions = {
                "code-search": bom["components"]["code-search"]["install"].get(
                    "revision"
                ),
                "code-graph": bom["components"]["code-graph"]["install"].get("tag"),
            }
            for component, expected_version in expected_versions.items():
                component_evidence = evidence_components.get(component)
                if not isinstance(component_evidence, dict):
                    errors.append(
                        f"{evidence_label}: {component} readiness evidence missing"
                    )
                elif component_evidence.get("version") != expected_version:
                    errors.append(
                        f"{evidence_label}: {component} version does not match the BOM"
                    )

            search_evidence = evidence_components.get("code-search", {})
            if isinstance(search_evidence, dict):
                completion = search_evidence.get("completion")
                if not isinstance(completion, dict):
                    errors.append(
                        f"{evidence_label}: code-search completion evidence missing"
                    )
                else:
                    if completion.get("success") is not True:
                        errors.append(
                            f"{evidence_label}: code-search completion success "
                            "must be true"
                        )
                    if completion.get("error") not in (None, "", []):
                        errors.append(
                            f"{evidence_label}: code-search completion error "
                            "must be empty"
                        )
                if search_evidence.get("index_ready") is not True:
                    errors.append(
                        f"{evidence_label}: code-search index_ready must be true"
                    )

            graph_evidence = evidence_components.get("code-graph", {})
            if (
                isinstance(graph_evidence, dict)
                and graph_evidence.get("status") != "ready"
            ):
                errors.append(
                    f"{evidence_label}: code-graph status must be ready"
                )

            def evidence_identity(component: str):
                component_evidence = evidence_components.get(component, {})
                identity = (
                    component_evidence.get("index_identity")
                    if isinstance(component_evidence, dict)
                    else None
                )
                if not isinstance(identity, dict):
                    errors.append(
                        f"{evidence_label}: {component} index_identity must be an object"
                    )
                    return None
                if (
                    isinstance(identity.get("schema_version"), bool)
                    or identity.get("schema_version") != 1
                ):
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "schema_version must be 1"
                    )
                    return None
                for field in REQUIRED_IDENTITY_FIELDS:
                    if (
                        not isinstance(identity.get(field), str)
                        or not identity[field]
                    ):
                        errors.append(
                            f"{evidence_label}: {component} index_identity "
                            f"{field} must be a nonempty string"
                        )
                        return None

                lower_sha256 = re.compile(r"[0-9a-f]{64}")
                for field in (
                    "repository_id",
                    "checkout_id",
                    "index_generation",
                ):
                    if lower_sha256.fullmatch(identity[field]) is None:
                        errors.append(
                            f"{evidence_label}: {component} index_identity "
                            f"{field} must be a lowercase SHA-256 digest"
                        )
                        return None
                source_revision = identity["source_revision"]
                if source_revision != "unborn" and re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})", source_revision
                ) is None:
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "source_revision must be unborn or a lowercase Git object ID"
                    )
                    return None
                dirty_fingerprint = identity["dirty_fingerprint"]
                if (
                    dirty_fingerprint != "clean"
                    and lower_sha256.fullmatch(dirty_fingerprint) is None
                ):
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "dirty_fingerprint must be clean or a lowercase SHA-256 digest"
                    )
                    return None

                captured_at = identity["captured_at"]
                if re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
                    r"(?:\.\d+)?(?:Z|\+00:00)",
                    captured_at,
                ) is None:
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "captured_at must be an RFC3339 UTC timestamp"
                    )
                    return None
                try:
                    parsed_timestamp = datetime.fromisoformat(
                        captured_at[:-1] + "+00:00"
                        if captured_at.endswith("Z")
                        else captured_at
                    )
                except ValueError:
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "captured_at must be an RFC3339 UTC timestamp"
                    )
                    return None
                if parsed_timestamp.utcoffset() != timezone.utc.utcoffset(
                    parsed_timestamp
                ):
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "captured_at must be UTC"
                    )
                    return None

                expected_generation = hashlib.sha256(
                    (
                        identity["repository_id"]
                        + "\0"
                        + identity["source_revision"]
                        + "\0"
                        + identity["dirty_fingerprint"]
                    ).encode("utf-8")
                ).hexdigest()
                if identity["index_generation"] != expected_generation:
                    errors.append(
                        f"{evidence_label}: {component} index_identity "
                        "index_generation does not match "
                        "repository_id/source_revision/dirty_fingerprint"
                    )
                    return None
                return identity

            search_identity = evidence_identity("code-search")
            graph_identity = evidence_identity("code-graph")
            if search_identity and graph_identity and any(
                search_identity[field] != graph_identity[field]
                for field in EQUAL_IDENTITY_FIELDS
            ):
                errors.append(
                    f"{evidence_label}: component index identities are not equal"
                )
            if evidence.get("checkout_unchanged") is not True:
                errors.append(
                    f"{evidence_label}: checkout_unchanged must be true"
                )

    index_skill = ROOT / "skills" / "index-repo" / "SKILL.md"
    if index_skill.is_file() and "skip_report=true" not in index_skill.read_text(
        encoding="utf-8"
    ):
        errors.append(
            "skills/index-repo/SKILL.md: graph indexing must use skip_report=true"
        )

if bom and isinstance(bom.get("components"), dict):
    for installer_rel in ("install.sh", "install.ps1"):
        installer_path = ROOT / installer_rel
        if not installer_path.is_file():
            errors.append(f"{installer_rel}: missing")
            continue
        installer = installer_path.read_text(encoding="utf-8")
        for required_text in ("component-bom.json", "validate_installed.py"):
            if required_text not in installer:
                errors.append(f"{installer_rel}: does not use {required_text}")
        for forbidden_text in (
            "gh release list",
            "api.github.com",
            "Invoke-RestMethod",
        ):
            if forbidden_text in installer:
                errors.append(
                    f"{installer_rel}: dynamically selects a component "
                    f"({forbidden_text})"
                )
        for component, details in bom["components"].items():
            install = details.get("install", {})
            version = install.get(
                "revision" if component == "code-search" else "tag"
            )
            if isinstance(version, str) and version in installer:
                errors.append(
                    f"{installer_rel}: duplicates {component} version instead "
                    "of reading the BOM"
                )

if errors:
    print("Plugin validation FAILED:")
    for err in errors:
        print(f"  - {err}")
    sys.exit(1)

print(
    "Plugin validation passed "
    f"({len(skill_files)} skill(s), {len(snapshots)} component contract(s) checked)."
)
