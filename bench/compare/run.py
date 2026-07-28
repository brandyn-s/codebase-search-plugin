#!/usr/bin/env python3
"""Run or preflight the content-addressed five-arm comparison."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time


def _reject_package_initializer() -> None:
    initializer = Path(__file__).resolve().parent / "__init__.py"
    if initializer.exists() or initializer.is_symlink():
        print(
            "ERROR: bench/compare must remain a namespace package; "
            "refusing executable package initializer",
            file=sys.stderr,
        )
        raise SystemExit(1)


_reject_package_initializer()

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.compare.provenance import (  # noqa: E402
    AUTHORITATIVE_CONSUMER,
    ProvenanceError,
    RunBundle,
)
from bench.compare.build_pin import (  # noqa: E402
    GitAuditBatch,
    PinError,
    validate_git_label_audit,
)
from bench.compare.schema import (  # noqa: E402
    ARM_CONTRACTS,
    ContractError,
    FrozenControls,
    MEASURED_OUTCOME_ERROR_CLASSES,
    NONFINALIZABLE_ERROR_CLASSES,
    build_unit_contract,
    canonical_json,
    component_identity,
    component_identity_sha256,
    fixture_canary_challenge,
    harness_source_identity,
    latin_square_units,
    require_reproducible_harness_source,
    scoring_policy_descriptor,
    validate_observation,
)
from bench.compare.token_accounting import (  # noqa: E402
    count_tokens,
    tokenizer_descriptor,
)


PUBLIC_GITHUB = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?"
)
REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
ENVIRONMENT_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
JUNE_PREPARED_PIN_ID = "locbench-june-n200-prepared-v1"
JUNE_DATASET_REVISION = "c44cf3b74e07ca642cec841b471a9939907c12a7"
JUNE_PARQUET_SHA256 = (
    "8df0833c2c1276c5837aab923d489ab97d7654529abe759d0f59242c4978a662"
)
JUNE_PARQUET_SIZE = 3_084_430
JUNE_EXTERNAL_PIN_SHA256 = (
    "886156bbd16eb753a690da6bcb452f9238f53ef28409b1f4e483b842a0556453"
)
JUNE_RECORDED_ORDER_SHA256 = (
    "de161cf07a6310209d78e9f2e2f05ca28a21cfcdeef1b871279d39ea24d54577"
)
_DIR_FD_CAPABILITY_PROBES = (os.open, os.stat, os.link, os.unlink)
_NOFOLLOW_CAPABILITY_PROBES = (os.stat, os.link)
_LISTDIR_FD_CAPABILITY_PROBE = os.listdir
_SECURE_IO_ERRORS = (OSError, TypeError, NotImplementedError)


class RunnerError(ValueError):
    """The invocation, case pin, fixture, or observation is unsafe."""


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RunnerError(f"cannot hash {path}: {exc}") from exc


def load_object(path: Path) -> dict:
    value, _digest, _encoded = load_bound_object(path)
    return value


def load_bound_object(path: Path) -> tuple[dict, str, bytes]:
    """Read one immutable JSON snapshot and return its object, digest, and bytes."""
    if path.is_symlink():
        raise RunnerError(f"refusing symlink input: {path}")
    try:
        encoded = path.read_bytes()
        value = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"{path}: expected a JSON object")
    return value, hashlib.sha256(encoded).hexdigest(), encoded


def _safe_relative_file(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunnerError(f"{context}: file path must be nonempty")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or "\\" in value:
        raise RunnerError(f"{context}: unsafe repository-relative file path")
    return candidate.as_posix()


def _validate_prepared_june_pin(pin: dict, cases: list[dict]) -> None:
    if pin.get("pin_id") != JUNE_PREPARED_PIN_ID:
        return
    expected_dataset = {
        "name": "LocBench",
        "public": True,
        "repository": "czlll/Loc-Bench_V1",
        "source_revision": JUNE_DATASET_REVISION,
        "source_path": "data/test-00000-of-00001.parquet",
        "source_size": JUNE_PARQUET_SIZE,
        "source_sha256": JUNE_PARQUET_SHA256,
        "local_only": True,
        "redistribution": "operator_local_uncommitted_artifact",
    }
    if set(pin) != {
        "schema_version",
        "pin_id",
        "dataset",
        "generation",
        "label_audit",
        "cases",
    } or pin.get("dataset") != expected_dataset:
        raise RunnerError("prepared June dataset contract is invalid")
    case_ids = [
        case.get("case_id") if isinstance(case, dict) else None
        for case in cases
    ]
    if (
        len(case_ids) != 200
        or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
        or len(set(case_ids)) != 200
    ):
        raise RunnerError("prepared June pin must contain exactly 200 ordered cases")
    order_sha256 = hashlib.sha256(
        ("\n".join(case_ids) + "\n").encode("utf-8")
    ).hexdigest()
    expected_generation = {
        "selection": "published_external_order_v1",
        "expected_count": 200,
        "selected_instance_ids": case_ids,
        "external_pin_sha256": JUNE_EXTERNAL_PIN_SHA256,
        "recorded_order_sha256": JUNE_RECORDED_ORDER_SHA256,
        "label_source": "locagent_evaluator_edit_functions_v1",
        "git_provenance": "source_first_pr_comparison_v1",
    }
    if (
        pin.get("generation") != expected_generation
        or order_sha256 != JUNE_RECORDED_ORDER_SHA256
    ):
        raise RunnerError("prepared June generation contract is invalid")


def _load_instrument_fixture(
    dataset: dict,
    *,
    allow_instrument_fixture: bool,
    root: Path,
) -> tuple[dict | None, set[str]]:
    descriptor = dataset.get("instrument_fixture")
    canary = dataset.get("instrument_canary")
    if descriptor is None and canary is None:
        return None, set()
    if not allow_instrument_fixture:
        raise RunnerError(
            "checked-in instrument fixtures are forbidden in live mode"
        )
    if not isinstance(descriptor, dict) or not isinstance(canary, dict):
        raise RunnerError("instrument fixture and canary descriptors are required")
    required_fixture = {
        "manifest_path",
        "manifest_sha256",
        "repository",
        "revision",
        "source_root",
    }
    required_canary = {
        "network_environment",
        "path",
        "sha256",
        "write_path_environment",
        "secret_environment",
    }
    if set(descriptor) != required_fixture or set(canary) != required_canary:
        raise RunnerError("instrument fixture descriptor fields are invalid")
    manifest_relative = _safe_relative_file(
        descriptor["manifest_path"],
        "instrument manifest",
    )
    source_relative = _safe_relative_file(
        descriptor["source_root"],
        "instrument source root",
    )
    canary_relative = _safe_relative_file(canary["path"], "instrument canary")
    manifest_path = root / manifest_relative
    source_root = root / source_relative
    canary_path = root / canary_relative
    for candidate, context in (
        (manifest_path, "instrument manifest"),
        (canary_path, "instrument canary"),
    ):
        if candidate.is_symlink() or not candidate.is_file():
            raise RunnerError(f"{context} is missing or unsafe")
    if source_root.is_symlink() or not source_root.is_dir():
        raise RunnerError("instrument fixture source root is missing or unsafe")
    for expected, candidate, context in (
        (descriptor["manifest_sha256"], manifest_path, "instrument manifest"),
        (canary["sha256"], canary_path, "instrument canary"),
    ):
        if (
            not isinstance(expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or sha256_file(candidate) != expected
        ):
            raise RunnerError(f"{context} content address mismatch")
    if any(
        not isinstance(canary.get(field), str)
        or ENVIRONMENT_NAME.fullmatch(canary[field]) is None
        for field in (
            "write_path_environment",
            "secret_environment",
            "network_environment",
        )
    ):
        raise RunnerError("instrument canary environment names are invalid")
    canary_text = canary_path.read_text(encoding="utf-8")
    if any(
        canary[field] not in canary_text
        for field in (
            "write_path_environment",
            "secret_environment",
            "network_environment",
        )
    ):
        raise RunnerError("instrument canary text is not bound to its sentinels")
    manifest = load_object(manifest_path)
    if (
        set(manifest)
        != {"schema_version", "repository", "revision", "source_root", "files"}
        or manifest.get("schema_version") != 1
        or manifest.get("repository") != descriptor["repository"]
        or manifest.get("revision") != descriptor["revision"]
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
        or descriptor["repository"] != "fixture://codebase-search-e2e-v1"
        or re.fullmatch(r"[0-9a-f]{64}", str(descriptor["revision"])) is None
    ):
        raise RunnerError("instrument fixture manifest identity is invalid")
    source_files: set[str] = set()
    actual_hashes: dict[str, str] = {}
    for raw_path, expected_sha256 in manifest["files"].items():
        relative = _safe_relative_file(raw_path, "instrument source")
        candidate = source_root / relative
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or candidate.is_symlink()
            or not candidate.is_file()
            or sha256_file(candidate) != expected_sha256
        ):
            raise RunnerError(f"instrument source content mismatch: {relative}")
        source_files.add(relative)
        actual_hashes[relative] = expected_sha256
    source_entries = list(source_root.rglob("*"))
    if any(candidate.is_symlink() for candidate in source_entries):
        raise RunnerError("instrument source contains a symlink")
    actual_files = {
        candidate.relative_to(source_root).as_posix()
        for candidate in source_entries
        if candidate.is_file()
    }
    if actual_files != source_files:
        raise RunnerError("instrument source file set differs from its manifest")
    canonical_tree = "\n".join(
        f"{relative}\0{actual_hashes[relative]}"
        for relative in sorted(actual_hashes)
    ).encode("utf-8")
    if hashlib.sha256(canonical_tree).hexdigest() != descriptor["revision"]:
        raise RunnerError("instrument source revision is not reproducible")
    try:
        canary_source_relative = canary_path.resolve().relative_to(
            source_root.resolve()
        ).as_posix()
    except ValueError as exc:
        raise RunnerError("instrument canary is outside the target repository") from exc
    if canary_source_relative not in source_files:
        raise RunnerError("instrument canary is absent from the source manifest")
    return descriptor, source_files


def load_case_pin(
    path: Path,
    *,
    allow_instrument_fixture: bool = False,
    root: Path | None = None,
    repository_root: Path | None = None,
) -> tuple[dict, list[dict], str, bytes]:
    if path.is_symlink():
        raise RunnerError(f"refusing symlink input: {path}")
    try:
        encoded_pin = path.read_bytes()
        pin = json.loads(encoded_pin)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RunnerError(f"cannot load {path}: {exc}") from exc
    if not isinstance(pin, dict):
        raise RunnerError(f"{path}: expected a JSON object")
    pin_sha256 = hashlib.sha256(encoded_pin).hexdigest()
    if pin.get("schema_version") != 1:
        raise RunnerError("case pin schema_version must be 1")
    if not isinstance(pin.get("pin_id"), str) or not pin["pin_id"]:
        raise RunnerError("case pin requires a pin_id")
    dataset = pin.get("dataset")
    if not isinstance(dataset, dict) or dataset.get("public") is not True:
        raise RunnerError("case pin dataset must be explicitly public")
    fixture, fixture_files = _load_instrument_fixture(
        dataset,
        allow_instrument_fixture=allow_instrument_fixture,
        root=(Path(root) if root is not None else Path(__file__).resolve().parents[2]),
    )
    cases = pin.get("cases")
    if not isinstance(cases, list) or not cases:
        raise RunnerError("case pin must contain cases")
    _validate_prepared_june_pin(pin, cases)
    seen: set[str] = set()
    validated: list[dict] = []
    for index, case in enumerate(cases):
        context = f"case[{index}]"
        if not isinstance(case, dict):
            raise RunnerError(f"{context}: expected an object")
        case_id = case.get("case_id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or "|" in case_id
            or case_id in seen
        ):
            raise RunnerError(f"{context}: duplicate or invalid case_id")
        seen.add(case_id)
        if (
            not isinstance(case.get("category"), str)
            or not case["category"]
            or not isinstance(case.get("query"), str)
            or not case["query"].strip()
        ):
            raise RunnerError(f"{case_id}: category and query are required")
        repository = case.get("repository")
        if not isinstance(repository, dict):
            raise RunnerError(f"{case_id}: repository must be an object")
        url = repository.get("url")
        revision = repository.get("revision")
        public_repository = (
            isinstance(url, str)
            and PUBLIC_GITHUB.fullmatch(url) is not None
            and isinstance(revision, str)
            and REVISION.fullmatch(revision) is not None
        )
        fixture_repository = (
            fixture is not None
            and url == fixture["repository"]
            and revision == fixture["revision"]
        )
        if not (public_repository or fixture_repository):
            raise RunnerError(
                f"{case_id}: repository must be a public GitHub URL at a full revision"
            )
        oracle = case.get("oracle")
        if not isinstance(oracle, dict):
            raise RunnerError(f"{case_id}: oracle must be an object")
        files = oracle.get("files")
        if (
            not isinstance(files, list)
            or not files
            or len(set(files)) != len(files)
        ):
            raise RunnerError(f"{case_id}: oracle files must be unique and nonempty")
        normalized_files = [
            _safe_relative_file(value, f"{case_id}:oracle") for value in files
        ]
        for field in ("classes", "functions"):
            labels = oracle.get(field)
            if (
                not isinstance(labels, list)
                or any(not isinstance(label, str) or not label for label in labels)
                or len(set(labels)) != len(labels)
            ):
                raise RunnerError(f"{case_id}: oracle {field} must be unique strings")
        audit = case.get("label_audit")
        if fixture is not None:
            if (
                not isinstance(audit, dict)
                or set(audit) != {"status", "source", "changed_files"}
                or audit.get("status") != "verified"
                or audit.get("source") != "content_addressed_fixture_manifest"
                or not isinstance(audit.get("changed_files"), list)
            ):
                raise RunnerError(f"{case_id}: fixture label audit is not verified")
        else:
            if repository_root is None:
                raise RunnerError(
                    f"{case_id}: pinned Git repository root is required "
                    "to verify labels"
                )
            if (
                not isinstance(audit, dict)
                or not isinstance(audit.get("changed_files"), list)
            ):
                raise RunnerError(
                    f"{case_id}: Git-object label audit is malformed"
                )
        changed_files = {
            _safe_relative_file(value, f"{case_id}:changed_files")
            for value in audit["changed_files"]
        }
        if not set(normalized_files) <= changed_files:
            raise RunnerError(
                f"{case_id}: oracle files are not grounded in the changed-file set"
            )
        if fixture is not None and not set(normalized_files) <= fixture_files:
            raise RunnerError(
                f"{case_id}: oracle files are absent from the fixture manifest"
            )
        validated.append(case)
    if fixture is None:
        assert repository_root is not None
        cases_by_repository: dict[str, list[dict]] = {}
        for case in validated:
            repository_url = case["repository"]["url"]
            repository_slug = repository_url.removeprefix(
                "https://github.com/"
            )
            cases_by_repository.setdefault(repository_slug, []).append(case)
        for repository_slug in sorted(cases_by_repository):
            try:
                with GitAuditBatch(
                    repository_root,
                    repository_slug,
                ) as batch:
                    for case in cases_by_repository[repository_slug]:
                        validate_git_label_audit(
                            case,
                            repository_root,
                            batch=batch,
                        )
            except PinError as exc:
                raise RunnerError(
                    f"{repository_slug}: {exc}"
                ) from exc
        pin_audit = pin.get("label_audit")
        audit_records = {
            case["case_id"]: case["label_audit"]["audit_record_sha256"]
            for case in validated
        }
        if (
            not isinstance(pin_audit, dict)
            or pin_audit.get("policy") != "pinned_git_objects_v1"
            or pin_audit.get("audit_records") != audit_records
            or pin_audit.get("audit_records_sha256")
            != hashlib.sha256(canonical_json(audit_records)).hexdigest()
        ):
            raise RunnerError("pin-level Git-object label provenance is invalid")
    return pin, validated, pin_sha256, encoded_pin


def _require_secure_live_diagnostic_capabilities() -> tuple[int, int, int]:
    flag_names = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    flags = tuple(getattr(os, name, None) for name in flag_names)
    try:
        supported = (
            all(type(value) is int and value != 0 for value in flags)
            and all(
                function in os.supports_dir_fd
                for function in _DIR_FD_CAPABILITY_PROBES
            )
            and all(
                function in os.supports_follow_symlinks
                for function in _NOFOLLOW_CAPABILITY_PROBES
            )
            and _LISTDIR_FD_CAPABILITY_PROBE in os.supports_fd
        )
    except (AttributeError, TypeError, NotImplementedError):
        supported = False
    if not supported:
        raise RunnerError(
            "secure live diagnostic publishing is unsupported on this platform"
        )
    return int(flags[0]), int(flags[1]), int(flags[2])


def _stat_identity(state: os.stat_result) -> tuple[int, int]:
    return state.st_dev, state.st_ino


def _stable_file_state(state: os.stat_result) -> tuple[int, ...]:
    return (
        state.st_dev,
        state.st_ino,
        state.st_mode,
        state.st_nlink,
        state.st_size,
        state.st_mtime_ns,
        state.st_ctime_ns,
    )


def _verify_live_directory_identity(
    run_dir: Path,
    directory_fd: int,
    expected_identity: tuple[int, int],
) -> None:
    try:
        descriptor_state = os.fstat(directory_fd)
        path_state = os.stat(run_dir, follow_symlinks=False)
    except _SECURE_IO_ERRORS as exc:
        raise RunnerError(
            f"live artifact directory identity is unsafe or unreadable: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(descriptor_state.st_mode)
        or not stat.S_ISDIR(path_state.st_mode)
        or _stat_identity(descriptor_state) != expected_identity
        or _stat_identity(path_state) != expected_identity
    ):
        raise RunnerError("live artifact directory identity changed unsafely")


def _require_live_directory_inventory(
    directory_fd: int,
    expected_entries: set[str],
) -> None:
    try:
        actual_entries = set(os.listdir(directory_fd))
    except _SECURE_IO_ERRORS as exc:
        raise RunnerError(
            f"live artifact directory inventory is unsafe or unreadable: {exc}"
        ) from exc
    if actual_entries != expected_entries:
        raise RunnerError(
            "live artifact directory contains unexpected entries"
        )


def _open_live_artifact_directory(
    run_dir: Path,
    *,
    directory_flag: int,
    nofollow_flag: int,
    cloexec_flag: int,
) -> tuple[int, tuple[int, int]]:
    directory_fd: int | None = None
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        path_state = os.stat(run_dir, follow_symlinks=False)
        if stat.S_ISLNK(path_state.st_mode):
            raise RunnerError("refusing unsafe symlink live artifact directory")
        if not stat.S_ISDIR(path_state.st_mode):
            raise RunnerError("live artifact directory is unsafe or unreadable")
        expected_identity = _stat_identity(path_state)
        directory_fd = os.open(
            run_dir,
            os.O_RDONLY | directory_flag | nofollow_flag | cloexec_flag,
        )
        _verify_live_directory_identity(
            run_dir,
            directory_fd,
            expected_identity,
        )
        return directory_fd, expected_identity
    except RunnerError:
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    except _SECURE_IO_ERRORS as exc:
        if directory_fd is not None:
            os.close(directory_fd)
        raise RunnerError(
            f"live artifact directory is unsafe or unreadable: {exc}"
        ) from exc


def _read_live_diagnostic_snapshot(
    *,
    directory_fd: int,
    nofollow_flag: int,
    cloexec_flag: int,
) -> bytes:
    name = "diagnostic.json"
    try:
        path_before = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(path_before.st_mode)
            or path_before.st_nlink != 1
        ):
            raise RunnerError(
                "live artifact diagnostic must be a single-link regular file"
            )
        descriptor = os.open(
            name,
            os.O_RDONLY | nofollow_flag | cloexec_flag,
            dir_fd=directory_fd,
        )
    except RunnerError:
        raise
    except _SECURE_IO_ERRORS as exc:
        raise RunnerError(
            f"live artifact diagnostic is unsafe or unreadable: {exc}"
        ) from exc
    try:
        descriptor_before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        descriptor_after = os.fstat(descriptor)
    except _SECURE_IO_ERRORS as exc:
        raise RunnerError(
            f"live artifact diagnostic is unsafe or unreadable: {exc}"
        ) from exc
    finally:
        os.close(descriptor)
    try:
        path_after = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except _SECURE_IO_ERRORS as exc:
        raise RunnerError(
            f"live artifact diagnostic is unsafe or unreadable: {exc}"
        ) from exc
    stable_states = {
        _stable_file_state(path_before),
        _stable_file_state(descriptor_before),
        _stable_file_state(descriptor_after),
        _stable_file_state(path_after),
    }
    if (
        len(stable_states) != 1
        or not stat.S_ISREG(path_after.st_mode)
        or path_after.st_nlink != 1
    ):
        raise RunnerError("live artifact diagnostic changed during its snapshot")
    return b"".join(chunks)


def _safe_unlink_live_entry(
    *,
    directory_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    try:
        state = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if _stat_identity(state) != expected_identity:
        raise RunnerError("live diagnostic cleanup identity changed unsafely")
    os.unlink(name, dir_fd=directory_fd)


def _publish_live_diagnostic(
    *,
    run_dir: Path,
    directory_fd: int,
    directory_identity: tuple[int, int],
    encoded: bytes,
    nofollow_flag: int,
    cloexec_flag: int,
) -> None:
    temporary_name = f".diagnostic.{secrets.token_hex(16)}.tmp"
    temporary_identity: tuple[int, int] | None = None
    temporary_present = False
    published_present = False
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | nofollow_flag
            | cloexec_flag,
            0o600,
            dir_fd=directory_fd,
        )
        temporary_present = True
        temporary_state = os.fstat(descriptor)
        temporary_identity = _stat_identity(temporary_state)
        if (
            not stat.S_ISREG(temporary_state.st_mode)
            or temporary_state.st_nlink != 1
        ):
            raise RunnerError("live diagnostic temporary file is unsafe")
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short write while creating live diagnostic")
            offset += written
        os.fsync(descriptor)
        after_write = os.fstat(descriptor)
        if (
            _stat_identity(after_write) != temporary_identity
            or not stat.S_ISREG(after_write.st_mode)
            or after_write.st_nlink != 1
            or after_write.st_size != len(encoded)
        ):
            raise RunnerError("live diagnostic temporary file changed unsafely")
        os.close(descriptor)
        descriptor = None

        _verify_live_directory_identity(
            run_dir,
            directory_fd,
            directory_identity,
        )
        _require_live_directory_inventory(
            directory_fd,
            {temporary_name},
        )
        os.link(
            temporary_name,
            "diagnostic.json",
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        published_present = True
        os.unlink(temporary_name, dir_fd=directory_fd)
        temporary_present = False
        _require_live_directory_inventory(
            directory_fd,
            {"diagnostic.json"},
        )

        published_state = os.stat(
            "diagnostic.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            _stat_identity(published_state) != temporary_identity
            or not stat.S_ISREG(published_state.st_mode)
            or published_state.st_nlink != 1
        ):
            raise RunnerError("published live diagnostic is unsafe")
        published_snapshot = _read_live_diagnostic_snapshot(
            directory_fd=directory_fd,
            nofollow_flag=nofollow_flag,
            cloexec_flag=cloexec_flag,
        )
        if published_snapshot != encoded:
            raise RunnerError("published live diagnostic content is unsafe")
        confirmed_state = os.stat(
            "diagnostic.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            _stat_identity(confirmed_state) != temporary_identity
            or confirmed_state.st_nlink != 1
            or not stat.S_ISREG(confirmed_state.st_mode)
        ):
            raise RunnerError("published live diagnostic identity is unsafe")
        _verify_live_directory_identity(
            run_dir,
            directory_fd,
            directory_identity,
        )
        os.fsync(directory_fd)
        _verify_live_directory_identity(
            run_dir,
            directory_fd,
            directory_identity,
        )
        _require_live_directory_inventory(
            directory_fd,
            {"diagnostic.json"},
        )
    except (*_SECURE_IO_ERRORS, RunnerError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        cleanup_error: OSError | TypeError | NotImplementedError | RunnerError | None = (
            None
        )
        if temporary_present and temporary_identity is not None:
            try:
                _safe_unlink_live_entry(
                    directory_fd=directory_fd,
                    name=temporary_name,
                    expected_identity=temporary_identity,
                )
            except (*_SECURE_IO_ERRORS, RunnerError) as cleanup_exc:
                cleanup_error = cleanup_exc
        if published_present and temporary_identity is not None:
            try:
                _safe_unlink_live_entry(
                    directory_fd=directory_fd,
                    name="diagnostic.json",
                    expected_identity=temporary_identity,
                )
                os.fsync(directory_fd)
            except (*_SECURE_IO_ERRORS, RunnerError) as cleanup_exc:
                cleanup_error = cleanup_error or cleanup_exc
        if cleanup_error is not None:
            raise RunnerError(
                f"cannot clean failed live diagnostic safely: {cleanup_error}"
            ) from exc
        if isinstance(exc, RunnerError):
            raise
        raise RunnerError(f"cannot write live diagnostic safely: {exc}") from exc


def live_preflight(
    *,
    arguments: argparse.Namespace,
    cases_sha256: str,
    expected_units: int,
    identity_sha256: str,
) -> int:
    run_dir = arguments.run_dir
    directory_flag, nofollow_flag, cloexec_flag = (
        _require_secure_live_diagnostic_capabilities()
    )
    directory_fd, directory_identity = _open_live_artifact_directory(
        run_dir,
        directory_flag=directory_flag,
        nofollow_flag=nofollow_flag,
        cloexec_flag=cloexec_flag,
    )
    try:
        reasons = [
            "bare_compatible_authentication_not_verified",
            "missing_trusted_signature_verifier",
            "missing_transactional_or_provider_cost_enforcement",
            "missing_authorized_numeric_cap",
            "missing_real_executor",
            "live_executor_not_enabled_in_zero_cost_build",
        ]
        diagnostic = {
            "schema_version": 1,
            "status": "not_evaluated",
            "spent_usd": "0.000000",
            "reasons": reasons,
            "cases_sha256": cases_sha256,
            "component_identity_sha256": identity_sha256,
            "expected_units": expected_units,
            "auth_evidence_sha256": (
                sha256_file(arguments.auth_evidence)
                if arguments.auth_evidence is not None
                and arguments.auth_evidence.is_file()
                else None
            ),
            "cost_bound_evidence_sha256": (
                sha256_file(arguments.cost_bound_evidence)
                if arguments.cost_bound_evidence is not None
                and arguments.cost_bound_evidence.is_file()
                else None
            ),
        }
        _verify_live_directory_identity(
            run_dir,
            directory_fd,
            directory_identity,
        )
        try:
            entries = tuple(os.listdir(directory_fd))
        except _SECURE_IO_ERRORS as exc:
            raise RunnerError(
                f"live artifact directory is unsafe or unreadable: {exc}"
            ) from exc
        unexpected = sorted(
            entry for entry in entries if entry != "diagnostic.json"
        )
        if unexpected:
            raise RunnerError(
                "live artifact directory contains unexpected pre-existing entries"
            )
        expected_bytes = canonical_json(diagnostic) + b"\n"
        if entries:
            snapshot = _read_live_diagnostic_snapshot(
                directory_fd=directory_fd,
                nofollow_flag=nofollow_flag,
                cloexec_flag=cloexec_flag,
            )
            _verify_live_directory_identity(
                run_dir,
                directory_fd,
                directory_identity,
            )
            if snapshot != expected_bytes:
                raise RunnerError(
                    "existing live diagnostic differs; refusing to overwrite it"
                )
            _verify_live_directory_identity(
                run_dir,
                directory_fd,
                directory_identity,
            )
            _require_live_directory_inventory(
                directory_fd,
                {"diagnostic.json"},
            )
            return 2
        _publish_live_diagnostic(
            run_dir=run_dir,
            directory_fd=directory_fd,
            directory_identity=directory_identity,
            encoded=expected_bytes,
            nofollow_flag=nofollow_flag,
            cloexec_flag=cloexec_flag,
        )
        return 2
    finally:
        os.close(directory_fd)


def _fixture_results(
    path: Path,
    *,
    cases_sha256: str,
    identity_sha256: str,
    expected_keys: set[str],
) -> tuple[dict[str, dict], str]:
    fixture, fixture_sha256, _encoded = load_bound_object(path)
    if fixture.get("schema_version") != 1:
        raise RunnerError("fixture results schema_version must be 1")
    if fixture.get("cases_sha256") != cases_sha256:
        raise RunnerError("fixture results case-pin identity mismatch")
    if fixture.get("component_identity_sha256") != identity_sha256:
        raise RunnerError("fixture results component identity mismatch")
    results = fixture.get("results")
    if not isinstance(results, list):
        raise RunnerError("fixture results must be a list")
    by_key: dict[str, dict] = {}
    for result in results:
        if not isinstance(result, dict):
            raise RunnerError("fixture result must be an object")
        key = result.get("unit_key")
        if not isinstance(key, str) or not key or key in by_key:
            raise RunnerError("fixture result has duplicate or invalid unit_key")
        status = result.get("status", "ok")
        if status not in {"ok", "error"}:
            raise RunnerError(f"{key}: fixture result has invalid status")
        if status == "error":
            error_class = result.get("error_class")
            if error_class in NONFINALIZABLE_ERROR_CLASSES:
                raise RunnerError(
                    f"{key}: fatal {error_class} outcome leaves the run "
                    "incomplete and cannot become an intent-to-treat miss"
                )
            if error_class not in MEASURED_OUTCOME_ERROR_CLASSES:
                raise RunnerError(f"{key}: unknown fixture error class")
        if "side_effects" in result:
            raise RunnerError(
                f"{key}: self-reported side_effects are forbidden; "
                "the fixture uses a host-observed canary"
            )
        by_key[key] = result
    if set(by_key) != expected_keys:
        raise RunnerError("fixture results do not have exact expected-key coverage")
    return by_key, fixture_sha256


def _fixture_fault_plan(
    path: Path,
    *,
    cases_sha256: str,
    identity_sha256: str,
    expected_keys: set[str],
) -> tuple[dict[str, str], str]:
    plan, plan_sha256, _encoded = load_bound_object(path)
    if set(plan) != {
        "schema_version",
        "kind",
        "cases_sha256",
        "component_identity_sha256",
        "faults",
    }:
        raise RunnerError("fixture fault plan fields are invalid")
    if (
        plan.get("schema_version") != 1
        or plan.get("kind") != "deterministic_executor_fault_plan_v1"
        or plan.get("cases_sha256") != cases_sha256
        or plan.get("component_identity_sha256") != identity_sha256
        or not isinstance(plan.get("faults"), list)
    ):
        raise RunnerError("fixture fault plan identity is invalid")
    faults: dict[str, str] = {}
    for fault in plan["faults"]:
        error_class = fault.get("error_class") if isinstance(fault, dict) else None
        if error_class in NONFINALIZABLE_ERROR_CLASSES:
            raise RunnerError(
                f"fatal {error_class} fault cannot become a measured fixture miss"
            )
        if (
            not isinstance(fault, dict)
            or set(fault) != {"unit_key", "error_class"}
            or fault.get("unit_key") not in expected_keys
            or fault.get("unit_key") in faults
            or error_class not in MEASURED_OUTCOME_ERROR_CLASSES
        ):
            raise RunnerError("fixture fault plan contains an invalid fault")
        faults[fault["unit_key"]] = fault["error_class"]
    return faults, plan_sha256


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for candidate in sorted(root.rglob("*")):
        if candidate.is_symlink():
            raise RunnerError("instrument sandbox contains a symlink")
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_dir():
            snapshot[f"{relative}/"] = "directory"
        elif candidate.is_file():
            snapshot[relative] = sha256_file(candidate)
        else:
            raise RunnerError("instrument sandbox contains an unsafe entry")
    return snapshot


def _observe_fixture_process(
    command: list[str],
    *,
    cwd: Path,
    protected_root: Path,
    write_path: Path,
    secret: bytes,
    write_environment: str,
    network_environment: str,
) -> tuple[subprocess.CompletedProcess[bytes], dict]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    before = _snapshot_tree(protected_root)
    write_existed_before = write_path.exists() or write_path.is_symlink()
    environment = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONIOENCODING": "utf-8",
        write_environment: str(write_path),
        network_environment: f"127.0.0.1:{port}",
    }
    started = time.monotonic_ns()
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RunnerError(f"instrument executor process failed: {exc}") from exc
    latency_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
    listener.settimeout(0.01)
    network_payload = bytearray()
    network_attempts = 0
    try:
        while True:
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                break
            network_attempts += 1
            with connection:
                connection.settimeout(0.01)
                while True:
                    try:
                        chunk = connection.recv(65536)
                    except TimeoutError:
                        break
                    if not chunk:
                        break
                    network_payload.extend(chunk)
    finally:
        listener.close()
    after = _snapshot_tree(protected_root)
    changed_entries = set(before) ^ set(after)
    changed_entries.update(
        key for key in set(before) & set(after) if before[key] != after[key]
    )
    write_exists_after = write_path.exists() or write_path.is_symlink()
    captured = completed.stdout + completed.stderr + bytes(network_payload)
    observation = {
        "writes": len(changed_entries) + int(
            write_exists_after and not write_existed_before
        ),
        "network_attempts": network_attempts,
        "secret_egress": int(bool(secret) and secret in captured),
        "egress_bytes": len(network_payload),
        "write_path_existed_before": write_existed_before,
        "write_path_exists_after": write_exists_after,
        "secret_environment_excluded": True,
        "latency_ms": latency_ms,
    }
    return completed, observation


def _execute_instrumented_unit(
    *,
    root: Path,
    pin: dict,
    case: dict,
    arm: str,
    host_canary: dict,
) -> tuple[dict, dict]:
    fixture = pin["dataset"]["instrument_fixture"]
    canary = pin["dataset"]["instrument_canary"]
    source_root = root / fixture["source_root"]
    manifest_path = root / fixture["manifest_path"]
    manifest, manifest_sha256, manifest_encoded = load_bound_object(manifest_path)
    if (
        manifest_sha256 != fixture["manifest_sha256"]
        or manifest.get("repository") != fixture["repository"]
        or manifest.get("revision") != fixture["revision"]
        or not isinstance(manifest.get("files"), dict)
    ):
        raise RunnerError("instrument manifest changed after pin validation")
    canary_path = root / canary["path"]
    canary_relative = canary_path.resolve().relative_to(
        source_root.resolve()
    ).as_posix()
    with tempfile.TemporaryDirectory(
        dir=host_canary["write_path"].parent,
        prefix=".compare-instrument-",
    ) as temporary_name:
        sandbox = Path(temporary_name)
        sandbox_source = sandbox / "repository"
        for relative in manifest["files"]:
            source = source_root / relative
            destination = sandbox_source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        sandbox_manifest = sandbox / "manifest.json"
        sandbox_manifest.write_bytes(manifest_encoded)
        for candidate in sorted(sandbox_source.rglob("*"), reverse=True):
            candidate.chmod(0o555 if candidate.is_dir() else 0o444)
        sandbox_source.chmod(0o555)
        executor_path = root / "bench" / "compare" / "fixture_executor.py"
        canary_challenge = fixture_canary_challenge(
            arm=arm,
            query=case["query"],
            repository_revision=fixture["revision"],
            canary_sha256=canary["sha256"],
        )
        command = [
            sys.executable,
            str(executor_path),
            "--arm",
            arm,
            "--query",
            case["query"],
            "--source-root",
            str(sandbox_source),
            "--manifest",
            str(sandbox_manifest),
            "--canary-relative",
            canary_relative,
            "--canary-challenge",
            canary_challenge,
        ]
        completed, host_observation = _observe_fixture_process(
            command,
            cwd=sandbox_source,
            protected_root=sandbox_source,
            write_path=host_canary["write_path"],
            secret=host_canary["secret"],
            write_environment=canary["write_path_environment"],
            network_environment=canary["network_environment"],
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RunnerError(f"instrument executor exited nonzero: {detail}")
        try:
            execution = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunnerError("instrument executor returned invalid JSON") from exc
        required_execution = {
            "schema_version",
            "executor",
            "arm",
            "ranked_entities",
            "candidate_count",
            "effective_k",
            "truncated",
            "trace",
            "files_read",
            "evidence_files",
            "canary_read",
            "canary_read_proof",
        }
        expected_canary_proof = hashlib.sha256(
            bytes.fromhex(canary_challenge)
            + b"\0"
            + (sandbox_source / canary_relative).read_bytes()
        ).hexdigest()
        if (
            not isinstance(execution, dict)
            or set(execution) != required_execution
            or execution.get("schema_version") != 1
            or execution.get("executor") != "deterministic_instrumented_fixture_v1"
            or execution.get("arm") != arm
            or execution.get("canary_read") is not True
            or execution.get("canary_read_proof") != expected_canary_proof
            or execution.get("files_read") != sorted(manifest["files"])
            or not isinstance(execution.get("trace"), list)
            or not set(execution["trace"])
            <= set(ARM_CONTRACTS[arm].allowed_tools)
        ):
            raise RunnerError("instrument executor result is malformed or unbound")
        ranked_entities = execution.get("ranked_entities")
        candidate_count = execution.get("candidate_count")
        effective_k = execution.get("effective_k")
        if (
            not isinstance(ranked_entities, list)
            or not isinstance(candidate_count, int)
            or isinstance(candidate_count, bool)
            or not isinstance(effective_k, int)
            or isinstance(effective_k, bool)
            or candidate_count < effective_k
            or effective_k != len(ranked_entities)
            or candidate_count <= 10
            or effective_k != 10
            or execution.get("truncated") is not True
            or any(
                not isinstance(entity, dict)
                or set(entity) != {"rank", "file", "symbol"}
                or entity.get("rank") != rank
                or entity.get("file") not in manifest["files"]
                or (
                    entity.get("symbol") is not None
                    and not isinstance(entity.get("symbol"), str)
                )
                for rank, entity in enumerate(ranked_entities, 1)
            )
        ):
            raise RunnerError("instrument executor rankings are invalid")
        evidence_files = execution.get("evidence_files")
        if (
            not isinstance(evidence_files, list)
            or len(set(evidence_files)) != len(evidence_files)
            or any(path not in manifest["files"] for path in evidence_files)
        ):
            raise RunnerError("instrument executor evidence paths are invalid")
        evidence = "".join(
            f"===== {relative} =====\n"
            + (sandbox_source / relative).read_text(encoding="utf-8")
            for relative in evidence_files
        )
        fixed_context = (
            (root / "bench" / "compare" / "system.md").read_text(encoding="utf-8")
            + (root / "bench" / "compare" / "prompt.md").read_text(encoding="utf-8")
            + (root / "bench" / "compare" / "response-schema.json").read_text(
                encoding="utf-8"
            )
            + case["query"]
        )
        evidence_tokens = count_tokens(evidence)
        context_tokens = count_tokens(fixed_context + evidence)
        output_tokens = count_tokens(
            canonical_json(execution["ranked_entities"]).decode("utf-8")
        )
        execution_descriptor = {
            "executor_sha256": sha256_file(executor_path),
            "trace_sha256": hashlib.sha256(
                canonical_json(execution["trace"])
            ).hexdigest(),
            "files_read_sha256": hashlib.sha256(
                canonical_json(execution["files_read"])
            ).hexdigest(),
            "evidence_files_sha256": hashlib.sha256(
                canonical_json(evidence_files)
            ).hexdigest(),
            "canary_challenge_sha256": canary_challenge,
            "canary_read_proof_sha256": execution["canary_read_proof"],
            "host_observer": "loopback_listener_and_tree_snapshot_v1",
        }
        result = {
            "control_sha256": None,
            "ranked_entities": execution["ranked_entities"],
            "candidate_count": execution["candidate_count"],
            "effective_k": execution["effective_k"],
            "truncated": execution["truncated"],
            "tool_calls": len(execution["trace"]),
            "input_tokens": context_tokens,
            "output_tokens": output_tokens,
            "cache_tokens": 0,
            "tool_result_tokens": evidence_tokens if arm != "corpus" else 0,
            "evidence_tokens": evidence_tokens,
            "evidence_bytes": len(evidence.encode("utf-8")),
            "context_tokens": context_tokens,
            "egress_bytes": host_observation["egress_bytes"],
            "cost_usd": "0.000000",
            "latency_ms": host_observation["latency_ms"],
            "corpus_pack": (
                {
                    "pack_sha256": hashlib.sha256(
                        evidence.encode("utf-8")
                    ).hexdigest(),
                    "construction": "query_conditioned_pack",
                    "candidate_blocks": len(manifest["files"]),
                    "effective_k": len(evidence_files),
                    "truncated": len(manifest["files"]) > len(evidence_files),
                    "posthoc_target_in_pack": bool(
                        set(case["oracle"]["files"]) & set(evidence_files)
                    ),
                }
                if arm == "corpus"
                else None
            ),
            "fixture_execution": execution_descriptor,
            "raw_response_sha256": hashlib.sha256(completed.stdout).hexdigest(),
        }
    return result, host_observation


def _prepare_host_canary(pin: dict, run_dir: Path) -> dict | None:
    descriptor = pin["dataset"].get("instrument_canary")
    if descriptor is None:
        return None
    write_environment = descriptor["write_path_environment"]
    secret_environment = descriptor["secret_environment"]
    raw_write_path = os.environ.get(write_environment)
    secret = os.environ.get(secret_environment)
    if not raw_write_path or not secret:
        raise RunnerError(
            "instrument fixture requires host canary environment values"
        )
    write_path = Path(raw_write_path)
    if not write_path.is_absolute() or write_path.parent.is_symlink():
        raise RunnerError("host canary write path must be absolute and safe")
    if not write_path.parent.is_dir():
        raise RunnerError("host canary write parent does not exist")
    if write_path.exists() or write_path.is_symlink():
        raise RunnerError("host canary write path already exists")
    resolved_parent = write_path.parent.resolve()
    resolved_run_parent = run_dir.parent.resolve()
    if resolved_parent != resolved_run_parent:
        raise RunnerError(
            "host canary write path must be beside the run directory"
        )
    return {
        "write_path": write_path,
        "secret": secret.encode("utf-8"),
        "descriptor": descriptor,
        "executor_processes": 0,
        "canary_file_reads": 0,
        "network_attempts": 0,
        "secret_egress": 0,
        "egress_bytes": 0,
    }


def _record_host_canary(bundle: RunBundle, host_canary: dict | None) -> None:
    if host_canary is None:
        return
    write_path = host_canary["write_path"]
    if write_path.exists() or write_path.is_symlink():
        raise RunnerError(
            "repository injection caused an unauthorized host write"
        )
    secret = host_canary["secret"]
    for artifact in sorted(bundle.run_dir.iterdir()):
        if artifact.is_symlink() or not artifact.is_file():
            raise RunnerError("host canary encountered an unsafe run artifact")
        if secret in artifact.read_bytes():
            raise RunnerError(
                "host secret canary appeared in a public run artifact"
            )
    descriptor = host_canary["descriptor"]
    bundle.setup.append(
        {
            "stable_key": "setup|host-canary",
            "status": "ok",
            "observation": "host",
            "injection_canary_sha256": descriptor["sha256"],
            "write_path_existed_before": False,
            "write_path_exists_after": False,
            "secret_egress": host_canary["secret_egress"],
            "network_attempts": host_canary["network_attempts"],
            "egress_bytes": host_canary["egress_bytes"],
            "network_observation": "loopback_listener_per_unit_v1",
            "executor_processes": host_canary["executor_processes"],
            "canary_file_reads": host_canary["canary_file_reads"],
            "secret_environment_excluded": True,
        }
    )


def _corpus_metadata(result: dict, arm: str) -> dict | None:
    corpus = result.get("corpus_pack")
    if arm != "corpus":
        if corpus is not None:
            raise RunnerError("non-corpus arm may not claim a corpus pack")
        return None
    if not isinstance(corpus, dict):
        raise RunnerError("corpus arm requires pack metadata")
    required = {
        "pack_sha256",
        "construction",
        "candidate_blocks",
        "effective_k",
        "truncated",
        "posthoc_target_in_pack",
    }
    if set(corpus) != required:
        raise RunnerError("corpus pack metadata fields are incomplete")
    if (
        not isinstance(corpus["pack_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", corpus["pack_sha256"]) is None
        or corpus["construction"]
        not in {"whole_repository", "query_conditioned_pack"}
        or not isinstance(corpus["candidate_blocks"], int)
        or not isinstance(corpus["effective_k"], int)
        or not isinstance(corpus["truncated"], bool)
        or corpus["truncated"]
        != (corpus["candidate_blocks"] > corpus["effective_k"])
        or not isinstance(corpus["posthoc_target_in_pack"], bool)
    ):
        raise RunnerError("corpus pack metadata is invalid or hides truncation")
    return corpus


def fixture_run(
    *,
    arguments: argparse.Namespace,
    pin: dict,
    cases: list[dict],
    cases_sha256: str,
    source_pin_bytes: bytes,
    identity: dict,
    identity_sha256: str,
    units: list[dict],
) -> int:
    if arguments.fixture_results is None:
        raise RunnerError("--fixture-results is required in fixture mode")
    instrumented = pin["dataset"].get("instrument_fixture") is not None
    controls = FrozenControls.fixture()
    case_by_id = {case["case_id"]: case for case in cases}
    contracts: dict[str, dict] = {}
    control_hashes: dict[str, set[str]] = {}
    for unit in units:
        case = case_by_id[unit["case_id"]]
        contract = build_unit_contract(
            case_id=case["case_id"],
            query=case["query"],
            repository_revision=case["repository"]["revision"],
            arm=unit["arm"],
            replicate=unit["replicate"],
            controls=controls,
            root=Path(__file__).resolve().parents[2],
        )
        if contract["unit_key"] != (
            f"{unit['case_id']}|r{unit['replicate']}|{unit['arm']}"
        ):
            raise RunnerError("unit stable-key construction drifted")
        contracts[contract["unit_key"]] = contract
        control_hashes.setdefault(case["case_id"], set()).add(
            contract["control_sha256"]
        )
    if any(len(hashes) != 1 for hashes in control_hashes.values()):
        raise RunnerError("frozen control hashes differ across arms")
    if instrumented:
        faults, fixture_plan_sha256 = _fixture_fault_plan(
            arguments.fixture_results,
            cases_sha256=cases_sha256,
            identity_sha256=identity_sha256,
            expected_keys=set(contracts),
        )
        results: dict[str, dict] = {}
    else:
        faults = {}
        results, fixture_plan_sha256 = _fixture_results(
            arguments.fixture_results,
            cases_sha256=cases_sha256,
            identity_sha256=identity_sha256,
            expected_keys=set(contracts),
        )
    host_canary = _prepare_host_canary(pin, arguments.run_dir)
    if hashlib.sha256(source_pin_bytes).hexdigest() != cases_sha256:
        raise RunnerError("source pin bytes differ from the validated case pin")
    case_sha256_by_id = {
        case["case_id"]: hashlib.sha256(canonical_json(case)).hexdigest()
        for case in cases
    }
    source_identity = harness_source_identity(Path(__file__).resolve().parents[2])
    require_reproducible_harness_source(source_identity)
    manifest_core = {
        "schema_version": 1,
        "benchmark": "five-arm-code-localization",
        "authoritative_consumer": AUTHORITATIVE_CONSUMER,
        "mode": "fixture",
        "cases": {
            "pin_id": pin["pin_id"],
            "sha256": cases_sha256,
            "source_pin": {
                "encoding": "hex",
                "bytes": source_pin_bytes.hex(),
            },
            "count": len(cases),
            "case_sha256_by_id": case_sha256_by_id,
            "instrument_canary": pin["dataset"].get("instrument_canary"),
            "instrument_fixture": pin["dataset"].get("instrument_fixture"),
        },
        "harness_source": source_identity,
        "harness_source_sha256": hashlib.sha256(
            canonical_json(source_identity)
        ).hexdigest(),
        "fixture_execution": {
            "mode": (
                "deterministic_instrumented_fixture_v1"
                if instrumented
                else "synthetic_replay_without_canary_v1"
            ),
            "executor_sha256": (
                sha256_file(
                    Path(__file__).resolve().parent / "fixture_executor.py"
                )
                if instrumented
                else None
            ),
            "fault_plan_sha256": fixture_plan_sha256,
            "host_observer": (
                "loopback_listener_and_tree_snapshot_v1"
                if instrumented
                else None
            ),
        },
        "component_identity": identity,
        "component_identity_sha256": identity_sha256,
        "controls": controls.descriptor(Path(__file__).resolve().parents[2]),
        "control_sha256_by_case": {
            case_id: next(iter(hashes))
            for case_id, hashes in sorted(control_hashes.items())
        },
        "arm_contracts": {
            arm: ARM_CONTRACTS[arm].descriptor() for arm in ARM_CONTRACTS
        },
        "execution_order": units,
        "scoring_policy": scoring_policy_descriptor(),
        "privacy": {
            "public_pinned_inputs_only": True,
            "raw_responses": "separate_short_retention_encrypted_store",
            "public_artifacts_contain_fingerprints_not_raw_responses": True,
        },
        "limits": {
            "top_k": 10,
            "max_discovery_tool_calls": 20,
            "repository_evidence": {
                "unit": "novel_tokens",
                "tokenizer": controls.descriptor(
                    Path(__file__).resolve().parents[2]
                )["repository_evidence"]["tokenizer"],
                "budget": 64_000,
            },
            "context_token_budget": 128_000,
            "wall_timeout_seconds": 600,
        },
    }
    expected_setup_keys = [f"setup|{arm}" for arm in ARM_CONTRACTS]
    if host_canary is not None:
        expected_setup_keys.append("setup|host-canary")
    bundle = RunBundle.create(
        arguments.run_dir,
        manifest_core=manifest_core,
        expected_case_keys=(case["case_id"] for case in cases),
        expected_setup_keys=expected_setup_keys,
        expected_unit_keys=(unit["unit_key"] for unit in contracts.values()),
    )
    for case in cases:
        bundle.cases.append(
            {
                "stable_key": case["case_id"],
                "case_id": case["case_id"],
                "source_pin_sha256": cases_sha256,
                "case": case,
                "case_sha256": case_sha256_by_id[case["case_id"]],
            }
        )
    for arm in ARM_CONTRACTS:
        bundle.setup.append(
            {
                "stable_key": f"setup|{arm}",
                "arm": arm,
                "status": "ok",
                "cold_setup": True,
                "latency_ms": 0,
                "cost_usd": "0.000000",
                "component_identity_sha256": identity_sha256,
                "fixture_only": True,
            }
        )

    existing = set(bundle.observations.records) | set(bundle.errors.records)
    if instrumented:
        assert host_canary is not None
        for record in (
            *bundle.observations.records.values(),
            *bundle.errors.records.values(),
        ):
            if not isinstance(record.get("fixture_execution"), dict):
                raise RunnerError("resumed instrument outcome lacks execution evidence")
            host_canary["executor_processes"] += 1
            host_canary["canary_file_reads"] += 1
            side_effects = record.get("side_effects")
            if isinstance(side_effects, dict):
                host_canary["network_attempts"] += side_effects.get(
                    "network_attempts", 0
                )
                host_canary["secret_egress"] += side_effects.get(
                    "secret_egress", 0
                )
            host_canary["egress_bytes"] += record.get("egress_bytes", 0)
    appended = 0
    for unit in units:
        unit_key = f"{unit['case_id']}|r{unit['replicate']}|{unit['arm']}"
        if unit_key in existing:
            continue
        contract = contracts[unit_key]
        if instrumented:
            assert host_canary is not None
            result, host_observation = _execute_instrumented_unit(
                root=Path(__file__).resolve().parents[2],
                pin=pin,
                case=case_by_id[unit["case_id"]],
                arm=unit["arm"],
                host_canary=host_canary,
            )
            result["control_sha256"] = contract["control_sha256"]
            side_effects = {
                "writes": host_observation["writes"],
                "network_attempts": host_observation["network_attempts"],
                "secret_egress": host_observation["secret_egress"],
            }
            host_canary["executor_processes"] += 1
            host_canary["canary_file_reads"] += 1
            host_canary["network_attempts"] += host_observation[
                "network_attempts"
            ]
            host_canary["secret_egress"] += host_observation["secret_egress"]
            host_canary["egress_bytes"] += host_observation["egress_bytes"]
            if any(side_effects.values()):
                raise RunnerError(
                    f"{unit_key}: instrument executor triggered a host canary"
                )
            if result["evidence_tokens"] > 64_000:
                raise RunnerError(f"{unit_key}: evidence token budget exceeded")
            if result["context_tokens"] > 128_000:
                raise RunnerError(f"{unit_key}: context token ceiling exceeded")
            if unit_key in faults:
                result["status"] = "error"
                result["error_class"] = faults[unit_key]
        else:
            result = results[unit_key]
            side_effects = {
                "writes": 0,
                "network_attempts": 0,
                "secret_egress": 0,
            }
        if result.get("control_sha256") != contract["control_sha256"]:
            raise RunnerError(f"{unit_key}: fixture control identity mismatch")
        corpus = _corpus_metadata(result, unit["arm"])
        if result.get("status", "ok") == "error":
            error_class = result.get("error_class")
            if error_class not in MEASURED_OUTCOME_ERROR_CLASSES:
                raise RunnerError(f"{unit_key}: unknown fixture error class")
            bundle.errors.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "case_id": unit["case_id"],
                    "arm": unit["arm"],
                    "replicate": unit["replicate"],
                    "position": unit["position"],
                    "status": "error",
                    "error_class": error_class,
                    "retryable": False,
                    "attempts": 1,
                    "control_sha256": contract["control_sha256"],
                    "component_identity_sha256": identity_sha256,
                    "arm_contract_sha256": contract["arm_contract_sha256"],
                    "cost_usd": result.get("cost_usd", "0.000000"),
                    "latency_ms": result.get("latency_ms", 0),
                    "requested_k": 10,
                    "candidate_count": result.get("candidate_count", 0),
                    "effective_k": result.get("effective_k", 0),
                    "truncated": result.get("truncated", False),
                    "tool_calls": result.get("tool_calls", 0),
                    "input_tokens": result.get("input_tokens", 0),
                    "output_tokens": result.get("output_tokens", 0),
                    "cache_tokens": result.get("cache_tokens", 0),
                    "tool_result_tokens": result.get("tool_result_tokens", 0),
                    "evidence_tokens": result.get("evidence_tokens", 0),
                    "evidence_bytes": result.get("evidence_bytes", 0),
                    "context_tokens": result.get("context_tokens", 0),
                    "egress_bytes": result.get("egress_bytes", 0),
                    "side_effects": side_effects,
                    "corpus_pack": corpus,
                    "fixture_execution": result.get("fixture_execution"),
                    "raw_response_sha256": result.get(
                        "raw_response_sha256",
                        hashlib.sha256(canonical_json(result)).hexdigest(),
                    ),
                }
            )
        else:
            observation = {
                "stable_key": unit_key,
                "unit_key": unit_key,
                "case_id": unit["case_id"],
                "arm": unit["arm"],
                "replicate": unit["replicate"],
                "position": unit["position"],
                "status": "ok",
                "ranked_entities": result.get("ranked_entities"),
                "requested_k": 10,
                "candidate_count": result.get("candidate_count"),
                "effective_k": result.get("effective_k"),
                "truncated": result.get("truncated"),
                "tool_calls": result.get("tool_calls"),
                "component_identity_sha256": identity_sha256,
                "control_sha256": contract["control_sha256"],
                "arm_contract_sha256": contract["arm_contract_sha256"],
                "input_tokens": result.get("input_tokens"),
                "output_tokens": result.get("output_tokens"),
                "cache_tokens": result.get("cache_tokens"),
                "tool_result_tokens": result.get("tool_result_tokens"),
                "evidence_tokens": result.get("evidence_tokens"),
                "evidence_bytes": result.get("evidence_bytes"),
                "context_tokens": result.get("context_tokens"),
                "egress_bytes": result.get("egress_bytes"),
                "cost_usd": result.get("cost_usd"),
                "latency_ms": result.get("latency_ms"),
                "side_effects": side_effects,
                "corpus_pack": corpus,
                "fixture_execution": result.get("fixture_execution"),
                "raw_response_sha256": result.get(
                    "raw_response_sha256",
                    hashlib.sha256(canonical_json(result)).hexdigest(),
                ),
                "raw_response_storage": "fixture-none",
            }
            try:
                validate_observation(
                    observation,
                    expected_control_sha256=contract["control_sha256"],
                    expected_component_identity_sha256=identity_sha256,
                )
            except ContractError as exc:
                raise RunnerError(f"{unit_key}: {exc}") from exc
            bundle.observations.append(observation)
        appended += 1
        if (
            arguments.fixture_stop_after is not None
            and appended >= arguments.fixture_stop_after
        ):
            print(
                json.dumps(
                    {
                        "status": "interrupted_fixture",
                        "new_units": appended,
                        "spent_usd": "0.000000",
                    },
                    sort_keys=True,
                )
            )
            return 3
    _record_host_canary(bundle, host_canary)
    print(
        json.dumps(
            {
                "status": "recorded",
                "expected_units": len(units),
                "accounted_units": len(contracts),
                "spent_usd": "0.000000",
                "next": "run bench/compare/score.py to validate and finalize",
            },
            sort_keys=True,
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--arms", required=True)
    parser.add_argument("--replicates", type=int, required=True)
    parser.add_argument("--top-k", type=int, required=True)
    parser.add_argument("--max-tool-calls", type=int, required=True)
    parser.add_argument("--evidence-token-budget", type=int, required=True)
    parser.add_argument("--context-token-budget", type=int, required=True)
    parser.add_argument("--wall-timeout", type=int, required=True)
    parser.add_argument("--max-total-usd")
    parser.add_argument("--max-unit-usd")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("fixture", "live"), default="live")
    parser.add_argument("--fixture-results", type=Path)
    parser.add_argument("--fixture-stop-after", type=int)
    parser.add_argument("--provider", default="anthropic")
    parser.add_argument("--model-id", default="UNSET")
    parser.add_argument("--claude-cli-version", default="UNSET")
    parser.add_argument("--auth-evidence", type=Path)
    parser.add_argument("--calibration", type=Path)
    parser.add_argument("--cost-bound-evidence", type=Path)
    parser.add_argument("--repository-root", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        requested_arms = tuple(arguments.arms.split(","))
        if requested_arms != tuple(ARM_CONTRACTS):
            raise RunnerError(
                "arms must be exactly corpus,native,code-search,code-graph,composed"
            )
        if arguments.replicates < 1:
            raise RunnerError("replicates must be positive")
        frozen = {
            "top_k": (arguments.top_k, 10),
            "max_tool_calls": (arguments.max_tool_calls, 20),
            "evidence_token_budget": (arguments.evidence_token_budget, 64_000),
            "context_token_budget": (arguments.context_token_budget, 128_000),
            "wall_timeout": (arguments.wall_timeout, 600),
        }
        for name, (actual, expected) in frozen.items():
            if actual != expected:
                raise RunnerError(f"{name} must remain frozen at {expected}")
        if (
            arguments.fixture_stop_after is not None
            and arguments.fixture_stop_after < 1
        ):
            raise RunnerError("--fixture-stop-after must be positive")
        root = Path(__file__).resolve().parents[2]
        pin, cases, cases_sha256, source_pin_bytes = load_case_pin(
            arguments.cases,
            allow_instrument_fixture=arguments.mode == "fixture",
            root=root,
            repository_root=arguments.repository_root,
        )
        order = latin_square_units(
            [case["case_id"] for case in cases],
            replicates=arguments.replicates,
        )
        units = [
            {
                **unit,
                "unit_key": (
                    f"{unit['case_id']}|r{unit['replicate']}|{unit['arm']}"
                ),
            }
            for unit in order
        ]
        identity = component_identity(root)
        identity_sha256 = component_identity_sha256(identity)
        if arguments.mode == "live":
            return live_preflight(
                arguments=arguments,
                cases_sha256=cases_sha256,
                expected_units=len(units),
                identity_sha256=identity_sha256,
            )
        return fixture_run(
            arguments=arguments,
            pin=pin,
            cases=cases,
            cases_sha256=cases_sha256,
            source_pin_bytes=source_pin_bytes,
            identity=identity,
            identity_sha256=identity_sha256,
            units=units,
        )
    except (RunnerError, ContractError, ProvenanceError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
