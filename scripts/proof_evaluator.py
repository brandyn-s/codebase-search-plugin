#!/usr/bin/env python3
"""Deterministically evaluate evidence-backed code-intelligence claims.

The evaluator does not inspect source code or call a model. It consumes the
canonical claim/observation records produced by the retrieval engines and
applies fail-closed proof rules. A claim can be ``verified`` only when the
indexes are coherent and current, supporting evidence exists, coverage is
complete, and an explicit contradiction search was performed.
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
CONFIDENCE_BANDS = {"high", "medium", "low", "unknown"}


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


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProofInputError(f"{field} must be a non-negative integer")
    return value


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
    if evidence.get("symbol_ref") is not None:
        payload["symbol_ref"] = _validate_symbol_ref(
            evidence.get("symbol_ref"), f"{field}.symbol_ref"
        )
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

    return {
        "schema_version": SCHEMA_VERSION,
        "claim": claim,
        "index_state": normalized_index_state,
        "observations": validated_observations,
        "contradiction_search": normalized_contradiction,
        "coverage": normalized_coverage,
        "invariant": normalized_invariant,
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
        return {
            "band": "high" if contradicting else "medium",
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
    independent = len(engines) >= 2 or len(derivations) >= 2
    if independent:
        return {
            "band": "high",
            "rationale": [
                "complete proof with an explicit contradiction pass",
                "support is corroborated by independent engines or derivations",
            ],
        }
    return {
        "band": "medium",
        "rationale": [
            "complete proof with an explicit contradiction pass",
            "support comes from one engine and derivation",
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

    supporting = [item for item in observations if item["stance"] == "support"]
    contradicting = [
        item for item in observations if item["stance"] == "contradict"
    ]
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
    return {
        "proof_id": _stable_id("proof", proof_payload),
        **proof_payload,
        "confidence": _confidence(verdict, supporting, contradicting),
        "coverage": coverage,
        "contradiction_search": contradiction,
        "invariant": invariant,
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
