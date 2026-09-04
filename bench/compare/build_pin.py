#!/usr/bin/env python3
"""Build, verify, or locally prepare content-addressed LocBench pins."""

from __future__ import annotations

import argparse
import ast
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Self


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

from bench.compare.provenance import atomic_write_json  # noqa: E402
from bench.compare.schema import canonical_json  # noqa: E402


SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
    r"(?=[A-Za-z0-9_.-]{1,100}\Z)"
    r"(?=[A-Za-z0-9_.-]*[A-Za-z0-9])"
    r"[A-Za-z0-9_.-]+"
)
SYMBOL = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)
CATEGORY_MAP = {
    "Bug Report": "Bug",
    "Feature Request": "Feature",
    "Performance Issue": "Performance",
    "Security Vulnerability": "Security",
}
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
JUNE_REFERENCE_PATH = (
    PLUGIN_ROOT / "bench" / "compare" / "pins" / "locbench-june-n200.external.json"
)
JUNE_EXTERNAL_PIN_SHA256 = (
    "886156bbd16eb753a690da6bcb452f9238f53ef28409b1f4e483b842a0556453"
)
JUNE_RECORDED_ORDER_SHA256 = (
    "de161cf07a6310209d78e9f2e2f05ca28a21cfcdeef1b871279d39ea24d54577"
)
JUNE_DATASET_REVISION = "c44cf3b74e07ca642cec841b471a9939907c12a7"
JUNE_PARQUET_SHA256 = (
    "8df0833c2c1276c5837aab923d489ab97d7654529abe759d0f59242c4978a662"
)
JUNE_PARQUET_SIZE = 3_084_430
GIT_EXECUTABLE = shutil.which("git") or "git"
GIT_DIFF_ARGUMENTS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--diff-algorithm=myers",
    "--no-indent-heuristic",
    "--no-color",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--unified=3",
    "--inter-hunk-context=0",
    "--ignore-submodules=none",
    "--no-relative",
    f"-O{os.devnull}",
)
GIT_RAW_OBJECT_DELTA_FORMAT = "git_raw_object_delta_v1"
LOCBENCH_PYTHON_GRAMMAR = (3, 12)
LOCBENCH_SYMBOL_VERIFICATION = "locagent_evaluator_python_ast_py312_v3"
MAX_GIT_OBJECT_STORE_DEPTH = 128


class PinError(ValueError):
    """Source labels or content addresses are insufficient for a valid pin."""


def _require_directory_fd_capabilities(
    context: str,
    *,
    dir_fd_functions: tuple[object, ...],
    follow_symlinks_functions: tuple[object, ...] = (),
) -> None:
    dir_fd_support = getattr(os, "supports_dir_fd", ())
    follow_support = getattr(os, "supports_follow_symlinks", ())
    if any(function not in dir_fd_support for function in dir_fd_functions):
        raise PinError(
            f"{context}: required dir_fd operations are unavailable "
            "on this platform"
        )
    if any(
        function not in follow_support
        for function in follow_symlinks_functions
    ):
        raise PinError(
            f"{context}: required no-follow operations are unavailable "
            "on this platform"
        )
    if not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0):
        raise PinError(
            f"{context}: required no-follow directory flags are unavailable "
            "on this platform"
        )


class _PinnedGitCheckout:
    """Retained checkout and Git-admin descriptors for one audit."""

    def __init__(
        self,
        *,
        slug: str,
        checkout_descriptor: int,
        git_descriptor: int,
    ) -> None:
        self.slug = slug
        self.checkout_descriptor = checkout_descriptor
        self.git_descriptor = git_descriptor
        self._generation: _GitAdminGeneration | None = None
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                if self._generation is not None:
                    self._generation.close()
            finally:
                try:
                    os.close(self.git_descriptor)
                finally:
                    os.close(self.checkout_descriptor)

    def retain_generation(self, generation: _GitAdminGeneration) -> None:
        if self._generation is not None:
            raise PinError(f"{self.slug}: Git admin generation is already retained")
        self._generation = generation

    def generation(self) -> _GitAdminGeneration:
        if self._generation is None:
            raise PinError(f"{self.slug}: Git admin generation is unavailable")
        return self._generation


def _descriptor_generation(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
    )


def _descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _object_store_metadata(
    metadata: os.stat_result,
) -> tuple[str, int, int, int, int, int, int]:
    if stat.S_ISDIR(metadata.st_mode):
        entry_type = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        entry_type = "regular"
    else:
        entry_type = "unsafe"
    return (
        entry_type,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_ctime_ns,
        metadata.st_mtime_ns,
        metadata.st_nlink,
    )


def _snapshot_git_object_store(
    git_descriptor: int,
    slug: str,
) -> tuple[
    tuple[str, str, int, int, int, int, int, int],
    ...,
]:
    _require_directory_fd_capabilities(
        f"{slug}: Git object generation",
        dir_fd_functions=(os.open, os.stat),
        follow_symlinks_functions=(os.stat,),
    )
    base_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    directory_flags = base_flags | getattr(os, "O_DIRECTORY", 0)
    entries: list[tuple[str, str, int, int, int, int, int, int]] = []

    objects_descriptor = -1
    stack: list[
        tuple[
            int,
            str,
            int,
            tuple[str, int, int, int, int, int, int],
            list[str],
        ]
    ] = []
    next_indexes: list[int] = []
    try:
        objects_descriptor = os.open(
            "objects",
            directory_flags,
            dir_fd=git_descriptor,
        )
        initial_snapshot = _object_store_metadata(
            os.fstat(objects_descriptor)
        )
        if initial_snapshot[0] != "directory":
            raise PinError(f"{slug}: Git object store contains an unsafe entry")
        entries.append(("objects", *initial_snapshot))
        stack.append(
            (
                objects_descriptor,
                "objects",
                0,
                initial_snapshot,
                sorted(os.listdir(objects_descriptor)),
            )
        )
        next_indexes.append(0)
        objects_descriptor = -1
        while stack:
            (
                directory,
                relative_path,
                depth,
                directory_snapshot,
                names,
            ) = stack[-1]
            index = next_indexes[-1]
            if index == len(names):
                if (
                    _object_store_metadata(os.fstat(directory))
                    != directory_snapshot
                ):
                    raise PinError(
                        f"{slug}: Git object store changed during "
                        "generation capture"
                    )
                os.close(directory)
                stack.pop()
                next_indexes.pop()
                continue
            next_indexes[-1] += 1
            name = names[index]
            before_snapshot = _object_store_metadata(
                os.stat(
                    name,
                    dir_fd=directory,
                    follow_symlinks=False,
                )
            )
            if before_snapshot[0] == "unsafe":
                raise PinError(
                    f"{slug}: Git object store contains an unsafe entry"
                )
            descriptor = os.open(
                name,
                (
                    directory_flags
                    if before_snapshot[0] == "directory"
                    else base_flags
                ),
                dir_fd=directory,
            )
            try:
                opened_snapshot = _object_store_metadata(
                    os.fstat(descriptor)
                )
                if opened_snapshot != before_snapshot:
                    raise PinError(
                        f"{slug}: Git object store changed during "
                        "generation capture"
                    )
                child_path = f"{relative_path}/{name}"
                if opened_snapshot[0] == "directory":
                    if depth >= MAX_GIT_OBJECT_STORE_DEPTH:
                        raise PinError(
                            f"{slug}: Git object store depth exceeds "
                            "the supported limit"
                        )
                    child_names = sorted(os.listdir(descriptor))
                    entries.append((child_path, *opened_snapshot))
                    stack.append(
                        (
                            descriptor,
                            child_path,
                            depth + 1,
                            opened_snapshot,
                            child_names,
                        )
                    )
                    next_indexes.append(0)
                    descriptor = -1
                else:
                    if (
                        _object_store_metadata(os.fstat(descriptor))
                        != opened_snapshot
                    ):
                        raise PinError(
                            f"{slug}: Git object store changed during "
                            "generation capture"
                        )
                    entries.append((child_path, *opened_snapshot))
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    except PinError:
        raise
    except (OSError, NotImplementedError) as exc:
        raise PinError(
            f"{slug}: Git object store is missing, unsafe, or changed "
            "during generation capture"
        ) from exc
    finally:
        if objects_descriptor >= 0:
            os.close(objects_descriptor)
        for descriptor, *_frame in reversed(stack):
            os.close(descriptor)
    return tuple(entries)


class _GitAdminGeneration:
    """Retained Git-admin and object state for one repository snapshot."""

    def __init__(
        self,
        *,
        slug: str,
        directories: list[tuple[str, int, tuple[int, int, int, int]]],
        files: list[
            tuple[str, int, tuple[int, int, int, int], int, str]
        ],
        object_store: tuple[
            tuple[str, str, int, int, int, int, int, int],
            ...,
        ],
    ) -> None:
        self.slug = slug
        self.directories = directories
        self.files = files
        self.object_store = object_store

    def validate(self) -> None:
        try:
            for _path, descriptor, expected in self.directories:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or _descriptor_generation(metadata) != expected
                ):
                    raise PinError(
                        f"{self.slug}: Git admin state changed during "
                        "pinned Git inspection"
                    )
            for _path, descriptor, expected, expected_size, expected_hash in (
                self.files
            ):
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or _descriptor_generation(metadata) != expected
                    or metadata.st_size != expected_size
                    or _descriptor_sha256(descriptor) != expected_hash
                ):
                    raise PinError(
                        f"{self.slug}: Git admin state changed during "
                        "pinned Git inspection"
                    )
        except OSError as exc:
            raise PinError(
                f"{self.slug}: Git admin state changed during "
                "pinned Git inspection"
            ) from exc

    def validate_object_store(self) -> None:
        self.validate()
        actual = _snapshot_git_object_store(
            self.directories[0][1],
            self.slug,
        )
        self.validate()
        if actual != self.object_store:
            raise PinError(
                f"{self.slug}: Git object store changed during "
                "pinned Git inspection"
            )

    def close(self) -> None:
        for _path, descriptor, *_snapshot in reversed(
            [*self.directories, *self.files]
        ):
            os.close(descriptor)


def _open_git_admin_descriptor(
    git_descriptor: int,
    relative_path: str,
    *,
    directory: bool,
) -> int | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
    current = os.dup(git_descriptor)
    try:
        parts = Path(relative_path).parts
        for component in parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=current)
            os.close(current)
            current = child
        child = os.open(
            parts[-1],
            directory_flags if directory else flags,
            dir_fd=current,
        )
        os.close(current)
        return child
    except FileNotFoundError:
        os.close(current)
        return None
    except Exception:
        os.close(current)
        raise


def _capture_git_admin_generation(
    repository: _PinnedGitCheckout,
) -> _GitAdminGeneration:
    _require_directory_fd_capabilities(
        f"{repository.slug}: Git admin generation",
        dir_fd_functions=(os.open,),
    )
    directories = [
        (
            ".",
            os.dup(repository.git_descriptor),
            _descriptor_generation(os.fstat(repository.git_descriptor)),
        )
    ]
    files: list[
        tuple[str, int, tuple[int, int, int, int], int, str]
    ] = []
    pending_files: list[tuple[str, int]] = []
    try:
        for relative_path in ("info", "objects", "objects/info"):
            descriptor = _open_git_admin_descriptor(
                repository.git_descriptor,
                relative_path,
                directory=True,
            )
            if descriptor is None:
                continue
            metadata = os.fstat(descriptor)
            directories.append(
                (
                    relative_path,
                    descriptor,
                    _descriptor_generation(metadata),
                )
            )
        for relative_path in ("config", "config.worktree"):
            try:
                descriptor = _open_git_admin_descriptor(
                    repository.git_descriptor,
                    relative_path,
                    directory=False,
                )
                if descriptor is None:
                    continue
                pending_files.append((relative_path, descriptor))
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise PinError(
                        f"{repository.slug}: Git admin config is unsafe"
                    )
                descriptor_hash = _descriptor_sha256(descriptor)
            except PinError:
                raise
            except (OSError, NotImplementedError) as exc:
                raise PinError(
                    f"{repository.slug}: Git admin config is missing or unsafe"
                ) from exc
            files.append(
                (
                    relative_path,
                    descriptor,
                    _descriptor_generation(metadata),
                    metadata.st_size,
                    descriptor_hash,
                )
            )
            pending_files.pop()
        generation = _GitAdminGeneration(
            slug=repository.slug,
            directories=directories,
            files=files,
            object_store=_snapshot_git_object_store(
                repository.git_descriptor,
                repository.slug,
            ),
        )
        generation.validate()
        return generation
    except Exception:
        for _path, descriptor, *_snapshot in reversed(
            [*directories, *files, *pending_files]
        ):
            os.close(descriptor)
        raise


def _root_owned_nonwritable_directory_chain(path: Path) -> bool:
    if not path.is_absolute():
        return False
    for candidate in reversed((path, *path.parents)):
        try:
            metadata = os.lstat(candidate)
        except OSError:
            return False
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            return False
    return True


def _safe_system_alias_target(
    path: Path,
    metadata: os.stat_result,
) -> Path | None:
    if (
        metadata.st_uid != 0
        or not _root_owned_nonwritable_directory_chain(path.parent)
    ):
        return None
    try:
        raw_target = Path(os.readlink(path))
    except OSError:
        return None
    target = (
        raw_target
        if raw_target.is_absolute()
        else path.parent / raw_target
    )
    target = Path(os.path.abspath(target))
    if not _root_owned_nonwritable_directory_chain(target):
        return None
    return target


def _require_external_artifact_path(path: Path, kind: str) -> Path:
    requested = Path(os.path.abspath(path))
    for _ in range(8):
        alias_resolved = False
        for candidate in reversed((requested, *requested.parents)):
            try:
                metadata = os.lstat(candidate)
            except FileNotFoundError:
                break
            except OSError as exc:
                raise PinError(
                    f"prepare-june {kind} path cannot be inspected safely"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                target = (
                    None
                    if candidate == requested
                    else _safe_system_alias_target(candidate, metadata)
                )
                if target is None:
                    raise PinError(
                        f"prepare-june {kind} path contains a symlink: "
                        f"{candidate}"
                    )
                requested = target.joinpath(
                    *requested.relative_to(candidate).parts
                )
                alias_resolved = True
                break
            if candidate != requested and not stat.S_ISDIR(metadata.st_mode):
                raise PinError(
                    f"prepare-june {kind} path cannot be inspected safely"
                )
        if not alias_resolved:
            break
    else:
        raise PinError(f"prepare-june {kind} path has too many system aliases")
    _require_external_artifact_ancestry(requested.parent, kind)
    return requested


def validate_june_reference(reference: dict) -> None:
    expected = {
        "schema_version": 1,
        "kind": "external_content_address",
        "repository": "brandyn-s/code-graph",
        "source_revision": "d7b93959dace3215cd096a13c1a27e259063dc95",
        "path": (
            "bench/accuracy/baselines/data/"
            "2026-06-12-matched-depth-n200/locbench-n200-pin.json"
        ),
        "sha256": JUNE_EXTERNAL_PIN_SHA256,
        "expected_count": 200,
        "score_depth": 10,
        "recorded_order_sha256": JUNE_RECORDED_ORDER_SHA256,
        "availability": "published",
        "runnable": False,
        "blockers": ["missing_query_oracle_labels"],
        "dataset": {
            "repository": "czlll/Loc-Bench_V1",
            "revision": JUNE_DATASET_REVISION,
            "parquet_sha256": JUNE_PARQUET_SHA256,
        },
    }
    if any(reference.get(key) != value for key, value in expected.items()):
        raise PinError("checked-in June reference identity is unexpected")
    if "cases" in reference or "queries" in reference:
        raise PinError("checked-in June reference contains forbidden case data")


def _read_input_snapshot(path: Path, context: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
        os,
        "O_NOFOLLOW",
        0,
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise PinError(f"cannot read {context} {path}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PinError(f"{context} must be a regular no-follow file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise PinError(f"cannot read {context} {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def _object_from_snapshot(encoded: bytes, path: Path) -> dict:
    try:
        value = json.loads(encoded)
    except UnicodeDecodeError as exc:
        raise PinError(f"cannot load {path}: malformed UTF-8 input") from exc
    except json.JSONDecodeError as exc:
        raise PinError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PinError(f"{path}: expected an object")
    return value


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise PinError(f"cannot hash {path}: {exc}") from exc


def load_object(path: Path) -> dict:
    if path.is_symlink():
        raise PinError(f"refusing symlink input: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PinError(f"cannot load {path}: malformed UTF-8 input") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PinError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PinError(f"{path}: expected an object")
    return value


def _safe_path(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise PinError(f"{context}: path must be nonempty")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise PinError(f"{context}: path must be repository-relative")
    return path.as_posix()


def _read_contained_snapshot(
    root: Path,
    relative_path: str,
    context: str,
) -> bytes:
    """Read a regular cache file without following any relative component."""
    _require_directory_fd_capabilities(
        context,
        dir_fd_functions=(os.open,),
    )
    relative = Path(_safe_path(relative_path, context))
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = (root / relative).resolve(strict=True)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PinError(f"{context}: cache path is missing or unsafe") from exc

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(root, directory_flags))
        for component in relative.parts[:-1]:
            descriptors.append(
                os.open(
                    component,
                    directory_flags,
                    dir_fd=descriptors[-1],
                )
            )
        descriptor = os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=descriptors[-1],
        )
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PinError(f"{context}: cache file is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except (OSError, NotImplementedError) as exc:
        raise PinError(f"{context}: cache path is missing or unsafe") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _selection_digest(seed: int, category: str, case_id: str) -> str:
    material = (
        f"locbench-n40|selection=sha256_priority_v1|seed={seed}|"
        f"category={category}|case_id={case_id}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _locbench_oracle(edit_functions: object, case_id: str) -> dict:
    if (
        not isinstance(edit_functions, list)
        or not edit_functions
        or any(not isinstance(value, str) for value in edit_functions)
    ):
        raise PinError(f"{case_id}: edit_functions must be a nonempty string list")
    files: list[str] = []
    classes: list[str] = []
    functions: list[str] = []
    for value in edit_functions:
        path, separator, symbol = value.partition(":")
        if (
            not separator
            or ":" in symbol
            or SYMBOL.fullmatch(symbol) is None
        ):
            raise PinError(f"{case_id}: malformed edit_functions label")
        files.append(_safe_path(path, f"{case_id}:edit_functions"))
        classes.append(symbol.split(".", 1)[0])
        functions.append(
            symbol.removesuffix(".__init__")
            if symbol.endswith(".__init__")
            else symbol
        )
    return {
        "files": _ordered_unique(files),
        "classes": _ordered_unique(classes),
        "functions": _ordered_unique(functions),
    }


def _validated_june_source_rows(
    rows: list[dict],
    external_pin: dict,
) -> list[tuple[dict, dict]]:
    """Validate the complete selected population without reading label fields."""
    external_cases = external_pin.get("cases")
    pinned_ids = external_pin.get("pinned_instance_ids")
    if (
        external_pin.get("schema_version") != 1
        or external_pin.get("n") != 200
        or external_pin.get("score_depth") != 10
        or not isinstance(external_cases, list)
        or len(external_cases) != 200
        or not isinstance(pinned_ids, list)
        or len(pinned_ids) != 200
        or any(
            not isinstance(case_id, str) or not case_id
            for case_id in pinned_ids
        )
        or len(set(pinned_ids)) != 200
        or [
            case.get("instance_id") if isinstance(case, dict) else None
            for case in external_cases
        ]
        != pinned_ids
    ):
        raise PinError("external pin does not define exactly 200 ordered cases")
    if not isinstance(rows, list) or len(rows) != 560:
        raise PinError("LocBench parquet must contain exactly 560 source rows")
    by_id: dict[str, dict] = {}
    for index, row in enumerate(rows):
        case_id = row.get("instance_id") if isinstance(row, dict) else None
        if (
            not isinstance(case_id, str)
            or not case_id
            or case_id in by_id
        ):
            raise PinError(
                f"LocBench row[{index}] has duplicate or invalid instance_id"
            )
        by_id[case_id] = row
    selected: list[tuple[dict, dict]] = []
    for external in external_cases:
        assert isinstance(external, dict)
        case_id = external["instance_id"]
        row = by_id.get(case_id)
        if row is None:
            raise PinError(f"{case_id}: selected source row is missing")
        repository = row.get("repo")
        base_commit = row.get("base_commit")
        category = row.get("category")
        if (
            repository != external.get("repo")
            or base_commit != external.get("base_commit")
            or category != external.get("category")
        ):
            raise PinError(f"{case_id}: source row identity differs from external pin")
        if (
            not isinstance(repository, str)
            or REPOSITORY.fullmatch(repository) is None
            or not isinstance(base_commit, str)
            or REVISION.fullmatch(base_commit) is None
            or category not in CATEGORY_MAP
        ):
            raise PinError(f"{case_id}: source row identity is malformed")
        selected.append((external, row))
    return selected


def _prepare_june_source_case(external: dict, row: dict) -> dict:
    case_id = external["instance_id"]
    query = row.get("problem_statement")
    if not isinstance(query, str) or not query.strip():
        raise PinError(f"{case_id}: source row query is malformed")
    return {
        "case_id": case_id,
        "category": CATEGORY_MAP[row["category"]],
        "query": query,
        "repository": {
            "url": f"https://github.com/{row['repo']}",
            "revision": row["base_commit"],
        },
        "oracle": _locbench_oracle(row.get("edit_functions"), case_id),
    }


def prepare_june_source_cases(rows: list[dict], external_pin: dict) -> list[dict]:
    """Strictly derive evaluator labels after validating every identity."""
    return [
        _prepare_june_source_case(external, row)
        for external, row in _validated_june_source_rows(rows, external_pin)
    ]


def prepare_june_source_cases_with_quarantine(
    rows: list[dict],
    external_pin: dict,
) -> tuple[list[dict], list[dict]]:
    """Derive labels, sanitizing label-only failures into quarantine records."""
    prepared: list[dict] = []
    quarantined: list[dict] = []
    for external, row in _validated_june_source_rows(rows, external_pin):
        case_id = external["instance_id"]
        try:
            prepared.append(_prepare_june_source_case(external, row))
        except PinError:
            quarantined.append(
                {
                    "case_id": case_id,
                    "category": CATEGORY_MAP[row["category"]],
                    "stage": "source_labels",
                    "reason_code": "source_label_unverified",
                    "identities": {
                        "requested_repository": row["repo"],
                        "base_commit": row["base_commit"],
                        "pr_number": _pull_request_number(case_id),
                    },
                }
            )
    return prepared, quarantined


def read_june_parquet_rows(source: bytes | Path) -> list[dict]:
    """Read only preparation columns; pyarrow is an operator-only dependency."""
    encoded = (
        _read_input_snapshot(source, "LocBench parquet")
        if isinstance(source, Path)
        else source
    )
    try:
        from pyarrow import BufferReader
        from pyarrow import parquet
    except (ImportError, OSError) as exc:
        raise PinError(
            "prepare-june pyarrow loader is unavailable; "
            "pyarrow is required only for parquet reading; "
            "run `python3 -m pip install pyarrow` in the operator environment"
        ) from exc
    try:
        table = parquet.read_table(
            BufferReader(encoded),
            columns=[
                "instance_id",
                "repo",
                "base_commit",
                "category",
                "problem_statement",
                "edit_functions",
            ],
        )
        rows = table.to_pylist()
    except Exception as exc:
        raise PinError(f"cannot parse LocBench parquet snapshot: {exc}") from exc
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) for row in rows
    ):
        raise PinError("LocBench parquet did not produce object rows")
    return rows


def load_verified_june_parquet_rows(path: Path) -> list[dict]:
    encoded = _read_input_snapshot(path, "LocBench parquet")
    if len(encoded) != JUNE_PARQUET_SIZE:
        raise PinError(
            "LocBench parquet size mismatch: "
            f"expected {JUNE_PARQUET_SIZE}, got {len(encoded)}"
        )
    parquet_sha256 = hashlib.sha256(encoded).hexdigest()
    if parquet_sha256 != JUNE_PARQUET_SHA256:
        raise PinError(
            "LocBench parquet SHA-256 mismatch: "
            f"expected {JUNE_PARQUET_SHA256}, got {parquet_sha256}"
        )
    return read_june_parquet_rows(encoded)


def _pull_request_number(case_id: str) -> int:
    match = re.search(r"-([1-9][0-9]*)$", case_id)
    if match is None:
        raise PinError(f"{case_id}: terminal pull-request number is missing")
    return int(match.group(1))


def load_cached_pull_request(
    cache_root: Path,
    *,
    case_id: str,
    repository: str,
) -> dict:
    """Load one GitHub PR response through an explicit content-addressed index."""
    if (
        REPOSITORY.fullmatch(repository) is None
        or not cache_root.is_dir()
        or cache_root.is_symlink()
    ):
        raise PinError(f"{case_id}: GitHub PR response cache is missing or unsafe")
    pr_number = _pull_request_number(case_id)
    request_url = (
        f"https://api.github.com/repos/{repository}/pulls/{pr_number}"
    )
    index_path = cache_root / "index.json"
    index = _object_from_snapshot(
        _read_contained_snapshot(
            cache_root,
            "index.json",
            f"{case_id}:PR-cache index",
        ),
        index_path,
    )
    responses = index.get("responses")
    entry = responses.get(request_url) if isinstance(responses, dict) else None
    if index.get("schema_version") != 1 or not isinstance(entry, dict):
        raise PinError(f"{case_id}: cached GitHub PR response is not indexed")
    response_sha256 = entry.get("sha256")
    response_path = entry.get("path")
    expected_path = (
        f"responses/{response_sha256}.json"
        if isinstance(response_sha256, str)
        else None
    )
    if (
        SHA256.fullmatch(str(response_sha256)) is None
        or response_path != expected_path
    ):
        raise PinError(f"{case_id}: GitHub PR cache entry is not content addressed")
    relative = _safe_path(response_path, f"{case_id}:PR-cache")
    response_bytes = _read_contained_snapshot(
        cache_root,
        relative,
        f"{case_id}:PR-cache response",
    )
    if hashlib.sha256(response_bytes).hexdigest() != response_sha256:
        raise PinError(f"{case_id}: cached GitHub PR response SHA-256 mismatch")
    try:
        response = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PinError(f"{case_id}: cached GitHub PR response is invalid JSON") from exc
    if not isinstance(response, dict):
        raise PinError(f"{case_id}: cached GitHub PR response must be an object")
    base = response.get("base")
    head = response.get("head")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    head_repo = head.get("repo") if isinstance(head, dict) else None
    resolved_repository = (
        base_repo.get("full_name") if isinstance(base_repo, dict) else None
    )
    head_repository = (
        head_repo.get("full_name") if isinstance(head_repo, dict) else None
    )
    base_commit = base.get("sha") if isinstance(base, dict) else None
    head_commit = head.get("sha") if isinstance(head, dict) else None
    merge_commit = response.get("merge_commit_sha")
    api_url = response.get("url")
    html_url = response.get("html_url")
    if (
        response.get("number") != pr_number
        or response.get("state") != "closed"
        or response.get("merged") is not True
        or not isinstance(resolved_repository, str)
        or REPOSITORY.fullmatch(resolved_repository) is None
        or not isinstance(head_repository, str)
        or REPOSITORY.fullmatch(head_repository) is None
        or REVISION.fullmatch(str(base_commit)) is None
        or REVISION.fullmatch(str(head_commit)) is None
        or REVISION.fullmatch(str(merge_commit)) is None
        or api_url
        != (
            "https://api.github.com/repos/"
            f"{resolved_repository}/pulls/{pr_number}"
        )
        or html_url
        != f"https://github.com/{resolved_repository}/pull/{pr_number}"
    ):
        raise PinError(f"{case_id}: cached GitHub PR identity is malformed")
    return {
        "request_api_url": request_url,
        "api_url": api_url,
        "html_url": html_url,
        "requested_repository": repository,
        "resolved_repository": resolved_repository,
        "repository_redirected": resolved_repository != repository,
        "head_repository": head_repository,
        "pr_number": pr_number,
        "merged": True,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "merge_commit": merge_commit,
        "response_sha256": response_sha256,
    }


def _patch_changed_files(patch: str, case_id: str) -> list[str]:
    changed: set[str] = set()
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            raise PinError(f"{case_id}: invalid unified diff header") from exc
        if len(parts) != 4 or parts[:2] != ["diff", "--git"]:
            raise PinError(f"{case_id}: invalid unified diff header")
        old_path, new_path = parts[2:]
        selected = new_path if new_path != "/dev/null" else old_path
        if selected.startswith(("a/", "b/")):
            selected = selected[2:]
        changed.add(_safe_path(selected, f"{case_id}:patch"))
    if not changed:
        raise PinError(f"{case_id}: patch has no derivable changed files")
    return sorted(changed)


def _symbol_is_defined(
    contents: list[str],
    symbol: str,
    *,
    kind: str,
) -> bool:
    """Apply the language-neutral definition_pattern_v1 contract."""
    leaf = symbol.rsplit(".", 1)[-1]
    if not leaf or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", leaf) is None:
        return False
    escaped = re.escape(leaf)
    if kind == "class":
        patterns = (
            rf"\bclass\s+{escaped}\b",
            rf"\b(struct|interface|enum|trait|type)\s+{escaped}\b",
        )
    else:
        patterns = (
            rf"\b(?:async\s+)?def\s+{escaped}\s*\(",
            rf"\bfn\s+{escaped}\s*\(",
            rf"\bfunction\s+{escaped}\s*\(",
            rf"\bfunc\s+(?:\([^)]*\)\s*)?{escaped}\s*\(",
        )
    return any(
        re.search(pattern, content) is not None
        for content in contents
        for pattern in patterns
    )


def _definition_pattern_unverified_symbols(
    oracle: dict,
    contents: dict[str, str],
) -> list[str] | None:
    classes = oracle.get("classes")
    functions = oracle.get("functions")
    if (
        not isinstance(classes, list)
        or not isinstance(functions, list)
        or any(
            not isinstance(symbol, str)
            for symbol in (*classes, *functions)
        )
    ):
        return None
    snapshots = list(contents.values())
    return _ordered_unique(
        [
            symbol
            for kind, symbols in (
                ("class", classes),
                ("function", functions),
            )
            for symbol in symbols
            if not _symbol_is_defined(snapshots, symbol, kind=kind)
        ]
    )


class _PythonDefinitionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.classes: set[str] = set()
        self.functions: set[str] = set()

    def _qualified(self, name: str) -> str:
        return ".".join((*self.scope, name))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.add(self._qualified(node.name))
        self.scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    def visit_FunctionDef(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.functions.add(self._qualified(node.name))
        self.scope.append(node.name)
        for child in node.body:
            self.visit(child)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def _python_definitions(
    contents: dict[str, str],
) -> tuple[set[str], set[str]] | None:
    visitor = _PythonDefinitionVisitor()
    for path, content in sorted(contents.items()):
        if Path(path).suffix not in {".py", ".pyi"}:
            return None
        try:
            tree = ast.parse(
                content,
                filename=path,
                type_comments=True,
                feature_version=LOCBENCH_PYTHON_GRAMMAR,
            )
        except (SyntaxError, ValueError):
            return None
        visitor.visit(tree)
    return visitor.classes, visitor.functions


def _python_oracle_unverified_symbols(
    oracle: dict,
    contents: dict[str, str],
    *,
    locbench_normalization: bool,
) -> list[str] | None:
    classes = oracle.get("classes")
    functions = oracle.get("functions")
    if (
        not isinstance(classes, list)
        or not isinstance(functions, list)
        or any(
            not isinstance(symbol, str)
            or SYMBOL.fullmatch(symbol) is None
            for symbol in (*classes, *functions)
        )
    ):
        return None
    definitions = _python_definitions(contents)
    if definitions is None:
        return None
    defined_classes, defined_functions = definitions
    unverified = [
        symbol
        for symbol in classes
        if symbol not in defined_classes
        and (
            not locbench_normalization
            or symbol not in defined_functions
        )
    ]
    unverified.extend(
        symbol
        for symbol in functions
        if symbol not in defined_functions
        and (
            not locbench_normalization
            or symbol not in defined_classes
        )
    )
    return _ordered_unique(unverified)


def _audit_cases(evidence: dict) -> dict[str, dict]:
    if evidence.get("schema_version") != 1:
        raise PinError("audit evidence schema_version must be 1")
    cases = evidence.get("cases")
    if not isinstance(cases, list) or not cases:
        raise PinError("audit evidence must contain cases")
    by_id: dict[str, dict] = {}
    for index, case in enumerate(cases):
        case_id = case.get("instance_id") if isinstance(case, dict) else None
        if not isinstance(case_id, str) or not case_id or case_id in by_id:
            raise PinError(
                f"audit evidence case[{index}] has duplicate or invalid instance_id"
            )
        by_id[case_id] = case
    return by_id


def _git_bytes(
    repository: _PinnedGitCheckout,
    *arguments: str,
    allow_failure: bool = False,
    attributes_source: str | None = None,
) -> bytes | None:
    if (
        attributes_source is not None
        and REVISION.fullmatch(attributes_source) is None
    ):
        raise PinError(
            f"{repository.slug}: Git attribute source is not an exact "
            "object pin"
        )
    generation = repository.generation()
    environment = {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_COMMON_DIR": ".",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_DIR": ".",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }

    def enter_pinned_git_directory() -> None:
        os.fchdir(repository.git_descriptor)

    try:
        _validate_git_admin_state(repository)
        generation.validate()
        # build_pin is a single-threaded operator CLI. Retaining the directory
        # descriptor across fork and changing directory in the child prevents a
        # checkout-path rename/swap from redirecting any audit command.
        completed = subprocess.run(
            [
                GIT_EXECUTABLE,
                "--no-replace-objects",
                "--no-optional-locks",
                "-c",
                "core.quotepath=false",
                "-c",
                f"core.attributesFile={os.devnull}",
                "-c",
                "diff.algorithm=myers",
                "-c",
                "diff.renames=false",
                "-c",
                "diff.external=",
                "-c",
                "diff.indentHeuristic=false",
                "-c",
                "fetch.writeCommitGraph=false",
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                *(
                    [f"--attr-source={attributes_source}"]
                    if attributes_source is not None
                    else []
                ),
                *arguments,
            ],
            capture_output=True,
            check=False,
            env=environment,
            pass_fds=(repository.git_descriptor,),
            preexec_fn=enter_pinned_git_directory,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PinError(f"cannot inspect pinned Git objects: {exc}") from exc
    finally:
        _validate_git_admin_state(repository)
        generation.validate()
    if completed.returncode != 0:
        if allow_failure:
            return None
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PinError(f"pinned Git object verification failed: {detail}")
    return completed.stdout


def _git_admin_entry_exists(
    common_descriptor: int,
    relative_path: str,
) -> bool:
    _require_directory_fd_capabilities(
        "Git common directory inspection",
        dir_fd_functions=(os.open, os.stat),
        follow_symlinks_functions=(os.stat,),
    )
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    current = common_descriptor
    try:
        parts = Path(relative_path).parts
        for component in parts[:-1]:
            try:
                current = os.open(
                    component,
                    directory_flags,
                    dir_fd=current,
                )
            except FileNotFoundError:
                return False
            descriptors.append(current)
        try:
            os.stat(
                parts[-1],
                dir_fd=current,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        return True
    except (OSError, NotImplementedError) as exc:
        raise PinError(
            "Git common directory contains an unsafe topology path"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _validate_git_admin_state(repository: _PinnedGitCheckout) -> None:
    try:
        checkout = os.fstat(repository.checkout_descriptor)
        git_admin = os.fstat(repository.git_descriptor)
    except OSError as exc:
        raise PinError(
            f"{repository.slug}: pinned Git descriptors are unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(checkout.st_mode)
        or not stat.S_ISDIR(git_admin.st_mode)
    ):
        raise PinError(f"{repository.slug}: pinned Git descriptors are unsafe")
    forbidden = {
        "info/attributes": "mutable local Git attributes",
        "info/grafts": "legacy graft",
        "objects/info/alternates": "alternate object database",
        "objects/info/http-alternates": "HTTP alternate object database",
        "shallow": "shallow topology",
    }
    for relative_path, mechanism in forbidden.items():
        if _git_admin_entry_exists(
            repository.git_descriptor,
            relative_path,
        ):
            raise PinError(
                f"{repository.slug}: {mechanism} is forbidden alternate "
                "Git topology"
            )


def _verify_git_object_integrity(repository: _PinnedGitCheckout) -> None:
    result = _git_bytes(
        repository,
        "fsck",
        "--full",
        "--strict",
        "--no-dangling",
    )
    assert result is not None


def _repository_checkout(
    repository_root: Path,
    slug: str,
) -> _PinnedGitCheckout:
    root = Path(os.path.abspath(repository_root))
    if REPOSITORY.fullmatch(slug) is None:
        raise PinError(f"{slug}: pinned Git checkout is missing or unsafe")
    _require_directory_fd_capabilities(
        f"{slug}: pinned Git checkout",
        dir_fd_functions=(os.open,),
    )
    parts = slug.split("/")
    assert len(parts) == 2
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    checkout_descriptor = -1
    git_descriptor = -1
    try:
        checkout_descriptor = os.open(root, flags)
        for component in parts:
            child = os.open(
                component,
                flags,
                dir_fd=checkout_descriptor,
            )
            os.close(checkout_descriptor)
            checkout_descriptor = child
        git_descriptor = os.open(
            ".git",
            flags,
            dir_fd=checkout_descriptor,
        )
        pinned = _PinnedGitCheckout(
            slug=slug,
            checkout_descriptor=checkout_descriptor,
            git_descriptor=git_descriptor,
        )
        checkout_descriptor = -1
        git_descriptor = -1
        try:
            _validate_git_admin_state(pinned)
            pinned.retain_generation(
                _capture_git_admin_generation(pinned)
            )
            _verify_git_object_integrity(pinned)
        except Exception:
            pinned.close()
            raise
        return pinned
    except (OSError, NotImplementedError) as exc:
        raise PinError(f"{slug}: pinned Git checkout is missing or unsafe") from exc
    finally:
        if git_descriptor >= 0:
            os.close(git_descriptor)
        if checkout_descriptor >= 0:
            os.close(checkout_descriptor)


class GitAuditBatch:
    """Lazily retain one repository generation through a case batch."""

    def __init__(self, repository_root: Path, repository: str) -> None:
        self.repository_root = repository_root
        self.repository = repository
        self._checkout: _PinnedGitCheckout | None = None
        self._entered = False
        self._closed = False

    def __enter__(self) -> Self:
        if self._entered or self._closed:
            raise PinError(f"{self.repository}: Git audit batch cannot be reused")
        self._entered = True
        return self

    def __exit__(self, *_exc_info: object) -> None:
        try:
            if self._checkout is not None:
                _validate_git_admin_state(self._checkout)
                self._checkout.generation().validate_object_store()
        finally:
            self._closed = True
            if self._checkout is not None:
                self._checkout.close()

    def checkout(self) -> _PinnedGitCheckout:
        if not self._entered or self._closed:
            raise PinError(f"{self.repository}: Git audit batch is not active")
        if self._checkout is None:
            self._checkout = _repository_checkout(
                self.repository_root,
                self.repository,
            )
        return self._checkout


def _selected_audit_checkout(
    repository: str,
    *,
    checkout: _PinnedGitCheckout | None,
    batch: GitAuditBatch | None,
) -> _PinnedGitCheckout | None:
    if checkout is not None and batch is not None:
        raise PinError(
            f"{repository}: Git audit checkout and batch are mutually exclusive"
        )
    if batch is not None:
        if batch.repository != repository:
            raise PinError(f"{repository}: Git audit batch identity mismatch")
        return batch.checkout()
    if checkout is not None and checkout.slug != repository:
        raise PinError(f"{repository}: Git audit checkout identity mismatch")
    return checkout


def _validated_audit(
    case_id: str,
    repository: str,
    base_commit: str,
    evidence: dict,
    *,
    repository_root: Path,
    oracle_files: list[str],
    checkout: _PinnedGitCheckout | None = None,
) -> tuple[
    list[str],
    dict[str, str],
    str,
    str,
    dict[str, str],
    dict[str, str],
]:
    head_commit = evidence.get("head_commit")
    if (
        set(evidence)
        != {
            "instance_id",
            "repository",
            "base_commit",
            "head_commit",
        }
        or evidence.get("instance_id") != case_id
        or evidence.get("repository") != repository
        or evidence.get("base_commit") != base_commit
        or not isinstance(head_commit, str)
        or REVISION.fullmatch(head_commit) is None
    ):
        raise PinError(f"{case_id}: malformed Git-object audit evidence")
    if checkout is None:
        with GitAuditBatch(repository_root, repository) as batch:
            return _validated_audit(
                case_id,
                repository,
                base_commit,
                evidence,
                repository_root=repository_root,
                oracle_files=oracle_files,
                checkout=batch.checkout(),
            )
    if checkout.slug != repository:
        raise PinError(f"{case_id}: Git audit checkout identity mismatch")
    return _validated_audit_with_checkout(
        checkout,
        case_id=case_id,
        base_commit=base_commit,
        head_commit=head_commit,
        oracle_files=oracle_files,
    )


def _validated_audit_with_checkout(
    checkout: _PinnedGitCheckout,
    *,
    case_id: str,
    base_commit: str,
    head_commit: str,
    oracle_files: list[str],
) -> tuple[
    list[str],
    dict[str, str],
    str,
    str,
    dict[str, str],
    dict[str, str],
]:
    raw_base = _git_bytes(checkout, "rev-parse", f"{base_commit}^{{commit}}")
    raw_head = _git_bytes(checkout, "rev-parse", f"{head_commit}^{{commit}}")
    assert raw_base is not None and raw_head is not None
    resolved_base = raw_base.decode("ascii").strip()
    resolved_head = raw_head.decode("ascii").strip()
    if resolved_base != base_commit or resolved_head != head_commit:
        raise PinError(f"{case_id}: Git object IDs are not exact commit pins")
    if (
        _git_bytes(
            checkout,
            "merge-base",
            "--is-ancestor",
            resolved_base,
            resolved_head,
            allow_failure=True,
        )
        is None
    ):
        raise PinError(f"{case_id}: head commit does not descend from base commit")
    patch = _git_bytes(
        checkout,
        "diff",
        "--binary",
        "--full-index",
        *GIT_DIFF_ARGUMENTS,
        resolved_base,
        resolved_head,
        "--",
        attributes_source=resolved_head,
    )
    changed_raw = _git_bytes(
        checkout,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=ACDMRTUXB",
        *GIT_DIFF_ARGUMENTS,
        resolved_base,
        resolved_head,
        "--",
        attributes_source=resolved_head,
    )
    assert patch is not None and changed_raw is not None
    try:
        changed = sorted(
            _safe_path(encoded.decode("utf-8"), f"{case_id}:git-diff")
            for encoded in changed_raw.split(b"\0")
            if encoded
        )
    except UnicodeDecodeError as exc:
        raise PinError(f"{case_id}: changed Git path is not UTF-8") from exc
    if not changed:
        raise PinError(f"{case_id}: pinned Git diff has no changed files")
    snapshots: dict[str, str] = {}
    contents: dict[str, str] = {}
    blob_oids: dict[str, str] = {}
    for path in oracle_files:
        content_bytes = _git_bytes(
            checkout,
            "show",
            f"{resolved_head}:{path}",
            allow_failure=True,
        )
        blob_bytes = _git_bytes(
            checkout,
            "rev-parse",
            f"{resolved_head}:{path}",
            allow_failure=True,
        )
        if content_bytes is None or blob_bytes is None:
            continue
        try:
            contents[path] = content_bytes.decode("utf-8")
            blob_oid = blob_bytes.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise PinError(f"{case_id}: oracle Git blob is not UTF-8") from exc
        if REVISION.fullmatch(blob_oid) is None:
            raise PinError(f"{case_id}: oracle Git blob identity is malformed")
        snapshots[path] = hashlib.sha256(content_bytes).hexdigest()
        blob_oids[path] = blob_oid
    return (
        changed,
        snapshots,
        hashlib.sha256(patch).hexdigest(),
        resolved_head,
        contents,
        blob_oids,
    )


def _validated_git_object_delta(
    checkout: _PinnedGitCheckout,
    base_commit: str,
    head_commit: str,
    *,
    context: str,
) -> tuple[list[str], str]:
    raw = _git_bytes(
        checkout,
        "diff-tree",
        "--raw",
        "-z",
        "-r",
        "--no-commit-id",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "--full-index",
        "--abbrev=64",
        "--ignore-submodules=none",
        "--diff-filter=ADMTUXB",
        base_commit,
        head_commit,
        "--",
    )
    assert raw is not None
    fields = raw.split(b"\0")
    if fields[-1:] != [b""] or (len(fields) - 1) % 2 != 0:
        raise PinError(f"{context}: malformed raw Git object delta")
    entries: list[dict[str, str]] = []
    paths: set[str] = set()
    for index in range(0, len(fields) - 1, 2):
        metadata_raw = fields[index]
        path_raw = fields[index + 1]
        try:
            metadata = metadata_raw.decode("ascii")
            path = path_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PinError(f"{context}: raw Git object delta is not UTF-8") from exc
        metadata_fields = (
            metadata[1:].split(" ") if metadata.startswith(":") else []
        )
        if len(metadata_fields) != 5:
            raise PinError(f"{context}: malformed raw Git object delta entry")
        old_mode, new_mode, old_oid, new_oid, status = metadata_fields
        safe_path = _safe_path(path, context)
        if (
            safe_path != path
            or safe_path in paths
            or re.fullmatch(r"[0-7]{6}", old_mode) is None
            or re.fullmatch(r"[0-7]{6}", new_mode) is None
            or REVISION.fullmatch(old_oid) is None
            or REVISION.fullmatch(new_oid) is None
            or len(old_oid) != len(new_oid)
            or status not in {"A", "D", "M", "T", "U", "X", "B"}
        ):
            raise PinError(f"{context}: unsafe raw Git object delta entry")
        old_absent = old_mode == "000000"
        new_absent = new_mode == "000000"
        if (
            old_absent != (old_oid == "0" * len(old_oid))
            or new_absent != (new_oid == "0" * len(new_oid))
            or (status == "A" and (not old_absent or new_absent))
            or (status == "D" and (old_absent or not new_absent))
            or (status in {"M", "T", "B"} and (old_absent or new_absent))
        ):
            raise PinError(f"{context}: inconsistent raw Git object delta entry")
        paths.add(safe_path)
        entries.append(
            {
                "status": status,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "old_oid": old_oid,
                "new_oid": new_oid,
                "path": safe_path,
            }
        )
    entries.sort(
        key=lambda entry: (
            entry["path"],
            entry["status"],
            entry["old_mode"],
            entry["new_mode"],
            entry["old_oid"],
            entry["new_oid"],
        )
    )
    comparison = {
        "format": GIT_RAW_OBJECT_DELTA_FORMAT,
        "entries": entries,
    }
    return (
        sorted(paths),
        hashlib.sha256(canonical_json(comparison)).hexdigest(),
    )


def _validated_pr_comparison(
    case_id: str,
    repository: str,
    base_commit: str,
    head_commit: str,
    merge_commit: str,
    *,
    repository_root: Path,
    oracle_files: list[str],
    checkout: _PinnedGitCheckout | None = None,
) -> tuple[
    list[str],
    dict[str, str],
    str,
    str,
    str,
    str,
    str,
    dict[str, str],
    dict[str, str],
]:
    if checkout is None:
        with GitAuditBatch(repository_root, repository) as batch:
            return _validated_pr_comparison(
                case_id,
                repository,
                base_commit,
                head_commit,
                merge_commit,
                repository_root=repository_root,
                oracle_files=oracle_files,
                checkout=batch.checkout(),
            )
    if checkout.slug != repository:
        raise PinError(f"{case_id}: Git audit checkout identity mismatch")
    return _validated_pr_comparison_with_checkout(
        checkout,
        case_id=case_id,
        base_commit=base_commit,
        head_commit=head_commit,
        merge_commit=merge_commit,
        oracle_files=oracle_files,
    )


def _validated_pr_comparison_with_checkout(
    checkout: _PinnedGitCheckout,
    *,
    case_id: str,
    base_commit: str,
    head_commit: str,
    merge_commit: str,
    oracle_files: list[str],
) -> tuple[
    list[str],
    dict[str, str],
    str,
    str,
    str,
    str,
    str,
    dict[str, str],
    dict[str, str],
]:
    resolved: dict[str, str] = {}
    for name, object_id in (
        ("base", base_commit),
        ("head", head_commit),
        ("merge", merge_commit),
    ):
        raw = _git_bytes(
            checkout,
            "rev-parse",
            f"{object_id}^{{commit}}",
            allow_failure=True,
        )
        if raw is None:
            raise PinError(f"{case_id}: pinned PR {name} object is missing")
        try:
            resolved[name] = raw.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise PinError(f"{case_id}: pinned PR object ID is malformed") from exc
        if resolved[name] != object_id:
            raise PinError(f"{case_id}: PR object IDs are not exact commit pins")
    raw_merge_bases = _git_bytes(
        checkout,
        "merge-base",
        "--all",
        resolved["base"],
        resolved["head"],
    )
    assert raw_merge_bases is not None
    try:
        merge_bases = raw_merge_bases.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise PinError(f"{case_id}: PR merge-base is malformed") from exc
    if len(merge_bases) != 1 or REVISION.fullmatch(merge_bases[0]) is None:
        raise PinError(
            f"{case_id}: PR comparison requires exactly one best merge-base"
        )
    merge_base = merge_bases[0]
    topology = (
        "descendant"
        if _git_bytes(
            checkout,
            "merge-base",
            "--is-ancestor",
            resolved["base"],
            resolved["head"],
            allow_failure=True,
        )
        is not None
        else "non_descendant"
    )
    comparison_base = merge_base
    changed, comparison_sha256 = _validated_git_object_delta(
        checkout,
        comparison_base,
        resolved["head"],
        context=f"{case_id}:PR-diff",
    )
    if not changed:
        raise PinError(f"{case_id}: pinned PR comparison has no changed files")
    raw_merge_parents = _git_bytes(
        checkout,
        "rev-list",
        "--parents",
        "-n",
        "1",
        resolved["merge"],
    )
    assert raw_merge_parents is not None
    try:
        merge_line = raw_merge_parents.decode("ascii").strip().split()
    except UnicodeDecodeError as exc:
        raise PinError(f"{case_id}: PR merge ancestry is malformed") from exc
    merge_parents = merge_line[1:] if merge_line[:1] == [resolved["merge"]] else []
    if resolved["merge"] == resolved["head"] and topology == "descendant":
        merge_relation = "head_fast_forward_v1"
    elif merge_parents == [resolved["base"], resolved["head"]]:
        merge_relation = "exact_two_parent_merge_v1"
    elif merge_parents == [resolved["base"]]:
        _, merge_comparison_sha256 = _validated_git_object_delta(
            checkout,
            resolved["base"],
            resolved["merge"],
            context=f"{case_id}:PR-merge-diff",
        )
        if merge_comparison_sha256 != comparison_sha256:
            raise PinError(f"{case_id}: PR merge object is unrelated to comparison")
        merge_relation = "exact_single_parent_object_delta_v2"
    else:
        raise PinError(f"{case_id}: PR merge object is unrelated to comparison")
    snapshots: dict[str, str] = {}
    contents: dict[str, str] = {}
    blob_oids: dict[str, str] = {}
    for path in oracle_files:
        content_bytes = _git_bytes(
            checkout,
            "show",
            f"{resolved['head']}:{path}",
            allow_failure=True,
        )
        blob_bytes = _git_bytes(
            checkout,
            "rev-parse",
            f"{resolved['head']}:{path}",
            allow_failure=True,
        )
        if content_bytes is None or blob_bytes is None:
            continue
        try:
            contents[path] = content_bytes.decode("utf-8")
            blob_oid = blob_bytes.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise PinError(f"{case_id}: oracle PR blob is not UTF-8") from exc
        if REVISION.fullmatch(blob_oid) is None:
            raise PinError(f"{case_id}: oracle PR blob identity is malformed")
        snapshots[path] = hashlib.sha256(content_bytes).hexdigest()
        blob_oids[path] = blob_oid
    return (
        changed,
        snapshots,
        comparison_sha256,
        resolved["head"],
        merge_base,
        topology,
        merge_relation,
        contents,
        blob_oids,
    )


def _pull_request_evidence_is_valid(
    case_id: str,
    repository: str,
    base_commit: str,
    evidence: object,
) -> bool:
    if not isinstance(evidence, dict):
        return False
    required = {
        "request_api_url",
        "api_url",
        "html_url",
        "requested_repository",
        "resolved_repository",
        "repository_redirected",
        "head_repository",
        "pr_number",
        "merged",
        "base_commit",
        "head_commit",
        "merge_commit",
        "response_sha256",
    }
    number = _pull_request_number(case_id)
    requested = evidence.get("requested_repository")
    return (
        set(evidence) == required
        and evidence.get("resolved_repository") == repository
        and evidence.get("base_commit") == base_commit
        and evidence.get("pr_number") == number
        and evidence.get("merged") is True
        and evidence.get("repository_redirected")
        is (requested != repository)
        and isinstance(requested, str)
        and REPOSITORY.fullmatch(requested) is not None
        and REPOSITORY.fullmatch(str(evidence.get("head_repository"))) is not None
        and REVISION.fullmatch(str(evidence.get("head_commit"))) is not None
        and REVISION.fullmatch(str(evidence.get("merge_commit"))) is not None
        and SHA256.fullmatch(str(evidence.get("response_sha256"))) is not None
        and evidence.get("request_api_url")
        == f"https://api.github.com/repos/{requested}/pulls/{number}"
        and evidence.get("api_url")
        == f"https://api.github.com/repos/{repository}/pulls/{number}"
        and evidence.get("html_url")
        == f"https://github.com/{repository}/pull/{number}"
    )


def _locbench_symbols_are_defined(
    oracle: dict,
    contents: dict[str, str],
) -> bool:
    unverified = _python_oracle_unverified_symbols(
        oracle,
        contents,
        locbench_normalization=True,
    )
    return unverified == []


def derive_pr_label_audit(
    *,
    case_id: str,
    repository: str,
    base_commit: str,
    oracle: dict,
    pull_request: dict,
    repository_root: Path,
    checkout: _PinnedGitCheckout | None = None,
    batch: GitAuditBatch | None = None,
) -> dict:
    """Bind source labels to the exact PR merge-base-to-head comparison."""
    files = oracle.get("files") if isinstance(oracle, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(path, str) for path in files)
        or not _pull_request_evidence_is_valid(
            case_id,
            repository,
            base_commit,
            pull_request,
        )
    ):
        raise PinError(f"{case_id}: PR label inputs are malformed")
    normalized_files = [_safe_path(path, f"{case_id}:oracle") for path in files]
    checkout = _selected_audit_checkout(
        repository,
        checkout=checkout,
        batch=batch,
    )
    (
        changed,
        snapshots,
        comparison_sha256,
        head_commit,
        merge_base,
        topology,
        merge_relation,
        contents,
        blob_oids,
    ) = _validated_pr_comparison(
        case_id,
        repository,
        base_commit,
        pull_request["head_commit"],
        pull_request["merge_commit"],
        repository_root=repository_root,
        oracle_files=normalized_files,
        checkout=checkout,
    )
    if (
        not set(normalized_files) <= set(changed)
        or not set(normalized_files) <= set(snapshots)
        or not _locbench_symbols_are_defined(
            oracle,
            {
                path: contents[path]
                for path in normalized_files
                if path in contents
            },
        )
    ):
        raise PinError(f"{case_id}: source labels are not grounded in PR Git blobs")
    audit = {
        "status": "verified",
        "verifier": "pinned_pr_object_delta_v2",
        "repository": repository,
        "changed_files": changed,
        "changed_files_source": GIT_RAW_OBJECT_DELTA_FORMAT,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "merge_base_commit": merge_base,
        "comparison_base_commit": merge_base,
        "comparison_head_commit": head_commit,
        "comparison_format": GIT_RAW_OBJECT_DELTA_FORMAT,
        "comparison_sha256": comparison_sha256,
        "topology": topology,
        "merge_relation": merge_relation,
        "merge_commit": pull_request["merge_commit"],
        "oracle_file_sha256": {
            path: snapshots[path] for path in normalized_files
        },
        "oracle_blob_oid": {
            path: blob_oids[path] for path in normalized_files
        },
        "symbol_verification": LOCBENCH_SYMBOL_VERIFICATION,
        "pull_request": pull_request,
    }
    audit["audit_record_sha256"] = hashlib.sha256(canonical_json(audit)).hexdigest()
    return audit


def derive_git_label_audit(
    *,
    case_id: str,
    repository: str,
    base_commit: str,
    head_commit: str,
    oracle: dict,
    repository_root: Path,
    checkout: _PinnedGitCheckout | None = None,
    batch: GitAuditBatch | None = None,
) -> dict:
    files = oracle.get("files") if isinstance(oracle, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(path, str) for path in files)
    ):
        raise PinError(f"{case_id}: oracle files are malformed")
    normalized_files = [_safe_path(path, f"{case_id}:oracle") for path in files]
    checkout = _selected_audit_checkout(
        repository,
        checkout=checkout,
        batch=batch,
    )
    (
        changed,
        snapshots,
        patch_sha256,
        resolved_head,
        contents,
        blob_oids,
    ) = _validated_audit(
        case_id,
        repository,
        base_commit,
        {
            "instance_id": case_id,
            "repository": repository,
            "base_commit": base_commit,
            "head_commit": head_commit,
        },
        repository_root=repository_root,
        oracle_files=normalized_files,
        checkout=checkout,
    )
    if not set(normalized_files) <= set(changed) or not set(normalized_files) <= set(
        snapshots
    ):
        raise PinError(f"{case_id}: oracle files are not grounded in the Git diff")
    unverified = _definition_pattern_unverified_symbols(
        oracle,
        {
            path: contents[path]
            for path in normalized_files
            if path in contents
        },
    )
    if unverified is None or unverified:
        raise PinError(f"{case_id}: oracle symbol is not defined in Git blobs")
    audit = {
        "status": "verified",
        "verifier": "pinned_git_objects_v1",
        "repository": repository,
        "changed_files": changed,
        "changed_files_source": "git_diff_base_head_v1",
        "base_commit": base_commit,
        "head_commit": resolved_head,
        "patch_sha256": patch_sha256,
        "oracle_file_sha256": {
            path: snapshots[path] for path in normalized_files
        },
        "oracle_blob_oid": {
            path: blob_oids[path] for path in normalized_files
        },
        "symbol_verification": "definition_pattern_v1",
    }
    audit["audit_record_sha256"] = hashlib.sha256(canonical_json(audit)).hexdigest()
    return audit


def _validate_pr_label_audit(
    case: dict,
    repository_root: Path,
    *,
    checkout: _PinnedGitCheckout | None = None,
    batch: GitAuditBatch | None = None,
) -> None:
    case_id = case.get("case_id")
    repository = case.get("repository")
    oracle = case.get("oracle")
    audit = case.get("label_audit")
    expected_keys = {
        "status",
        "verifier",
        "repository",
        "changed_files",
        "changed_files_source",
        "base_commit",
        "head_commit",
        "merge_base_commit",
        "comparison_base_commit",
        "comparison_head_commit",
        "comparison_format",
        "comparison_sha256",
        "topology",
        "merge_relation",
        "merge_commit",
        "oracle_file_sha256",
        "oracle_blob_oid",
        "symbol_verification",
        "pull_request",
        "audit_record_sha256",
    }
    if (
        not isinstance(case_id, str)
        or not isinstance(repository, dict)
        or not isinstance(oracle, dict)
        or not isinstance(audit, dict)
        or set(audit) != expected_keys
    ):
        raise PinError(f"{case_id}: PR-comparison label audit is malformed")
    digest_payload = dict(audit)
    expected_digest = digest_payload.pop("audit_record_sha256")
    url = repository.get("url")
    prefix = "https://github.com/"
    slug = url.removeprefix(prefix) if isinstance(url, str) else ""
    base_commit = repository.get("revision")
    if (
        audit.get("status") != "verified"
        or audit.get("verifier") != "pinned_pr_object_delta_v2"
        or audit.get("changed_files_source")
        != GIT_RAW_OBJECT_DELTA_FORMAT
        or audit.get("comparison_format") != GIT_RAW_OBJECT_DELTA_FORMAT
        or SHA256.fullmatch(str(audit.get("comparison_sha256"))) is None
        or audit.get("symbol_verification")
        != LOCBENCH_SYMBOL_VERIFICATION
        or expected_digest
        != hashlib.sha256(canonical_json(digest_payload)).hexdigest()
        or REPOSITORY.fullmatch(slug) is None
        or audit.get("repository") != slug
        or audit.get("base_commit") != base_commit
        or not _pull_request_evidence_is_valid(
            case_id,
            slug,
            base_commit,
            audit.get("pull_request"),
        )
    ):
        raise PinError(f"{case_id}: PR-comparison label audit digest is invalid")
    checkout = _selected_audit_checkout(
        slug,
        checkout=checkout,
        batch=batch,
    )
    files = oracle.get("files")
    if not isinstance(files, list) or any(
        not isinstance(path, str) for path in files
    ):
        raise PinError(f"{case_id}: PR-comparison oracle files are malformed")
    (
        changed,
        snapshots,
        comparison_sha256,
        head_commit,
        merge_base,
        topology,
        merge_relation,
        contents,
        blob_oids,
    ) = _validated_pr_comparison(
        case_id,
        slug,
        base_commit,
        audit["head_commit"],
        audit["merge_commit"],
        repository_root=repository_root,
        oracle_files=files,
        checkout=checkout,
    )
    if (
        audit.get("head_commit") != head_commit
        or audit.get("head_commit")
        != audit["pull_request"].get("head_commit")
        or audit.get("merge_base_commit") != merge_base
        or audit.get("comparison_base_commit") != merge_base
        or audit.get("comparison_head_commit") != head_commit
        or audit.get("topology") != topology
        or audit.get("merge_relation") != merge_relation
        or audit.get("merge_commit")
        != audit["pull_request"].get("merge_commit")
        or audit.get("changed_files") != changed
        or audit.get("comparison_sha256") != comparison_sha256
        or audit.get("oracle_file_sha256") != snapshots
        or audit.get("oracle_blob_oid") != blob_oids
        or not set(files) <= set(changed)
        or not _locbench_symbols_are_defined(
            oracle,
            {
                path: contents[path]
                for path in files
                if path in contents
            },
        )
        or set(files) != set(contents)
    ):
        raise PinError(
            f"{case_id}: PR-comparison label audit does not match repository"
        )


def validate_git_label_audit(
    case: dict,
    repository_root: Path,
    *,
    checkout: _PinnedGitCheckout | None = None,
    batch: GitAuditBatch | None = None,
) -> None:
    case_id = case.get("case_id")
    repository = case.get("repository")
    oracle = case.get("oracle")
    audit = case.get("label_audit")
    if (
        isinstance(audit, dict)
        and audit.get("verifier") == "pinned_pr_object_delta_v2"
    ):
        _validate_pr_label_audit(
            case,
            repository_root,
            checkout=checkout,
            batch=batch,
        )
        return
    if (
        not isinstance(case_id, str)
        or not isinstance(repository, dict)
        or not isinstance(oracle, dict)
        or not isinstance(audit, dict)
        or set(audit)
        != {
            "status",
            "verifier",
            "repository",
            "changed_files",
            "changed_files_source",
            "base_commit",
            "head_commit",
            "patch_sha256",
            "oracle_file_sha256",
            "oracle_blob_oid",
            "symbol_verification",
            "audit_record_sha256",
        }
    ):
        raise PinError(f"{case_id}: Git-object label audit is malformed")
    digest_payload = dict(audit)
    expected_digest = digest_payload.pop("audit_record_sha256")
    if (
        audit.get("status") != "verified"
        or audit.get("verifier") != "pinned_git_objects_v1"
        or audit.get("changed_files_source") != "git_diff_base_head_v1"
        or audit.get("symbol_verification") != "definition_pattern_v1"
        or expected_digest
        != hashlib.sha256(canonical_json(digest_payload)).hexdigest()
    ):
        raise PinError(f"{case_id}: Git-object label audit digest is invalid")
    url = repository.get("url")
    prefix = "https://github.com/"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise PinError(f"{case_id}: Git-object repository URL is malformed")
    slug = url.removeprefix(prefix)
    if REPOSITORY.fullmatch(slug) is None or audit.get("repository") != slug:
        raise PinError(f"{case_id}: Git-object repository identity mismatch")
    checkout = _selected_audit_checkout(
        slug,
        checkout=checkout,
        batch=batch,
    )
    files = oracle.get("files")
    if not isinstance(files, list):
        raise PinError(f"{case_id}: Git-object oracle files are malformed")
    (
        changed,
        snapshots,
        patch_sha256,
        head_commit,
        contents,
        blob_oids,
    ) = _validated_audit(
        case_id,
        slug,
        repository.get("revision"),
        {
            "instance_id": case_id,
            "repository": slug,
            "base_commit": audit.get("base_commit"),
            "head_commit": audit.get("head_commit"),
        },
        repository_root=repository_root,
        oracle_files=files,
        checkout=checkout,
    )
    if (
        audit.get("base_commit") != repository.get("revision")
        or audit.get("head_commit") != head_commit
        or audit.get("changed_files") != changed
        or audit.get("patch_sha256") != patch_sha256
        or audit.get("oracle_file_sha256") != snapshots
        or audit.get("oracle_blob_oid") != blob_oids
        or not set(files) <= set(changed)
    ):
        raise PinError(f"{case_id}: Git-object label audit does not match repository")
    unverified = _definition_pattern_unverified_symbols(
        oracle,
        {path: contents[path] for path in files if path in contents},
    )
    if unverified is None or unverified:
        raise PinError(f"{case_id}: Git-object oracle symbol is not defined")


def build_balanced_pin(
    source: dict,
    *,
    source_sha256: str,
    source_repository: str,
    source_revision: str,
    source_path: str,
    audit_evidence: dict,
    audit_evidence_sha256: str,
    audit_evidence_path: str,
    repository_root: Path,
    seed: int,
) -> dict:
    if SHA256.fullmatch(source_sha256) is None:
        raise PinError("source SHA-256 must be 64 lowercase hex characters")
    if REPOSITORY.fullmatch(source_repository) is None:
        raise PinError("source repository must be an owner/name slug")
    if REVISION.fullmatch(source_revision) is None:
        raise PinError("source revision must be a full object ID")
    source_path = _safe_path(source_path, "source")
    audit_evidence_path = _safe_path(audit_evidence_path, "audit evidence")
    if SHA256.fullmatch(audit_evidence_sha256) is None:
        raise PinError("audit evidence SHA-256 must be 64 lowercase hex characters")
    if seed != 42:
        raise PinError("calibration seed must remain 42")
    if source.get("schema_version") != 1:
        raise PinError("source schema_version must be 1")
    source_cases = source.get("cases")
    if not isinstance(source_cases, list) or not source_cases:
        raise PinError("source must contain cases")
    audit_by_id = _audit_cases(audit_evidence)

    eligible: dict[str, list[tuple[str, dict]]] = {
        category: [] for category in CATEGORY_MAP.values()
    }
    quarantined: list[dict] = []
    candidates_by_repository: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for index, source_case in enumerate(source_cases):
        context = f"source case[{index}]"
        if not isinstance(source_case, dict):
            raise PinError(f"{context}: expected an object")
        case_id = source_case.get("instance_id")
        if (
            not isinstance(case_id, str)
            or not case_id
            or "|" in case_id
            or case_id in seen
        ):
            raise PinError(f"{context}: duplicate or invalid instance_id")
        seen.add(case_id)
        category = CATEGORY_MAP.get(source_case.get("category"))
        if category is None:
            raise PinError(f"{case_id}: unsupported category")
        query = source_case.get("query")
        repo = source_case.get("repo")
        revision = source_case.get("base_commit")
        oracle = source_case.get("oracle")
        if "changed_files" in source_case:
            raise PinError(
                f"{case_id}: colocated changed_files are forbidden; "
                "derive them from audit evidence"
            )
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(repo, str)
            or REPOSITORY.fullmatch(repo) is None
            or not isinstance(revision, str)
            or REVISION.fullmatch(revision) is None
            or not isinstance(oracle, dict)
        ):
            raise PinError(f"{case_id}: missing query, public repo, pin, or labels")
        files = oracle.get("files")
        if (
            not isinstance(files, list)
            or not files
            or len(set(files)) != len(files)
        ):
            raise PinError(f"{case_id}: oracle files must be unique and nonempty")
        normalized_files = [_safe_path(value, f"{case_id}:oracle") for value in files]
        for field in ("classes", "functions"):
            labels = oracle.get(field)
            if (
                not isinstance(labels, list)
                or len(set(labels)) != len(labels)
                or any(not isinstance(label, str) or not label for label in labels)
            ):
                raise PinError(f"{case_id}: oracle {field} must be unique strings")
        evidence = audit_by_id.get(case_id)
        if evidence is None:
            raise PinError(f"{case_id}: missing independent audit evidence")
        candidates_by_repository.setdefault(repo, []).append(
            {
                "case_id": case_id,
                "category": category,
                "query": query,
                "repository": repo,
                "revision": revision,
                "oracle": oracle,
                "normalized_files": normalized_files,
                "evidence": evidence,
            }
        )

    if set(audit_by_id) != seen:
        raise PinError("audit evidence case IDs do not exactly match label source")

    for repository in sorted(candidates_by_repository):
        with GitAuditBatch(repository_root, repository) as batch:
            checkout = batch.checkout()
            for candidate in candidates_by_repository[repository]:
                case_id = candidate["case_id"]
                category = candidate["category"]
                revision = candidate["revision"]
                oracle = candidate["oracle"]
                normalized_files = candidate["normalized_files"]
                (
                    normalized_changed,
                    snapshot_hashes,
                    patch_sha256,
                    head_commit,
                    snapshot_contents,
                    blob_oids,
                ) = _validated_audit(
                    case_id,
                    repository,
                    revision,
                    candidate["evidence"],
                    repository_root=repository_root,
                    oracle_files=normalized_files,
                    checkout=checkout,
                )
                if not set(normalized_files) <= set(normalized_changed):
                    quarantined.append(
                        {
                            "case_id": case_id,
                            "category": category,
                            "reason": (
                                "oracle_file_not_in_derived_changed_file_set"
                            ),
                        }
                    )
                    continue
                if not set(normalized_files) <= set(snapshot_hashes):
                    quarantined.append(
                        {
                            "case_id": case_id,
                            "category": category,
                            "reason": "oracle_file_snapshot_missing",
                        }
                    )
                    continue
                unverified_symbols = _definition_pattern_unverified_symbols(
                    oracle,
                    {
                        path: snapshot_contents[path]
                        for path in normalized_files
                        if path in snapshot_contents
                    },
                )
                if unverified_symbols is None or unverified_symbols:
                    quarantined.append(
                        {
                            "case_id": case_id,
                            "category": category,
                            "reason": (
                                "oracle_symbol_not_defined_in_hashed_snapshot"
                            ),
                            "symbols": sorted(
                                (
                                    oracle["classes"] + oracle["functions"]
                                    if unverified_symbols is None
                                    else unverified_symbols
                                )
                            ),
                        }
                    )
                    continue
                output_case = {
                    "case_id": case_id,
                    "category": category,
                    "query": candidate["query"],
                    "repository": {
                        "url": f"https://github.com/{repository}",
                        "revision": revision,
                    },
                    "oracle": {
                        "files": normalized_files,
                        "classes": oracle["classes"],
                        "functions": oracle["functions"],
                    },
                    "label_audit": {
                        "status": "verified",
                        "verifier": "pinned_git_objects_v1",
                        "repository": repository,
                        "changed_files": normalized_changed,
                        "changed_files_source": "git_diff_base_head_v1",
                        "base_commit": revision,
                        "head_commit": head_commit,
                        "patch_sha256": patch_sha256,
                        "oracle_file_sha256": {
                            path: snapshot_hashes[path]
                            for path in normalized_files
                        },
                        "oracle_blob_oid": {
                            path: blob_oids[path]
                            for path in normalized_files
                        },
                        "symbol_verification": "definition_pattern_v1",
                    },
                }
                output_case["label_audit"][
                    "audit_record_sha256"
                ] = hashlib.sha256(
                    canonical_json(output_case["label_audit"])
                ).hexdigest()
                eligible[category].append(
                    (
                        _selection_digest(seed, category, case_id),
                        output_case,
                    )
                )

    selected: list[dict] = []
    category_counts: dict[str, int] = {}
    for category in CATEGORY_MAP.values():
        candidates = sorted(
            eligible[category],
            key=lambda item: (item[0], item[1]["case_id"]),
        )
        if len(candidates) < 10:
            raise PinError(
                f"{category}: fewer than 10 eligible audited cases "
                f"({len(candidates)})"
            )
        chosen = candidates[:10]
        category_counts[category] = len(chosen)
        selected.extend(case for _digest, case in chosen)

    audit_records = {
        case["case_id"]: case["label_audit"]["audit_record_sha256"]
        for case in selected
    }
    return {
        "schema_version": 1,
        "pin_id": "locbench-n40-seed42",
        "dataset": {
            "name": "LocBench",
            "public": True,
            "repository": source_repository,
            "source_revision": source_revision,
            "source_path": source_path,
            "source_sha256": source_sha256,
            "audit_evidence_path": audit_evidence_path,
            "audit_evidence_sha256": audit_evidence_sha256,
        },
        "generation": {
            "seed": seed,
            "selection": "sha256_priority_v1",
            "per_category": 10,
            "category_counts": category_counts,
        },
        "label_audit": {
            "policy": "pinned_git_objects_v1",
            "audit_records_sha256": hashlib.sha256(
                canonical_json(audit_records)
            ).hexdigest(),
            "audit_records": audit_records,
            "quarantined": sorted(
                quarantined,
                key=lambda item: (item["category"], item["case_id"]),
            ),
        },
        "cases": selected,
    }


def _case_has_runnable_labels(case: dict) -> bool:
    query = case.get("query")
    oracle = case.get("oracle")
    audit = case.get("label_audit")
    if (
        not isinstance(query, str)
        or not query.strip()
        or not isinstance(oracle, dict)
        or not isinstance(audit, dict)
        or audit.get("status") != "verified"
    ):
        return False
    files = oracle.get("files")
    changed_files = audit.get("changed_files")
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(path, str) for path in files)
        or len(set(files)) != len(files)
        or not isinstance(changed_files, list)
        or any(not isinstance(path, str) for path in changed_files)
    ):
        return False
    try:
        normalized_files = {
            _safe_path(path, "external oracle") for path in files
        }
        normalized_changed = {
            _safe_path(path, "external changed files") for path in changed_files
        }
    except PinError:
        return False
    if not normalized_files <= normalized_changed:
        return False
    for field in ("classes", "functions"):
        labels = oracle.get(field)
        if (
            not isinstance(labels, list)
            or any(not isinstance(label, str) or not label for label in labels)
            or len(set(labels)) != len(labels)
        ):
            return False
    return True


def _load_verified_external_pin(
    reference: dict,
    external_path: Path,
) -> tuple[dict, dict]:
    required = {
        "schema_version",
        "kind",
        "repository",
        "source_revision",
        "path",
        "sha256",
        "expected_count",
        "score_depth",
        "recorded_order_sha256",
        "availability",
    }
    if (
        not required <= set(reference)
        or reference.get("schema_version") != 1
        or reference.get("kind") != "external_content_address"
        or REPOSITORY.fullmatch(str(reference.get("repository"))) is None
        or REVISION.fullmatch(str(reference.get("source_revision"))) is None
        or SHA256.fullmatch(str(reference.get("sha256"))) is None
        or SHA256.fullmatch(str(reference.get("recorded_order_sha256"))) is None
        or reference.get("expected_count") != 200
        or reference.get("score_depth") != 10
        or reference.get("availability") not in {"pending_publication", "published"}
    ):
        raise PinError("external pin reference is malformed")
    _safe_path(reference["path"], "external reference")
    encoded_pin = _read_input_snapshot(external_path, "external pin")
    actual_sha256 = hashlib.sha256(encoded_pin).hexdigest()
    if actual_sha256 != reference["sha256"]:
        raise PinError(
            "external pin SHA-256 mismatch: "
            f"expected {reference['sha256']}, got {actual_sha256}"
        )
    pin = _object_from_snapshot(encoded_pin, external_path)
    cases = pin.get("cases")
    pinned_ids = pin.get("pinned_instance_ids")
    if (
        pin.get("schema_version") != 1
        or pin.get("n") != 200
        or pin.get("score_depth") != 10
        or pin.get("recorded_order_sha256")
        != reference["recorded_order_sha256"]
        or not isinstance(cases, list)
        or len(cases) != 200
        or not isinstance(pinned_ids, list)
        or len(pinned_ids) != 200
        or any(not isinstance(case_id, str) or not case_id for case_id in pinned_ids)
        or len(set(pinned_ids)) != 200
    ):
        raise PinError("external pin does not match the June n=200 contract")
    identifiers: set[str] = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("instance_id"), str)
            or not case["instance_id"]
            or case["instance_id"] in identifiers
            or REPOSITORY.fullmatch(str(case.get("repo"))) is None
            or REVISION.fullmatch(str(case.get("base_commit"))) is None
            or case.get("category") not in CATEGORY_MAP
        ):
            raise PinError("external pin contains an invalid or duplicate case")
        identifiers.add(case["instance_id"])
    ordered_ids = [case["instance_id"] for case in cases]
    if ordered_ids != pinned_ids:
        raise PinError("external pin case order differs from pinned_instance_ids")
    computed_order_sha256 = hashlib.sha256(
        ("\n".join(pinned_ids) + "\n").encode("utf-8")
    ).hexdigest()
    if computed_order_sha256 != reference["recorded_order_sha256"]:
        raise PinError("external pin recorded order SHA-256 is not reproducible")
    labels_present = all(_case_has_runnable_labels(case) for case in cases)
    blockers: list[str] = []
    if not labels_present:
        blockers.append("missing_query_oracle_labels")
    else:
        # An external JSON file can carry internally consistent but invented
        # labels. This address-only verifier has no source repositories and
        # therefore cannot upgrade any label claim to runnable.
        blockers.append("git_object_label_provenance_not_verified")
    if reference.get("availability") != "published":
        blockers.append("pending_publication")
    return (
        {
        "schema_version": 1,
        "status": "address_verified_not_runnable",
        "runnable": False,
        "blockers": blockers,
        "sha256": actual_sha256,
        "verified_count": len(cases),
        "score_depth": 10,
        "recorded_order_sha256": pin["recorded_order_sha256"],
        },
        pin,
    )


def verify_external_pin(reference: dict, external_path: Path) -> dict:
    result, _pin = _load_verified_external_pin(reference, external_path)
    return result


def _source_label_quarantine_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "case_id",
        "category",
        "stage",
        "reason_code",
        "identities",
    }:
        return False
    case_id = value.get("case_id")
    identities = value.get("identities")
    if (
        not isinstance(case_id, str)
        or not case_id
        or value.get("category") not in set(CATEGORY_MAP.values())
        or value.get("stage") != "source_labels"
        or value.get("reason_code") != "source_label_unverified"
        or not isinstance(identities, dict)
        or set(identities)
        != {"requested_repository", "base_commit", "pr_number"}
        or REPOSITORY.fullmatch(
            str(identities.get("requested_repository"))
        )
        is None
        or REVISION.fullmatch(str(identities.get("base_commit"))) is None
    ):
        return False
    try:
        return identities.get("pr_number") == _pull_request_number(case_id)
    except PinError:
        return False


def build_prepared_june_pin(
    cases: list[dict],
    *,
    github_pr_cache: Path,
    repository_root: Path,
    external_pin_sha256: str,
    recorded_order_sha256: str,
    parquet_sha256: str,
    initial_quarantined: list[dict] | None = None,
    expected_case_ids: list[str] | None = None,
) -> tuple[dict | None, dict]:
    source_quarantined = (
        [] if initial_quarantined is None else initial_quarantined
    )
    source_case_ids = [
        case.get("case_id") if isinstance(case, dict) else None
        for case in cases
    ] if isinstance(cases, list) else []
    quarantined_case_ids = [
        item.get("case_id") if isinstance(item, dict) else None
        for item in source_quarantined
    ] if isinstance(source_quarantined, list) else []
    bound_case_ids = source_case_ids + quarantined_case_ids
    if expected_case_ids is None:
        ordered_case_ids = source_case_ids
    else:
        ordered_case_ids = expected_case_ids
    if (
        not isinstance(cases, list)
        or not isinstance(source_quarantined, list)
        or any(
            not isinstance(case_id, str) or not case_id
            for case_id in bound_case_ids
        )
        or len(bound_case_ids) != 200
        or len(set(bound_case_ids)) != 200
        or any(
            not _source_label_quarantine_is_valid(item)
            for item in source_quarantined
        )
        or (
            source_quarantined
            and expected_case_ids is None
        )
        or not isinstance(ordered_case_ids, list)
        or len(ordered_case_ids) != 200
        or any(
            not isinstance(case_id, str) or not case_id
            for case_id in ordered_case_ids
        )
        or len(set(ordered_case_ids)) != 200
        or set(ordered_case_ids) != set(bound_case_ids)
        or source_case_ids
        != [
            case_id
            for case_id in ordered_case_ids
            if case_id not in set(quarantined_case_ids)
        ]
        or quarantined_case_ids
        != [
            case_id
            for case_id in ordered_case_ids
            if case_id in set(quarantined_case_ids)
        ]
        or SHA256.fullmatch(external_pin_sha256) is None
        or SHA256.fullmatch(recorded_order_sha256) is None
        or SHA256.fullmatch(parquet_sha256) is None
    ):
        raise PinError("prepared June inputs must bind exactly 200 unique cases")
    prepared_by_index: dict[int, dict] = {}
    quarantined: list[dict] = [
        {
            "case_id": item["case_id"],
            "category": item["category"],
            "stage": item["stage"],
            "reason_code": item["reason_code"],
            "identities": dict(item["identities"]),
        }
        for item in source_quarantined
    ]
    candidates_by_repository: dict[str, list[dict]] = {}
    for case_index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise PinError("prepared June source case must be an object")
        case_id = case.get("case_id")
        category = case.get("category")
        repository = case.get("repository")
        if (
            not isinstance(case_id, str)
            or not isinstance(category, str)
            or not isinstance(repository, dict)
        ):
            raise PinError("prepared June source case identity is malformed")
        url = repository.get("url")
        prefix = "https://github.com/"
        requested_repository = (
            url.removeprefix(prefix) if isinstance(url, str) else ""
        )
        base_commit = repository.get("revision")
        identities = {
            "requested_repository": requested_repository,
            "base_commit": base_commit,
            "pr_number": _pull_request_number(case_id),
        }
        try:
            pull_request = load_cached_pull_request(
                github_pr_cache,
                case_id=case_id,
                repository=requested_repository,
            )
        except PinError:
            quarantined.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "stage": "pr_cache",
                    "reason_code": "pr_response_unverified",
                    "identities": identities,
                }
            )
            continue
        if pull_request["base_commit"] != base_commit:
            quarantined.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "stage": "pr_identity",
                    "reason_code": "pr_base_commit_mismatch",
                    "identities": {
                        **identities,
                        "resolved_repository": pull_request[
                            "resolved_repository"
                        ],
                        "response_sha256": pull_request["response_sha256"],
                    },
                }
            )
            continue
        resolved_repository = pull_request["resolved_repository"]
        candidates_by_repository.setdefault(
            resolved_repository,
            [],
        ).append(
            {
                "index": case_index,
                "case": case,
                "case_id": case_id,
                "category": category,
                "base_commit": base_commit,
                "identities": identities,
                "pull_request": pull_request,
            }
        )

    def git_quarantine(candidate: dict) -> dict:
        pull_request = candidate["pull_request"]
        return {
            "case_id": candidate["case_id"],
            "category": candidate["category"],
            "stage": "git_audit",
            "reason_code": "pr_git_comparison_unverified",
            "identities": {
                **candidate["identities"],
                "resolved_repository": pull_request[
                    "resolved_repository"
                ],
                "head_commit": pull_request["head_commit"],
                "merge_commit": pull_request["merge_commit"],
                "response_sha256": pull_request["response_sha256"],
            },
        }

    for resolved_repository in sorted(candidates_by_repository):
        repository_candidates = candidates_by_repository[
            resolved_repository
        ]
        group_prepared: dict[int, dict] = {}
        group_quarantined: list[dict] = []
        try:
            with GitAuditBatch(
                repository_root,
                resolved_repository,
            ) as batch:
                for candidate in repository_candidates:
                    output_case = {
                        **candidate["case"],
                        "repository": {
                            "url": (
                                "https://github.com/"
                                f"{resolved_repository}"
                            ),
                            "revision": candidate["base_commit"],
                        },
                    }
                    try:
                        output_case["label_audit"] = derive_pr_label_audit(
                            case_id=candidate["case_id"],
                            repository=resolved_repository,
                            base_commit=candidate["base_commit"],
                            oracle=candidate["case"].get("oracle"),
                            pull_request=candidate["pull_request"],
                            repository_root=repository_root,
                            batch=batch,
                        )
                    except PinError:
                        group_quarantined.append(
                            git_quarantine(candidate)
                        )
                        continue
                    group_prepared[candidate["index"]] = output_case
        except PinError:
            quarantined.extend(
                git_quarantine(candidate)
                for candidate in repository_candidates
            )
            continue
        prepared_by_index.update(group_prepared)
        quarantined.extend(group_quarantined)
    prepared = [
        prepared_by_index[index]
        for index in range(len(cases))
        if index in prepared_by_index
    ]
    order_index = {
        case_id: index for index, case_id in enumerate(ordered_case_ids)
    }
    quarantined.sort(key=lambda item: order_index[item["case_id"]])
    report = {
        "schema_version": 1,
        "kind": "locbench_june_preparation_quarantine",
        "status": "complete" if not quarantined else "incomplete",
        "expected_count": 200,
        "prepared_count": len(prepared),
        "quarantined_count": len(quarantined),
        "dataset": {
            "repository": "czlll/Loc-Bench_V1",
            "revision": JUNE_DATASET_REVISION,
            "parquet_size": JUNE_PARQUET_SIZE,
            "parquet_sha256": parquet_sha256,
            "external_pin_sha256": external_pin_sha256,
            "recorded_order_sha256": recorded_order_sha256,
        },
        "cases": quarantined,
    }
    if quarantined:
        return None, report
    audit_records = {
        case["case_id"]: case["label_audit"]["audit_record_sha256"]
        for case in prepared
    }
    pin = {
        "schema_version": 1,
        "pin_id": "locbench-june-n200-prepared-v1",
        "dataset": {
            "name": "LocBench",
            "public": True,
            "repository": "czlll/Loc-Bench_V1",
            "source_revision": JUNE_DATASET_REVISION,
            "source_path": "data/test-00000-of-00001.parquet",
            "source_size": JUNE_PARQUET_SIZE,
            "source_sha256": parquet_sha256,
            "local_only": True,
            "redistribution": "operator_local_uncommitted_artifact",
        },
        "generation": {
            "selection": "published_external_order_v1",
            "expected_count": 200,
            "selected_instance_ids": [
                case["case_id"] for case in prepared
            ],
            "external_pin_sha256": external_pin_sha256,
            "recorded_order_sha256": recorded_order_sha256,
            "label_source": "locagent_evaluator_edit_functions_v1",
            "git_provenance": "source_first_pr_comparison_v1",
        },
        "label_audit": {
            "policy": "pinned_git_objects_v1",
            "audit_records": audit_records,
            "audit_records_sha256": hashlib.sha256(
                canonical_json(audit_records)
            ).hexdigest(),
        },
        "cases": prepared,
    }
    return pin, report


def _close_directory_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _pinned_directory_ancestry(
    path: Path,
    context: str,
    *,
    create_missing: bool,
    allow_missing_tail: bool,
    forbidden_identity: tuple[int, int] | None = None,
) -> list[int]:
    """Retain a no-follow descriptor for every opened directory in a path."""
    if not path.is_absolute():
        raise PinError(f"{context} directory must be absolute")
    dir_fd_functions: tuple[object, ...] = (
        (os.open, os.mkdir) if create_missing else (os.open,)
    )
    _require_directory_fd_capabilities(
        f"{context} directory ancestry",
        dir_fd_functions=dir_fd_functions,
    )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []

    def retain(descriptor: int) -> None:
        descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if (
            forbidden_identity is not None
            and (metadata.st_dev, metadata.st_ino) == forbidden_identity
        ):
            raise PinError(
                f"{context} must be outside the plugin repository"
            )

    try:
        retain(os.open(path.anchor, flags))
        for component in path.parts[1:]:
            try:
                child = os.open(
                    component,
                    flags,
                    dir_fd=descriptors[-1],
                )
            except FileNotFoundError:
                if allow_missing_tail:
                    return descriptors
                if not create_missing:
                    raise
                try:
                    os.mkdir(
                        component,
                        mode=0o700,
                        dir_fd=descriptors[-1],
                    )
                except FileExistsError:
                    pass
                child = os.open(
                    component,
                    flags,
                    dir_fd=descriptors[-1],
                )
            retain(child)
        return descriptors
    except PinError:
        _close_directory_descriptors(descriptors)
        raise
    except (OSError, NotImplementedError) as exc:
        _close_directory_descriptors(descriptors)
        raise PinError(f"{context} directory is missing or unsafe") from exc


def _open_pinned_existing_directory(path: Path, context: str) -> int:
    descriptors = _pinned_directory_ancestry(
        path,
        context,
        create_missing=False,
        allow_missing_tail=False,
    )
    descriptor = descriptors.pop()
    _close_directory_descriptors(descriptors)
    return descriptor


def _require_external_artifact_ancestry(path: Path, kind: str) -> None:
    plugin_descriptor = _open_pinned_existing_directory(
        PLUGIN_ROOT,
        "prepare-june plugin repository",
    )
    descriptors: list[int] = []
    try:
        plugin_metadata = os.fstat(plugin_descriptor)
        descriptors = _pinned_directory_ancestry(
            path,
            f"prepare-june {kind}",
            create_missing=False,
            allow_missing_tail=True,
            forbidden_identity=(
                plugin_metadata.st_dev,
                plugin_metadata.st_ino,
            ),
        )
    finally:
        _close_directory_descriptors(descriptors)
        os.close(plugin_descriptor)


def _open_pinned_artifact_directory(
    path: Path,
    context: str,
    *,
    forbidden_identity: tuple[int, int],
) -> int:
    """Open and safely create an external directory without alias traversal."""
    descriptors = _pinned_directory_ancestry(
        path,
        f"prepare-june {context}",
        create_missing=True,
        allow_missing_tail=False,
        forbidden_identity=forbidden_identity,
    )
    descriptor = descriptors.pop()
    _close_directory_descriptors(descriptors)
    return descriptor


def _normalized_artifact_name(name: str) -> bytes:
    normalized_name = unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", name).casefold(),
    )
    return os.fsencode(normalized_name)


def _require_absent_artifact(
    parent_descriptor: int,
    name: str,
    context: str,
) -> None:
    _require_directory_fd_capabilities(
        f"prepare-june {context} inspection",
        dir_fd_functions=(os.stat,),
        follow_symlinks_functions=(os.stat,),
    )
    normalized_name = _normalized_artifact_name(name)
    try:
        existing_names = os.listdir(parent_descriptor)
        if any(
            _normalized_artifact_name(existing_name) == normalized_name
            for existing_name in existing_names
        ):
            raise PinError(
                f"prepare-june {context} already exists; "
                "refusing stale artifact"
            )
        os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except (OSError, NotImplementedError) as exc:
        raise PinError(f"cannot inspect prepare-june {context}") from exc
    raise PinError(
        f"prepare-june {context} already exists; refusing stale artifact"
    )


def _artifact_lock_module():
    try:
        import fcntl
    except (ImportError, OSError) as exc:
        raise PinError(
            "prepare-june artifact publication requires POSIX fcntl locking; "
            "no safe artifact-endpoint lock is available on this platform"
        ) from exc
    if (
        not callable(getattr(fcntl, "flock", None))
        or not isinstance(getattr(fcntl, "LOCK_EX", None), int)
        or not isinstance(getattr(fcntl, "LOCK_UN", None), int)
    ):
        raise PinError(
            "prepare-june artifact locking is incomplete on this platform"
        )
    return fcntl


def _validate_artifact_lock(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise PinError("prepare-june artifact-endpoint lock is unsafe")


def _artifact_endpoint_identity(
    parent_descriptor: int,
    name: str,
) -> tuple[int, int, bytes]:
    parent = os.fstat(parent_descriptor)
    return parent.st_dev, parent.st_ino, _normalized_artifact_name(name)


def _lock_artifact_endpoints(
    endpoints: tuple[tuple[int, str], tuple[int, str]],
) -> list[int]:
    """Lock each artifact name, in inode order, so overlapping pairs serialize."""
    _require_directory_fd_capabilities(
        "prepare-june artifact locking",
        dir_fd_functions=(os.open,),
    )
    fcntl = _artifact_lock_module()
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    ordered: list[tuple[tuple[int, int, bytes], int, str]] = []
    for parent_descriptor, name in endpoints:
        identity = _artifact_endpoint_identity(parent_descriptor, name)
        ordered.append(
            (
                identity,
                parent_descriptor,
                name,
            )
        )
    ordered.sort(key=lambda item: item[0])
    if ordered[0][0] == ordered[1][0]:
        raise PinError(
            "prepare-june output and quarantine report must be distinct"
        )
    descriptors: list[int] = []
    try:
        for identity, parent_descriptor, _name in ordered:
            endpoint_sha256 = hashlib.sha256(
                b"prepare-june-artifact-endpoint-v2\0"
                + str(identity[0]).encode("ascii")
                + b"\0"
                + str(identity[1]).encode("ascii")
                + b"\0"
                + identity[2]
            ).hexdigest()
            descriptor = os.open(
                f".prepare-june-{endpoint_sha256}.lock",
                flags,
                0o600,
                dir_fd=parent_descriptor,
            )
            descriptors.append(descriptor)
            _validate_artifact_lock(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _validate_artifact_lock(descriptor)
        return descriptors
    except (OSError, NotImplementedError, PinError) as exc:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
        if isinstance(exc, PinError):
            raise
        raise PinError(
            "cannot acquire prepare-june artifact-endpoint locks"
        ) from exc


def _write_json_non_clobbering(
    parent_descriptor: int,
    name: str,
    value: dict,
    context: str,
) -> None:
    """Atomically publish JSON by linking a private file to an absent name."""
    _require_directory_fd_capabilities(
        f"prepare-june {context} publication",
        dir_fd_functions=(os.open, os.link, os.unlink),
        follow_symlinks_functions=(os.link,),
    )
    encoded = canonical_json(value) + b"\n"
    temporary_name = (
        f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.fsync(parent_descriptor)
    except (OSError, NotImplementedError) as exc:
        if getattr(exc, "errno", None) in {errno.EEXIST, errno.ELOOP}:
            raise PinError(
                f"prepare-june {context} already exists; "
                "refusing non-clobber write"
            ) from exc
        raise PinError(
            f"cannot write prepare-june {context} safely: {exc}"
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


def _write_prepared_june_artifacts(
    cases: list[dict],
    *,
    github_pr_cache: Path,
    repository_root: Path,
    output: Path,
    quarantine_report: Path,
    external_pin_sha256: str,
    recorded_order_sha256: str,
    parquet_sha256: str,
    initial_quarantined: list[dict] | None = None,
    expected_case_ids: list[str] | None = None,
) -> dict:
    output = _require_external_artifact_path(output, "output")
    quarantine_report = _require_external_artifact_path(
        quarantine_report,
        "quarantine report",
    )
    if output == quarantine_report:
        raise PinError(
            "prepare-june output and quarantine report must be distinct"
        )
    plugin_descriptor = -1
    output_parent = -1
    report_parent = -1
    endpoint_locks: list[int] = []
    try:
        plugin_descriptor = _open_pinned_existing_directory(
            PLUGIN_ROOT,
            "prepare-june plugin repository",
        )
        plugin_metadata = os.fstat(plugin_descriptor)
        plugin_identity = (
            plugin_metadata.st_dev,
            plugin_metadata.st_ino,
        )
        output_parent = _open_pinned_artifact_directory(
            output.parent,
            "output",
            forbidden_identity=plugin_identity,
        )
        report_parent = _open_pinned_artifact_directory(
            quarantine_report.parent,
            "quarantine report",
            forbidden_identity=plugin_identity,
        )
        if _artifact_endpoint_identity(
            output_parent,
            output.name,
        ) == _artifact_endpoint_identity(
            report_parent,
            quarantine_report.name,
        ):
            raise PinError(
                "prepare-june output and quarantine report must be distinct"
            )
        endpoint_locks = _lock_artifact_endpoints(
            (
                (output_parent, output.name),
                (report_parent, quarantine_report.name),
            )
        )
        _require_absent_artifact(output_parent, output.name, "output")
        _require_absent_artifact(
            report_parent,
            quarantine_report.name,
            "quarantine report",
        )
        pin, report = build_prepared_june_pin(
            cases,
            github_pr_cache=github_pr_cache,
            repository_root=repository_root,
            external_pin_sha256=external_pin_sha256,
            recorded_order_sha256=recorded_order_sha256,
            parquet_sha256=parquet_sha256,
            initial_quarantined=initial_quarantined,
            expected_case_ids=expected_case_ids,
        )
        prepared_count = report.get("prepared_count")
        quarantined_count = report.get("quarantined_count")
        if (
            not isinstance(prepared_count, int)
            or not isinstance(quarantined_count, int)
            or prepared_count < 0
            or quarantined_count < 0
            or prepared_count + quarantined_count != 200
        ):
            raise PinError("prepared June artifact counts are malformed")
        if pin is None:
            _write_json_non_clobbering(
                report_parent,
                quarantine_report.name,
                report,
                "quarantine report",
            )
            return {
                "exit_code": 2,
                "status": "quarantined",
                "prepared_count": prepared_count,
                "quarantined_count": quarantined_count,
                "artifact_sha256": hashlib.sha256(
                    canonical_json(report) + b"\n"
                ).hexdigest(),
            }
        _write_json_non_clobbering(
            output_parent,
            output.name,
            pin,
            "output",
        )
        return {
            "exit_code": 0,
            "status": "written",
            "prepared_count": prepared_count,
            "quarantined_count": quarantined_count,
            "artifact_sha256": hashlib.sha256(
                canonical_json(pin) + b"\n"
            ).hexdigest(),
        }
    finally:
        if endpoint_locks:
            fcntl = _artifact_lock_module()
            for descriptor in reversed(endpoint_locks):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        if report_parent >= 0:
            os.close(report_parent)
        if output_parent >= 0:
            os.close(output_parent)
        if plugin_descriptor >= 0:
            os.close(plugin_descriptor)


def write_prepared_june_artifacts(
    cases: list[dict],
    *,
    github_pr_cache: Path,
    repository_root: Path,
    output: Path,
    quarantine_report: Path,
    external_pin_sha256: str,
    recorded_order_sha256: str,
    parquet_sha256: str,
    initial_quarantined: list[dict] | None = None,
    expected_case_ids: list[str] | None = None,
) -> int:
    outcome = _write_prepared_june_artifacts(
        cases,
        github_pr_cache=github_pr_cache,
        repository_root=repository_root,
        output=output,
        quarantine_report=quarantine_report,
        external_pin_sha256=external_pin_sha256,
        recorded_order_sha256=recorded_order_sha256,
        parquet_sha256=parquet_sha256,
        initial_quarantined=initial_quarantined,
        expected_case_ids=expected_case_ids,
    )
    return outcome["exit_code"]


def _prepare_june_command(arguments: argparse.Namespace) -> int:
    supplied_reference = load_object(arguments.reference)
    checked_reference = load_object(JUNE_REFERENCE_PATH)
    validate_june_reference(checked_reference)
    validate_june_reference(supplied_reference)
    if canonical_json(supplied_reference) != canonical_json(checked_reference):
        raise PinError(
            "supplied June reference differs from the checked-in June reference"
        )
    verification, external_pin = _load_verified_external_pin(
        checked_reference,
        arguments.external_pin,
    )
    if (
        verification.get("sha256") != JUNE_EXTERNAL_PIN_SHA256
        or verification.get("verified_count") != 200
        or verification.get("recorded_order_sha256")
        != JUNE_RECORDED_ORDER_SHA256
    ):
        raise PinError("verified external June pin identity is unexpected")
    rows = load_verified_june_parquet_rows(arguments.parquet)
    cases, source_quarantined = prepare_june_source_cases_with_quarantine(
        rows,
        external_pin,
    )
    outcome = _write_prepared_june_artifacts(
        cases,
        github_pr_cache=arguments.github_pr_cache,
        repository_root=arguments.repository_root,
        output=arguments.output,
        quarantine_report=arguments.quarantine_report,
        external_pin_sha256=JUNE_EXTERNAL_PIN_SHA256,
        recorded_order_sha256=JUNE_RECORDED_ORDER_SHA256,
        parquet_sha256=JUNE_PARQUET_SHA256,
        initial_quarantined=source_quarantined,
        expected_case_ids=external_pin["pinned_instance_ids"],
    )
    print(
        json.dumps(
            {
                "status": outcome["status"],
                "prepared_count": outcome["prepared_count"],
                "quarantined_count": outcome["quarantined_count"],
                "artifact_sha256": outcome["artifact_sha256"],
            },
            sort_keys=True,
        )
    )
    return outcome["exit_code"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-n40")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--source-sha256", required=True)
    build.add_argument("--source-repository", required=True)
    build.add_argument("--source-revision", required=True)
    build.add_argument("--source-path", required=True)
    build.add_argument("--audit-evidence", type=Path, required=True)
    build.add_argument("--audit-evidence-sha256", required=True)
    build.add_argument("--audit-evidence-path", required=True)
    build.add_argument("--repository-root", type=Path, required=True)
    build.add_argument("--seed", type=int, default=42)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--check", action="store_true")
    verify = subparsers.add_parser("verify-june")
    verify.add_argument("--reference", type=Path, required=True)
    verify.add_argument("--external-pin", type=Path, required=True)
    prepare = subparsers.add_parser("prepare-june")
    prepare.add_argument("--reference", type=Path, required=True)
    prepare.add_argument("--external-pin", type=Path, required=True)
    prepare.add_argument("--parquet", type=Path, required=True)
    prepare.add_argument("--github-pr-cache", type=Path, required=True)
    prepare.add_argument("--repository-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--quarantine-report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if arguments.command == "verify-june":
            result = verify_external_pin(
                load_object(arguments.reference),
                arguments.external_pin,
            )
            print(json.dumps(result, sort_keys=True))
            return 0 if result["runnable"] else 2
        if arguments.command == "prepare-june":
            _require_external_artifact_path(arguments.output, "output")
            _require_external_artifact_path(
                arguments.quarantine_report,
                "quarantine report",
            )
            return _prepare_june_command(arguments)
        source_snapshot = _read_input_snapshot(arguments.source, "source")
        source = _object_from_snapshot(source_snapshot, arguments.source)
        actual_source_sha256 = hashlib.sha256(source_snapshot).hexdigest()
        if actual_source_sha256 != arguments.source_sha256:
            raise PinError(
                "source SHA-256 mismatch: "
                f"expected {arguments.source_sha256}, got {actual_source_sha256}"
            )
        audit_snapshot = _read_input_snapshot(
            arguments.audit_evidence,
            "audit evidence",
        )
        audit_evidence = _object_from_snapshot(
            audit_snapshot,
            arguments.audit_evidence,
        )
        actual_audit_sha256 = hashlib.sha256(audit_snapshot).hexdigest()
        if actual_audit_sha256 != arguments.audit_evidence_sha256:
            raise PinError(
                "audit evidence SHA-256 mismatch: "
                f"expected {arguments.audit_evidence_sha256}, "
                f"got {actual_audit_sha256}"
            )
        pin = build_balanced_pin(
            source,
            source_sha256=arguments.source_sha256,
            source_repository=arguments.source_repository,
            source_revision=arguments.source_revision,
            source_path=arguments.source_path,
            audit_evidence=audit_evidence,
            audit_evidence_sha256=arguments.audit_evidence_sha256,
            audit_evidence_path=arguments.audit_evidence_path,
            repository_root=arguments.repository_root,
            seed=arguments.seed,
        )
        if arguments.check:
            current = load_object(arguments.output)
            if canonical_json(current) != canonical_json(pin):
                raise PinError("checked-in n=40 pin differs from deterministic build")
        else:
            atomic_write_json(arguments.output, pin)
        print(
            json.dumps(
                {
                    "status": "verified" if arguments.check else "written",
                    "output_sha256": hashlib.sha256(
                        canonical_json(pin) + b"\n"
                    ).hexdigest(),
                    "cases": len(pin["cases"]),
                    "quarantined": len(pin["label_audit"]["quarantined"]),
                },
                sort_keys=True,
            )
        )
        return 0
    except PinError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
