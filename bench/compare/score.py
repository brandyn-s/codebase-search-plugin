#!/usr/bin/env python3
"""Score all expected comparison units intent-to-treat and finalize the bundle."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
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

from bench.compare.provenance import (  # noqa: E402
    AUTHORITATIVE_CONSUMER,
    ProvenanceError,
    RunBundle,
)
from bench.compare.fixture_executor import (  # noqa: E402
    FixtureExecutorError,
    execute_unit,
)
from bench.compare.schema import (  # noqa: E402
    ARM_CONTRACTS,
    ContractError,
    MEASURED_OUTCOME_ERROR_CLASSES,
    canonical_json,
    component_identity_sha256,
    fixture_canary_challenge,
    harness_source_identity,
    require_reproducible_harness_source,
    scoring_policy_descriptor,
    sha256_bytes,
    validate_observation,
    validate_unit_binding,
)
from bench.compare.token_accounting import count_tokens  # noqa: E402


class ScoreError(ValueError):
    """The run cannot be scored without weakening intent-to-treat."""


FROZEN_FIXTURE_THRESHOLDS = {
    "schema_version": 1,
    "fixture_only": True,
    "required_arms": list(ARM_CONTRACTS),
    "min_file_acc_at_10": 1.0,
    "max_failure_rate": 0.0,
    "max_unauthorized_side_effects": 0,
}


def _source_pin_case_digests(cases_descriptor: dict) -> dict[str, str]:
    source_pin = cases_descriptor.get("source_pin")
    claimed_sha256 = cases_descriptor.get("sha256")
    if (
        not isinstance(source_pin, dict)
        or set(source_pin) != {"encoding", "bytes"}
        or source_pin.get("encoding") != "hex"
        or not isinstance(source_pin.get("bytes"), str)
        or not isinstance(claimed_sha256, str)
        or len(claimed_sha256) != 64
    ):
        raise ScoreError("manifest source pin descriptor is invalid")
    encoded = source_pin["bytes"]
    try:
        source_bytes = bytes.fromhex(encoded)
    except ValueError as exc:
        raise ScoreError("manifest source pin encoding is invalid") from exc
    if source_bytes.hex() != encoded:
        raise ScoreError("manifest source pin encoding is not canonical")
    if hashlib.sha256(source_bytes).hexdigest() != claimed_sha256:
        raise ScoreError("manifest source pin byte digest mismatch")
    try:
        pin = json.loads(source_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ScoreError("manifest source pin is not valid JSON") from exc
    if (
        not isinstance(pin, dict)
        or pin.get("schema_version") != 1
        or pin.get("pin_id") != cases_descriptor.get("pin_id")
    ):
        raise ScoreError("manifest source pin identity mismatch")
    source_cases = pin.get("cases")
    if (
        not isinstance(source_cases, list)
        or not source_cases
        or len(source_cases) != cases_descriptor.get("count")
    ):
        raise ScoreError("manifest source pin case inventory mismatch")
    dataset = pin.get("dataset")
    if (
        not isinstance(dataset, dict)
        or dataset.get("instrument_canary")
        != cases_descriptor.get("instrument_canary")
        or dataset.get("instrument_fixture")
        != cases_descriptor.get("instrument_fixture")
    ):
        raise ScoreError("manifest source pin dataset descriptor mismatch")
    derived: dict[str, str] = {}
    for case in source_cases:
        case_id = case.get("case_id") if isinstance(case, dict) else None
        if not isinstance(case_id, str) or not case_id or case_id in derived:
            raise ScoreError("manifest source pin contains invalid case identities")
        derived[case_id] = hashlib.sha256(canonical_json(case)).hexdigest()
    if cases_descriptor.get("case_sha256_by_id") != derived:
        raise ScoreError("manifest case digest map differs from the source pin")
    return derived


def _validated_case_records(
    records: dict[str, dict],
    expected_sha256_by_id: dict[str, str],
    *,
    source_pin_sha256: str,
) -> dict[str, dict]:
    if (
        not isinstance(source_pin_sha256, str)
        or len(source_pin_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in source_pin_sha256
        )
    ):
        raise ScoreError("manifest source pin SHA-256 is invalid")
    if set(records) != set(expected_sha256_by_id):
        raise ScoreError("case ledger does not match the pinned case set")
    cases: dict[str, dict] = {}
    for case_id, record in records.items():
        if not isinstance(record, dict) or set(record) != {
            "stable_key",
            "case_id",
            "source_pin_sha256",
            "case",
            "case_sha256",
            "record_sha256",
        }:
            raise ScoreError(f"{case_id}: malformed pinned case record")
        case = record.get("case")
        digest = (
            hashlib.sha256(canonical_json(case)).hexdigest()
            if isinstance(case, dict)
            else None
        )
        if (
            record.get("stable_key") != case_id
            or record.get("case_id") != case_id
            or not isinstance(case, dict)
            or case.get("case_id") != case_id
            or record.get("case_sha256") != digest
            or digest != expected_sha256_by_id[case_id]
            or record.get("source_pin_sha256") != source_pin_sha256
        ):
            raise ScoreError(f"{case_id}: pinned case content digest mismatch")
        cases[case_id] = case
    return cases


def load_object(path: Path) -> dict:
    if path.is_symlink():
        raise ScoreError(f"refusing symlink input: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoreError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ScoreError(f"{path}: expected an object")
    return value


def exact_decimal(value: object, context: str) -> Decimal:
    if not isinstance(value, str):
        raise ScoreError(f"{context}: cost must be an exact decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ScoreError(f"{context}: invalid decimal cost") from exc
    if not result.is_finite() or result < 0:
        raise ScoreError(f"{context}: cost must be finite and non-negative")
    return result


def percentile(values: list[int], percent: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percent * len(ordered) / 100) - 1)
    return ordered[index]


def _rank_for_files(observation: dict | None, expected: set[str]) -> int | None:
    if observation is None:
        return None
    for entity in observation["ranked_entities"][:10]:
        if entity["file"] in expected:
            return entity["rank"]
    return None


def _symbol_hit(
    observation: dict | None,
    expected_files: set[str],
    expected_symbols: set[str],
) -> bool | None:
    if not expected_symbols:
        return None
    if observation is None:
        return False
    return any(
        entity["file"] in expected_files
        and entity.get("symbol") in expected_symbols
        for entity in observation["ranked_entities"][:10]
    )


def _metric_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _bootstrap_delta(
    case_deltas: dict[str, float],
    *,
    resamples: int,
    seed: int,
) -> list[float]:
    case_ids = sorted(case_deltas)
    if not case_ids:
        raise ScoreError("primary contrast has no paired cases")
    samples: list[float] = []
    for resample in range(resamples):
        drawn: list[float] = []
        for draw in range(len(case_ids)):
            counter = f"{seed}:{resample}:{draw}".encode("ascii")
            index = int.from_bytes(hashlib.sha256(counter).digest(), "big") % len(
                case_ids
            )
            drawn.append(case_deltas[case_ids[index]])
        samples.append(_metric_mean(drawn))
    return sorted(samples)


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        raise ScoreError("cannot take quantile of empty values")
    index = min(len(values) - 1, max(0, round((len(values) - 1) * fraction)))
    return values[index]


def _exact_mcnemar_p(discordant_a: int, discordant_b: int) -> float:
    count = discordant_a + discordant_b
    if count == 0:
        return 1.0
    tail = sum(
        math.comb(count, index) for index in range(min(discordant_a, discordant_b) + 1)
    ) / (2**count)
    return min(1.0, 2 * tail)


def _all_replicates_success(
    pairs_by_case: dict[str, list[tuple[float, float]]],
) -> dict[str, tuple[float, float]]:
    aggregated: dict[str, tuple[float, float]] = {}
    for case_id, pairs in pairs_by_case.items():
        if not pairs:
            raise ScoreError(f"{case_id}: paired case cluster is empty")
        if any(value not in {0.0, 1.0} for pair in pairs for value in pair):
            raise ScoreError(f"{case_id}: paired case outcomes must be binary")
        aggregated[case_id] = (
            float(all(pair[0] == 1.0 for pair in pairs)),
            float(all(pair[1] == 1.0 for pair in pairs)),
        )
    return aggregated


def _case_clustered_mcnemar_counts(
    pairs_by_case: dict[str, list[tuple[float, float]]],
) -> tuple[int, int]:
    treatment_only = 0
    baseline_only = 0
    for treatment, baseline in _all_replicates_success(pairs_by_case).values():
        if treatment and not baseline:
            treatment_only += 1
        elif baseline and not treatment:
            baseline_only += 1
    return treatment_only, baseline_only


def _holm_adjust(p_values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(p_values, key=lambda name: (p_values[name], name))
    adjusted: dict[str, float] = {}
    running = 0.0
    total = len(ordered)
    for index, name in enumerate(ordered):
        running = max(running, min(1.0, p_values[name] * (total - index)))
        adjusted[name] = running
    return adjusted


def _validate_error(
    error: dict,
    *,
    expected_identity: str,
    expected_control: str,
    expected_unit: dict,
    expected_arm_contract_sha256: str,
) -> None:
    try:
        validate_unit_binding(
            error,
            unit_key=expected_unit["unit_key"],
            case_id=expected_unit["case_id"],
            arm=expected_unit["arm"],
            replicate=expected_unit["replicate"],
            position=expected_unit["position"],
            arm_contract_sha256=expected_arm_contract_sha256,
        )
    except ContractError as exc:
        raise ScoreError(str(exc)) from exc
    if (
        error.get("status") != "error"
        or error.get("component_identity_sha256") != expected_identity
        or error.get("control_sha256") != expected_control
        or not isinstance(error.get("error_class"), str)
        or error["error_class"] not in MEASURED_OUTCOME_ERROR_CLASSES
        or error.get("side_effects")
        != {"writes": 0, "network_attempts": 0, "secret_egress": 0}
    ):
        raise ScoreError(f"{error.get('stable_key')}: malformed error outcome")
    exact_decimal(error.get("cost_usd"), error["stable_key"])
    for field in (
        "latency_ms",
        "requested_k",
        "candidate_count",
        "effective_k",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cache_tokens",
        "tool_result_tokens",
        "evidence_tokens",
        "evidence_bytes",
        "context_tokens",
        "egress_bytes",
    ):
        value = error.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ScoreError(f"{error['stable_key']}: invalid {field}")
    if (
        error["requested_k"] != 10
        or error["effective_k"] > error["requested_k"]
        or error["candidate_count"] < error["effective_k"]
        or not isinstance(error.get("truncated"), bool)
        or error["truncated"]
        != (error["candidate_count"] > error["effective_k"])
        or error["tool_calls"] > 20
        or error["evidence_tokens"] > 64_000
        or error["context_tokens"] > 128_000
    ):
        raise ScoreError(f"{error['stable_key']}: invalid failed-unit depth metadata")


def _safe_fixture_relative(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScoreError(f"{context} path is invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ScoreError(f"{context} path is unsafe")
    return path.as_posix()


def _validate_fixture_execution(
    record: dict,
    policy: dict,
    *,
    case: dict,
    arm: str,
    fixture: object,
    canary: object,
    root: Path,
) -> None:
    execution = record.get("fixture_execution")
    required_policy = {
        "mode",
        "executor_sha256",
        "fault_plan_sha256",
        "host_observer",
    }
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
    if (
        not isinstance(policy, dict)
        or set(policy) != required_policy
        or policy.get("mode") != "deterministic_instrumented_fixture_v1"
        or policy.get("host_observer")
        != "loopback_listener_and_tree_snapshot_v1"
        or not isinstance(fixture, dict)
        or set(fixture) != required_fixture
        or not isinstance(canary, dict)
        or set(canary) != required_canary
    ):
        raise ScoreError(
            f"{record.get('stable_key')}: fixture execution evidence is invalid"
        )
    for value in (
        policy.get("executor_sha256"),
        policy.get("fault_plan_sha256"),
        fixture.get("manifest_sha256"),
        fixture.get("revision"),
        canary.get("sha256"),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ScoreError(
                f"{record.get('stable_key')}: fixture execution identity is invalid"
            )
    manifest_relative = _safe_fixture_relative(
        fixture["manifest_path"],
        "instrument manifest",
    )
    source_relative = _safe_fixture_relative(
        fixture["source_root"],
        "instrument source",
    )
    canary_relative_from_root = _safe_fixture_relative(
        canary["path"],
        "instrument canary",
    )
    manifest_path = root / manifest_relative
    source_root = root / source_relative
    canary_path = root / canary_relative_from_root
    try:
        manifest_encoded = manifest_path.read_bytes()
        canary_encoded = canary_path.read_bytes()
        manifest = json.loads(manifest_encoded)
        canary_relative = canary_path.resolve().relative_to(
            source_root.resolve()
        ).as_posix()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ScoreError(
            f"{record.get('stable_key')}: cannot verify fixture source"
        ) from exc
    if (
        manifest_path.is_symlink()
        or source_root.is_symlink()
        or not source_root.is_dir()
        or canary_path.is_symlink()
        or hashlib.sha256(manifest_encoded).hexdigest()
        != fixture["manifest_sha256"]
        or hashlib.sha256(canary_encoded).hexdigest() != canary["sha256"]
        or not isinstance(manifest, dict)
        or manifest.get("repository") != fixture["repository"]
        or manifest.get("revision") != fixture["revision"]
    ):
        raise ScoreError(
            f"{record.get('stable_key')}: fixture source identity mismatch"
        )
    challenge = fixture_canary_challenge(
        arm=arm,
        query=case["query"],
        repository_revision=fixture["revision"],
        canary_sha256=canary["sha256"],
    )
    try:
        expected = execute_unit(
            arm=arm,
            query=case["query"],
            source_root=source_root,
            manifest=manifest,
            canary_relative=canary_relative,
            canary_challenge=challenge,
        )
    except FixtureExecutorError as exc:
        raise ScoreError(
            f"{record.get('stable_key')}: fixture replay failed"
        ) from exc
    expected_execution = {
        "executor_sha256": policy["executor_sha256"],
        "trace_sha256": hashlib.sha256(
            canonical_json(expected["trace"])
        ).hexdigest(),
        "files_read_sha256": hashlib.sha256(
            canonical_json(expected["files_read"])
        ).hexdigest(),
        "evidence_files_sha256": hashlib.sha256(
            canonical_json(expected["evidence_files"])
        ).hexdigest(),
        "canary_challenge_sha256": challenge,
        "canary_read_proof_sha256": expected["canary_read_proof"],
        "host_observer": "loopback_listener_and_tree_snapshot_v1",
    }
    expected_raw = (
        json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    evidence = "".join(
        f"===== {relative} =====\n"
        + (source_root / relative).read_text(encoding="utf-8")
        for relative in expected["evidence_files"]
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
    expected_fields = {
        "candidate_count": expected["candidate_count"],
        "effective_k": expected["effective_k"],
        "truncated": expected["truncated"],
        "tool_calls": len(expected["trace"]),
        "input_tokens": context_tokens,
        "output_tokens": count_tokens(
            canonical_json(expected["ranked_entities"]).decode("utf-8")
        ),
        "cache_tokens": 0,
        "tool_result_tokens": evidence_tokens if arm != "corpus" else 0,
        "evidence_tokens": evidence_tokens,
        "evidence_bytes": len(evidence.encode("utf-8")),
        "context_tokens": context_tokens,
        "egress_bytes": 0,
        "cost_usd": "0.000000",
    }
    expected_corpus = (
        {
            "pack_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
            "construction": "query_conditioned_pack",
            "candidate_blocks": len(manifest["files"]),
            "effective_k": len(expected["evidence_files"]),
            "truncated": len(manifest["files"]) > len(expected["evidence_files"]),
            "posthoc_target_in_pack": bool(
                set(case["oracle"]["files"]) & set(expected["evidence_files"])
            ),
        }
        if arm == "corpus"
        else None
    )
    if (
        execution != expected_execution
        or record.get("raw_response_sha256")
        != hashlib.sha256(expected_raw).hexdigest()
        or any(record.get(field) != value for field, value in expected_fields.items())
        or record.get("corpus_pack") != expected_corpus
        or (
            record.get("status") == "ok"
            and record.get("ranked_entities") != expected["ranked_entities"]
        )
    ):
        raise ScoreError(
            f"{record.get('stable_key')}: fixture execution evidence mismatch"
        )


def score_bundle(
    bundle: RunBundle,
    *,
    bootstrap: int,
    seed: int,
    primary_contrasts: tuple[str, ...],
    thresholds: dict | None,
) -> tuple[dict, bool]:
    bundle.require_exact_coverage()
    manifest_core = bundle.manifest.get("manifest_core")
    if not isinstance(manifest_core, dict):
        raise ScoreError("manifest has no core")
    if manifest_core.get("authoritative_consumer") != AUTHORITATIVE_CONSUMER:
        raise ScoreError("manifest has no recognized authoritative consumer")
    source_identity = manifest_core.get("harness_source")
    try:
        current_source_identity = harness_source_identity(
            Path(__file__).resolve().parents[2]
        )
        require_reproducible_harness_source(current_source_identity)
    except ContractError as exc:
        raise ScoreError(f"cannot verify harness source identity: {exc}") from exc
    if (
        not isinstance(source_identity, dict)
        or source_identity != current_source_identity
        or manifest_core.get("harness_source_sha256")
        != hashlib.sha256(canonical_json(source_identity)).hexdigest()
    ):
        raise ScoreError("manifest harness source identity differs from the scorer")
    identity = manifest_core.get("component_identity")
    expected_identity = manifest_core.get("component_identity_sha256")
    if (
        not isinstance(identity, dict)
        or expected_identity != component_identity_sha256(identity)
    ):
        raise ScoreError("manifest component identity digest mismatch")
    control_by_case = manifest_core.get("control_sha256_by_case")
    if not isinstance(control_by_case, dict):
        raise ScoreError("manifest has no case control identities")
    expected_arm_contracts = {
        arm: contract.descriptor() for arm, contract in ARM_CONTRACTS.items()
    }
    if manifest_core.get("arm_contracts") != expected_arm_contracts:
        raise ScoreError("manifest arm contracts differ from the frozen scorer")
    policy = scoring_policy_descriptor()
    if manifest_core.get("scoring_policy") != policy:
        raise ScoreError("manifest scoring policy differs from the frozen scorer")
    if (
        bootstrap != policy["bootstrap_resamples"]
        or seed != policy["bootstrap_seed"]
        or list(primary_contrasts) != policy["primary_contrasts"]
    ):
        raise ScoreError("requested statistics differ from the frozen scoring policy")

    cases_descriptor = manifest_core.get("cases")
    if not isinstance(cases_descriptor, dict):
        raise ScoreError("manifest has no case descriptor")
    case_sha256_by_id = _source_pin_case_digests(cases_descriptor)
    fixture_execution_policy = manifest_core.get("fixture_execution")
    if not isinstance(fixture_execution_policy, dict):
        raise ScoreError("manifest has no fixture execution policy")
    instrumented_fixture = (
        fixture_execution_policy.get("mode")
        == "deterministic_instrumented_fixture_v1"
    )
    fixture_descriptor = cases_descriptor.get("instrument_fixture")
    canary_descriptor = cases_descriptor.get("instrument_canary")
    cases = _validated_case_records(
        bundle.cases._load(),
        case_sha256_by_id,
        source_pin_sha256=cases_descriptor.get("sha256"),
    )
    observations = bundle.observations._load()
    errors = bundle.errors._load()
    setup = bundle.setup._load()
    outcome_by_key: dict[str, dict | None] = {}
    success_at_10: dict[tuple[str, int, str], float] = {}
    rows_by_arm: dict[str, list[dict]] = {arm: [] for arm in ARM_CONTRACTS}
    unauthorized_side_effects = 0

    execution_order = manifest_core.get("execution_order")
    if not isinstance(execution_order, list):
        raise ScoreError("manifest has no execution order")
    expected_unit_keys = set(bundle.manifest["expected_unit_keys"])
    order_keys = {
        unit.get("unit_key") for unit in execution_order if isinstance(unit, dict)
    }
    if order_keys != expected_unit_keys:
        raise ScoreError("execution order does not match expected unit keys")
    if len(execution_order) != len(expected_unit_keys):
        raise ScoreError("execution order contains duplicate unit keys")

    for unit in execution_order:
        unit_key = unit["unit_key"]
        case_id = unit["case_id"]
        arm = unit["arm"]
        replicate = unit["replicate"]
        case = cases.get(case_id)
        if case is None or arm not in ARM_CONTRACTS:
            raise ScoreError(f"{unit_key}: missing case or unknown arm")
        expected_control = control_by_case.get(case_id)
        expected_arm_contract_sha256 = sha256_bytes(
            canonical_json(ARM_CONTRACTS[arm].descriptor())
        )
        if unit_key in observations:
            observation = observations[unit_key]
            if instrumented_fixture:
                _validate_fixture_execution(
                    observation,
                    fixture_execution_policy,
                    case=case,
                    arm=arm,
                    fixture=fixture_descriptor,
                    canary=canary_descriptor,
                    root=Path(__file__).resolve().parents[2],
                )
            try:
                validate_unit_binding(
                    observation,
                    unit_key=unit_key,
                    case_id=case_id,
                    arm=arm,
                    replicate=replicate,
                    position=unit["position"],
                    arm_contract_sha256=expected_arm_contract_sha256,
                )
                validate_observation(
                    observation,
                    expected_control_sha256=expected_control,
                    expected_component_identity_sha256=expected_identity,
                )
            except ContractError as exc:
                raise ScoreError(f"{unit_key}: {exc}") from exc
            side_effects = observation.get("side_effects")
            if side_effects != {
                "writes": 0,
                "network_attempts": 0,
                "secret_egress": 0,
            }:
                raise ScoreError(f"{unit_key}: unauthorized side effect")
            unauthorized_side_effects += sum(side_effects.values())
            outcome_by_key[unit_key] = observation
            failed = False
            cost = exact_decimal(observation.get("cost_usd"), unit_key)
            latency = observation["latency_ms"]
            tokens = {
                name: observation[name]
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "cache_tokens",
                    "tool_result_tokens",
                )
            }
            evidence_bytes = observation["evidence_bytes"]
            evidence_tokens = observation["evidence_tokens"]
            context_tokens = observation["context_tokens"]
            egress_bytes = observation["egress_bytes"]
        else:
            error = errors[unit_key]
            if instrumented_fixture:
                _validate_fixture_execution(
                    error,
                    fixture_execution_policy,
                    case=case,
                    arm=arm,
                    fixture=fixture_descriptor,
                    canary=canary_descriptor,
                    root=Path(__file__).resolve().parents[2],
                )
            _validate_error(
                error,
                expected_identity=expected_identity,
                expected_control=expected_control,
                expected_unit=unit,
                expected_arm_contract_sha256=expected_arm_contract_sha256,
            )
            outcome_by_key[unit_key] = None
            failed = True
            cost = exact_decimal(error["cost_usd"], unit_key)
            latency = error["latency_ms"]
            tokens = {
                name: error[name]
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "cache_tokens",
                    "tool_result_tokens",
                )
            }
            evidence_bytes = error["evidence_bytes"]
            evidence_tokens = error["evidence_tokens"]
            context_tokens = error["context_tokens"]
            egress_bytes = error["egress_bytes"]
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in tokens.values()
        ):
            raise ScoreError(f"{unit_key}: invalid token accounting")
        oracle = case.get("oracle")
        assert isinstance(oracle, dict)
        expected_files = set(oracle["files"])
        observation = outcome_by_key[unit_key]
        rank = _rank_for_files(observation, expected_files)
        class_hit = _symbol_hit(
            observation,
            expected_files,
            set(oracle["classes"]),
        )
        function_hit = _symbol_hit(
            observation,
            expected_files,
            set(oracle["functions"]),
        )
        row = {
            "unit_key": unit_key,
            "case_id": case_id,
            "replicate": replicate,
            "failed": failed,
            "file_rank": rank,
            "file_acc_at_1": float(rank == 1),
            "file_acc_at_3": float(rank is not None and rank <= 3),
            "file_acc_at_10": float(rank is not None and rank <= 10),
            "mrr_at_10": 0.0 if rank is None else 1.0 / rank,
            "class_acc_at_10": None if class_hit is None else float(class_hit),
            "function_acc_at_10": (
                None if function_hit is None else float(function_hit)
            ),
            "cost_usd": cost,
            "latency_ms": latency,
            "tokens": tokens,
            "evidence_bytes": evidence_bytes,
            "evidence_tokens": evidence_tokens,
            "context_tokens": context_tokens,
            "egress_bytes": egress_bytes,
        }
        rows_by_arm[arm].append(row)
        success_at_10[(case_id, replicate, arm)] = row["file_acc_at_10"]

    setup_cost: dict[str, Decimal] = {}
    setup_latency: dict[str, int] = {}
    for arm in ARM_CONTRACTS:
        record = setup.get(f"setup|{arm}")
        if (
            not isinstance(record, dict)
            or record.get("arm") != arm
            or record.get("status") != "ok"
            or record.get("component_identity_sha256") != expected_identity
        ):
            raise ScoreError(f"setup|{arm}: malformed or unbound setup record")
        setup_cost[arm] = exact_decimal(record.get("cost_usd"), f"setup|{arm}")
        latency = record.get("latency_ms")
        if isinstance(latency, bool) or not isinstance(latency, int) or latency < 0:
            raise ScoreError(f"setup|{arm}: invalid setup latency")
        setup_latency[arm] = latency
    canary_descriptor = cases_descriptor.get("instrument_canary")
    host_canary_status = "not_configured"
    if canary_descriptor is not None:
        host_record = setup.get("setup|host-canary")
        if (
            not isinstance(canary_descriptor, dict)
            or not isinstance(host_record, dict)
            or host_record.get("status") != "ok"
            or host_record.get("observation") != "host"
            or host_record.get("injection_canary_sha256")
            != canary_descriptor.get("sha256")
            or host_record.get("write_path_existed_before") is not False
            or host_record.get("write_path_exists_after") is not False
            or host_record.get("secret_egress") != 0
            or host_record.get("network_attempts") != 0
            or host_record.get("egress_bytes") != 0
            or host_record.get("network_observation")
            != "loopback_listener_per_unit_v1"
            or host_record.get("executor_processes") != len(expected_unit_keys)
            or host_record.get("canary_file_reads") != len(expected_unit_keys)
            or host_record.get("secret_environment_excluded") is not True
            or not instrumented_fixture
        ):
            raise ScoreError("setup|host-canary: malformed host observation")
        host_canary_status = "pass"

    arm_summaries: dict[str, dict] = {}
    for arm, rows in rows_by_arm.items():
        if not rows:
            raise ScoreError(f"{arm}: no expected units")
        warm_cost = sum((row["cost_usd"] for row in rows), Decimal("0"))
        mean_warm_cost = warm_cost / len(rows)
        latencies = [row["latency_ms"] for row in rows]
        token_totals = {
            name: sum(row["tokens"][name] for row in rows)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_tokens",
                "tool_result_tokens",
            )
        }
        class_values = [
            row["class_acc_at_10"]
            for row in rows
            if row["class_acc_at_10"] is not None
        ]
        function_values = [
            row["function_acc_at_10"]
            for row in rows
            if row["function_acc_at_10"] is not None
        ]
        amortized: dict[str, dict] = {}
        for query_count in (1, 5, 20, 100):
            total = setup_cost[arm] + mean_warm_cost * query_count
            amortized[str(query_count)] = {
                "total_usd": format(total, "f"),
                "per_query_usd": format(total / query_count, "f"),
            }
        arm_summaries[arm] = {
            "expected_units": len(rows),
            "failures": sum(row["failed"] for row in rows),
            "failure_rate": _metric_mean(
                [float(row["failed"]) for row in rows]
            ),
            "file_acc_at_1": _metric_mean(
                [row["file_acc_at_1"] for row in rows]
            ),
            "file_acc_at_3": _metric_mean(
                [row["file_acc_at_3"] for row in rows]
            ),
            "file_acc_at_10": _metric_mean(
                [row["file_acc_at_10"] for row in rows]
            ),
            "mrr_at_10": _metric_mean([row["mrr_at_10"] for row in rows]),
            "class_acc_at_10": (
                _metric_mean(class_values) if class_values else None
            ),
            "class_denominator": len(class_values),
            "function_acc_at_10": (
                _metric_mean(function_values) if function_values else None
            ),
            "function_denominator": len(function_values),
            "warm_cost_usd": format(warm_cost, "f"),
            "setup_cost_usd": format(setup_cost[arm], "f"),
            "setup_latency_ms": setup_latency[arm],
            "amortized": amortized,
            "latency_ms": {
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
            },
            "tokens": token_totals,
            "evidence_bytes": sum(row["evidence_bytes"] for row in rows),
            "evidence_tokens": sum(row["evidence_tokens"] for row in rows),
            "context_tokens": sum(row["context_tokens"] for row in rows),
            "egress_bytes": sum(row["egress_bytes"] for row in rows),
        }

    raw_p_values: dict[str, float] = {}
    contrasts: dict[str, dict] = {}
    for contrast in primary_contrasts:
        try:
            treatment, baseline = contrast.split("-", 1)
        except ValueError as exc:
            raise ScoreError(f"invalid primary contrast {contrast!r}") from exc
        if treatment not in ARM_CONTRACTS or baseline not in ARM_CONTRACTS:
            raise ScoreError(f"unknown arm in primary contrast {contrast}")
        paired_outcomes: dict[str, list[tuple[float, float]]] = {}
        for case_id in sorted(cases):
            replicates = sorted(
                unit["replicate"]
                for unit in execution_order
                if unit["case_id"] == case_id and unit["arm"] == treatment
            )
            for replicate in replicates:
                treatment_value = success_at_10[(case_id, replicate, treatment)]
                baseline_value = success_at_10[(case_id, replicate, baseline)]
                paired_outcomes.setdefault(case_id, []).append(
                    (treatment_value, baseline_value)
                )
        aggregated = _all_replicates_success(paired_outcomes)
        case_deltas = {
            case_id: treatment_value - baseline_value
            for case_id, (treatment_value, baseline_value) in aggregated.items()
        }
        (
            discordant_treatment,
            discordant_baseline,
        ) = _case_clustered_mcnemar_counts(paired_outcomes)
        samples = _bootstrap_delta(
            case_deltas,
            resamples=bootstrap,
            seed=seed,
        )
        delta = _metric_mean(list(case_deltas.values()))
        p_value = _exact_mcnemar_p(
            discordant_treatment,
            discordant_baseline,
        )
        raw_p_values[contrast] = p_value
        contrasts[contrast] = {
            "endpoint": "file_acc_at_10",
            "delta": delta,
            "paired_cases": len(case_deltas),
            "bootstrap_resamples": bootstrap,
            "bootstrap_seed": seed,
            "bootstrap_stream": "sha256_counter_modulo_v1",
            "repeat_aggregation": "all_replicates_success_v1",
            "ci95": [
                _quantile(samples, 0.025),
                _quantile(samples, 0.975),
            ],
            "mcnemar": {
                "unit": "case",
                "repeat_aggregation": "all_replicates_success_v1",
                "treatment_only": discordant_treatment,
                "baseline_only": discordant_baseline,
                "exact_p": p_value,
            },
        }
    adjusted = _holm_adjust(raw_p_values)
    for contrast, value in adjusted.items():
        contrasts[contrast]["holm_adjusted_p"] = value

    mode = manifest_core.get("mode")
    if mode not in {"fixture", "live"}:
        raise ScoreError("manifest mode must be fixture or live")
    failures: list[str] = []
    thresholds_sha256: str | None = None
    if mode == "fixture":
        if (
            thresholds is None
            or canonical_json(thresholds)
            != canonical_json(FROZEN_FIXTURE_THRESHOLDS)
        ):
            raise ScoreError("fixture thresholds are required and must not drift")
        for arm in ARM_CONTRACTS:
            if (
                arm_summaries[arm]["file_acc_at_10"]
                < thresholds["min_file_acc_at_10"]
            ):
                failures.append(f"{arm}:file_acc_at_10")
            if arm_summaries[arm]["failure_rate"] > thresholds["max_failure_rate"]:
                failures.append(f"{arm}:failure_rate")
        if unauthorized_side_effects > thresholds["max_unauthorized_side_effects"]:
            failures.append("unauthorized_side_effects")
        thresholds_sha256 = hashlib.sha256(canonical_json(thresholds)).hexdigest()
    elif thresholds is not None:
        raise ScoreError("fixture thresholds must not grade a live run")

    summary = {
        "schema_version": 1,
        "producer": "bench/compare/score.py:v1",
        "run_id": bundle.manifest["run_id"],
        "intent_to_treat": True,
        "expected_units": len(expected_unit_keys),
        "accounted_units": len(observations) + len(errors),
        "scoring": policy,
        "arms": arm_summaries,
        "primary_contrasts": contrasts,
        "fixture_contract": {
            "status": (
                "not_applicable"
                if mode == "live"
                else ("fail" if failures else "pass")
            ),
            "failures": failures,
            "thresholds_sha256": thresholds_sha256,
        },
        "privacy": {
            "unauthorized_side_effects": unauthorized_side_effects,
            "raw_responses_in_public_bundle": False,
            "host_injection_canary": host_canary_status,
        },
    }
    return summary, not failures


def verify_finalized_bundle_semantics(
    bundle: RunBundle,
    manifest: dict,
    expected_summary: dict,
) -> None:
    """Replay the authoritative scorer without opening or finalizing a bundle."""
    if canonical_json(manifest) != canonical_json(bundle.manifest):
        raise ScoreError("authoritative verifier received a different manifest")
    manifest_core = manifest.get("manifest_core")
    if not isinstance(manifest_core, dict):
        raise ScoreError("manifest has no core")
    thresholds = (
        FROZEN_FIXTURE_THRESHOLDS
        if manifest_core.get("mode") == "fixture"
        else None
    )
    actual_summary, _passed = score_bundle(
        bundle,
        bootstrap=10_000,
        seed=42,
        primary_contrasts=("composed-corpus", "composed-native"),
        thresholds=thresholds,
    )
    if canonical_json(actual_summary) != canonical_json(expected_summary):
        raise ScoreError(
            "authoritative semantic summary differs from finalized content"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--intent-to-treat", action="store_true")
    parser.add_argument("--bootstrap", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--holm-primary",
        default="composed-corpus,composed-native",
    )
    parser.add_argument(
        "--thresholds",
        type=Path,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        if not arguments.intent_to_treat:
            raise ScoreError("--intent-to-treat is mandatory")
        policy = scoring_policy_descriptor()
        if (
            arguments.bootstrap != policy["bootstrap_resamples"]
            or arguments.seed != policy["bootstrap_seed"]
        ):
            raise ScoreError(
                "statistics are frozen at 10000 bootstrap resamples and seed 42"
            )
        primary = tuple(arguments.holm_primary.split(","))
        if primary != ("composed-corpus", "composed-native"):
            raise ScoreError(
                "primary contrasts must remain composed-corpus,composed-native"
            )
        bundle = RunBundle.open_existing(arguments.run_dir)
        thresholds = (
            load_object(arguments.thresholds)
            if arguments.thresholds is not None
            else None
        )
        summary, passed = score_bundle(
            bundle,
            bootstrap=arguments.bootstrap,
            seed=arguments.seed,
            primary_contrasts=primary,
            thresholds=thresholds,
        )
        bundle.finalize(summary)
        print(json.dumps(summary, sort_keys=True))
        return 0 if passed else 1
    except (ScoreError, ProvenanceError, ContractError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
