#!/usr/bin/env python3
"""Deterministically evaluate evidence-backed code-intelligence claims.

The evaluator does not inspect source code or call a model. It consumes the
canonical claim/observation records produced by the retrieval engines and
applies fail-closed proof rules. A claim can be ``verified`` only when the
indexes are coherent and current, supporting evidence exists, coverage is
complete, and an explicit contradiction search was performed.

Relationship evidence is validated recursively. Resolver provenance, runtime
confirmation, source/target symbol identity, and index generation therefore
participate in the canonical observation ID and cannot be edited after the
fact without invalidating the proof bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
VERDICTS = {"verified", "contradicted", "unresolved", "blocked"}
STANCES = {"support", "contradict"}
FRESHNESS = {"current", "stale", "unknown"}
COVERAGE_STATES = {"complete", "partial", "unknown"}
INVARIANT_STATES = {"pass", "fail", "unresolved"}
CONFIDENCE_BANDS = {"high", "medium", "low", "speculative", "unknown"}
TRUSTED_SUPPORT_BANDS = {"high", "medium"}
ASSURANCE_CAPABILITIES = {
    "source_coordinates",
    "semantic_retrieval",
    "lexical_retrieval",
    "structural_relationship",
    "compiler_resolution",
    "runtime_observation",
    "variable_level_taint",
}


class ProofInputError(ValueError):
    """Raised when a proof bundle is malformed or internally incoherent."""


def _canonical_path(path: str) -> str:
    value = path.replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value


def _canonical_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _canonical_token(value: str) -> str:
    return value.strip().lower()


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return f"{prefix}:v{SCHEMA_VERSION}:" + hashlib.sha256(encoded).hexdigest()


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProofInputError(f"{field} must be an object")
    return value


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProofInputError(f"{field} must be a non-empty string")
    return value.strip()


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProofInputError(f"{field} must be a boolean")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProofInputError(f"{field} must be a non-negative integer")
    return value


def _optional_sha256(value: object, field: str) -> str | None:
    if value is None:
        return None
    digest = _string(value, field)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ProofInputError(f"{field} must be 64 lowercase hex characters")
    return digest


def _token_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProofInputError(f"{field} must be an array")
    tokens = [
        _canonical_token(_string(item, f"{field}[{position}]"))
        for position, item in enumerate(value)
    ]
    if len(tokens) != len(set(tokens)):
        raise ProofInputError(f"{field} must contain unique values")
    unknown = sorted(set(tokens) - ASSURANCE_CAPABILITIES)
    if unknown:
        raise ProofInputError(
            f"{field} contains unsupported capabilities: {', '.join(unknown)}"
        )
    return sorted(tokens)


def _validate_symbol_ref(value: object, field: str) -> dict[str, Any]:
    symbol = _object(value, field)
    if symbol.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError(f"{field}.schema_version must equal 1")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": _string(
            symbol.get("repository_id"), f"{field}.repository_id"
        ),
        "source_revision": _string(
            symbol.get("source_revision"), f"{field}.source_revision"
        ),
        "relative_path": _canonical_path(
            _string(symbol.get("relative_path"), f"{field}.relative_path")
        ),
        "symbol_kind": _canonical_token(
            _string(symbol.get("symbol_kind"), f"{field}.symbol_kind")
        ),
        "qualified_name": _string(
            symbol.get("qualified_name"), f"{field}.qualified_name"
        ),
        "start_line": _nonnegative_int(
            symbol.get("start_line"), f"{field}.start_line"
        ),
        "end_line": _nonnegative_int(
            symbol.get("end_line"), f"{field}.end_line"
        ),
    }
    if payload["end_line"] < payload["start_line"]:
        raise ProofInputError(f"{field}.end_line cannot precede start_line")
    expected_id = _stable_id("sym", payload)
    actual_id = _string(symbol.get("id"), f"{field}.id")
    if actual_id != expected_id:
        raise ProofInputError(f"{field}.id does not match canonical contents")
    return {"id": expected_id, **payload}


def _validate_relationship_ref(
    value: object,
    field: str,
) -> dict[str, Any]:
    relationship = _object(value, field)
    if relationship.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError(f"{field}.schema_version must equal 1")

    repository_id = _string(
        relationship.get("repository_id"),
        f"{field}.repository_id",
    )
    source_revision = _string(
        relationship.get("source_revision"),
        f"{field}.source_revision",
    )
    index_generation = _string(
        relationship.get("index_generation"),
        f"{field}.index_generation",
    )
    source_symbol = _validate_symbol_ref(
        relationship.get("source_symbol_ref"),
        f"{field}.source_symbol_ref",
    )
    target_symbol = _validate_symbol_ref(
        relationship.get("target_symbol_ref"),
        f"{field}.target_symbol_ref",
    )
    for role, symbol in (
        ("source", source_symbol),
        ("target", target_symbol),
    ):
        if symbol["repository_id"] != repository_id:
            raise ProofInputError(
                f"{field}.{role}_symbol_ref belongs to a different repository"
            )
        if symbol["source_revision"] != source_revision:
            raise ProofInputError(
                f"{field}.{role}_symbol_ref belongs to a different source revision"
            )

    confidence_band = _canonical_token(
        _string(
            relationship.get("confidence_band"),
            f"{field}.confidence_band",
        )
    )
    if confidence_band not in CONFIDENCE_BANDS:
        raise ProofInputError(f"{field}.confidence_band is invalid")
    runtime_observed = _boolean(
        relationship.get("runtime_observed"),
        f"{field}.runtime_observed",
    )
    observation_count = _nonnegative_int(
        relationship.get("observation_count"),
        f"{field}.observation_count",
    )
    if runtime_observed and observation_count == 0:
        raise ProofInputError(
            f"{field}.runtime_observed requires a positive observation_count"
        )
    if not runtime_observed and observation_count != 0:
        raise ProofInputError(
            f"{field}.observation_count requires runtime_observed=true"
        )

    resolution_source = _canonical_token(
        _string(
            relationship.get("resolution_source"),
            f"{field}.resolution_source",
        )
    )
    resolution_artifact_sha256 = _optional_sha256(
        relationship.get("resolution_artifact_sha256"),
        f"{field}.resolution_artifact_sha256",
    )
    if (
        resolution_artifact_sha256 is not None
        and "scip-ingest" not in resolution_source.split("+")
    ):
        raise ProofInputError(
            f"{field}.resolution_artifact_sha256 requires scip-ingest provenance"
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": repository_id,
        "source_revision": source_revision,
        "index_generation": index_generation,
        "relation_type": _canonical_token(
            _string(
                relationship.get("relation_type"),
                f"{field}.relation_type",
            )
        ),
        "source_symbol_ref": source_symbol,
        "target_symbol_ref": target_symbol,
        "resolution_source": resolution_source,
        "confidence_band": confidence_band,
        "runtime_observed": runtime_observed,
        "observation_count": observation_count,
    }
    if resolution_artifact_sha256 is not None:
        payload["resolution_artifact_sha256"] = resolution_artifact_sha256
    expected_id = _stable_id("rel", payload)
    actual_id = _string(relationship.get("id"), f"{field}.id")
    if actual_id != expected_id:
        raise ProofInputError(f"{field}.id does not match canonical contents")
    return {"id": expected_id, **payload}


def _validate_analysis_ref(value: object, field: str) -> dict[str, Any]:
    analysis = _object(value, field)
    if analysis.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError(f"{field}.schema_version must equal 1")
    path_steps = analysis.get("path_steps")
    if not isinstance(path_steps, list) or len(path_steps) < 2:
        raise ProofInputError(f"{field}.path_steps must contain at least two steps")
    normalized_steps: list[dict[str, Any]] = []
    for position, item in enumerate(path_steps):
        step_field = f"{field}.path_steps[{position}]"
        step = _object(item, step_field)
        role = _canonical_token(_string(step.get("role"), f"{step_field}.role"))
        expected_role = "intermediate"
        if position == 0:
            expected_role = "source"
        elif position == len(path_steps) - 1:
            expected_role = "sink"
        if role != expected_role:
            raise ProofInputError(
                f"{step_field}.role must be {expected_role}"
            )
        normalized_step = {
            "position": _nonnegative_int(
                step.get("position"), f"{step_field}.position"
            ),
            "role": role,
            "relative_path": _canonical_path(
                _string(step.get("relative_path"), f"{step_field}.relative_path")
            ),
            "start_line": _nonnegative_int(
                step.get("start_line"), f"{step_field}.start_line"
            ),
            "start_column": _nonnegative_int(
                step.get("start_column"), f"{step_field}.start_column"
            ),
            "end_line": _nonnegative_int(
                step.get("end_line"), f"{step_field}.end_line"
            ),
            "end_column": _nonnegative_int(
                step.get("end_column"), f"{step_field}.end_column"
            ),
        }
        if normalized_step["position"] != position:
            raise ProofInputError(f"{step_field}.position must equal {position}")
        if (
            normalized_step["end_line"],
            normalized_step["end_column"],
        ) < (
            normalized_step["start_line"],
            normalized_step["start_column"],
        ):
            raise ProofInputError(f"{step_field} end precedes start")
        normalized_steps.append(normalized_step)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": _string(
            analysis.get("repository_id"), f"{field}.repository_id"
        ),
        "source_revision": _string(
            analysis.get("source_revision"), f"{field}.source_revision"
        ),
        "index_generation": _string(
            analysis.get("index_generation"), f"{field}.index_generation"
        ),
        "analysis_kind": _canonical_token(
            _string(analysis.get("analysis_kind"), f"{field}.analysis_kind")
        ),
        "analyzer": _canonical_token(
            _string(analysis.get("analyzer"), f"{field}.analyzer")
        ),
        "analyzer_version": _string(
            analysis.get("analyzer_version"), f"{field}.analyzer_version"
        ),
        "extractor_version": _string(
            analysis.get("extractor_version"), f"{field}.extractor_version"
        ),
        "language": _canonical_token(
            _string(analysis.get("language"), f"{field}.language")
        ),
        "database_manifest_sha256": _string(
            analysis.get("database_manifest_sha256"),
            f"{field}.database_manifest_sha256",
        ),
        "database_content_sha256": _string(
            analysis.get("database_content_sha256"),
            f"{field}.database_content_sha256",
        ),
        "database_quality": {
            "status": _canonical_token(
                _string(
                    _object(
                        analysis.get("database_quality"),
                        f"{field}.database_quality",
                    ).get("status"),
                    f"{field}.database_quality.status",
                )
            ),
            "source_files": _nonnegative_int(
                _object(
                    analysis.get("database_quality"),
                    f"{field}.database_quality",
                ).get("source_files"),
                f"{field}.database_quality.source_files",
            ),
            "baseline_lines": _nonnegative_int(
                _object(
                    analysis.get("database_quality"),
                    f"{field}.database_quality",
                ).get("baseline_lines"),
                f"{field}.database_quality.baseline_lines",
            ),
            "extractor_errors": _nonnegative_int(
                _object(
                    analysis.get("database_quality"),
                    f"{field}.database_quality",
                ).get("extractor_errors"),
                f"{field}.database_quality.extractor_errors",
            ),
        },
        "query_pack_manifest_sha256": _string(
            analysis.get("query_pack_manifest_sha256"),
            f"{field}.query_pack_manifest_sha256",
        ),
        "sarif_sha256": _string(
            analysis.get("sarif_sha256"), f"{field}.sarif_sha256"
        ),
        "query_id": _string(analysis.get("query_id"), f"{field}.query_id"),
        "result_index": _nonnegative_int(
            analysis.get("result_index"), f"{field}.result_index"
        ),
        "code_flow_index": _nonnegative_int(
            analysis.get("code_flow_index"), f"{field}.code_flow_index"
        ),
        "thread_flow_index": _nonnegative_int(
            analysis.get("thread_flow_index"), f"{field}.thread_flow_index"
        ),
        "path_steps": normalized_steps,
    }
    if payload["analysis_kind"] != "variable_level_taint":
        raise ProofInputError(f"{field}.analysis_kind must be variable_level_taint")
    if payload["analyzer"] != "codeql":
        raise ProofInputError(f"{field}.analyzer must be codeql")
    if (
        payload["database_quality"]["status"] != "pass"
        or payload["database_quality"]["source_files"] == 0
        or payload["database_quality"]["baseline_lines"] == 0
    ):
        raise ProofInputError(f"{field}.database_quality is not passing")
    expected_id = _stable_id("analysis", payload)
    actual_id = _string(analysis.get("id"), f"{field}.id")
    if actual_id != expected_id:
        raise ProofInputError(f"{field}.id does not match canonical contents")
    return {"id": expected_id, **payload}


def _validate_evidence_ref(value: object, field: str) -> dict[str, Any]:
    evidence = _object(value, field)
    if evidence.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError(f"{field}.schema_version must equal 1")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": _string(
            evidence.get("repository_id"), f"{field}.repository_id"
        ),
        "source_revision": _string(
            evidence.get("source_revision"), f"{field}.source_revision"
        ),
        "index_generation": _string(
            evidence.get("index_generation"), f"{field}.index_generation"
        ),
        "relative_path": _canonical_path(
            _string(evidence.get("relative_path"), f"{field}.relative_path")
        ),
        "start_line": _nonnegative_int(
            evidence.get("start_line"), f"{field}.start_line"
        ),
        "end_line": _nonnegative_int(
            evidence.get("end_line"), f"{field}.end_line"
        ),
        "evidence_type": _canonical_token(
            _string(evidence.get("evidence_type"), f"{field}.evidence_type")
        ),
    }
    if payload["end_line"] < payload["start_line"]:
        raise ProofInputError(f"{field}.end_line cannot precede start_line")

    symbol_ref = None
    if evidence.get("symbol_ref") is not None:
        symbol_ref = _validate_symbol_ref(
            evidence.get("symbol_ref"), f"{field}.symbol_ref"
        )
        if symbol_ref["repository_id"] != payload["repository_id"]:
            raise ProofInputError(
                f"{field}.symbol_ref belongs to a different repository"
            )
        if symbol_ref["source_revision"] != payload["source_revision"]:
            raise ProofInputError(
                f"{field}.symbol_ref belongs to a different source revision"
            )
        if (
            symbol_ref["relative_path"] != payload["relative_path"]
            or symbol_ref["start_line"] != payload["start_line"]
            or symbol_ref["end_line"] != payload["end_line"]
        ):
            raise ProofInputError(
                f"{field}.symbol_ref does not match the evidence location"
            )
        payload["symbol_ref"] = symbol_ref

    if evidence.get("relationship_ref") is not None:
        relationship_ref = _validate_relationship_ref(
            evidence.get("relationship_ref"),
            f"{field}.relationship_ref",
        )
        for key in (
            "repository_id",
            "source_revision",
            "index_generation",
        ):
            if relationship_ref[key] != payload[key]:
                raise ProofInputError(
                    f"{field}.relationship_ref has a different {key}"
                )
        source_symbol = relationship_ref["source_symbol_ref"]
        if (
            source_symbol["relative_path"] != payload["relative_path"]
            or source_symbol["start_line"] != payload["start_line"]
            or source_symbol["end_line"] != payload["end_line"]
        ):
            raise ProofInputError(
                f"{field}.relationship_ref source does not match the evidence location"
            )
        if symbol_ref is None:
            raise ProofInputError(
                f"{field}.relationship_ref requires the source symbol_ref"
            )
        if source_symbol["id"] != symbol_ref["id"]:
            raise ProofInputError(
                f"{field}.relationship_ref source does not match symbol_ref"
            )
        payload["relationship_ref"] = relationship_ref

    if evidence.get("analysis_ref") is not None:
        analysis_ref = _validate_analysis_ref(
            evidence.get("analysis_ref"), f"{field}.analysis_ref"
        )
        for key in ("repository_id", "source_revision", "index_generation"):
            if analysis_ref[key] != payload[key]:
                raise ProofInputError(
                    f"{field}.analysis_ref has a different {key}"
                )
        source = analysis_ref["path_steps"][0]
        if (
            source["relative_path"] != payload["relative_path"]
            or source["start_line"] != payload["start_line"]
            or source["end_line"] != payload["end_line"]
        ):
            raise ProofInputError(
                f"{field}.analysis_ref source does not match the evidence location"
            )
        if payload["evidence_type"] != "codeql_path":
            raise ProofInputError(
                f"{field}.analysis_ref requires evidence_type codeql_path"
            )
        payload["analysis_ref"] = analysis_ref

    expected_id = _stable_id("ev", payload)
    actual_id = _string(evidence.get("id"), f"{field}.id")
    if actual_id != expected_id:
        raise ProofInputError(f"{field}.id does not match canonical contents")
    return {"id": expected_id, **payload}


def _validate_observation(
    observation: object,
    *,
    position: int,
    repository_id: str,
    index_generation: str,
) -> dict[str, Any]:
    field = f"observations[{position}]"
    value = _object(observation, field)
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError(f"{field}.schema_version must equal 1")
    stance = _canonical_token(_string(value.get("stance"), f"{field}.stance"))
    if stance not in STANCES:
        raise ProofInputError(f"{field}.stance is invalid")
    source_engine = _canonical_token(
        _string(value.get("source_engine"), f"{field}.source_engine")
    )
    derivation = _canonical_token(
        _string(value.get("derivation"), f"{field}.derivation")
    )
    band = _canonical_token(
        _string(
            value.get("confidence_band", "unknown"),
            f"{field}.confidence_band",
        )
    )
    if band not in CONFIDENCE_BANDS:
        raise ProofInputError(f"{field}.confidence_band is invalid")

    evidence = _validate_evidence_ref(
        value.get("evidence_ref"), f"{field}.evidence_ref"
    )
    if evidence["repository_id"] != repository_id:
        raise ProofInputError(f"{field} belongs to a different repository")
    if evidence["index_generation"] != index_generation:
        raise ProofInputError(f"{field} belongs to a different index generation")

    relationship = evidence.get("relationship_ref")
    if relationship is not None:
        if relationship["confidence_band"] != band:
            raise ProofInputError(
                f"{field}.confidence_band disagrees with relationship_ref"
            )
        expected_derivation = relationship["resolution_source"]
        if derivation != expected_derivation:
            raise ProofInputError(
                f"{field}.derivation disagrees with relationship_ref"
            )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "evidence_ref": evidence,
        "stance": stance,
        "source_engine": source_engine,
        "derivation": derivation,
        "confidence_band": band,
    }
    expected_id = _stable_id("obs", payload)
    actual_id = _string(value.get("id"), f"{field}.id")
    if actual_id != expected_id:
        raise ProofInputError(f"{field}.id does not match canonical contents")
    return {"id": expected_id, **payload}


def _validate_claim(value: object) -> dict[str, Any]:
    claim = _object(value, "claim")
    if claim.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError("claim.schema_version must equal 1")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "repository_id": _string(claim.get("repository_id"), "claim.repository_id"),
        "claim_kind": _canonical_token(
            _string(claim.get("claim_kind"), "claim.claim_kind")
        ),
        "claim_text": _canonical_text(
            _string(claim.get("claim_text"), "claim.claim_text")
        ),
    }
    expected_id = _stable_id("claim", payload)
    actual_id = _string(claim.get("id"), "claim.id")
    if actual_id != expected_id:
        raise ProofInputError("claim.id does not match canonical contents")
    return {"id": expected_id, **payload}


def validate_bundle(bundle: object) -> dict[str, Any]:
    value = _object(bundle, "bundle")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ProofInputError("schema_version must equal 1")

    claim = _validate_claim(value.get("claim"))
    repository_id = claim["repository_id"]

    index_state = _object(value.get("index_state"), "index_state")
    if not isinstance(index_state.get("coherent"), bool):
        raise ProofInputError("index_state.coherent must be a boolean")
    freshness = _canonical_token(
        _string(index_state.get("freshness"), "index_state.freshness")
    )
    if freshness not in FRESHNESS:
        raise ProofInputError("index_state.freshness is invalid")
    index_generation = _string(
        index_state.get("index_generation"),
        "index_state.index_generation",
    )
    normalized_index_state = {
        "coherent": index_state["coherent"],
        "freshness": freshness,
        "index_generation": index_generation,
    }

    assurance_requirement = value.get("assurance_requirement")
    normalized_assurance_requirement = None
    if assurance_requirement is not None:
        assurance_requirement = _object(
            assurance_requirement,
            "assurance_requirement",
        )
        normalized_assurance_requirement = {
            "required_capabilities": _token_list(
                assurance_requirement.get("required_capabilities"),
                "assurance_requirement.required_capabilities",
            )
        }
        if not normalized_assurance_requirement["required_capabilities"]:
            raise ProofInputError(
                "assurance_requirement.required_capabilities cannot be empty"
            )

    observations = value.get("observations")
    if not isinstance(observations, list):
        raise ProofInputError("observations must be an array")
    validated_observations = [
        _validate_observation(
            observation,
            position=position,
            repository_id=repository_id,
            index_generation=index_generation,
        )
        for position, observation in enumerate(observations)
    ]
    observation_ids = [item["id"] for item in validated_observations]
    if len(observation_ids) != len(set(observation_ids)):
        raise ProofInputError("observation ids must be unique")

    contradiction = _object(
        value.get("contradiction_search"),
        "contradiction_search",
    )
    if not isinstance(contradiction.get("performed"), bool):
        raise ProofInputError("contradiction_search.performed must be a boolean")
    normalized_contradiction = {
        "performed": contradiction["performed"],
        "strategy": _canonical_token(
            _string(contradiction.get("strategy"), "contradiction_search.strategy")
        ),
        "candidate_count": _nonnegative_int(
            contradiction.get("candidate_count"),
            "contradiction_search.candidate_count",
        ),
    }

    coverage = _object(value.get("coverage"), "coverage")
    coverage_state = _canonical_token(
        _string(coverage.get("state"), "coverage.state")
    )
    if coverage_state not in COVERAGE_STATES:
        raise ProofInputError("coverage.state is invalid")
    examined = _nonnegative_int(coverage.get("examined"), "coverage.examined")
    unresolved = _nonnegative_int(coverage.get("unresolved"), "coverage.unresolved")
    expected = coverage.get("expected")
    if expected is not None:
        expected = _nonnegative_int(expected, "coverage.expected")
        if examined > expected:
            raise ProofInputError("coverage.examined cannot exceed coverage.expected")
    if coverage_state == "complete" and expected is None:
        raise ProofInputError(
            "complete coverage requires a known expected count"
        )
    if coverage_state == "complete" and examined != expected:
        raise ProofInputError(
            "complete coverage requires examined to equal expected"
        )
    normalized_coverage = {
        "state": coverage_state,
        "examined": examined,
        "expected": expected,
        "unresolved": unresolved,
    }

    invariant = value.get("invariant")
    normalized_invariant = None
    if invariant is not None:
        invariant = _object(invariant, "invariant")
        invariant_state = _canonical_token(
            _string(invariant.get("status"), "invariant.status")
        )
        if invariant_state not in INVARIANT_STATES:
            raise ProofInputError("invariant.status is invalid")
        checked = _nonnegative_int(invariant.get("checked"), "invariant.checked")
        violations = _nonnegative_int(
            invariant.get("violations"),
            "invariant.violations",
        )
        invariant_unresolved = _nonnegative_int(
            invariant.get("unresolved"),
            "invariant.unresolved",
        )
        if invariant_state == "pass" and (violations or invariant_unresolved):
            raise ProofInputError(
                "a passing invariant cannot contain violations or unresolved subjects"
            )
        if invariant_state == "fail" and violations == 0:
            raise ProofInputError("a failing invariant must contain a violation")
        normalized_invariant = {
            "id": _string(invariant.get("id"), "invariant.id"),
            "status": invariant_state,
            "checked": checked,
            "violations": violations,
            "unresolved": invariant_unresolved,
        }

    normalized_bundle = {
        "schema_version": SCHEMA_VERSION,
        "claim": claim,
        "index_state": normalized_index_state,
        "observations": validated_observations,
        "contradiction_search": normalized_contradiction,
        "coverage": normalized_coverage,
        "invariant": normalized_invariant,
    }
    if normalized_assurance_requirement is not None:
        normalized_bundle["assurance_requirement"] = normalized_assurance_requirement
    return normalized_bundle


def _runtime_confirmed(observation: dict[str, Any]) -> bool:
    relationship = observation["evidence_ref"].get("relationship_ref")
    return bool(relationship and relationship["runtime_observed"])


def _observation_capabilities(observation: dict[str, Any]) -> set[str]:
    evidence = observation["evidence_ref"]
    capabilities = {"source_coordinates"}
    evidence_type = evidence["evidence_type"]
    if evidence_type in {"semantic_match", "hybrid_match"}:
        capabilities.add("semantic_retrieval")
    if evidence_type == "lexical_match":
        capabilities.add("lexical_retrieval")

    relationship = evidence.get("relationship_ref")
    if relationship is not None:
        capabilities.add("structural_relationship")
        resolution_source = relationship["resolution_source"]
        if (
            "scip-ingest" in resolution_source.split("+")
            and relationship.get("resolution_artifact_sha256") is not None
        ):
            capabilities.add("compiler_resolution")
        if relationship["runtime_observed"]:
            capabilities.add("runtime_observation")

    if evidence_type in {"codeql_path", "variable_level_taint"}:
        analysis = evidence.get("analysis_ref")
        if (
            analysis is not None
            and analysis["analyzer"] == "codeql"
            and analysis["analysis_kind"] == "variable_level_taint"
        ):
            capabilities.add("variable_level_taint")
    return capabilities


def _assurance_lattice(
    required: list[str],
    supporting: list[dict[str, Any]],
    contradicting: list[dict[str, Any]],
) -> dict[str, Any]:
    supporting_capabilities = set().union(
        *(_observation_capabilities(item) for item in supporting),
    ) if supporting else set()
    contradicting_capabilities = set().union(
        *(_observation_capabilities(item) for item in contradicting),
    ) if contradicting else set()
    required_capabilities = set(required)
    missing_supporting = required_capabilities - supporting_capabilities
    missing_contradicting = required_capabilities - contradicting_capabilities
    satisfied_by = None
    if required_capabilities and not missing_contradicting and contradicting:
        satisfied_by = "contradiction"
    elif required_capabilities and not missing_supporting and supporting:
        satisfied_by = "support"
    return {
        "required_capabilities": sorted(required_capabilities),
        "supporting_capabilities": sorted(supporting_capabilities),
        "contradicting_capabilities": sorted(contradicting_capabilities),
        "missing_supporting_capabilities": sorted(missing_supporting),
        "missing_contradicting_capabilities": sorted(missing_contradicting),
        "satisfied_by": satisfied_by,
    }


def _relationship_summary(
    observations: list[dict[str, Any]],
) -> dict[str, Any]:
    relationships = [
        item["evidence_ref"]["relationship_ref"]
        for item in observations
        if item["evidence_ref"].get("relationship_ref") is not None
    ]
    return {
        "count": len(relationships),
        "runtime_confirmed": sum(
            1 for item in relationships if item["runtime_observed"]
        ),
        "resolution_sources": sorted(
            {item["resolution_source"] for item in relationships}
        ),
    }


def _confidence(
    verdict: str,
    supporting: list[dict[str, Any]],
    contradicting: list[dict[str, Any]],
) -> dict[str, Any]:
    if verdict == "blocked":
        return {
            "band": "unknown",
            "rationale": ["proof evaluation was blocked before evidence could be trusted"],
        }
    if verdict == "contradicted":
        trusted_counterexample = any(
            item["confidence_band"] in TRUSTED_SUPPORT_BANDS
            or _runtime_confirmed(item)
            for item in contradicting
        )
        return {
            "band": "high" if trusted_counterexample else "medium",
            "rationale": [
                "a counterexample or invariant violation directly contradicts the claim"
            ],
        }
    if verdict == "unresolved":
        return {
            "band": "low",
            "rationale": ["one or more proof completeness requirements were not met"],
        }

    engines = {item["source_engine"] for item in supporting}
    derivations = {item["derivation"] for item in supporting}
    runtime_corroborated = any(_runtime_confirmed(item) for item in supporting)
    independent = len(engines) >= 2 or len(derivations) >= 2
    if independent or runtime_corroborated:
        rationale = ["complete proof with an explicit contradiction pass"]
        if independent:
            rationale.append(
                "support is corroborated by independent engines or derivations"
            )
        if runtime_corroborated:
            rationale.append(
                "at least one static relationship is confirmed by runtime traces"
            )
        return {"band": "high", "rationale": rationale}
    return {
        "band": "medium",
        "rationale": [
            "complete proof with an explicit contradiction pass",
            "support comes from one trusted engine and derivation",
        ],
    }


def evaluate(bundle: object) -> dict[str, Any]:
    value = validate_bundle(bundle)
    claim = value["claim"]
    index_state = value["index_state"]
    observations = value["observations"]
    contradiction = value["contradiction_search"]
    coverage = value["coverage"]
    invariant = value.get("invariant")
    assurance_requirement = value.get("assurance_requirement") or {
        "required_capabilities": []
    }

    supporting = [item for item in observations if item["stance"] == "support"]
    contradicting = [
        item for item in observations if item["stance"] == "contradict"
    ]
    assurance_lattice = _assurance_lattice(
        assurance_requirement["required_capabilities"],
        supporting,
        contradicting,
    )
    blockers: list[str] = []
    caveats: list[str] = []

    if index_state["coherent"] is not True:
        blockers.append("cross_engine_index_incoherent")
    if index_state["freshness"] != "current":
        blockers.append(f"index_{index_state['freshness']}")

    invariant_failed = bool(
        invariant
        and (
            invariant["status"] == "fail"
            or invariant.get("violations", 0) > 0
        )
    )
    invariant_unresolved = bool(
        invariant
        and (
            invariant["status"] == "unresolved"
            or invariant.get("unresolved", 0) > 0
        )
    )

    if blockers:
        verdict = "blocked"
    elif contradicting or invariant_failed:
        if (
            assurance_lattice["required_capabilities"]
            and assurance_lattice["satisfied_by"] != "contradiction"
        ):
            caveats.append("required_assurance_not_satisfied")
            verdict = "unresolved"
        else:
            verdict = "contradicted"
    else:
        if not contradiction["performed"]:
            caveats.append("contradiction_search_not_performed")
        if coverage["state"] != "complete":
            caveats.append(f"coverage_{coverage['state']}")
        if coverage["unresolved"] > 0:
            caveats.append("coverage_has_unresolved_subjects")
        if invariant_unresolved:
            caveats.append("invariant_unresolved")
        if not supporting:
            caveats.append("no_supporting_evidence")
        elif not any(
            item["confidence_band"] in TRUSTED_SUPPORT_BANDS
            or _runtime_confirmed(item)
            for item in supporting
        ):
            caveats.append("supporting_evidence_not_trustworthy")
        if (
            assurance_lattice["required_capabilities"]
            and assurance_lattice["satisfied_by"] != "support"
        ):
            caveats.append("required_assurance_not_satisfied")
        verdict = "unresolved" if caveats else "verified"

    if verdict not in VERDICTS:  # defensive guard for future edits
        raise AssertionError(f"unexpected verdict: {verdict}")

    supporting_ids = sorted(item["id"] for item in supporting)
    contradicting_ids = sorted(item["id"] for item in contradicting)
    proof_payload = {
        "schema_version": SCHEMA_VERSION,
        "claim_id": claim["id"],
        "index_generation": index_state["index_generation"],
        "verdict": verdict,
        "supporting_observation_ids": supporting_ids,
        "contradicting_observation_ids": contradicting_ids,
        "blockers": sorted(blockers),
        "caveats": sorted(caveats),
    }
    if assurance_lattice["required_capabilities"]:
        proof_payload["assurance_lattice"] = assurance_lattice
    return {
        "proof_id": _stable_id("proof", proof_payload),
        **proof_payload,
        "confidence": _confidence(verdict, supporting, contradicting),
        "coverage": coverage,
        "contradiction_search": contradiction,
        "invariant": invariant,
        "relationship_evidence": _relationship_summary(observations),
        "assurance_lattice": assurance_lattice,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    try:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = evaluate(bundle)
    except (OSError, json.JSONDecodeError, ProofInputError) as exc:
        rendered = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "error": {
                    "code": "invalid_proof_bundle",
                    "message": str(exc),
                },
            },
            indent=2,
            sort_keys=True,
        ) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 2

    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
