#!/usr/bin/env python3
"""Build the audited n=40 calibration pin or verify the external June n=200 pin."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys


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
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
CATEGORY_MAP = {
    "Bug Report": "Bug",
    "Feature Request": "Feature",
    "Performance Issue": "Performance",
    "Security Vulnerability": "Security",
}


class PinError(ValueError):
    """Source labels or content addresses are insufficient for a valid pin."""


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


def _selection_digest(seed: int, category: str, case_id: str) -> str:
    material = (
        f"locbench-n40|selection=sha256_priority_v1|seed={seed}|"
        f"category={category}|case_id={case_id}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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


def _symbol_is_defined(contents: list[str], symbol: str, *, kind: str) -> bool:
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
    repository: Path,
    *arguments: str,
    allow_failure: bool = False,
) -> bytes | None:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(repository),
                *arguments,
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise PinError(f"cannot inspect pinned Git objects: {exc}") from exc
    if completed.returncode != 0:
        if allow_failure:
            return None
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PinError(f"pinned Git object verification failed: {detail}")
    return completed.stdout


def _repository_checkout(repository_root: Path, slug: str) -> Path:
    root = Path(repository_root).resolve()
    candidate = (root / slug).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PinError("repository checkout escapes the repository root") from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise PinError(f"{slug}: pinned Git checkout is missing or unsafe")
    raw_root = _git_bytes(candidate, "rev-parse", "--show-toplevel")
    assert raw_root is not None
    try:
        actual_root = Path(raw_root.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as exc:
        raise PinError(f"{slug}: Git checkout root is not UTF-8") from exc
    if actual_root != candidate:
        raise PinError(f"{slug}: checkout path is not the Git repository root")
    return candidate


def _validated_audit(
    case_id: str,
    repository: str,
    base_commit: str,
    evidence: dict,
    *,
    repository_root: Path,
    oracle_files: list[str],
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
    checkout = _repository_checkout(repository_root, repository)
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
        "--no-ext-diff",
        resolved_base,
        resolved_head,
        "--",
    )
    changed_raw = _git_bytes(
        checkout,
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=ACDMRTUXB",
        resolved_base,
        resolved_head,
        "--",
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


def derive_git_label_audit(
    *,
    case_id: str,
    repository: str,
    base_commit: str,
    head_commit: str,
    oracle: dict,
    repository_root: Path,
) -> dict:
    files = oracle.get("files") if isinstance(oracle, dict) else None
    if (
        not isinstance(files, list)
        or not files
        or any(not isinstance(path, str) for path in files)
    ):
        raise PinError(f"{case_id}: oracle files are malformed")
    normalized_files = [_safe_path(path, f"{case_id}:oracle") for path in files]
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
    )
    if not set(normalized_files) <= set(changed) or not set(normalized_files) <= set(
        snapshots
    ):
        raise PinError(f"{case_id}: oracle files are not grounded in the Git diff")
    oracle_contents = [contents[path] for path in normalized_files]
    for kind, field in (("class", "classes"), ("function", "functions")):
        labels = oracle.get(field)
        if (
            not isinstance(labels, list)
            or any(not isinstance(symbol, str) for symbol in labels)
            or any(
                not _symbol_is_defined(oracle_contents, symbol, kind=kind)
                for symbol in labels
            )
        ):
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


def validate_git_label_audit(case: dict, repository_root: Path) -> None:
    case_id = case.get("case_id")
    repository = case.get("repository")
    oracle = case.get("oracle")
    audit = case.get("label_audit")
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
    oracle_contents = [contents[path] for path in files]
    for kind, field in (("class", "classes"), ("function", "functions")):
        labels = oracle.get(field)
        if not isinstance(labels, list) or any(
            not _symbol_is_defined(oracle_contents, symbol, kind=kind)
            for symbol in labels
        ):
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
        (
            normalized_changed,
            snapshot_hashes,
            patch_sha256,
            head_commit,
            snapshot_contents,
            blob_oids,
        ) = _validated_audit(
            case_id,
            repo,
            revision,
            evidence,
            repository_root=repository_root,
            oracle_files=normalized_files,
        )
        if not set(normalized_files) <= set(normalized_changed):
            quarantined.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "reason": "oracle_file_not_in_derived_changed_file_set",
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
        oracle_contents = [snapshot_contents[path] for path in normalized_files]
        unverified_symbols = [
            symbol
            for kind, field in (("class", "classes"), ("function", "functions"))
            for symbol in oracle[field]
            if not _symbol_is_defined(oracle_contents, symbol, kind=kind)
        ]
        if unverified_symbols:
            quarantined.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "reason": "oracle_symbol_not_defined_in_hashed_snapshot",
                    "symbols": sorted(unverified_symbols),
                }
            )
            continue
        output_case = {
            "case_id": case_id,
            "category": category,
            "query": query,
            "repository": {
                "url": f"https://github.com/{repo}",
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
                "repository": repo,
                "changed_files": normalized_changed,
                "changed_files_source": "git_diff_base_head_v1",
                "base_commit": revision,
                "head_commit": head_commit,
                "patch_sha256": patch_sha256,
                "oracle_file_sha256": {
                    path: snapshot_hashes[path] for path in normalized_files
                },
                "oracle_blob_oid": {
                    path: blob_oids[path] for path in normalized_files
                },
                "symbol_verification": "definition_pattern_v1",
            },
        }
        output_case["label_audit"]["audit_record_sha256"] = hashlib.sha256(
            canonical_json(output_case["label_audit"])
        ).hexdigest()
        eligible[category].append(
            (_selection_digest(seed, category, case_id), output_case)
        )

    if set(audit_by_id) != seen:
        raise PinError("audit evidence case IDs do not exactly match label source")

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


def verify_external_pin(reference: dict, external_path: Path) -> dict:
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
    actual_sha256 = sha256_file(external_path)
    if actual_sha256 != reference["sha256"]:
        raise PinError(
            "external pin SHA-256 mismatch: "
            f"expected {reference['sha256']}, got {actual_sha256}"
        )
    pin = load_object(external_path)
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
    return {
        "schema_version": 1,
        "status": "address_verified_not_runnable",
        "runnable": False,
        "blockers": blockers,
        "sha256": actual_sha256,
        "verified_count": len(cases),
        "score_depth": 10,
        "recorded_order_sha256": pin["recorded_order_sha256"],
    }


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
        source = load_object(arguments.source)
        actual_source_sha256 = sha256_file(arguments.source)
        if actual_source_sha256 != arguments.source_sha256:
            raise PinError(
                "source SHA-256 mismatch: "
                f"expected {arguments.source_sha256}, got {actual_source_sha256}"
            )
        audit_evidence = load_object(arguments.audit_evidence)
        actual_audit_sha256 = sha256_file(arguments.audit_evidence)
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
