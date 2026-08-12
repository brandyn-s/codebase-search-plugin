#!/usr/bin/env python3
"""Project one CodeQL SARIF path into immutable code-intelligence evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any
from urllib.parse import unquote, urlparse

from proof_evaluator import SCHEMA_VERSION, _stable_id


class CodeQLEvidenceError(ValueError):
    """Raised when CodeQL artifacts cannot support one canonical path."""


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodeQLEvidenceError(f"{field} must be an object")
    return value


def _array(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise CodeQLEvidenceError(f"{field} must be an array")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CodeQLEvidenceError(f"{field} must be a non-empty string")
    return value.strip()


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CodeQLEvidenceError(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: object, field: str) -> int:
    result = _nonnegative_int(value, field)
    if result == 0:
        raise CodeQLEvidenceError(f"{field} must be positive")
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_uri(value: object, field: str) -> str:
    raw = _string(value, field).replace("\\", "/")
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        raise CodeQLEvidenceError(f"{field} must be a repository-relative URI")
    path = unquote(parsed.path)
    while path.startswith("./"):
        path = path[2:]
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or not path or ".." in normalized.parts:
        raise CodeQLEvidenceError(f"{field} must stay within the repository")
    return normalized.as_posix()


def _load_json(path: Path, field: str) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), field)
    except json.JSONDecodeError as exc:
        raise CodeQLEvidenceError(f"{field} is not valid JSON: {exc}") from exc


def _select_one(items: object, index: int, field: str) -> dict[str, Any]:
    values = _array(items, field)
    if index < 0 or index >= len(values):
        raise CodeQLEvidenceError(
            f"{field} index {index} is outside 0..{max(len(values) - 1, 0)}"
        )
    return _object(values[index], f"{field}[{index}]")


def _rule_id(run: dict[str, Any], result: dict[str, Any]) -> str:
    result_rule = result.get("ruleId")
    indexed_rule = None
    rule_index = result.get("ruleIndex")
    if rule_index is not None:
        rule_index = _nonnegative_int(rule_index, "result.ruleIndex")
        driver = _object(
            _object(_object(run.get("tool"), "run.tool").get("driver"), "run.tool.driver"),
            "run.tool.driver",
        )
        rule = _select_one(driver.get("rules"), rule_index, "run.tool.driver.rules")
        indexed_rule = _string(rule.get("id"), "selected rule.id")
    if result_rule is not None:
        result_rule = _string(result_rule, "result.ruleId")
    if result_rule and indexed_rule and result_rule != indexed_rule:
        raise CodeQLEvidenceError("result.ruleId disagrees with ruleIndex")
    if not result_rule and not indexed_rule:
        raise CodeQLEvidenceError("selected result has no rule identity")
    return str(result_rule or indexed_rule)


def _path_steps(thread_flow: dict[str, Any]) -> list[dict[str, Any]]:
    locations = _array(thread_flow.get("locations"), "threadFlow.locations")
    if len(locations) < 2:
        raise CodeQLEvidenceError("CodeQL path must contain at least two locations")
    steps: list[dict[str, Any]] = []
    for position, item in enumerate(locations):
        location = _object(_object(item, f"locations[{position}]").get("location"), f"locations[{position}].location")
        physical = _object(location.get("physicalLocation"), f"locations[{position}].physicalLocation")
        artifact = _object(physical.get("artifactLocation"), f"locations[{position}].artifactLocation")
        region = _object(physical.get("region"), f"locations[{position}].region")
        start_line = _positive_int(region.get("startLine"), f"locations[{position}].startLine")
        end_line = _positive_int(region.get("endLine", start_line), f"locations[{position}].endLine")
        start_column = _positive_int(region.get("startColumn", 1), f"locations[{position}].startColumn")
        end_column = _positive_int(region.get("endColumn", start_column), f"locations[{position}].endColumn")
        if (end_line, end_column) < (start_line, start_column):
            raise CodeQLEvidenceError(
                f"locations[{position}] end coordinate precedes start coordinate"
            )
        role = "intermediate"
        if position == 0:
            role = "source"
        elif position == len(locations) - 1:
            role = "sink"
        steps.append(
            {
                "position": position,
                "role": role,
                "relative_path": _relative_uri(
                    artifact.get("uri"),
                    f"locations[{position}].artifactLocation.uri",
                ),
                "start_line": start_line,
                "start_column": start_column,
                "end_line": end_line,
                "end_column": end_column,
            }
        )
    return steps


def ingest(
    sarif_path: Path,
    database_manifest_path: Path,
    query_pack_manifest_path: Path,
    index_generation: str,
    *,
    repository_id: str | None = None,
    source_revision: str | None = None,
    run_index: int = 0,
    result_index: int = 0,
    code_flow_index: int = 0,
    thread_flow_index: int = 0,
) -> dict[str, Any]:
    sarif = _load_json(sarif_path, "sarif")
    database = _load_json(database_manifest_path, "database_manifest")
    if database.get("schema_version") != SCHEMA_VERSION:
        raise CodeQLEvidenceError("database_manifest.schema_version must equal 1")

    database_repository_id = _string(
        database.get("repository_id"), "database_manifest.repository_id"
    )
    database_source_revision = _string(
        database.get("source_revision"), "database_manifest.source_revision"
    )
    if repository_id is not None and repository_id != database_repository_id:
        raise CodeQLEvidenceError(
            "database repository identity disagrees with the requested checkout"
        )
    if source_revision is not None and source_revision != database_source_revision:
        raise CodeQLEvidenceError(
            "database source revision disagrees with the requested checkout"
        )
    repository_id = database_repository_id
    source_revision = database_source_revision
    language = _string(database.get("language"), "database_manifest.language").lower()
    cli_version = _string(database.get("codeql_cli_version"), "database_manifest.codeql_cli_version")
    extractor_version = _string(database.get("extractor_version"), "database_manifest.extractor_version")
    database_content_sha256 = _string(
        database.get("database_content_sha256"),
        "database_manifest.database_content_sha256",
    )
    quality = _object(database.get("quality"), "database_manifest.quality")
    quality_status = _string(
        quality.get("status"), "database_manifest.quality.status"
    ).lower()
    source_files = _positive_int(
        quality.get("source_files"), "database_manifest.quality.source_files"
    )
    baseline_lines = _positive_int(
        quality.get("baseline_lines"), "database_manifest.quality.baseline_lines"
    )
    extractor_errors = _nonnegative_int(
        quality.get("extractor_errors"),
        "database_manifest.quality.extractor_errors",
    )
    if quality_status != "pass":
        raise CodeQLEvidenceError("database quality status must be pass")
    normalized_quality = {
        "status": quality_status,
        "source_files": source_files,
        "baseline_lines": baseline_lines,
        "extractor_errors": extractor_errors,
    }
    generation = _string(index_generation, "index_generation")

    run = _select_one(sarif.get("runs"), run_index, "sarif.runs")
    driver = _object(_object(run.get("tool"), "run.tool").get("driver"), "run.tool.driver")
    analyzer = _string(driver.get("name"), "run.tool.driver.name").lower()
    if analyzer != "codeql":
        raise CodeQLEvidenceError("SARIF analyzer must be CodeQL")
    sarif_version = _string(
        driver.get("semanticVersion", driver.get("version")),
        "run.tool.driver version",
    )
    if sarif_version != cli_version:
        raise CodeQLEvidenceError(
            "SARIF CodeQL version disagrees with database manifest"
        )

    result = _select_one(run.get("results"), result_index, "run.results")
    query_id = _rule_id(run, result)
    code_flow = _select_one(result.get("codeFlows"), code_flow_index, "result.codeFlows")
    thread_flow = _select_one(code_flow.get("threadFlows"), thread_flow_index, "codeFlow.threadFlows")
    steps = _path_steps(thread_flow)

    analysis_payload = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": repository_id,
        "source_revision": source_revision,
        "index_generation": generation,
        "analysis_kind": "variable_level_taint",
        "analyzer": "codeql",
        "analyzer_version": cli_version,
        "extractor_version": extractor_version,
        "language": language,
        "database_manifest_sha256": _sha256(database_manifest_path),
        "database_content_sha256": database_content_sha256,
        "database_quality": normalized_quality,
        "query_pack_manifest_sha256": _sha256(query_pack_manifest_path),
        "sarif_sha256": _sha256(sarif_path),
        "query_id": query_id,
        "result_index": result_index,
        "code_flow_index": code_flow_index,
        "thread_flow_index": thread_flow_index,
        "path_steps": steps,
    }
    analysis_ref = {
        "id": _stable_id("analysis", analysis_payload),
        **analysis_payload,
    }
    source = steps[0]
    evidence_payload = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": repository_id,
        "source_revision": source_revision,
        "index_generation": generation,
        "relative_path": source["relative_path"],
        "start_line": source["start_line"],
        "end_line": source["end_line"],
        "evidence_type": "codeql_path",
        "analysis_ref": analysis_ref,
    }
    evidence_ref = {
        "id": _stable_id("ev", evidence_payload),
        **evidence_payload,
    }
    observation_payload = {
        "schema_version": SCHEMA_VERSION,
        "evidence_ref": evidence_ref,
        "stance": "support",
        "source_engine": "codeql",
        "derivation": "codeql_path",
        "confidence_band": "high",
    }
    return {
        "id": _stable_id("obs", observation_payload),
        **observation_payload,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest_parser = commands.add_parser("ingest", help="ingest one CodeQL path")
    ingest_parser.add_argument("sarif", type=Path)
    ingest_parser.add_argument("--database-manifest", type=Path, required=True)
    ingest_parser.add_argument("--query-pack-manifest", type=Path, required=True)
    ingest_parser.add_argument("--index-generation", required=True)
    ingest_parser.add_argument("--repository-id")
    ingest_parser.add_argument("--source-revision")
    ingest_parser.add_argument("--run-index", type=int, default=0)
    ingest_parser.add_argument("--result-index", type=int, default=0)
    ingest_parser.add_argument("--code-flow-index", type=int, default=0)
    ingest_parser.add_argument("--thread-flow-index", type=int, default=0)
    ingest_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        observation = ingest(
            args.sarif,
            args.database_manifest,
            args.query_pack_manifest,
            args.index_generation,
            repository_id=args.repository_id,
            source_revision=args.source_revision,
            run_index=args.run_index,
            result_index=args.result_index,
            code_flow_index=args.code_flow_index,
            thread_flow_index=args.thread_flow_index,
        )
        args.output.write_text(_canonical_json(observation), encoding="utf-8")
    except (OSError, CodeQLEvidenceError) as exc:
        print(
            _canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "invalid",
                    "error": str(exc),
                }
            ),
            end="",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
