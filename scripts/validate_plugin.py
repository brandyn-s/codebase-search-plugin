#!/usr/bin/env python3
"""Validate the codebase-search plugin's manifest, MCP config, and skills.

Run locally with:  python3 scripts/validate_plugin.py

Exits non-zero (printing each problem) if anything is malformed. Uses only the
standard library so it runs anywhere without extra dependencies.
"""
import argparse
import json
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from component_descriptor import (
    DescriptorError,
    install_descriptor_sha256,
    validate_install_descriptor_shape,
)

ROOT = Path(__file__).resolve().parent.parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component-bom",
        default="component-bom.json",
        help="exact candidate component BOM to validate",
    )
    return parser.parse_args(argv)


ARGS = parse_args()
COMPONENT_BOM_INPUT = ARGS.component_bom
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
GO_SCIP_RELEASE_REPOSITORY = "scip-code/scip-go"
GO_SCIP_ASSET_NAMES = {
    "darwin-arm64": "scip-go-darwin-arm64.tar.gz",
    "linux-amd64": "scip-go-linux-amd64.tar.gz",
    "linux-arm64": "scip-go-linux-arm64.tar.gz",
}
TYPESCRIPT_SCIP_PACKAGE = "@sourcegraph/scip-typescript"
TYPESCRIPT_SCIP_SOURCE_REPOSITORY = "sourcegraph/scip-typescript"
NODE_RUNTIME_ASSET_NAMES = {
    "darwin-amd64": "node-v22.23.2-darwin-x64.tar.gz",
    "darwin-arm64": "node-v22.23.2-darwin-arm64.tar.gz",
    "linux-amd64": "node-v22.23.2-linux-x64.tar.xz",
    "linux-arm64": "node-v22.23.2-linux-arm64.tar.xz",
    "windows-amd64": "node-v22.23.2-win-x64.zip",
}
CODE_SEARCH_GIT_REPOSITORY = (
    "https://github.com/redacted-org/code-search.git"
)
# TODO(brandyn-s primary): component pins still reference the redacted-org
# releases they were built from; re-point when the first brandyn-s releases are promoted.
CODE_SEARCH_RELEASE_REPOSITORY = "redacted-org/code-search"
CODE_SEARCH_RELEASE_SIGNER_WORKFLOW = (
    "redacted-org/code-search/.github/workflows/release.yml"
)
CODE_SEARCH_RELEASE_SOURCE_REF = "refs/heads/main"
CODE_GRAPH_RELEASE_REPOSITORY = "redacted-org/code-graph"
CODE_GRAPH_RELEASE_SIGNER_WORKFLOW = (
    "redacted-org/code-graph/.github/workflows/release.yml"
)
CODE_GRAPH_RELEASE_SOURCE_REF = "refs/heads/main"
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
        "evidence_coordinate": "src/config.py:3-3",
        "index_ready": True,
    },
    "code-graph": {
        "index_status.status": "ready",
        "index_repository.skip_report": True,
    },
    "readiness_evidence": {
        "schema_version": 1,
        "component_install_descriptors_match_bom": True,
        "checkout_unchanged": True,
    },
}


def component_install_version(component: str, install: dict):
    if component == "code-search" and install.get("kind") == "git":
        return install.get("revision")
    return install.get("tag")


def safe_release_asset_name(value, suffix: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "/" not in value
        and "\\" not in value
        and all(ord(character) >= 32 for character in value)
        and value.endswith(suffix)
    )


def validate_code_search_install(install: dict) -> None:
    try:
        validate_install_descriptor_shape("code-search", install)
    except DescriptorError as exc:
        errors.append(f"component-bom.json: {exc}")
        return
    kind = install.get("kind")
    if kind == "git":
        if install.get("repository") != CODE_SEARCH_GIT_REPOSITORY:
            errors.append(
                "component-bom.json: code-search Git repository must be "
                f"{CODE_SEARCH_GIT_REPOSITORY}"
            )
        revision = install.get("revision")
        if not isinstance(revision, str) or re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", revision
        ) is None:
            errors.append(
                "component-bom.json: code-search.install.revision must be "
                "a full lowercase Git object ID"
            )
        return

    if kind != "github-release":
        errors.append(
            "component-bom.json: code-search.install.kind must be git "
            "or github-release"
        )
        return
    if install.get("repository") != CODE_SEARCH_RELEASE_REPOSITORY:
        errors.append(
            "component-bom.json: code-search release repository must be "
            f"{CODE_SEARCH_RELEASE_REPOSITORY}"
        )
    tag = install.get("tag")
    if (
        not isinstance(tag, str)
        or re.fullmatch(r"v[0-9][0-9A-Za-z._+-]*", tag) is None
    ):
        errors.append(
            "component-bom.json: code-search.install.tag must be a "
            "safe version tag beginning with v"
        )
    source_revision = install.get("source_revision")
    if not isinstance(source_revision, str) or re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", source_revision
    ) is None:
        errors.append(
            "component-bom.json: code-search.install.source_revision must "
            "be a full lowercase Git object ID"
        )

    asset = install.get("asset")
    release_version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else ""
    expected_wheel_name = (
        f"redacted_code_search-{release_version}-py3-none-any.whl"
    )
    if not isinstance(asset, dict):
        errors.append(
            "component-bom.json: code-search.install.asset must be an object"
        )
    else:
        name = asset.get("name")
        if not safe_release_asset_name(name, ".whl") or not name.startswith(
            "redacted_code_search-"
        ):
            errors.append(
                "component-bom.json: code-search release asset must be a "
                "safe redacted_code_search wheel filename"
            )
        elif name != expected_wheel_name:
            errors.append(
                "component-bom.json: code-search wheel filename must encode "
                f"the release tag exactly ({expected_wheel_name})"
            )
        sha256 = asset.get("sha256")
        if not isinstance(sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", sha256
        ) is None:
            errors.append(
                "component-bom.json: code-search release asset sha256 must "
                "be 64 lowercase hex characters"
            )

    checksums = install.get("checksums")
    if not isinstance(checksums, dict):
        errors.append(
            "component-bom.json: code-search.install.checksums must be an object"
        )
    else:
        if checksums.get("name") != "SHA256SUMS":
            errors.append(
                "component-bom.json: code-search checksums name must be "
                "SHA256SUMS"
            )
        checksums_sha256 = checksums.get("sha256")
        if not isinstance(checksums_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", checksums_sha256
        ) is None:
            errors.append(
                "component-bom.json: code-search checksums sha256 must be "
                "64 lowercase hex characters"
            )

    attestation = install.get("attestation")
    if not isinstance(attestation, dict):
        errors.append(
            "component-bom.json: code-search.install.attestation must be "
            "an object"
        )
        return
    bundle = attestation.get("bundle")
    if not isinstance(bundle, dict):
        errors.append(
            "component-bom.json: code-search attestation bundle must be "
            "an object"
        )
    else:
        bundle_name = bundle.get("name")
        if not safe_release_asset_name(bundle_name, ".jsonl"):
            errors.append(
                "component-bom.json: code-search attestation bundle name "
                "must be a safe JSONL filename"
            )
        elif bundle_name != (
            f"redacted_code_search-{release_version}-provenance.jsonl"
        ):
            errors.append(
                "component-bom.json: code-search attestation bundle must "
                "encode the release tag exactly"
            )
        bundle_sha256 = bundle.get("sha256")
        if not isinstance(bundle_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", bundle_sha256
        ) is None:
            errors.append(
                "component-bom.json: code-search attestation bundle sha256 "
                "must be 64 lowercase hex characters"
            )
    signer_workflow = attestation.get("signer_workflow")
    if signer_workflow != CODE_SEARCH_RELEASE_SIGNER_WORKFLOW:
        errors.append(
            "component-bom.json: code-search attestation signer_workflow "
            f"must be {CODE_SEARCH_RELEASE_SIGNER_WORKFLOW}"
        )
    source_ref = attestation.get("source_ref")
    if source_ref != CODE_SEARCH_RELEASE_SOURCE_REF:
        errors.append(
            "component-bom.json: code-search attestation source_ref must "
            f"be {CODE_SEARCH_RELEASE_SOURCE_REF}"
        )
    if attestation.get("deny_self_hosted_runners") is not True:
        errors.append(
            "component-bom.json: code-search attestation "
            "deny_self_hosted_runners must be true"
        )


def validate_code_graph_install(install: dict) -> None:
    try:
        validate_install_descriptor_shape("code-graph", install)
    except DescriptorError as exc:
        errors.append(f"component-bom.json: {exc}")
        return
    if install.get("kind") != "github-release":
        errors.append(
            "component-bom.json: code-graph.install.kind must be github-release"
        )
    if install.get("repository") != CODE_GRAPH_RELEASE_REPOSITORY:
        errors.append(
            "component-bom.json: code-graph release repository must be "
            f"{CODE_GRAPH_RELEASE_REPOSITORY}"
        )
    tag = install.get("tag")
    if (
        not isinstance(tag, str)
        or re.fullmatch(r"v[0-9][0-9A-Za-z._+-]*", tag) is None
    ):
        errors.append(
            "component-bom.json: code-graph.install.tag must be a safe "
            "version tag beginning with v"
        )
    source_revision = install.get("source_revision")
    if not isinstance(source_revision, str) or re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", source_revision
    ) is None:
        errors.append(
            "component-bom.json: code-graph.install.source_revision must be "
            "a full lowercase Git object ID"
        )

    checksums = install.get("checksums")
    if not isinstance(checksums, dict):
        errors.append(
            "component-bom.json: code-graph.install.checksums must be an object"
        )
    else:
        if checksums.get("name") != "checksums.txt":
            errors.append(
                "component-bom.json: code-graph checksums name must be "
                "checksums.txt"
            )
        checksums_sha256 = checksums.get("sha256")
        if not isinstance(checksums_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", checksums_sha256
        ) is None:
            errors.append(
                "component-bom.json: code-graph checksums sha256 must be "
                "64 lowercase hex characters"
            )

    attestation = install.get("attestation")
    if not isinstance(attestation, dict):
        errors.append(
            "component-bom.json: code-graph.install.attestation must be an object"
        )
        return
    bundle = attestation.get("bundle")
    expected_bundle_path = (
        f"compatibility/attestations/code-graph-{tag}-provenance.jsonl"
    )
    bundle_path = bundle.get("path") if isinstance(bundle, dict) else None
    bundle_sha256 = bundle.get("sha256") if isinstance(bundle, dict) else None
    if bundle_path != expected_bundle_path:
        errors.append(
            "component-bom.json: code-graph attestation bundle path must be "
            f"{expected_bundle_path}"
        )
    elif (
        not isinstance(bundle_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", bundle_sha256) is None
    ):
        errors.append(
            "component-bom.json: code-graph attestation bundle sha256 must "
            "be 64 lowercase hex characters"
        )
    else:
        vendored_bundle = ROOT / bundle_path
        if not vendored_bundle.is_file() or vendored_bundle.is_symlink():
            errors.append(
                "component-bom.json: code-graph attestation bundle is missing "
                "or is not a regular repository file"
            )
        elif hashlib.sha256(vendored_bundle.read_bytes()).hexdigest() != bundle_sha256:
            errors.append(
                "component-bom.json: code-graph attestation bundle sha256 "
                "does not match the vendored file"
            )
    if attestation.get("signer_workflow") != CODE_GRAPH_RELEASE_SIGNER_WORKFLOW:
        errors.append(
            "component-bom.json: code-graph attestation signer_workflow "
            f"must be {CODE_GRAPH_RELEASE_SIGNER_WORKFLOW}"
        )
    if attestation.get("source_ref") != CODE_GRAPH_RELEASE_SOURCE_REF:
        errors.append(
            "component-bom.json: code-graph attestation source_ref must be "
            f"{CODE_GRAPH_RELEASE_SOURCE_REF}"
        )
    if attestation.get("deny_self_hosted_runners") is not True:
        errors.append(
            "component-bom.json: code-graph attestation "
            "deny_self_hosted_runners must be true"
        )


def validate_precision_generators(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "go-scip",
        "typescript-scip",
    }:
        errors.append(
            "component-bom.json: precision_generators must contain exactly "
            "go-scip and typescript-scip"
        )
        return
    generator = value["go-scip"]
    if not isinstance(generator, dict):
        errors.append("component-bom.json: go-scip must be an object")
        return
    expected_keys = {
        "assets",
        "kind",
        "repository",
        "source_revision",
        "tag",
        "version_output",
    }
    if set(generator) != expected_keys:
        errors.append(
            "component-bom.json: go-scip keys must be exactly "
            + ", ".join(sorted(expected_keys))
        )
    if generator.get("kind") != "github-release":
        errors.append("component-bom.json: go-scip.kind must be github-release")
    if generator.get("repository") != GO_SCIP_RELEASE_REPOSITORY:
        errors.append(
            "component-bom.json: go-scip.repository must be "
            + GO_SCIP_RELEASE_REPOSITORY
        )
    if not isinstance(generator.get("tag"), str) or re.fullmatch(
        r"v[0-9][0-9A-Za-z._+-]*", generator.get("tag", "")
    ) is None:
        errors.append("component-bom.json: go-scip.tag must be a safe version tag")
    if not isinstance(generator.get("source_revision"), str) or re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", generator.get("source_revision", "")
    ) is None:
        errors.append(
            "component-bom.json: go-scip.source_revision must be a full object ID"
        )
    version = generator.get("version_output")
    if not isinstance(version, str) or not version or version != version.strip():
        errors.append(
            "component-bom.json: go-scip.version_output must be non-empty"
        )
    assets = generator.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(GO_SCIP_ASSET_NAMES):
        errors.append(
            "component-bom.json: go-scip.assets must cover exactly "
            + ", ".join(sorted(GO_SCIP_ASSET_NAMES))
        )
        return
    for platform_key, expected_name in GO_SCIP_ASSET_NAMES.items():
        asset = assets.get(platform_key)
        if not isinstance(asset, dict):
            errors.append(
                f"component-bom.json: go-scip asset {platform_key} missing"
            )
            continue
        expected_asset_keys = {"archive_sha256", "binary_sha256", "name"}
        if set(asset) != expected_asset_keys:
            errors.append(
                f"component-bom.json: go-scip asset {platform_key} keys must be "
                + ", ".join(sorted(expected_asset_keys))
            )
        if asset.get("name") != expected_name:
            errors.append(
                f"component-bom.json: go-scip asset {platform_key}.name must be "
                + expected_name
            )
        for digest_name in ("archive_sha256", "binary_sha256"):
            digest = asset.get(digest_name)
            if not isinstance(digest, str) or re.fullmatch(
                r"[0-9a-f]{64}", digest
            ) is None:
                errors.append(
                    f"component-bom.json: go-scip asset {platform_key}."
                    f"{digest_name} must be 64 lowercase hex characters"
                )

    typescript = value["typescript-scip"]
    if not isinstance(typescript, dict):
        errors.append("component-bom.json: typescript-scip must be an object")
        return
    expected_typescript_keys = {
        "entrypoint",
        "entrypoint_sha256",
        "kind",
        "lockfile",
        "lockfile_sha256",
        "node_runtime",
        "package",
        "package_integrity",
        "package_manifest",
        "source_repository",
        "source_revision",
        "supported_node_majors",
        "version_output",
    }
    if set(typescript) != expected_typescript_keys:
        errors.append(
            "component-bom.json: typescript-scip keys must be exactly "
            + ", ".join(sorted(expected_typescript_keys))
        )
    if typescript.get("kind") != "npm-lockfile":
        errors.append("component-bom.json: typescript-scip.kind must be npm-lockfile")
    if typescript.get("package") != TYPESCRIPT_SCIP_PACKAGE:
        errors.append(
            "component-bom.json: typescript-scip.package must be "
            + TYPESCRIPT_SCIP_PACKAGE
        )
    if typescript.get("source_repository") != TYPESCRIPT_SCIP_SOURCE_REPOSITORY:
        errors.append(
            "component-bom.json: typescript-scip.source_repository must be "
            + TYPESCRIPT_SCIP_SOURCE_REPOSITORY
        )
    if re.fullmatch(r"[0-9a-f]{40}", typescript.get("source_revision", "")) is None:
        errors.append(
            "component-bom.json: typescript-scip.source_revision must be a full Git SHA"
        )
    if typescript.get("version_output") != "0.4.0":
        errors.append(
            "component-bom.json: typescript-scip.version_output must be 0.4.0"
        )
    if not isinstance(typescript.get("package_integrity"), str) or not typescript[
        "package_integrity"
    ].startswith("sha512-"):
        errors.append(
            "component-bom.json: typescript-scip.package_integrity must be npm SHA-512 SRI"
        )
    expected_paths = {
        "package_manifest": "compatibility/scip-typescript-package.json",
        "lockfile": "compatibility/scip-typescript-package-lock.json",
        "entrypoint": "node_modules/@sourcegraph/scip-typescript/dist/src/main.js",
    }
    for field, expected in expected_paths.items():
        if typescript.get(field) != expected:
            errors.append(
                f"component-bom.json: typescript-scip.{field} must be {expected}"
            )
    for field in ("lockfile_sha256", "entrypoint_sha256"):
        if re.fullmatch(r"[0-9a-f]{64}", typescript.get(field, "")) is None:
            errors.append(
                f"component-bom.json: typescript-scip.{field} must be a SHA-256 digest"
            )
    lockfile_path = ROOT / str(typescript.get("lockfile", ""))
    if not lockfile_path.is_file():
        errors.append("component-bom.json: TypeScript SCIP lockfile is missing")
    elif hashlib.sha256(lockfile_path.read_bytes()).hexdigest() != typescript.get(
        "lockfile_sha256"
    ):
        errors.append(
            "component-bom.json: TypeScript SCIP lockfile digest does not match"
        )
    package_manifest_path = ROOT / str(typescript.get("package_manifest", ""))
    if not package_manifest_path.is_file():
        errors.append("component-bom.json: TypeScript SCIP package manifest is missing")
    if typescript.get("supported_node_majors") != [22]:
        errors.append(
            "component-bom.json: typescript-scip.supported_node_majors must be [22]"
        )
    runtime = typescript.get("node_runtime")
    if not isinstance(runtime, dict) or set(runtime) != {
        "assets",
        "base_url",
        "version",
    }:
        errors.append(
            "component-bom.json: typescript-scip.node_runtime shape is invalid"
        )
        return
    if runtime.get("version") != "v22.23.2":
        errors.append(
            "component-bom.json: typescript-scip.node_runtime.version must be v22.23.2"
        )
    if runtime.get("base_url") != "https://nodejs.org/download/release/v22.23.2":
        errors.append(
            "component-bom.json: typescript-scip.node_runtime.base_url is invalid"
        )
    runtime_assets = runtime.get("assets")
    if not isinstance(runtime_assets, dict) or set(runtime_assets) != set(
        NODE_RUNTIME_ASSET_NAMES
    ):
        errors.append(
            "component-bom.json: TypeScript Node runtime assets are incomplete"
        )
        return
    for platform_key, expected_name in NODE_RUNTIME_ASSET_NAMES.items():
        asset = runtime_assets.get(platform_key)
        if not isinstance(asset, dict) or set(asset) != {
            "archive_sha256",
            "binary_sha256",
            "name",
        }:
            errors.append(
                f"component-bom.json: TypeScript Node asset {platform_key} shape is invalid"
            )
            continue
        if asset.get("name") != expected_name:
            errors.append(
                f"component-bom.json: TypeScript Node asset {platform_key}.name is invalid"
            )
        for field in ("archive_sha256", "binary_sha256"):
            if re.fullmatch(r"[0-9a-f]{64}", asset.get(field, "")) is None:
                errors.append(
                    f"component-bom.json: TypeScript Node asset {platform_key}.{field} is invalid"
                )


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
        if not any(
            line.strip().startswith(f"{key}:")
            for line in frontmatter.splitlines()
        ):
            errors.append(f"{rel}: frontmatter missing '{key}'")

# Tested component BOM and tool-schema snapshots.
bom = load_json(COMPONENT_BOM_INPUT, ("schema_version", "components"))
snapshots: dict[str, dict] = {}
if bom:
    if (
        isinstance(bom.get("schema_version"), bool)
        or bom.get("schema_version") != 1
    ):
        errors.append("component-bom.json: schema_version must be 1")
    components = bom.get("components")
    validate_precision_generators(bom.get("precision_generators"))
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
            if component == "code-search":
                validate_code_search_install(install)
            elif component == "code-graph":
                validate_code_graph_install(install)
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

            install_version = component_install_version(component, install)
            snapshot_source = snapshot.get("source", {})
            source_version = snapshot_source.get("version")
            if not isinstance(install_version, str) or not install_version:
                errors.append(
                    f"component-bom.json: {component} install version missing"
                )
            elif source_version != install_version:
                errors.append(
                    f"{snapshot_rel}: source version does not match BOM "
                    f"{component} install version"
                )
            if snapshot_source.get("kind") != install.get("kind"):
                errors.append(
                    f"{snapshot_rel}: source kind does not match BOM "
                    f"{component} install kind"
                )
            try:
                expected_descriptor_sha256 = install_descriptor_sha256(install)
            except DescriptorError as exc:
                errors.append(
                    f"component-bom.json: {component} install descriptor "
                    f"cannot be canonicalized ({exc})"
                )
            else:
                if (
                    snapshot_source.get("install_descriptor_sha256")
                    != expected_descriptor_sha256
                ):
                    errors.append(
                        f"{snapshot_rel}: source install descriptor does not "
                        f"match BOM {component} install descriptor"
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
                                f"{platform_key}.sha256 must be 64 lowercase "
                                "hex characters"
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

    def optional_typed_property(schema, name: str, expected_type: str):
        if not isinstance(schema, dict):
            return False, False
        properties = schema.get("properties")
        if not isinstance(properties, dict) or name not in properties:
            return False, False
        property_schema = properties[name]
        required = schema.get("required", [])
        annotation_keys = {
            "$comment",
            "default",
            "deprecated",
            "description",
            "examples",
            "readOnly",
            "title",
            "writeOnly",
        }
        root_keys = annotation_keys | {
            "additionalProperties",
            "properties",
            "required",
            "type",
        }
        root_shape_is_safe = set(schema).issubset(root_keys)
        required_is_valid = (
            isinstance(required, list)
            and all(isinstance(item, str) for item in required)
            and len(required) == len(set(required))
        )
        declared_types = None
        if isinstance(property_schema, dict):
            direct_type = property_schema.get("type")
            if "type" in property_schema and set(property_schema).issubset(
                annotation_keys | {"type"}
            ):
                if isinstance(direct_type, str):
                    type_names = [direct_type]
                elif (
                    isinstance(direct_type, list)
                    and direct_type
                    and all(isinstance(item, str) for item in direct_type)
                ):
                    type_names = direct_type
                else:
                    type_names = []
                if len(type_names) == len(set(type_names)):
                    declared_types = set(type_names)
            elif set(property_schema).issubset(annotation_keys | {"anyOf"}):
                alternatives = property_schema.get("anyOf")
                if (
                    isinstance(alternatives, list)
                    and alternatives
                    and all(
                        isinstance(option, dict)
                        and isinstance(option.get("type"), str)
                        and set(option).issubset(annotation_keys | {"type"})
                        for option in alternatives
                    )
                ):
                    type_names = [option["type"] for option in alternatives]
                    if len(type_names) == len(set(type_names)):
                        declared_types = set(type_names)
        valid = (
            schema.get("type") == "object"
            and root_shape_is_safe
            and isinstance(property_schema, dict)
            and declared_types in (
                {expected_type},
                {expected_type, "null"},
            )
            and required_is_valid
            and name not in required
        )
        return True, valid

    search_status_schema = (
        snapshots.get("code-search", {})
        .get("tools", {})
        .get("get_index_status", {})
        .get("input_schema", {})
    )
    search_project_path_present, search_supports_project_path = (
        optional_typed_property(search_status_schema, "project_path", "string")
    )
    if search_project_path_present and not search_supports_project_path:
        errors.append(
            "compatibility/code-search-tools.json: "
            "get_index_status.project_path must be an optional string"
        )

    graph_index_schema = (
        snapshots.get("code-graph", {})
        .get("tools", {})
        .get("index_repository", {})
        .get("input_schema", {})
    )
    graph_skip_report_present, graph_supports_skip_report = (
        optional_typed_property(graph_index_schema, "skip_report", "boolean")
    )
    if graph_skip_report_present and not graph_supports_skip_report:
        errors.append(
            "compatibility/code-graph-tools.json: "
            "index_repository.skip_report must be an optional boolean"
        )
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
            "every identity, status, skip_report, coordinate, and evidence gate"
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
        if not search_supports_project_path:
            ready_capability_errors.append(
                "code-search get_index_status.project_path must be an optional string"
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
        evidence_override = os.environ.get(
            "CODE_INTEL_READINESS_EVIDENCE_OVERRIDE", ""
        ).strip()
        if not isinstance(evidence_rel, str) or not evidence_rel:
            errors.append(
                "component-bom.json: ready status requires a readiness evidence path"
            )
        else:
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
            if (
                evidence.get("producer")
                != "scripts/generate_live_readiness_evidence.py:v3"
            ):
                errors.append(
                    f"{evidence_label}: readiness evidence producer is invalid"
                )
            expected_evidence_mode = (
                "ready-validation"
                if evidence_override
                else "promotion-candidate"
            )
            if evidence.get("evidence_mode") != expected_evidence_mode:
                errors.append(
                    f"{evidence_label}: evidence_mode must be "
                    f"{expected_evidence_mode}"
                )
            expected_bom_status = "ready" if evidence_override else "blocked"
            if evidence.get("bom_readiness_status") != expected_bom_status:
                errors.append(
                    f"{evidence_label}: bom_readiness_status must be "
                    f"{expected_bom_status}"
                )
            evidence_components = evidence.get("components")
            if not isinstance(evidence_components, dict):
                errors.append(
                    f"{evidence_label}: readiness evidence components missing"
                )
                evidence_components = {}

            expected_versions = {
                component: component_install_version(
                    component,
                    bom["components"][component]["install"],
                )
                for component in ("code-search", "code-graph")
            }
            expected_descriptor_hashes = {
                component: install_descriptor_sha256(
                    bom["components"][component]["install"]
                )
                for component in ("code-search", "code-graph")
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
                if (
                    isinstance(component_evidence, dict)
                    and component_evidence.get("install_descriptor_sha256")
                    != expected_descriptor_hashes[component]
                ):
                    errors.append(
                        f"{evidence_label}: {component} install descriptor "
                        "does not match the BOM"
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
                coordinate = search_evidence.get("evidence_coordinate")
                search_identity_for_coordinate = search_evidence.get(
                    "index_identity"
                )
                expected_generation = (
                    search_identity_for_coordinate.get("index_generation")
                    if isinstance(search_identity_for_coordinate, dict)
                    else None
                )
                if (
                    not isinstance(coordinate, dict)
                    or set(coordinate)
                    != {
                        "status",
                        "relative_path",
                        "start_line",
                        "end_line",
                        "index_generation",
                    }
                    or coordinate.get("status") != "verified"
                    or coordinate.get("relative_path") != "src/config.py"
                    or coordinate.get("start_line") != 3
                    or coordinate.get("end_line") != 3
                    or not isinstance(expected_generation, str)
                    or coordinate.get("index_generation")
                    != expected_generation
                ):
                    errors.append(
                        f"{evidence_label}: code-search evidence coordinate "
                        "must verify src/config.py:3-3 against the indexed "
                        "generation"
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
                        f"{evidence_label}: {component} index_identity "
                        "must be an object"
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
        # Installers may talk to the GitHub API to resolve the pinned tag, but
        # must never pick a moving release.
        for forbidden_text in (
            "gh release list",
            "releases/latest",
            "/releases?",
        ):
            if forbidden_text in installer:
                errors.append(
                    f"{installer_rel}: dynamically selects a component "
                    f"({forbidden_text})"
                )
        for component, details in bom["components"].items():
            install = details.get("install", {})
            version = component_install_version(component, install)
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
