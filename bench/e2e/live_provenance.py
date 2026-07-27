"""Build and validate content-addressed live benchmark provenance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


ARTIFACT_KEYS = {
    "cases",
    "runs",
    "thresholds",
    "component_bom",
    "component_evidence",
    "target_manifest",
    "raw_mcp_transcript",
    "final_answers",
    "claim_extraction",
}
IDENTITY_FIELDS = (
    "repository_id",
    "checkout_id",
    "source_revision",
    "dirty_fingerprint",
    "index_generation",
    "captured_at",
)
EQUAL_IDENTITY_FIELDS = IDENTITY_FIELDS[:-1]
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")
GIT_REVISION = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


class ProvenanceError(ValueError):
    """A live recording is incomplete, unbound, or internally inconsistent."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProvenanceError(f"{path}: cannot hash artifact: {exc}") from exc
    return digest.hexdigest()


def install_descriptor_sha256(install: object) -> str:
    """Hash the complete install descriptor with the plugin's canonical JSON."""
    if not isinstance(install, dict):
        raise ProvenanceError("component install descriptor must be an object")
    try:
        canonical = json.dumps(
            install,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProvenanceError(
            f"component install descriptor is not canonical JSON: {exc}"
        ) from exc
    return hashlib.sha256(canonical).hexdigest()


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProvenanceError(f"{path}: expected a JSON object")
    return value


def load_jsonl(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProvenanceError(f"{path}: cannot read JSONL: {exc}") from exc
    records: list[dict] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProvenanceError(
                f"{path}:{line_number}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise ProvenanceError(f"{path}:{line_number}: record must be an object")
        records.append(record)
    if not records:
        raise ProvenanceError(f"{path}: no records")
    return records


def _relative_artifact(path: Path, base: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(base.resolve())
    except ValueError as exc:
        raise ProvenanceError(
            f"artifact must be under provenance directory: {resolved}"
        ) from exc
    return relative.as_posix()


def build_provenance(
    *,
    output: Path,
    run_id: str,
    paths: dict[str, Path],
    target: dict,
) -> dict:
    if set(paths) != ARTIFACT_KEYS:
        raise ProvenanceError(
            "recording artifacts must be exactly " + ", ".join(sorted(ARTIFACT_KEYS))
        )
    base = output.resolve().parent
    artifacts = {}
    for name, path in sorted(paths.items()):
        if not path.is_file():
            raise ProvenanceError(f"{name} artifact is missing: {path}")
        artifacts[name] = {
            "path": _relative_artifact(path, base),
            "sha256": sha256_file(path),
        }
    return {
        "schema_version": 1,
        "producer": "bench/e2e/record_live.py:v1",
        "run_id": run_id,
        "target": target,
        "artifacts": artifacts,
    }


def _resolve_artifacts(provenance_path: Path, provenance: dict) -> dict[str, Path]:
    artifacts = provenance.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != ARTIFACT_KEYS:
        raise ProvenanceError(
            "provenance artifacts must be exactly "
            + ", ".join(sorted(ARTIFACT_KEYS))
        )
    base = provenance_path.resolve().parent
    resolved: dict[str, Path] = {}
    for name, descriptor in artifacts.items():
        if not isinstance(descriptor, dict):
            raise ProvenanceError(f"artifact {name}: descriptor must be an object")
        relative = descriptor.get("path")
        expected = descriptor.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or not isinstance(expected, str)
            or LOWER_SHA256.fullmatch(expected) is None
        ):
            raise ProvenanceError(f"artifact {name}: invalid path or SHA-256")
        path = (base / relative).resolve()
        try:
            path.relative_to(base)
        except ValueError as exc:
            raise ProvenanceError(f"artifact {name}: path escapes bundle") from exc
        if not path.is_file():
            raise ProvenanceError(f"artifact {name}: file is missing")
        actual = sha256_file(path)
        if actual != expected:
            raise ProvenanceError(
                f"artifact {name}: SHA-256 mismatch "
                f"(expected {expected}, got {actual})"
            )
        resolved[name] = path
    return resolved


def _validate_target(manifest_path: Path, expected_target: object) -> None:
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != 1:
        raise ProvenanceError("target manifest schema_version must be 1")
    repository = manifest.get("repository")
    revision = manifest.get("revision")
    source_root = manifest.get("source_root")
    files = manifest.get("files")
    if (
        not isinstance(repository, str)
        or not repository
        or not isinstance(revision, str)
        or LOWER_SHA256.fullmatch(revision) is None
        or not isinstance(source_root, str)
        or not source_root
        or not isinstance(files, dict)
        or not files
    ):
        raise ProvenanceError("target manifest is malformed")
    if expected_target != {"repository": repository, "revision": revision}:
        raise ProvenanceError("provenance target does not match target manifest")

    base = manifest_path.resolve().parent
    root = (base / source_root).resolve()
    try:
        root.relative_to(base)
    except ValueError as exc:
        raise ProvenanceError("target source_root escapes recording bundle") from exc
    if not root.is_dir():
        raise ProvenanceError("target source_root is missing")

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    }
    unmanifested = sorted(actual_files - set(files))
    if unmanifested:
        raise ProvenanceError(
            "unmanifested target fixture file: " + ", ".join(unmanifested)
        )
    missing = sorted(set(files) - actual_files)
    if missing:
        raise ProvenanceError(
            "manifested target fixture file is missing: " + ", ".join(missing)
        )

    actual_hashes: dict[str, str] = {}
    for relative, expected_sha256 in files.items():
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or not isinstance(expected_sha256, str)
            or LOWER_SHA256.fullmatch(expected_sha256) is None
        ):
            raise ProvenanceError("target manifest contains an invalid file entry")
        source = (root / relative).resolve()
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ProvenanceError(
                f"target manifest path escapes source root: {relative}"
            ) from exc
        if not source.is_file():
            raise ProvenanceError(f"target fixture file is missing: {relative}")
        actual = sha256_file(source)
        if actual != expected_sha256:
            raise ProvenanceError(f"target fixture hash mismatch: {relative}")
        actual_hashes[relative] = actual

    canonical_tree = "\n".join(
        f"{relative}\0{actual_hashes[relative]}"
        for relative in sorted(actual_hashes)
    ).encode("utf-8")
    actual_revision = hashlib.sha256(canonical_tree).hexdigest()
    if actual_revision != revision:
        raise ProvenanceError(
            "target fixture revision does not match its content manifest"
        )


def _validate_identity(component: str, identity: object) -> dict:
    if not isinstance(identity, dict) or identity.get("schema_version") != 1:
        raise ProvenanceError(f"{component}: index_identity must use schema_version 1")
    for field in IDENTITY_FIELDS:
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise ProvenanceError(f"{component}: index_identity.{field} is missing")
    for field in ("repository_id", "checkout_id", "index_generation"):
        if LOWER_SHA256.fullmatch(identity[field]) is None:
            raise ProvenanceError(f"{component}: index_identity.{field} is invalid")
    if (
        identity["source_revision"] != "unborn"
        and GIT_REVISION.fullmatch(identity["source_revision"]) is None
    ):
        raise ProvenanceError(f"{component}: source_revision is invalid")
    if (
        identity["dirty_fingerprint"] != "clean"
        and LOWER_SHA256.fullmatch(identity["dirty_fingerprint"]) is None
    ):
        raise ProvenanceError(f"{component}: dirty_fingerprint is invalid")
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
        raise ProvenanceError(f"{component}: index_generation is invalid")
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
        identity["captured_at"],
    ) is None:
        raise ProvenanceError(f"{component}: captured_at must be UTC RFC3339")
    return identity


def validate_component_evidence(bom_path: Path, evidence_path: Path) -> None:
    bom = load_json(bom_path)
    readiness = bom.get("integrated_readiness")
    if not isinstance(readiness, dict) or readiness.get("status") != "ready":
        raise ProvenanceError("live scoring requires a readiness-approved BOM")
    components = bom.get("components")
    if not isinstance(components, dict):
        raise ProvenanceError("component BOM is malformed")
    search_install = (
        components.get("code-search", {}).get("install", {})
    )
    search_kind = search_install.get("kind")
    if search_kind == "git":
        search_version = search_install.get("revision")
    elif search_kind == "github-release":
        search_version = search_install.get("tag")
    else:
        raise ProvenanceError(
            "code-search: component BOM install kind is unsupported"
        )
    expected_versions = {
        "code-search": search_version,
        "code-graph": components.get("code-graph", {})
        .get("install", {})
        .get("tag"),
    }
    expected_descriptors = {
        component: install_descriptor_sha256(
            components.get(component, {}).get("install")
        )
        for component in ("code-search", "code-graph")
    }

    evidence = load_json(evidence_path)
    if (
        evidence.get("schema_version") != 1
        or evidence.get("checkout_unchanged") is not True
    ):
        raise ProvenanceError(
            "component evidence must be schema v1 with checkout_unchanged true"
        )
    observed = evidence.get("components")
    if not isinstance(observed, dict) or set(observed) != set(expected_versions):
        raise ProvenanceError("component evidence must contain both exact components")
    identities = {}
    for component, expected_version in expected_versions.items():
        details = observed.get(component)
        if (
            not isinstance(expected_version, str)
            or not expected_version
            or not isinstance(details, dict)
            or details.get("version") != expected_version
        ):
            raise ProvenanceError(
                f"{component}: evidence version does not match component BOM"
            )
        if (
            details.get("install_descriptor_sha256")
            != expected_descriptors[component]
        ):
            raise ProvenanceError(
                f"{component}: evidence install descriptor does not match "
                "component BOM"
            )
        identities[component] = _validate_identity(
            component, details.get("index_identity")
        )
    if observed["code-search"].get("index_ready") is not True:
        raise ProvenanceError("code-search evidence is not index_ready")
    if observed["code-graph"].get("status") != "ready":
        raise ProvenanceError("code-graph evidence status is not ready")
    for field in EQUAL_IDENTITY_FIELDS:
        if identities["code-search"][field] != identities["code-graph"][field]:
            raise ProvenanceError(f"component identity mismatch: {field}")


def _records_by_case(path: Path, run_id: str, case_ids: set[str]) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for record in load_jsonl(path):
        if record.get("schema_version") != 1 or record.get("run_id") != run_id:
            raise ProvenanceError(f"{path}: schema_version/run_id mismatch")
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or case_id in records:
            raise ProvenanceError(f"{path}: duplicate or invalid case_id")
        records[case_id] = record
    if set(records) != case_ids:
        raise ProvenanceError(f"{path}: case coverage does not match cases artifact")
    return records


def _validate_recording_artifacts(
    artifacts: dict[str, Path],
    run_id: str,
) -> None:
    cases = load_jsonl(artifacts["cases"])
    runs = load_jsonl(artifacts["runs"])
    case_ids = {record.get("case_id") for record in cases}
    if None in case_ids or len(case_ids) != len(cases):
        raise ProvenanceError("cases artifact has duplicate or missing case IDs")
    run_by_case = _records_by_case(artifacts["runs"], run_id, case_ids)
    transcript = _records_by_case(
        artifacts["raw_mcp_transcript"], run_id, case_ids
    )
    answers = _records_by_case(artifacts["final_answers"], run_id, case_ids)
    extraction = _records_by_case(
        artifacts["claim_extraction"], run_id, case_ids
    )

    for case_id in case_ids:
        run = run_by_case[case_id]
        if run.get("run_mode") != "live":
            raise ProvenanceError(f"{case_id}: provenanced run_mode must be live")
        raw = transcript[case_id]
        raw_calls = raw.get("tool_calls")
        if not isinstance(raw_calls, list):
            raise ProvenanceError(f"{case_id}: raw transcript lacks tool_calls")
        projected_calls = []
        for call in raw_calls:
            if (
                not isinstance(call, dict)
                or "response" not in call
                or call["response"] in (None, "", [], {})
            ):
                raise ProvenanceError(
                    f"{case_id}: every raw tool call requires a recorded response"
                )
            projected_calls.append(
                {
                    key: call[key]
                    for key in ("tool", "arguments", "latency_ms")
                    if key in call
                }
            )
        if projected_calls != run.get("tool_calls"):
            raise ProvenanceError(
                f"{case_id}: scored tool calls differ from raw transcript"
            )
        for field in ("evidence", "index_error", "latency_ms"):
            if raw.get(field) != run.get(field):
                raise ProvenanceError(
                    f"{case_id}: scored {field} differs from raw transcript"
                )

        answer = answers[case_id].get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise ProvenanceError(f"{case_id}: final answer is missing")
        extracted = extraction[case_id]
        if extracted.get("answer_sha256") != hashlib.sha256(
            answer.encode("utf-8")
        ).hexdigest():
            raise ProvenanceError(f"{case_id}: claim extraction answer hash mismatch")
        if extracted.get("claims") != run.get("claims"):
            raise ProvenanceError(
                f"{case_id}: scored claims differ from claim extraction"
            )
        normalized_answer = " ".join(answer.split())
        for claim in run.get("claims", []):
            text = claim.get("text") if isinstance(claim, dict) else None
            if not isinstance(text, str) or " ".join(text.split()) not in normalized_answer:
                raise ProvenanceError(
                    f"{case_id}: extracted claim is absent from final answer"
                )


def validate_live_provenance(
    *,
    provenance_path: Path,
    run_id: str,
    cases_path: Path,
    runs_path: Path,
    thresholds_path: Path,
    bom_path: Path,
) -> None:
    provenance = load_json(provenance_path)
    if (
        provenance.get("schema_version") != 1
        or provenance.get("producer") != "bench/e2e/record_live.py:v1"
        or provenance.get("run_id") != run_id
    ):
        raise ProvenanceError("live provenance header does not match the run")
    artifacts = _resolve_artifacts(provenance_path, provenance)
    supplied = {
        "cases": cases_path.resolve(),
        "runs": runs_path.resolve(),
        "thresholds": thresholds_path.resolve(),
        "component_bom": bom_path.resolve(),
    }
    for name, path in supplied.items():
        if artifacts[name] != path:
            raise ProvenanceError(
                f"scored {name} path differs from the provenance artifact"
            )
    _validate_target(artifacts["target_manifest"], provenance.get("target"))
    validate_component_evidence(
        artifacts["component_bom"],
        artifacts["component_evidence"],
    )
    _validate_recording_artifacts(artifacts, run_id)
