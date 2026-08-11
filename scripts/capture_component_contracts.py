#!/usr/bin/env python3
"""Capture pinned MCP input schemas into a blocked candidate contract.

The command is offline: callers provide both the candidate component BOM and
the already-installed server executables.  It prints a deterministic preview
by default and only creates the output directory when ``--write`` is present.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from component_descriptor import (
    DescriptorError,
    install_descriptor_sha256,
    validate_install_descriptor_shape,
)
from validate_installed import ContractError, list_tools, parse_servers


COMPONENTS = {"code-search", "code-graph"}
SNAPSHOT_PATHS = {
    "code-search": Path("compatibility/code-search-tools.json"),
    "code-graph": Path("compatibility/code-graph-tools.json"),
}
CODE_SEARCH_GIT_REPOSITORY = (
    "https://github.com/redacted-org/code-search.git"
)
CODE_SEARCH_RELEASE_REPOSITORY = "redacted-org/code-search"
CODE_SEARCH_RELEASE_SIGNER_WORKFLOW = (
    "redacted-org/code-search/.github/workflows/release.yml"
)
CODE_SEARCH_RELEASE_SOURCE_REF = "refs/heads/main"
CODE_GRAPH_REPOSITORY = "redacted-org/code-graph"
CODE_GRAPH_RELEASE_SIGNER_WORKFLOW = (
    "redacted-org/code-graph/.github/workflows/release.yml"
)
CODE_GRAPH_RELEASE_SOURCE_REF = "refs/heads/main"
GRAPH_ASSET_NAMES = {
    "darwin-amd64": "codebase-memory-mcp-darwin-amd64.tar.gz",
    "darwin-arm64": "codebase-memory-mcp-darwin-arm64.tar.gz",
    "linux-amd64": "codebase-memory-mcp-linux-amd64.tar.gz",
    "linux-arm64": "codebase-memory-mcp-linux-arm64.tar.gz",
    "windows-amd64": "codebase-memory-mcp-windows-amd64.zip",
}
LOWER_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
LOWER_HEX_COMMIT = re.compile(r"[0-9a-f]{40}")
FINGERPRINT = {
    "algorithm": "sha256",
    "canonicalization": "UTF-8 JSON with sorted keys and separators comma/colon",
    "document": "inputSchema",
}
READINESS_REQUIREMENTS = {
    "index_identity": {
        "schema_version": 1,
        "required_fields": [
            "repository_id",
            "checkout_id",
            "source_revision",
            "dirty_fingerprint",
            "index_generation",
            "captured_at",
        ],
        "equal_fields": [
            "repository_id",
            "checkout_id",
            "source_revision",
            "dirty_fingerprint",
            "index_generation",
        ],
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
RUNTIME_ENV_ALLOWLIST = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATHEXT",
    "SYSTEMROOT",
    "TZ",
    "WINDIR",
}


class CaptureError(RuntimeError):
    """The candidate or live MCP contract is unsafe to capture."""


def _json_bytes(document: dict) -> bytes:
    try:
        rendered = json.dumps(
            document,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CaptureError(f"document is not valid JSON: {exc}") from exc
    return (rendered + "\n").encode("utf-8")


def _load_candidate(path: Path) -> dict:
    try:
        candidate = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise CaptureError(f"{path}: cannot load candidate BOM: {exc}") from exc
    if not isinstance(candidate, dict):
        raise CaptureError(f"{path}: candidate BOM must be an object")
    if (
        isinstance(candidate.get("schema_version"), bool)
        or candidate.get("schema_version") != 1
    ):
        raise CaptureError(f"{path}: candidate BOM must use schema_version 1")
    components = candidate.get("components")
    if not isinstance(components, dict) or set(components) != COMPONENTS:
        raise CaptureError(
            f"{path}: components must exactly match "
            + ", ".join(sorted(COMPONENTS))
        )
    readiness = candidate.get("integrated_readiness")
    if not isinstance(readiness, dict):
        raise CaptureError(f"{path}: integrated_readiness must be an object")
    if readiness.get("status") not in {"blocked", "ready"}:
        raise CaptureError(
            f"{path}: integrated_readiness.status must be blocked or ready"
        )
    if readiness.get("requires") != READINESS_REQUIREMENTS:
        raise CaptureError(
            f"{path}: integrated_readiness.requires must preserve every "
            "readiness gate"
        )

    for component in sorted(COMPONENTS):
        details = components[component]
        if not isinstance(details, dict):
            raise CaptureError(f"{component}: component details must be an object")
        expected_snapshot = str(SNAPSHOT_PATHS[component])
        if details.get("schema_snapshot") != expected_snapshot:
            raise CaptureError(
                f"{component}: schema_snapshot must be {expected_snapshot}"
            )
        _source_for(component, details)
    return candidate


def _source_for(component: str, details: dict) -> dict[str, str]:
    install = details.get("install")
    if not isinstance(install, dict):
        raise CaptureError(f"{component}: missing install object")
    try:
        validate_install_descriptor_shape(component, install)
    except DescriptorError as exc:
        raise CaptureError(f"{component}: {exc}") from exc
    if component == "code-search":
        kind = install.get("kind")
        if kind == "git":
            if install.get("repository") != CODE_SEARCH_GIT_REPOSITORY:
                raise CaptureError(
                    "code-search: Git repository must be "
                    f"{CODE_SEARCH_GIT_REPOSITORY}"
                )
            version = install.get("revision")
            if (
                not isinstance(version, str)
                or LOWER_HEX_COMMIT.fullmatch(version) is None
            ):
                raise CaptureError(
                    "code-search: revision must be a full 40-character "
                    "lowercase hex commit"
                )
        elif kind == "github-release":
            _validate_code_search_release(install)
            version = install["tag"]
        else:
            raise CaptureError(
                "code-search: install kind must be git or github-release"
            )
    else:
        _validate_code_graph_release(install)
        version = install.get("tag")
        kind = "github-release"
    return {"kind": kind, "version": version}


def _safe_asset_name(value, suffix: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "/" not in value
        and "\\" not in value
        and all(ord(character) >= 32 for character in value)
        and value.endswith(suffix)
    )


def _validate_code_search_release(install: dict) -> None:
    if install.get("repository") != CODE_SEARCH_RELEASE_REPOSITORY:
        raise CaptureError(
            "code-search: release repository must be "
            f"{CODE_SEARCH_RELEASE_REPOSITORY}"
        )
    tag = install.get("tag")
    if (
        not isinstance(tag, str)
        or re.fullmatch(r"v[0-9][0-9A-Za-z._+-]*", tag) is None
    ):
        raise CaptureError(
            "code-search: release tag must be a safe version beginning with v"
        )
    source_revision = install.get("source_revision")
    if (
        not isinstance(source_revision, str)
        or LOWER_HEX_COMMIT.fullmatch(source_revision) is None
    ):
        raise CaptureError(
            "code-search: release source_revision must be a full "
            "40-character lowercase hex commit"
        )
    asset = install.get("asset")
    release_version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else ""
    if (
        not isinstance(asset, dict)
        or not _safe_asset_name(asset.get("name"), ".whl")
        or asset["name"]
        != f"redacted_code_search-{release_version}-py3-none-any.whl"
        or not isinstance(asset.get("sha256"), str)
        or LOWER_HEX_SHA256.fullmatch(asset["sha256"]) is None
    ):
        raise CaptureError(
            "code-search: release asset must name a safe wheel and pinned SHA-256"
        )
    checksums = install.get("checksums")
    if (
        not isinstance(checksums, dict)
        or checksums.get("name") != "SHA256SUMS"
        or not isinstance(checksums.get("sha256"), str)
        or LOWER_HEX_SHA256.fullmatch(checksums["sha256"]) is None
    ):
        raise CaptureError(
            "code-search: checksums must name SHA256SUMS with a pinned SHA-256"
        )
    attestation = install.get("attestation")
    bundle = (
        attestation.get("bundle") if isinstance(attestation, dict) else None
    )
    if (
        not isinstance(bundle, dict)
        or bundle.get("name")
        != f"redacted_code_search-{release_version}-provenance.jsonl"
        or not isinstance(bundle.get("sha256"), str)
        or LOWER_HEX_SHA256.fullmatch(bundle["sha256"]) is None
    ):
        raise CaptureError(
            "code-search: attestation bundle must name a safe JSONL "
            "asset and pinned SHA-256"
        )
    signer_workflow = attestation.get("signer_workflow")
    if signer_workflow != CODE_SEARCH_RELEASE_SIGNER_WORKFLOW:
        raise CaptureError(
            "code-search: attestation signer_workflow must be "
            f"{CODE_SEARCH_RELEASE_SIGNER_WORKFLOW}"
        )
    source_ref = attestation.get("source_ref")
    if source_ref != CODE_SEARCH_RELEASE_SOURCE_REF:
        raise CaptureError(
            "code-search: attestation source_ref must be "
            f"{CODE_SEARCH_RELEASE_SOURCE_REF}"
        )
    if attestation.get("deny_self_hosted_runners") is not True:
        raise CaptureError(
            "code-search: attestation deny_self_hosted_runners must be true"
        )


def _validate_code_graph_release(install: dict) -> None:
    if install.get("kind") != "github-release":
        raise CaptureError("code-graph: install kind must be github-release")
    if install.get("repository") != CODE_GRAPH_REPOSITORY:
        raise CaptureError(
            f"code-graph: repository must be {CODE_GRAPH_REPOSITORY}"
        )
    tag = install.get("tag")
    if (
        not isinstance(tag, str)
        or re.fullmatch(r"v[0-9][0-9A-Za-z._+-]*", tag) is None
    ):
        raise CaptureError(
            "code-graph: release tag must be a safe version beginning with v"
        )
    source_revision = install.get("source_revision")
    if (
        not isinstance(source_revision, str)
        or LOWER_HEX_COMMIT.fullmatch(source_revision) is None
    ):
        raise CaptureError(
            "code-graph: source_revision must be a full 40-character "
            "lowercase hex commit"
        )

    assets = install.get("assets")
    if not isinstance(assets, dict) or set(assets) != set(GRAPH_ASSET_NAMES):
        raise CaptureError(
            "code-graph: assets must exactly match "
            + ", ".join(sorted(GRAPH_ASSET_NAMES))
        )
    for platform, expected_name in GRAPH_ASSET_NAMES.items():
        asset = assets.get(platform)
        if not isinstance(asset, dict):
            raise CaptureError(f"code-graph: asset {platform} must be an object")
        if asset.get("name") != expected_name:
            raise CaptureError(
                f"code-graph: asset {platform} name must be {expected_name}"
            )
        digest = asset.get("sha256")
        if (
            not isinstance(digest, str)
            or LOWER_HEX_SHA256.fullmatch(digest) is None
        ):
            raise CaptureError(
                f"code-graph: asset {platform} sha256 must be 64 lowercase "
                "hex characters"
            )

    checksums = install.get("checksums")
    if (
        not isinstance(checksums, dict)
        or checksums.get("name") != "checksums.txt"
        or not isinstance(checksums.get("sha256"), str)
        or LOWER_HEX_SHA256.fullmatch(checksums["sha256"]) is None
    ):
        raise CaptureError(
            "code-graph: checksums must name checksums.txt with a pinned SHA-256"
        )
    attestation = install.get("attestation")
    if not isinstance(attestation, dict):
        raise CaptureError("code-graph: attestation must be an object")
    bundle = attestation.get("bundle")
    expected_bundle_path = (
        f"compatibility/attestations/code-graph-{tag}-provenance.jsonl"
    )
    if (
        not isinstance(bundle, dict)
        or bundle.get("path") != expected_bundle_path
        or not isinstance(bundle.get("sha256"), str)
        or LOWER_HEX_SHA256.fullmatch(bundle["sha256"]) is None
    ):
        raise CaptureError(
            "code-graph: attestation bundle must name the tag-bound vendored "
            "JSONL path with a pinned SHA-256"
        )
    if attestation.get("signer_workflow") != CODE_GRAPH_RELEASE_SIGNER_WORKFLOW:
        raise CaptureError(
            "code-graph: attestation signer_workflow must be "
            f"{CODE_GRAPH_RELEASE_SIGNER_WORKFLOW}"
        )
    if attestation.get("source_ref") != CODE_GRAPH_RELEASE_SOURCE_REF:
        raise CaptureError(
            "code-graph: attestation source_ref must be "
            f"{CODE_GRAPH_RELEASE_SOURCE_REF}"
        )
    if attestation.get("deny_self_hosted_runners") is not True:
        raise CaptureError(
            "code-graph: attestation deny_self_hosted_runners must be true"
        )


def _schema_fingerprint(schema: dict) -> str:
    try:
        canonical = json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CaptureError(f"inputSchema is not valid JSON: {exc}") from exc
    return hashlib.sha256(canonical).hexdigest()


def _install_descriptor_copy(details: dict) -> dict:
    """Preserve every validated descriptor field in captured evidence."""
    return deepcopy(details["install"])


def _captured_tools(component: str, live_tools: list[dict]) -> dict[str, dict]:
    if not live_tools:
        raise CaptureError(f"{component}: tools/list returned no tools")
    captured: dict[str, dict] = {}
    for tool in live_tools:
        if not isinstance(tool, dict):
            raise CaptureError(f"{component}: malformed tools/list entry")
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if not isinstance(name, str) or not name:
            raise CaptureError(f"{component}: tool name must be a non-empty string")
        if name in captured:
            raise CaptureError(f"{component}: duplicate tool name '{name}'")
        if not isinstance(schema, dict):
            raise CaptureError(f"{component}: tool '{name}' has malformed inputSchema")
        captured[name] = {
            "input_schema": schema,
            "input_schema_sha256": _schema_fingerprint(schema),
        }
    return {name: captured[name] for name in sorted(captured)}


def _is_optional_boolean(schema: dict, property_name: str) -> bool:
    properties = schema.get("properties")
    required = schema.get("required", [])
    if (
        schema.get("type") != "object"
        or not isinstance(properties, dict)
        or not isinstance(required, list)
    ):
        return False
    property_schema = properties.get(property_name)
    return (
        isinstance(property_schema, dict)
        and property_schema.get("type") == "boolean"
        and property_name not in required
    )


def _capabilities(component: str, tools: dict[str, dict]) -> dict:
    identity = {"supported": False, "schema_version": None, "fields": []}
    if component == "code-search":
        return {
            "outputs": {
                "index_identity": identity,
                "semantic_index_ready": False,
            }
        }

    index_repository = tools.get("index_repository", {}).get("input_schema", {})
    return {
        "inputs": {
            "index_repository.skip_report": _is_optional_boolean(
                index_repository, "skip_report"
            )
        },
        "outputs": {
            "index_identity": identity,
            "graph_status_ready": False,
        },
    }


def _capture_environment(runtime_root: Path) -> dict[str, str]:
    paths = {
        "HOME": runtime_root / "home",
        "USERPROFILE": runtime_root / "home",
        "XDG_CONFIG_HOME": runtime_root / "xdg-config",
        "XDG_CACHE_HOME": runtime_root / "xdg-cache",
        "XDG_DATA_HOME": runtime_root / "xdg-data",
        "CODE_SEARCH_STORAGE": runtime_root / "code-search-storage",
        "TMPDIR": runtime_root / "tmp",
        "TEMP": runtime_root / "tmp",
        "TMP": runtime_root / "tmp",
    }
    for path in set(paths.values()):
        path.mkdir(parents=True, exist_ok=True)

    environment = {
        name: os.environ[name]
        for name in RUNTIME_ENV_ALLOWLIST
        if name in os.environ
    }
    environment.update(
        {
            "PATH": os.defpath,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            **{name: str(path) for name, path in paths.items()},
        }
    )
    return environment


def _capture(
    candidate: dict,
    servers: dict[str, str],
    timeout: float,
    runtime_env: dict[str, str],
) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    proposed_bom = {
        "schema_version": 1,
        "integrated_readiness": {
            "status": "blocked",
            "reason": (
                "Input schemas were captured offline; output behavior and integrated "
                "readiness require separate runtime evidence."
            ),
            "requires": deepcopy(READINESS_REQUIREMENTS),
        },
        "components": {},
    }
    if "tested_at" in candidate:
        proposed_bom["tested_at"] = candidate["tested_at"]

    for component in sorted(COMPONENTS):
        details = candidate["components"][component]
        captured_install = _install_descriptor_copy(details)
        tools = _captured_tools(
            component,
            list_tools(servers[component], timeout, env=runtime_env),
        )
        capabilities = _capabilities(component, tools)
        snapshot = {
            "component": component,
            "fingerprint": FINGERPRINT,
            "schema_version": 1,
            "source": {
                **_source_for(component, details),
                "install_descriptor_sha256": install_descriptor_sha256(
                    captured_install
                ),
            },
            "tested_capabilities": capabilities,
            "tools": tools,
        }
        snapshots[component] = snapshot
        proposed_bom["components"][component] = {
            "install": captured_install,
            "schema_snapshot": str(SNAPSHOT_PATHS[component]),
            "tested_capabilities": capabilities,
        }

    return {"component-bom.json": proposed_bom, **{
        str(SNAPSHOT_PATHS[component]): snapshots[component]
        for component in sorted(COMPONENTS)
    }}


def _summary(documents: dict[str, dict], written: bool) -> dict:
    return {
        "files": {
            name: hashlib.sha256(_json_bytes(document)).hexdigest()
            for name, document in sorted(documents.items())
        },
        "tool_counts": {
            component: len(documents[str(SNAPSHOT_PATHS[component])]["tools"])
            for component in sorted(COMPONENTS)
        },
        "written": written,
    }


def _write_transactionally(output: Path, documents: dict[str, dict]) -> None:
    if os.path.lexists(output):
        raise CaptureError(f"{output}: output directory already exists")
    if not output.parent.is_dir():
        raise CaptureError(f"{output.parent}: output parent is not a directory")

    staging = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
    )
    try:
        for relative, document in documents.items():
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_json_bytes(document))
        os.replace(staging, output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture deterministic offline MCP input-schema contracts"
    )
    parser.add_argument("--component-bom", type=Path, required=True)
    parser.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="COMPONENT=EXECUTABLE",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--write",
        action="store_true",
        help="atomically create the output directory (default is preview only)",
    )
    args = parser.parse_args()

    try:
        candidate = _load_candidate(args.component_bom)
        servers = parse_servers(args.server)
        if set(servers) != COMPONENTS:
            raise CaptureError(
                "servers must exactly match components: "
                + ", ".join(sorted(COMPONENTS))
            )
        for component, command in servers.items():
            executable = Path(command)
            if not executable.is_absolute():
                raise CaptureError(
                    f"{component}: server executable path must be absolute"
                )
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise CaptureError(
                    f"{component}: server executable is missing or not executable: "
                    f"{executable}"
                )
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise CaptureError("timeout must be a finite positive number")
        with tempfile.TemporaryDirectory(
            prefix="codebase-contract-capture-runtime-"
        ) as runtime:
            documents = _capture(
                candidate,
                servers,
                args.timeout,
                runtime_env=_capture_environment(Path(runtime)),
            )
        if args.write:
            _write_transactionally(args.output_dir, documents)
        print(json.dumps(_summary(documents, args.write), sort_keys=True))
    except (CaptureError, ContractError, OSError) as exc:
        print(f"Component contract capture FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
