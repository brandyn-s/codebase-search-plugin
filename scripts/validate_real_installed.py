#!/usr/bin/env python3
"""Install the exact private BOM components and validate their real MCP schemas."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parent.parent


class RealInstallError(RuntimeError):
    """The real component installation cannot be validated safely."""


def run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> None:
    """Run one external command without logging secret-bearing environment values."""
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


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

    for executable in ("gh", "git"):
        if shutil.which(executable) is None:
            raise RealInstallError(f"required executable is unavailable: {executable}")
    return token, runner_temp


def build_subprocess_environments(
    token: str,
    source: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Separate authenticated fetches from secret-free build/runtime work."""
    runtime_env = dict(os.environ if source is None else source)
    runtime_env.pop("GH_TOKEN", None)
    runtime_env.pop("CODE_INTEL_COMPONENT_TOKEN", None)
    fetch_env = {**runtime_env, "GH_TOKEN": token}
    return fetch_env, runtime_env


def load_bom() -> dict:
    try:
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        components = bom["components"]
        search = components["code-search"]["install"]
        graph = components["code-graph"]["install"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RealInstallError(f"component-bom.json is malformed: {exc}") from exc

    if search.get("kind") != "git" or graph.get("kind") != "github-release":
        raise RealInstallError("component-bom.json uses unsupported install kinds")
    return bom


def generate_live_readiness_evidence(
    bom: dict,
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
    smoke_env = dict(runtime_env)
    for secret_name in (
        "GH_TOKEN",
        "CODE_INTEL_COMPONENT_TOKEN",
        "CODE_INTEL_LIVE_READINESS_EVIDENCE",
    ):
        smoke_env.pop(secret_name, None)
    run(
        [
            str(venv_python),
            str(ROOT / "scripts" / "generate_live_readiness_evidence.py"),
            "--component-bom",
            str(ROOT / "component-bom.json"),
            "--fixture",
            str(ROOT / "bench" / "e2e" / "target-repo"),
            "--server",
            f"code-search={code_search}",
            "--server",
            f"code-graph={code_graph}",
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


def verify_sha256(archive: Path, expected: str) -> None:
    """Fail closed unless an existing archive matches a pinned SHA-256."""
    if not archive.is_file():
        raise RealInstallError(f"downloaded release asset is missing: {archive}")
    if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
        raise RealInstallError(f"pinned SHA-256 is missing or invalid for {archive.name}")

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


def install_code_search(
    install: dict,
    destination: Path,
    fetch_env: dict[str, str],
    runtime_env: dict[str, str],
) -> tuple[Path, Path]:
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
        raise RealInstallError(f"code-graph BOM asset name is missing for {platform_key}")
    if not isinstance(sha256, str):
        raise RealInstallError(
            f"code-graph BOM asset SHA-256 is missing for {platform_key}"
        )
    return name, sha256


def install_code_graph(
    install: dict,
    destination: Path,
    fetch_env: dict[str, str],
) -> Path:
    repository = install.get("repository")
    tag = install.get("tag")
    if not isinstance(repository, str) or not isinstance(tag, str):
        raise RealInstallError("code-graph BOM entry lacks repository/tag")

    asset_name, expected_sha256 = linux_asset(install)
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
            "--dir",
            str(downloads),
            "--clobber",
        ],
        env=fetch_env,
    )
    archive = downloads / asset_name
    verify_sha256(archive, expected_sha256)

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


def main() -> int:
    try:
        token, runner_temp = require_environment()
        fetch_env, runtime_env = build_subprocess_environments(token)
        os.environ.pop("GH_TOKEN", None)
        os.environ.pop("CODE_INTEL_COMPONENT_TOKEN", None)
        bom = load_bom()
        runtime_env = {
            **runtime_env,
            "PYTHONUNBUFFERED": "1",
            "CODE_SEARCH_STORAGE": str(runner_temp / "code-search-storage"),
        }
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
            live_evidence = generate_live_readiness_evidence(
                bom,
                destination,
                venv_python,
                code_search,
                code_graph,
                runtime_env,
            )
            if live_evidence is not None:
                run(
                    [
                        str(venv_python),
                        str(ROOT / "scripts" / "validate_plugin.py"),
                    ],
                    env={
                        **runtime_env,
                        "CODE_INTEL_READINESS_EVIDENCE_OVERRIDE": str(
                            live_evidence
                        ),
                    },
                )
    except (RealInstallError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"Real installed MCP validation FAILED: {exc}", file=sys.stderr)
        return 1

    print("Real installed MCP validation passed for exact BOM components.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
