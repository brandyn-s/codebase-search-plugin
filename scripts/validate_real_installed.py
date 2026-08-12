#!/usr/bin/env python3
"""Install the exact private BOM components and validate their real MCP schemas."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from component_descriptor import (
    DescriptorError,
    validate_install_descriptor_shape,
)

ROOT = Path(__file__).resolve().parent.parent
SUBPROCESS_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
RUNTIME_OPERATION_ENV_ALLOWLIST = (
    *SUBPROCESS_ENV_ALLOWLIST,
    "HOME",
    "PYTHONUNBUFFERED",
    "CODE_SEARCH_STORAGE",
    "RUNNER_TEMP",
)


class RealInstallError(RuntimeError):
    """The real component installation cannot be validated safely."""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--component-bom",
        default=str(ROOT / "component-bom.json"),
        help="exact candidate component BOM to install and validate",
    )
    parser.add_argument(
        "--contract-evidence-output",
        help="write captured installed-component contracts beneath RUNNER_TEMP",
    )
    parser.add_argument(
        "--readiness-evidence-output",
        help="copy validated live readiness evidence to this RUNNER_TEMP path",
    )
    return parser.parse_args(argv)


def run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> None:
    """Run one external command without logging secret-bearing environment values."""
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def resolve_readiness_evidence_output(
    requested: str | None,
    runner_temp: Path,
) -> Path | None:
    """Resolve an optional evidence path without allowing RUNNER_TEMP escape."""
    return resolve_runner_temp_output(
        requested,
        runner_temp,
        "readiness evidence output",
    )


def resolve_runner_temp_output(
    requested: str | None,
    runner_temp: Path,
    label: str,
) -> Path | None:
    """Resolve an optional output without allowing RUNNER_TEMP escape."""
    if requested is None:
        return None
    resolved_runner_temp = runner_temp.resolve()
    output = Path(requested).resolve()
    if output == resolved_runner_temp or not output.is_relative_to(
        resolved_runner_temp
    ):
        raise RealInstallError(f"{label} must be beneath RUNNER_TEMP")
    return output


def require_environment() -> tuple[str, Path]:
    token = (
        os.environ.get("CODE_INTEL_COMPONENT_TOKEN", "").strip()
        or os.environ.get("GH_TOKEN", "").strip()
    )
    if not token:
        raise RealInstallError(
            "CODE_INTEL_COMPONENT_TOKEN is required for trusted main/manual "
            "component validation"
        )

    runner_temp_raw = os.environ.get("RUNNER_TEMP", "").strip()
    if not runner_temp_raw:
        raise RealInstallError("RUNNER_TEMP is required for isolated installation")
    runner_temp = Path(runner_temp_raw).resolve()
    if not runner_temp.is_dir():
        raise RealInstallError(f"RUNNER_TEMP is not a directory: {runner_temp}")

    for executable in ("curl", "gh", "git"):
        if shutil.which(executable) is None:
            raise RealInstallError(f"required executable is unavailable: {executable}")
    return token, runner_temp


def build_subprocess_environments(
    token: str,
    source: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Separate authenticated fetches from secret-free build/runtime work."""
    candidate = os.environ if source is None else source
    runtime_env = allowlisted_runtime_environment(candidate)
    fetch_env = {**runtime_env, "GH_TOKEN": token}
    return fetch_env, runtime_env


def build_isolated_runtime_environment(
    source: dict[str, str],
    runner_temp: Path,
) -> dict[str, str]:
    """Provide secret-free writable state without inheriting the runner home."""
    resolved_runner_temp = runner_temp.resolve()
    created_runtime_root = Path(
        tempfile.mkdtemp(
            prefix="code-intel-runtime-",
            dir=resolved_runner_temp,
        )
    )
    runtime_root = created_runtime_root.resolve()
    if (
        runtime_root == resolved_runner_temp
        or not runtime_root.is_relative_to(resolved_runner_temp)
        or created_runtime_root.is_symlink()
        or not runtime_root.is_dir()
    ):
        raise RealInstallError("isolated runtime root escaped RUNNER_TEMP")
    runtime_home = runtime_root / "home"
    code_search_storage = runtime_root / "code-search-storage"
    for directory in (runtime_home, code_search_storage):
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    return {
        **allowlisted_runtime_environment(source),
        "RUNNER_TEMP": str(resolved_runner_temp),
        "HOME": str(runtime_home),
        "CODE_SEARCH_STORAGE": str(code_search_storage),
        "PYTHONUNBUFFERED": "1",
    }


def allowlisted_runtime_environment(
    source: dict[str, str],
) -> dict[str, str]:
    """Copy only explicitly safe, non-credential runtime variables."""
    runtime_env = {
        name: source[name]
        for name in RUNTIME_OPERATION_ENV_ALLOWLIST
        if isinstance(source.get(name), str) and source[name]
    }
    runner_temp = runtime_env.get("RUNNER_TEMP")
    for name in ("HOME", "CODE_SEARCH_STORAGE"):
        candidate = runtime_env.get(name)
        if not candidate:
            continue
        if not runner_temp:
            runtime_env.pop(name, None)
            continue
        unresolved_candidate = Path(candidate)
        resolved_candidate = unresolved_candidate.resolve()
        resolved_runner_temp = Path(runner_temp).resolve()
        if (
            unresolved_candidate.is_symlink()
            or not resolved_candidate.is_dir()
            or resolved_candidate == resolved_runner_temp
            or not resolved_candidate.is_relative_to(resolved_runner_temp)
        ):
            runtime_env.pop(name, None)
    return runtime_env


def allowlisted_fetch_environment(source: dict[str, str]) -> dict[str, str]:
    """Copy only transport variables and the explicit GitHub fetch token."""
    fetch_env = allowlisted_runtime_environment(source)
    token = source.get("GH_TOKEN")
    if isinstance(token, str) and token:
        fetch_env["GH_TOKEN"] = token
    return fetch_env


def load_bom(path: Path) -> dict:
    try:
        bom = json.loads(path.read_text(encoding="utf-8"))
        components = bom["components"]
        search = components["code-search"]["install"]
        graph = components["code-graph"]["install"]
        generator = bom["precision_generators"]["go-scip"]
        typescript_generator = bom["precision_generators"]["typescript-scip"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RealInstallError(f"{path}: component BOM is malformed: {exc}") from exc

    try:
        validate_install_descriptor_shape("code-search", search)
        validate_install_descriptor_shape("code-graph", graph)
    except DescriptorError as exc:
        raise RealInstallError(f"{path}: {exc}") from exc
    if not isinstance(generator, dict):
        raise RealInstallError(f"{path}: component BOM is malformed: go-scip")
    if not isinstance(typescript_generator, dict):
        raise RealInstallError(
            f"{path}: component BOM is malformed: typescript-scip"
        )
    return bom


def build_readiness_model(
    destination: Path,
    venv_python: Path,
    runtime_env: dict[str, str],
) -> Path:
    """Build a tiny deterministic local model with the installed dependency set."""
    model = destination / "readiness-model"
    runtime_env = allowlisted_runtime_environment(runtime_env)
    run(
        [
            str(venv_python),
            str(ROOT / "scripts" / "build_readiness_model.py"),
            "--output",
            str(model),
        ],
        env={
            **runtime_env,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        },
    )
    required = (
        model / "modules.json",
        model / "config_sentence_transformers.json",
        model / "0_BoW" / "config.json",
    )
    if any(not path.is_file() for path in required):
        raise RealInstallError("readiness model builder omitted required files")
    return model


def capture_installed_contract_evidence(
    bom_path: Path,
    output: Path,
    venv_python: Path,
    code_search: Path,
    code_graph: Path,
    runtime_env: dict[str, str],
) -> None:
    """Capture schemas from the installed executables under the exact BOM."""
    if output.exists():
        raise RealInstallError(
            f"contract evidence output already exists: {output}"
        )
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RealInstallError(
            f"could not create contract evidence parent: {output.parent}"
        ) from exc
    run(
        [
            str(venv_python),
            str(ROOT / "scripts" / "capture_component_contracts.py"),
            "--component-bom",
            str(bom_path),
            "--server",
            f"code-search={code_search}",
            "--server",
            f"code-graph={code_graph}",
            "--output-dir",
            str(output),
            "--write",
        ],
        env=allowlisted_runtime_environment(runtime_env),
    )
    required = (
        output / "component-bom.json",
        output / "compatibility" / "code-search-tools.json",
        output / "compatibility" / "code-graph-tools.json",
    )
    if any(not path.is_file() for path in required):
        raise RealInstallError(
            "component contract capture omitted required evidence files"
        )


def generate_live_readiness_evidence(
    bom: dict,
    bom_path: Path,
    destination: Path,
    venv_python: Path,
    code_search: Path,
    code_graph: Path,
    runtime_env: dict[str, str],
) -> Path | None:
    """Generate readiness evidence using only the just-installed MCP servers."""
    readiness = bom.get("integrated_readiness")
    status = readiness.get("status") if isinstance(readiness, dict) else None
    if status == "blocked":
        return None
    if status != "ready":
        raise RealInstallError(f"unknown integrated readiness status: {status!r}")

    evidence = destination / "live-readiness-evidence.json"
    smoke_env = allowlisted_runtime_environment(runtime_env)
    local_model = build_readiness_model(
        destination,
        venv_python,
        smoke_env,
    )
    run(
        [
            str(venv_python),
            str(ROOT / "scripts" / "generate_live_readiness_evidence.py"),
            "--component-bom",
            str(bom_path),
            "--fixture",
            str(ROOT / "bench" / "e2e" / "target-repo"),
            "--server",
            f"code-search={code_search}",
            "--server",
            f"code-graph={code_graph}",
            "--local-model",
            str(local_model),
            "--output",
            str(evidence),
        ],
        env=smoke_env,
    )
    if not evidence.is_file():
        raise RealInstallError(
            "live smoke generator did not produce readiness evidence"
        )
    return evidence


def validate_and_publish_live_evidence(
    live_evidence: Path | None,
    bom_path: Path,
    readiness_evidence_output: Path | None,
    venv_python: Path,
    runtime_env: dict[str, str],
) -> None:
    """Validate freshly generated evidence before making it publishable."""
    if live_evidence is None:
        if readiness_evidence_output is not None:
            raise RealInstallError(
                "requested output requires live readiness evidence"
            )
        return
    run(
        [
            str(venv_python),
            str(ROOT / "scripts" / "validate_plugin.py"),
            "--component-bom",
            str(bom_path),
        ],
        env={
            **allowlisted_runtime_environment(runtime_env),
            "CODE_INTEL_READINESS_EVIDENCE_OVERRIDE": str(live_evidence),
        },
    )
    if readiness_evidence_output is not None:
        readiness_evidence_output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(live_evidence, readiness_evidence_output)


def verify_sha256(archive: Path, expected: str) -> None:
    """Fail closed unless an existing archive matches a pinned SHA-256."""
    if not archive.is_file():
        raise RealInstallError(f"downloaded release asset is missing: {archive}")
    if (
        not isinstance(expected, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None
    ):
        raise RealInstallError(
            f"pinned SHA-256 is missing or invalid for {archive.name}"
        )

    digest = hashlib.sha256()
    try:
        with archive.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RealInstallError(
            f"could not verify SHA-256 for {archive.name}: {exc}"
        ) from exc
    actual = digest.hexdigest()
    if actual.lower() != expected.lower():
        raise RealInstallError(
            f"checksum mismatch for {archive.name}: "
            f"expected {expected.lower()}, got {actual}"
        )


def verify_checksum_manifest(
    manifest: Path,
    artifact_name: str,
    expected_sha256: str,
) -> None:
    """Require exactly one manifest entry binding the artifact name and digest."""
    if (
        not manifest.is_file()
        or not isinstance(artifact_name, str)
        or Path(artifact_name).name != artifact_name
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
    ):
        raise RealInstallError("checksum manifest inputs are invalid")
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise RealInstallError(f"checksum manifest is unreadable: {exc}") from exc

    matches: list[str] = []
    for line in lines:
        parsed = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?(.+)", line)
        if parsed is None:
            if line.strip():
                raise RealInstallError("checksum manifest contains a malformed entry")
            continue
        digest, name = parsed.groups()
        if name == artifact_name:
            matches.append(digest.lower())
    if matches != [expected_sha256]:
        raise RealInstallError(
            "checksum manifest does not contain exactly one matching artifact entry"
        )


def install_code_search_git(
    install: dict,
    destination: Path,
    fetch_env: dict[str, str],
    runtime_env: dict[str, str],
) -> tuple[Path, Path]:
    fetch_env = allowlisted_fetch_environment(fetch_env)
    runtime_env = allowlisted_runtime_environment(runtime_env)
    repository = install.get("repository")
    revision = install.get("revision")
    if not isinstance(repository, str) or not isinstance(revision, str):
        raise RealInstallError("code-search BOM entry lacks repository/revision")

    source = destination / "code-search-source"
    run(
        [
            "gh",
            "repo",
            "clone",
            repository,
            str(source),
            "--",
            "--no-checkout",
        ],
        env=fetch_env,
    )
    run(
        ["git", "-C", str(source), "checkout", "--detach", revision],
        env=runtime_env,
    )
    actual_revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        env=runtime_env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_revision != revision:
        raise RealInstallError(
            f"code-search revision mismatch: expected {revision}, got {actual_revision}"
        )

    venv = destination / "code-search-venv"
    run([sys.executable, "-m", "venv", str(venv)], env=runtime_env)
    venv_python = venv / "bin" / "python"
    run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            str(source),
        ],
        env=runtime_env,
    )
    executable = venv / "bin" / "code-search-mcp"
    if not executable.is_file():
        raise RealInstallError(f"installed code-search MCP is missing: {executable}")
    return venv_python, executable


def release_asset(
    document: object,
    *,
    component: str,
    suffix: str,
) -> tuple[str, str]:
    if not isinstance(document, dict):
        raise RealInstallError(f"{component} release asset metadata is missing")
    name = document.get("name")
    sha256 = document.get("sha256")
    if (
        not isinstance(name, str)
        or not name
        or Path(name).name != name
        or "/" in name
        or "\\" in name
        or not name.endswith(suffix)
    ):
        raise RealInstallError(f"{component} release asset name is invalid")
    if not isinstance(sha256, str) or re.fullmatch(
        r"[0-9a-f]{64}", sha256
    ) is None:
        raise RealInstallError(f"{component} release asset SHA-256 is invalid")
    return name, sha256


def github_api_json(endpoint: str, fetch_env: dict[str, str]) -> dict:
    fetch_env = allowlisted_fetch_environment(fetch_env)
    completed = subprocess.run(
        ["gh", "api", "--method", "GET", endpoint],
        env=fetch_env,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RealInstallError(
            f"GitHub API returned malformed JSON for {endpoint}"
        ) from exc
    if not isinstance(document, dict):
        raise RealInstallError(
            f"GitHub API returned a non-object response for {endpoint}"
        )
    return document


def resolve_release_tag_commit(
    repository: str,
    tag: str,
    fetch_env: dict[str, str],
) -> str:
    """Resolve a lightweight or annotated release tag to its final commit."""
    document = github_api_json(
        f"repos/{repository}/git/ref/tags/{tag}",
        fetch_env,
    )
    visited: set[str] = set()
    for _depth in range(16):
        target = document.get("object")
        if not isinstance(target, dict):
            raise RealInstallError("GitHub tag response has no object target")
        target_type = target.get("type")
        target_sha = target.get("sha")
        if not isinstance(target_sha, str) or re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", target_sha
        ) is None:
            raise RealInstallError("GitHub tag response has an invalid object SHA")
        if target_type == "commit":
            return target_sha
        if target_type != "tag":
            raise RealInstallError(
                f"GitHub tag resolves to unsupported object type: {target_type!r}"
            )
        if target_sha in visited:
            raise RealInstallError("GitHub annotated tag chain contains a cycle")
        visited.add(target_sha)
        document = github_api_json(
            f"repos/{repository}/git/tags/{target_sha}",
            fetch_env,
        )
    raise RealInstallError("GitHub annotated tag chain exceeds 16 objects")


def install_code_search_release(
    install: dict,
    destination: Path,
    fetch_env: dict[str, str],
    runtime_env: dict[str, str],
) -> tuple[Path, Path]:
    fetch_env = allowlisted_fetch_environment(fetch_env)
    runtime_env = allowlisted_runtime_environment(runtime_env)
    repository = install.get("repository")
    tag = install.get("tag")
    source_revision = install.get("source_revision")
    attestation = install.get("attestation")
    if (
        repository != "redacted-org/code-search"
        or not isinstance(tag, str)
        or not tag.startswith("v")
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_revision or "")
        is None
        or not isinstance(attestation, dict)
    ):
        raise RealInstallError("code-search release BOM metadata is invalid")

    wheel_name, wheel_sha256 = release_asset(
        install.get("asset"),
        component="code-search wheel",
        suffix=".whl",
    )
    checksums_name, checksums_sha256 = release_asset(
        install.get("checksums"),
        component="code-search checksums",
        suffix="",
    )
    bundle_name, bundle_sha256 = release_asset(
        attestation.get("bundle"),
        component="code-search attestation bundle",
        suffix=".jsonl",
    )
    signer_workflow = attestation.get("signer_workflow")
    source_ref = attestation.get("source_ref")
    version = tag.removeprefix("v")
    if (
        wheel_name != f"redacted_code_search-{version}-py3-none-any.whl"
        or checksums_name != "SHA256SUMS"
        or bundle_name != f"redacted_code_search-{version}-provenance.jsonl"
        or signer_workflow
        != (
            "redacted-org/code-search/"
            ".github/workflows/release.yml"
        )
        or source_ref != "refs/heads/main"
        or attestation.get("deny_self_hosted_runners") is not True
    ):
        raise RealInstallError("code-search attestation policy is invalid")

    resolved_revision = resolve_release_tag_commit(repository, tag, fetch_env)
    if resolved_revision != source_revision:
        raise RealInstallError(
            "code-search tag source revision mismatch: "
            f"expected {source_revision}, got {resolved_revision}"
        )

    downloads = destination / "code-search-download"
    downloads.mkdir()
    run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            wheel_name,
            "--pattern",
            bundle_name,
            "--pattern",
            checksums_name,
            "--dir",
            str(downloads),
            "--clobber",
        ],
        env=fetch_env,
    )
    wheel = downloads / wheel_name
    bundle = downloads / bundle_name
    checksums = downloads / checksums_name
    verify_sha256(wheel, wheel_sha256)
    verify_sha256(bundle, bundle_sha256)
    verify_sha256(checksums, checksums_sha256)
    verify_checksum_manifest(checksums, wheel_name, wheel_sha256)
    run(
        [
            "gh",
            "attestation",
            "verify",
            wheel_name,
            "--bundle",
            bundle_name,
            "--repo",
            repository,
            "--signer-workflow",
            signer_workflow,
            "--source-digest",
            source_revision,
            "--source-ref",
            source_ref,
            "--deny-self-hosted-runners",
        ],
        env=runtime_env,
        cwd=downloads,
    )

    venv = destination / "code-search-venv"
    run([sys.executable, "-m", "venv", str(venv)], env=runtime_env)
    venv_python = venv / "bin" / "python"
    run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "--force-reinstall",
            str(wheel),
        ],
        env=runtime_env,
    )
    run(
        [
            str(venv_python),
            str(ROOT / "scripts" / "verify_code_search_wheel.py"),
            tag,
            "--asset-name",
            wheel_name,
            "--sha256",
            wheel_sha256,
        ],
        env=runtime_env,
    )
    executable = venv / "bin" / "code-search-mcp"
    if not executable.is_file():
        raise RealInstallError(f"installed code-search MCP is missing: {executable}")
    return venv_python, executable


def install_code_search(
    install: dict,
    destination: Path,
    fetch_env: dict[str, str],
    runtime_env: dict[str, str],
) -> tuple[Path, Path]:
    kind = install.get("kind")
    if kind == "git":
        return install_code_search_git(
            install,
            destination,
            fetch_env,
            runtime_env,
        )
    if kind == "github-release":
        return install_code_search_release(
            install,
            destination,
            fetch_env,
            runtime_env,
        )
    raise RealInstallError(f"unsupported code-search install kind: {kind!r}")


def linux_asset(install: dict) -> tuple[str, str]:
    if platform.system().lower() != "linux":
        raise RealInstallError("real installed-schema CI helper supports Linux only")
    architecture = platform.machine().lower()
    normalized = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64"}.get(
        architecture
    )
    if normalized is None:
        raise RealInstallError(f"unsupported Linux architecture: {architecture}")
    platform_key = f"linux-{normalized}"
    assets = install.get("assets")
    asset = assets.get(platform_key) if isinstance(assets, dict) else None
    if not isinstance(asset, dict):
        raise RealInstallError(f"code-graph BOM lacks asset for {platform_key}")
    name = asset.get("name")
    sha256 = asset.get("sha256")
    if not isinstance(name, str) or not name:
        raise RealInstallError(
            f"code-graph BOM asset name is missing for {platform_key}"
        )
    if not isinstance(sha256, str):
        raise RealInstallError(
            f"code-graph BOM asset SHA-256 is missing for {platform_key}"
        )
    return name, sha256


def linux_go_scip_asset(generator: dict) -> tuple[str, str, str]:
    """Return the exact Linux scip-go archive and binary digests."""
    if platform.system().lower() != "linux":
        raise RealInstallError("trusted Go SCIP validation supports Linux only")
    architecture = platform.machine().lower()
    normalized = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64"}.get(
        architecture
    )
    if normalized is None:
        raise RealInstallError(f"unsupported Linux architecture: {architecture}")
    platform_key = f"linux-{normalized}"
    assets = generator.get("assets")
    asset = assets.get(platform_key) if isinstance(assets, dict) else None
    if not isinstance(asset, dict):
        raise RealInstallError(f"go-scip BOM lacks asset for {platform_key}")
    name = asset.get("name")
    archive_sha256 = asset.get("archive_sha256")
    binary_sha256 = asset.get("binary_sha256")
    if name != f"scip-go-{platform_key}.tar.gz":
        raise RealInstallError(f"go-scip BOM asset name is invalid for {platform_key}")
    for label, digest in (
        ("archive", archive_sha256),
        ("binary", binary_sha256),
    ):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RealInstallError(
                f"go-scip BOM {label} SHA-256 is invalid for {platform_key}"
            )
    return name, archive_sha256, binary_sha256


def install_go_scip(
    generator: dict,
    bom_path: Path,
    destination: Path,
    fetch_env: dict[str, str],
    runtime_env: dict[str, str],
) -> Path:
    """Install and verify the optional compiler indexer used by auto precision."""
    fetch_env = allowlisted_fetch_environment(fetch_env)
    runtime_env = allowlisted_runtime_environment(runtime_env)
    repository = generator.get("repository")
    tag = generator.get("tag")
    source_revision = generator.get("source_revision")
    version = generator.get("version_output")
    if (
        generator.get("kind") != "github-release"
        or repository != "scip-code/scip-go"
        or not isinstance(tag, str)
        or re.fullmatch(r"v[0-9][0-9A-Za-z._+-]*", tag) is None
        or re.fullmatch(r"[0-9a-f]{40}", source_revision or "") is None
        or not isinstance(version, str)
        or not version
    ):
        raise RealInstallError("go-scip release BOM metadata is invalid")

    archive_name, archive_sha256, binary_sha256 = linux_go_scip_asset(generator)
    resolved_revision = resolve_release_tag_commit(repository, tag, fetch_env)
    if resolved_revision != source_revision:
        raise RealInstallError(
            "go-scip tag source revision mismatch: "
            f"expected {source_revision}, got {resolved_revision}"
        )

    downloads = destination / "go-scip-download"
    downloads.mkdir()
    run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            archive_name,
            "--dir",
            str(downloads),
            "--clobber",
        ],
        env=fetch_env,
    )
    archive_path = downloads / archive_name
    verify_sha256(archive_path, archive_sha256)

    extracted = destination / "go-scip-extracted"
    extracted.mkdir()
    with tarfile.open(archive_path, "r:gz") as bundle:
        if any(
            member.issym() or member.islnk() or member.isdev()
            for member in bundle.getmembers()
        ):
            raise RealInstallError("go-scip archive contains an unsafe member")
        bundle.extractall(extracted, filter="data")
    matches = [
        path
        for path in extracted.rglob("scip-go")
        if path.is_file() and not path.is_symlink()
    ]
    if len(matches) != 1:
        raise RealInstallError(f"expected one scip-go binary, found {len(matches)}")
    executable = matches[0]
    verify_sha256(executable, binary_sha256)
    executable.chmod(executable.stat().st_mode | 0o111)
    run(
        [
            sys.executable,
            str(ROOT / "scripts" / "prepare_scip_index.py"),
            "verify",
            "--generator",
            str(executable),
            "--component-bom",
            str(bom_path),
        ],
        env=runtime_env,
    )
    return executable


def linux_typescript_scip_asset(generator: dict) -> tuple[str, str, str]:
    """Return the exact Linux Node archive and binary digests."""
    if platform.system().lower() != "linux":
        raise RealInstallError("trusted TypeScript SCIP validation supports Linux only")
    architecture = platform.machine().lower()
    normalized = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64"}.get(
        architecture
    )
    if normalized is None:
        raise RealInstallError(f"unsupported Linux architecture: {architecture}")
    platform_key = f"linux-{normalized}"
    runtime = generator.get("node_runtime")
    assets = runtime.get("assets") if isinstance(runtime, dict) else None
    asset = assets.get(platform_key) if isinstance(assets, dict) else None
    if not isinstance(asset, dict):
        raise RealInstallError(
            f"typescript-scip BOM lacks Node asset for {platform_key}"
        )
    version = runtime.get("version")
    node_arch = "x64" if normalized == "amd64" else "arm64"
    expected_name = f"node-{version}-linux-{node_arch}.tar.xz"
    name = asset.get("name")
    archive_sha256 = asset.get("archive_sha256")
    binary_sha256 = asset.get("binary_sha256")
    if name != expected_name:
        raise RealInstallError(
            f"typescript-scip Node asset name is invalid for {platform_key}"
        )
    for label, digest in (
        ("archive", archive_sha256),
        ("binary", binary_sha256),
    ):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RealInstallError(
                f"typescript-scip Node {label} SHA-256 is invalid for {platform_key}"
            )
    return name, archive_sha256, binary_sha256


def install_typescript_scip(
    generator: dict,
    bom_path: Path,
    destination: Path,
    runtime_env: dict[str, str],
) -> tuple[Path, Path]:
    """Install the pinned Node runtime and lockfile-bound TypeScript indexer."""
    runtime_env = allowlisted_runtime_environment(runtime_env)
    node_runtime = generator.get("node_runtime")
    package_manifest = generator.get("package_manifest")
    lockfile = generator.get("lockfile")
    entrypoint_relative = generator.get("entrypoint")
    if (
        generator.get("kind") != "npm-lockfile"
        or generator.get("package") != "@sourcegraph/scip-typescript"
        or generator.get("source_repository") != "sourcegraph/scip-typescript"
        or generator.get("version_output") != "0.4.0"
        or re.fullmatch(r"[0-9a-f]{40}", generator.get("source_revision") or "")
        is None
        or not isinstance(generator.get("package_integrity"), str)
        or not generator["package_integrity"].startswith("sha512-")
        or not isinstance(node_runtime, dict)
        or not isinstance(node_runtime.get("version"), str)
        or re.fullmatch(r"v22\.[0-9]+\.[0-9]+", node_runtime["version"]) is None
        or node_runtime.get("base_url")
        != f"https://nodejs.org/download/release/{node_runtime.get('version')}"
        or package_manifest != "compatibility/scip-typescript-package.json"
        or lockfile != "compatibility/scip-typescript-package-lock.json"
        or entrypoint_relative
        != "node_modules/@sourcegraph/scip-typescript/dist/src/main.js"
    ):
        raise RealInstallError("typescript-scip BOM metadata is invalid")

    manifest_path = ROOT / package_manifest
    lockfile_path = ROOT / lockfile
    if not manifest_path.is_file() or not lockfile_path.is_file():
        raise RealInstallError("typescript-scip package inputs are missing")
    verify_sha256(lockfile_path, generator.get("lockfile_sha256"))
    archive_name, archive_sha256, binary_sha256 = linux_typescript_scip_asset(
        generator
    )
    archive_path = destination / archive_name
    run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            f"{node_runtime['base_url']}/{archive_name}",
            "--output",
            str(archive_path),
        ],
        env=runtime_env,
    )
    verify_sha256(archive_path, archive_sha256)

    extracted = destination / "typescript-node-extracted"
    extracted.mkdir()
    with tarfile.open(archive_path, "r:xz") as bundle:
        if any(member.islnk() or member.isdev() for member in bundle.getmembers()):
            raise RealInstallError("Node archive contains an unsafe member")
        # Official Node archives contain relative symlinks such as bin/npm.
        # Python's data filter accepts only links that resolve beneath the
        # extraction destination, so retain those while rejecting hard links
        # and device members explicitly above.
        bundle.extractall(extracted, filter="data")
    node_matches = [
        path
        for path in extracted.glob("node-*/bin/node")
        if path.is_file() and not path.is_symlink()
    ]
    npm_matches = [
        path
        for path in extracted.glob("node-*/lib/node_modules/npm/bin/npm-cli.js")
        if path.is_file() and not path.is_symlink()
    ]
    if len(node_matches) != 1 or len(npm_matches) != 1:
        raise RealInstallError("Node archive omitted the exact runtime or npm CLI")
    node = node_matches[0]
    npm_cli = npm_matches[0]
    verify_sha256(node, binary_sha256)
    node.chmod(node.stat().st_mode | 0o111)

    package_root = destination / "typescript-scip-package"
    package_root.mkdir()
    shutil.copyfile(manifest_path, package_root / "package.json")
    shutil.copyfile(lockfile_path, package_root / "package-lock.json")
    run(
        [
            str(node),
            str(npm_cli),
            "ci",
            "--prefix",
            str(package_root),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
        env=runtime_env,
    )
    entrypoint = package_root / entrypoint_relative
    verify_sha256(entrypoint, generator.get("entrypoint_sha256"))
    run(
        [
            sys.executable,
            str(ROOT / "scripts" / "prepare_scip_index.py"),
            "verify",
            "--language",
            "typescript",
            "--runtime",
            str(node),
            "--generator",
            str(entrypoint),
            "--component-bom",
            str(bom_path),
        ],
        env=runtime_env,
    )
    return node, entrypoint


def install_code_graph(
    install: dict,
    destination: Path,
    fetch_env: dict[str, str],
    runtime_env: dict[str, str],
) -> Path:
    fetch_env = allowlisted_fetch_environment(fetch_env)
    runtime_env = allowlisted_runtime_environment(runtime_env)
    repository = install.get("repository")
    tag = install.get("tag")
    source_revision = install.get("source_revision")
    attestation = install.get("attestation")
    if (
        repository != "redacted-org/code-graph"
        or not isinstance(tag, str)
        or re.fullmatch(r"v[0-9][0-9A-Za-z._+-]*", tag) is None
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_revision or "")
        is None
        or not isinstance(attestation, dict)
    ):
        raise RealInstallError("code-graph release BOM metadata is invalid")
    signer_workflow = attestation.get("signer_workflow")
    source_ref = attestation.get("source_ref")
    bundle = attestation.get("bundle")
    expected_bundle_path = (
        f"compatibility/attestations/code-graph-{tag}-provenance.jsonl"
    )
    bundle_path = bundle.get("path") if isinstance(bundle, dict) else None
    bundle_sha256 = bundle.get("sha256") if isinstance(bundle, dict) else None
    if (
        signer_workflow
        != "redacted-org/code-graph/.github/workflows/release.yml"
        or source_ref != "refs/heads/main"
        or attestation.get("deny_self_hosted_runners") is not True
        or bundle_path != expected_bundle_path
        or not isinstance(bundle_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", bundle_sha256) is None
    ):
        raise RealInstallError("code-graph attestation policy is invalid")
    vendored_bundle = ROOT / bundle_path
    verify_sha256(vendored_bundle, bundle_sha256)

    asset_name, expected_sha256 = linux_asset(install)
    checksums_name, checksums_sha256 = release_asset(
        install.get("checksums"),
        component="code-graph checksums",
        suffix=".txt",
    )
    if checksums_name != "checksums.txt":
        raise RealInstallError("code-graph checksums asset must be checksums.txt")

    resolved_revision = resolve_release_tag_commit(repository, tag, fetch_env)
    if resolved_revision != source_revision:
        raise RealInstallError(
            "code-graph tag source revision mismatch: "
            f"expected {source_revision}, got {resolved_revision}"
        )

    downloads = destination / "code-graph-download"
    downloads.mkdir()
    run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            asset_name,
            "--pattern",
            checksums_name,
            "--dir",
            str(downloads),
            "--clobber",
        ],
        env=fetch_env,
    )
    archive = downloads / asset_name
    checksums = downloads / checksums_name
    verify_sha256(archive, expected_sha256)
    verify_sha256(checksums, checksums_sha256)
    verify_checksum_manifest(checksums, asset_name, expected_sha256)
    run(
        [
            "gh",
            "attestation",
            "verify",
            asset_name,
            "--bundle",
            str(vendored_bundle),
            "--repo",
            repository,
            "--signer-workflow",
            signer_workflow,
            "--source-digest",
            source_revision,
            "--source-ref",
            source_ref,
            "--deny-self-hosted-runners",
        ],
        env=runtime_env,
        cwd=downloads,
    )

    extracted = destination / "code-graph-extracted"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(extracted, filter="data")
    matches = [
        path
        for path in extracted.rglob("codebase-memory-mcp")
        if path.is_file()
    ]
    if len(matches) != 1:
        raise RealInstallError(
            f"expected one codebase-memory-mcp binary, found {len(matches)}"
        )
    executable = matches[0]
    executable.chmod(executable.stat().st_mode | 0o111)
    return executable


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        token, runner_temp = require_environment()
        readiness_evidence_output = resolve_readiness_evidence_output(
            args.readiness_evidence_output,
            runner_temp,
        )
        contract_evidence_output = resolve_runner_temp_output(
            args.contract_evidence_output,
            runner_temp,
            "contract evidence output",
        )
        fetch_env, runtime_env = build_subprocess_environments(token)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("CODE_INTEL_COMPONENT_TOKEN", None)
        bom_path = Path(args.component_bom).resolve()
        bom = load_bom(bom_path)
        runtime_env = build_isolated_runtime_environment(
            runtime_env,
            runner_temp,
        )
        with tempfile.TemporaryDirectory(
            prefix="codebase-search-real-contract-", dir=runner_temp
        ) as temporary:
            destination = Path(temporary)
            venv_python, code_search = install_code_search(
                bom["components"]["code-search"]["install"],
                destination,
                fetch_env,
                runtime_env,
            )
            code_graph = install_code_graph(
                bom["components"]["code-graph"]["install"],
                destination,
                fetch_env,
                runtime_env,
            )
            install_go_scip(
                bom["precision_generators"]["go-scip"],
                bom_path,
                destination,
                fetch_env,
                runtime_env,
            )
            install_typescript_scip(
                bom["precision_generators"]["typescript-scip"],
                bom_path,
                destination,
                runtime_env,
            )
            run(
                [
                    str(venv_python),
                    str(ROOT / "scripts" / "validate_installed.py"),
                    "--server",
                    f"code-search={code_search}",
                    "--server",
                    f"code-graph={code_graph}",
                    "--timeout",
                    "30",
                ],
                env=runtime_env,
            )
            if contract_evidence_output is not None:
                capture_installed_contract_evidence(
                    bom_path,
                    contract_evidence_output,
                    venv_python,
                    code_search,
                    code_graph,
                    runtime_env,
                )
            live_evidence = generate_live_readiness_evidence(
                bom,
                bom_path,
                destination,
                venv_python,
                code_search,
                code_graph,
                runtime_env,
            )
            validate_and_publish_live_evidence(
                live_evidence,
                bom_path,
                readiness_evidence_output,
                venv_python,
                runtime_env,
            )
    except (RealInstallError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"Real installed MCP validation FAILED: {exc}", file=sys.stderr)
        return 1

    print("Real installed MCP validation passed for exact BOM components.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
