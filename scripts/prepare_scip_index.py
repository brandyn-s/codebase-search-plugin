#!/usr/bin/env python3
"""Prepare a checkout-bound SCIP index with a pinned compiler indexer.

The command deliberately does not install an indexer and does not modify the
repository.  It verifies one already-installed generator against the plugin
BOM, runs it with an explicit out-of-tree output, detects checkout mutation,
and publishes the result atomically into a content-addressed cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit


SCHEMA_VERSION = 1
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
SAFE_TAG = re.compile(r"v[0-9][0-9A-Za-z._+-]*")
NODE_VERSION = re.compile(r"v([1-9][0-9]*)(?:\.[0-9]+){2}")
SUPPORTED_PLATFORM_NAMES = {
    "darwin-arm64": "scip-go-darwin-arm64.tar.gz",
    "linux-amd64": "scip-go-linux-amd64.tar.gz",
    "linux-arm64": "scip-go-linux-arm64.tar.gz",
}


class SCIPPreparationError(ValueError):
    """The requested checkout cannot safely produce trusted SCIP evidence."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SCIPPreparationError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SCIPPreparationError(f"{field} must be a non-empty trimmed string")
    return value


def _digest(value: object, field: str) -> str:
    result = _string(value, field)
    if LOWER_SHA256.fullmatch(result) is None:
        raise SCIPPreparationError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SCIPPreparationError(
            f"command exceeded the {timeout}-second timeout: {Path(argv[0]).name}"
        ) from exc
    except OSError as exc:
        raise SCIPPreparationError(
            f"could not execute {Path(argv[0]).name}: {exc}"
        ) from exc


def _git(root: Path, *arguments: str) -> bytes:
    result = _run(
        ["git", "-C", str(root), *arguments],
        cwd=root,
        timeout=30,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        raise SCIPPreparationError(f"Git {' '.join(arguments)} failed{suffix}")
    return result.stdout


def _resolved_git_root(candidate: Path) -> Path:
    candidate = candidate.expanduser().resolve(strict=True)
    result = _run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        cwd=candidate,
        timeout=30,
    )
    if result.returncode != 0:
        raise SCIPPreparationError("repository must be a Git worktree")
    rendered = result.stdout.decode("utf-8", errors="strict").rstrip("\n")
    if not rendered or "\n" in rendered:
        raise SCIPPreparationError("Git returned an invalid worktree root")
    return Path(rendered).resolve(strict=True)


def _platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if architecture is None:
        raise SCIPPreparationError(f"unsupported architecture: {machine}")
    key = f"{system}-{architecture}"
    if key not in SUPPORTED_PLATFORM_NAMES:
        raise SCIPPreparationError(f"scip-go is not pinned for platform {key}")
    return key


def _normalize_origin(raw: str) -> str:
    origin = raw.strip()
    if not origin:
        return ""
    if "://" not in origin:
        colon = origin.find(":")
        if 0 < colon < len(origin) - 1:
            host = origin[:colon]
            windows_drive = len(host) == 1 and host.isalpha()
            if not windows_drive and "/" not in host and "\\" not in host:
                if "@" in host:
                    host = host.rsplit("@", 1)[1]
                origin = f"https://{host.lower()}/{origin[colon + 1:].lstrip('/')}"
    parsed = urlsplit(origin)
    if parsed.scheme and parsed.netloc:
        host = parsed.hostname.lower() if parsed.hostname else ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        origin = urlunsplit((parsed.scheme.lower(), host, parsed.path, "", ""))
    origin = origin.rstrip("/")
    if origin.endswith(".git"):
        origin = origin[:-4]
    return origin.rstrip("/")


def _identity(root: Path) -> dict[str, str]:
    source_revision = _git(root, "rev-parse", "HEAD").decode().strip()
    if GIT_OBJECT_ID.fullmatch(source_revision) is None:
        raise SCIPPreparationError("Git HEAD is not a full object ID")
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status:
        raise SCIPPreparationError(
            "automatic SCIP preparation requires a clean Git checkout"
        )
    origin_result = _run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        cwd=root,
        timeout=30,
    )
    origin = ""
    if origin_result.returncode == 0:
        origin = _normalize_origin(
            origin_result.stdout.decode("utf-8", errors="strict")
        )
    root_posix = root.as_posix()
    repository_seed = f"remote:{origin}" if origin else f"path:{root_posix}"
    repository_id = _sha256_bytes(repository_seed.encode("utf-8"))
    checkout_id = _sha256_bytes(f"path:{root_posix}".encode("utf-8"))
    index_generation = _sha256_bytes(
        f"{repository_id}\0{source_revision}\0clean".encode("utf-8")
    )
    return {
        "repository_id": repository_id,
        "checkout_id": checkout_id,
        "source_revision": source_revision,
        "dirty_fingerprint": "clean",
        "index_generation": index_generation,
    }


def _load_generator_contract(
    bom_path: Path,
    platform_key: str,
    language: str,
) -> dict[str, Any]:
    try:
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SCIPPreparationError(f"component BOM is unreadable: {exc}") from exc
    if not isinstance(bom.get("schema_version"), int) or bom["schema_version"] != 1:
        raise SCIPPreparationError("component BOM schema_version must equal 1")
    generators = _object(bom.get("precision_generators"), "precision_generators")
    if language == "typescript":
        generator = _object(
            generators.get("typescript-scip"),
            "precision_generators.typescript-scip",
        )
        if generator.get("kind") != "npm-lockfile":
            raise SCIPPreparationError(
                "typescript-scip kind must be npm-lockfile"
            )
        if generator.get("package") != "@sourcegraph/scip-typescript":
            raise SCIPPreparationError(
                "typescript-scip package must be @sourcegraph/scip-typescript"
            )
        if generator.get("source_repository") != "sourcegraph/scip-typescript":
            raise SCIPPreparationError(
                "typescript-scip source_repository must be sourcegraph/scip-typescript"
            )
        revision = _string(
            generator.get("source_revision"),
            "typescript-scip.source_revision",
        )
        if GIT_OBJECT_ID.fullmatch(revision) is None:
            raise SCIPPreparationError(
                "typescript-scip.source_revision must be a full object ID"
            )
        integrity = _string(
            generator.get("package_integrity"),
            "typescript-scip.package_integrity",
        )
        if not integrity.startswith("sha512-"):
            raise SCIPPreparationError(
                "typescript-scip.package_integrity must be an npm SHA-512 SRI"
            )
        supported = generator.get("supported_node_majors")
        if (
            not isinstance(supported, list)
            or not supported
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                for value in supported
            )
            or len(set(supported)) != len(supported)
        ):
            raise SCIPPreparationError(
                "typescript-scip.supported_node_majors must be unique positive integers"
            )
        runtime = _object(
            generator.get("node_runtime"),
            "typescript-scip.node_runtime",
        )
        runtime_version = _string(
            runtime.get("version"),
            "typescript-scip.node_runtime.version",
        )
        if NODE_VERSION.fullmatch(runtime_version) is None:
            raise SCIPPreparationError(
                "typescript-scip.node_runtime.version must be a full Node version"
            )
        runtime_assets = _object(
            runtime.get("assets"),
            "typescript-scip.node_runtime.assets",
        )
        runtime_asset = _object(
            runtime_assets.get(platform_key),
            f"typescript-scip.node_runtime.assets.{platform_key}",
        )
        version = _string(
            generator.get("version_output"),
            "typescript-scip.version_output",
        )
        return {
            "repository": "sourcegraph/scip-typescript",
            "package": "@sourcegraph/scip-typescript",
            "tag": f"v{version}",
            "source_revision": revision,
            "version": version,
            "package_integrity": integrity,
            "lockfile_sha256": _digest(
                generator.get("lockfile_sha256"),
                "typescript-scip.lockfile_sha256",
            ),
            "binary_sha256": _digest(
                generator.get("entrypoint_sha256"),
                "typescript-scip.entrypoint_sha256",
            ),
            "supported_node_majors": supported,
            "runtime_version": runtime_version,
            "runtime_binary_sha256": _digest(
                runtime_asset.get("binary_sha256"),
                f"typescript-scip.node_runtime.assets.{platform_key}.binary_sha256",
            ),
        }
    generator = _object(generators.get("go-scip"), "precision_generators.go-scip")
    if generator.get("kind") != "github-release":
        raise SCIPPreparationError("go-scip kind must be github-release")
    if generator.get("repository") != "scip-code/scip-go":
        raise SCIPPreparationError("go-scip repository must be scip-code/scip-go")
    tag = _string(generator.get("tag"), "go-scip.tag")
    if SAFE_TAG.fullmatch(tag) is None:
        raise SCIPPreparationError("go-scip.tag is invalid")
    revision = _string(generator.get("source_revision"), "go-scip.source_revision")
    if GIT_OBJECT_ID.fullmatch(revision) is None:
        raise SCIPPreparationError("go-scip.source_revision must be a full object ID")
    version = _string(generator.get("version_output"), "go-scip.version_output")
    assets = _object(generator.get("assets"), "go-scip.assets")
    asset = _object(assets.get(platform_key), f"go-scip.assets.{platform_key}")
    expected_name = SUPPORTED_PLATFORM_NAMES[platform_key]
    if asset.get("name") != expected_name:
        raise SCIPPreparationError(
            f"go-scip asset name for {platform_key} must be {expected_name}"
        )
    return {
        "repository": "scip-code/scip-go",
        "tag": tag,
        "source_revision": revision,
        "version": version,
        "archive_sha256": _digest(
            asset.get("archive_sha256"),
            f"go-scip.assets.{platform_key}.archive_sha256",
        ),
        "binary_sha256": _digest(
            asset.get("binary_sha256"),
            f"go-scip.assets.{platform_key}.binary_sha256",
        ),
    }


def _verified_generator(
    generator_path: Path,
    contract: dict[str, Any],
    root: Path,
    language: str,
    runtime_path: Path | None,
) -> tuple[Path, str, str | None]:
    generator = generator_path.expanduser().resolve(strict=True)
    if not generator.is_file():
        raise SCIPPreparationError("generator must be a regular file")
    actual_digest = _sha256_file(generator)
    if actual_digest != contract["binary_sha256"]:
        label = (
            "scip-typescript entrypoint"
            if language == "typescript"
            else "scip-go binary"
        )
        raise SCIPPreparationError(f"{label} SHA-256 does not match the pinned BOM")
    runtime_version: str | None = None
    if language == "typescript":
        if runtime_path is None:
            raise SCIPPreparationError(
                "TypeScript SCIP preparation requires the pinned Node runtime"
            )
        runtime_executable = runtime_path.expanduser().resolve(strict=True)
        if (
            not runtime_executable.is_file()
            or _sha256_file(runtime_executable)
            != contract["runtime_binary_sha256"]
        ):
            raise SCIPPreparationError(
                "Node runtime binary SHA-256 does not match the pinned BOM"
            )
        runtime = _run(
            [str(runtime_executable), "--version"], cwd=root, timeout=15
        )
        if runtime.returncode != 0:
            raise SCIPPreparationError("node --version failed")
        runtime_version = runtime.stdout.decode("utf-8", errors="strict").strip()
        match = NODE_VERSION.fullmatch(runtime_version)
        if match is None or runtime_version != contract["runtime_version"]:
            raise SCIPPreparationError(
                "Node runtime version does not match the pinned BOM"
            )
        version_argv = [str(runtime_executable), str(generator), "--version"]
    else:
        version_argv = [str(generator), "--version"]
    version = _run(version_argv, cwd=root, timeout=15)
    if version.returncode != 0:
        raise SCIPPreparationError(f"{language} SCIP generator --version failed")
    rendered_version = version.stdout.decode("utf-8", errors="strict").strip()
    if rendered_version != contract["version"]:
        raise SCIPPreparationError(
            f"{language} SCIP generator version output does not match the pinned BOM"
        )
    return generator, actual_digest, runtime_version


def _cache_receipt(
    cache_directory: Path,
    identity: dict[str, str],
    contract: dict[str, Any],
    generator_digest: str,
    runtime_version: str | None,
) -> dict[str, Any] | None:
    receipt_path = cache_directory / "receipt.json"
    index_path = cache_directory / "index.scip"
    if (
        cache_directory.is_symlink()
        or receipt_path.is_symlink()
        or index_path.is_symlink()
        or not receipt_path.is_file()
        or not index_path.is_file()
    ):
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict) or set(receipt) != {
        "cached",
        "checkout_id",
        "dirty_fingerprint",
        "generator",
        "index",
        "index_generation",
        "precision_tier",
        "repository_id",
        "schema_version",
        "source_revision",
        "status",
    }:
        return None
    if (
        receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("status") != "ready"
        or receipt.get("precision_tier") != "scip"
        or receipt.get("cached") is not False
        or any(receipt.get(field) != value for field, value in identity.items())
        or receipt.get("generator")
        != {
            "repository": contract["repository"],
            "tag": contract["tag"],
            "source_revision": contract["source_revision"],
            "version": contract["version"],
            "binary_sha256": generator_digest,
            **(
                {"runtime_version": runtime_version}
                if runtime_version is not None
                else {}
            ),
        }
    ):
        return None
    index = receipt.get("index")
    if not isinstance(index, dict) or set(index) != {
        "path",
        "sha256",
        "size_bytes",
    }:
        return None
    expected = index.get("sha256")
    if not isinstance(expected, str) or LOWER_SHA256.fullmatch(expected) is None:
        return None
    expected_path = str(index_path.resolve())
    if (
        index.get("path") != expected_path
        or not isinstance(index.get("size_bytes"), int)
        or isinstance(index.get("size_bytes"), bool)
        or index["size_bytes"] <= 0
        or index["size_bytes"] != index_path.stat().st_size
        or _sha256_file(index_path) != expected
    ):
        return None
    receipt["index"]["path"] = expected_path
    receipt["cached"] = True
    return receipt


def prepare(
    repository: Path,
    generator_path: Path,
    component_bom_path: Path,
    cache_root: Path,
    timeout_seconds: int,
    language: str = "auto",
    runtime_path: Path | None = None,
) -> dict[str, Any]:
    root = _resolved_git_root(repository)
    if language == "auto":
        has_go_root = (root / "go.mod").is_file()
        has_typescript_root = (root / "tsconfig.json").is_file()
        if has_go_root and has_typescript_root:
            raise SCIPPreparationError(
                "mixed Go and TypeScript roots must select --language go or typescript"
            )
        if has_go_root:
            language = "go"
        elif has_typescript_root:
            language = "typescript"
        else:
            raise SCIPPreparationError(
                "automatic SCIP preparation requires a root go.mod or tsconfig.json"
            )
    if language == "go" and not (root / "go.mod").is_file():
        raise SCIPPreparationError(
            "automatic Go SCIP preparation requires a root go.mod"
        )
    if language == "typescript":
        if not (root / "tsconfig.json").is_file():
            raise SCIPPreparationError(
                "automatic TypeScript SCIP preparation requires a root tsconfig.json"
            )
        if not (root / "node_modules").is_dir():
            raise SCIPPreparationError(
                "automatic TypeScript SCIP preparation requires an existing node_modules dependency tree"
            )
    identity_before = _identity(root)
    platform_key = _platform_key()
    contract = _load_generator_contract(
        component_bom_path.expanduser().resolve(strict=True),
        platform_key,
        language,
    )
    generator, generator_digest, runtime_version = _verified_generator(
        generator_path, contract, root, language, runtime_path
    )

    cache_root = cache_root.expanduser().resolve()
    try:
        cache_root.relative_to(root)
    except ValueError:
        pass
    else:
        raise SCIPPreparationError("SCIP cache root must be outside the repository")
    cache_key = _sha256_bytes(
        _canonical_json(
            {
                "schema_version": 1,
                "index_generation": identity_before["index_generation"],
                "generator_sha256": generator_digest,
                "generator_version": contract["version"],
                "command_contract": f"scip-{language}-index-v1",
            }
        )
    )
    cache_directory = cache_root / f"{language}-scip" / cache_key
    cached = _cache_receipt(
        cache_directory,
        identity_before,
        contract,
        generator_digest,
        runtime_version,
    )
    if cached is not None:
        return cached
    if cache_directory.exists() or cache_directory.is_symlink():
        raise SCIPPreparationError("an invalid SCIP cache entry already exists")

    cache_directory.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = Path(
        tempfile.mkdtemp(
            prefix=f".{cache_key}.",
            dir=cache_directory.parent,
        )
    )
    try:
        staged_index = staging / "index.scip"
        command = (
            [
                str(runtime_path.expanduser().resolve(strict=True)),
                str(generator),
                "index",
                "--cwd",
                str(root),
                "--output",
                str(staged_index),
                "--no-global-caches",
                "--no-progress-bar",
            ]
            if language == "typescript"
            else [
                str(generator),
                "index",
                "--module-root",
                ".",
                "--output",
                str(staged_index),
            ]
        )
        completed = _run(
            command,
            cwd=root,
            timeout=timeout_seconds,
        )
        try:
            identity_after = _identity(root)
        except SCIPPreparationError as exc:
            raise SCIPPreparationError(
                "checkout changed while "
                f"{language} SCIP generator was running; index discarded"
            ) from exc
        if identity_after != identity_before:
            raise SCIPPreparationError(
                "checkout changed while "
                f"{language} SCIP generator was running; index discarded"
            )
        if completed.returncode != 0:
            raise SCIPPreparationError(
                f"{language} SCIP index failed with exit status {completed.returncode}"
            )
        if not staged_index.is_file() or staged_index.stat().st_size == 0:
            raise SCIPPreparationError(
                f"{language} SCIP generator produced no non-empty index"
            )
        index_digest = _sha256_file(staged_index)
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "status": "ready",
            "precision_tier": "scip",
            **identity_before,
            "generator": {
                "repository": contract["repository"],
                "tag": contract["tag"],
                "source_revision": contract["source_revision"],
                "version": contract["version"],
                "binary_sha256": generator_digest,
                **(
                    {"runtime_version": runtime_version}
                    if runtime_version is not None
                    else {}
                ),
            },
            "index": {
                "path": str((cache_directory / "index.scip").resolve()),
                "sha256": index_digest,
                "size_bytes": staged_index.stat().st_size,
            },
            "cached": False,
        }
        (staging / "receipt.json").write_bytes(_canonical_json(receipt) + b"\n")
        try:
            staging.replace(cache_directory)
        except FileExistsError:
            reused = _cache_receipt(
                cache_directory,
                identity_before,
                contract,
                generator_digest,
                runtime_version,
            )
            if reused is None:
                raise SCIPPreparationError(
                    "an invalid SCIP cache entry already exists"
                )
            return reused
        staging = None
        return receipt
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)


def verify_generator(
    generator_path: Path,
    component_bom_path: Path,
    language: str = "go",
    runtime_path: Path | None = None,
) -> dict[str, Any]:
    platform_key = _platform_key()
    contract = _load_generator_contract(
        component_bom_path.expanduser().resolve(strict=True),
        platform_key,
        language,
    )
    generator, generator_digest, runtime_version = _verified_generator(
        generator_path,
        contract,
        Path.cwd(),
        language,
        runtime_path,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "platform": platform_key,
        "generator": {
            "path": str(generator),
            "repository": contract["repository"],
            "tag": contract["tag"],
            "source_revision": contract["source_revision"],
            "version": contract["version"],
            "binary_sha256": generator_digest,
            **(
                {"runtime_version": runtime_version}
                if runtime_version is not None
                else {}
            ),
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser(
        "prepare", help="prepare or reuse one trusted SCIP index"
    )
    command.add_argument("repository", type=Path)
    command.add_argument(
        "--language",
        choices=("auto", "go", "typescript"),
        default="auto",
    )
    command.add_argument("--generator", required=True, type=Path)
    command.add_argument("--runtime", type=Path)
    command.add_argument("--component-bom", required=True, type=Path)
    command.add_argument("--cache-root", required=True, type=Path)
    command.add_argument("--timeout-seconds", type=int, default=1800)
    verify = subparsers.add_parser(
        "verify", help="verify one installed SCIP generator against the host BOM"
    )
    verify.add_argument("--generator", required=True, type=Path)
    verify.add_argument("--component-bom", required=True, type=Path)
    verify.add_argument("--runtime", type=Path)
    verify.add_argument(
        "--language",
        choices=("go", "typescript"),
        default="go",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "verify":
            receipt = verify_generator(
                arguments.generator,
                arguments.component_bom,
                arguments.language,
                arguments.runtime,
            )
        else:
            if arguments.timeout_seconds <= 0:
                parser.error("--timeout-seconds must be positive")
            receipt = prepare(
                arguments.repository,
                arguments.generator,
                arguments.component_bom,
                arguments.cache_root,
                arguments.timeout_seconds,
                arguments.language,
                arguments.runtime,
            )
    except (SCIPPreparationError, OSError, UnicodeError) as exc:
        print(f"SCIP preparation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
