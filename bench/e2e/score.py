#!/usr/bin/env python3
"""Score recorded code-search/code-graph routing traces deterministically."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import sys

try:
    from live_provenance import ProvenanceError, validate_live_provenance
except ModuleNotFoundError:
    provenance_path = Path(__file__).with_name("live_provenance.py")
    provenance_spec = importlib.util.spec_from_file_location(
        "codebase_search_live_provenance",
        provenance_path,
    )
    if provenance_spec is None or provenance_spec.loader is None:
        raise
    provenance_module = importlib.util.module_from_spec(provenance_spec)
    provenance_spec.loader.exec_module(provenance_module)
    ProvenanceError = provenance_module.ProvenanceError
    validate_live_provenance = provenance_module.validate_live_provenance


SEMANTIC_TOOLS = {
    "mcp__code-search__search_code",
    "mcp__code-search__find_similar_code",
    "mcp__code-search__code_localize",
}
GRAPH_TOOLS = {
    "mcp__code-graph__search_graph",
    "mcp__code-graph__query_graph",
    "mcp__code-graph__trace_call_path",
    "mcp__code-graph__get_code_snippet",
    "mcp__code-graph__get_architecture",
    "mcp__code-graph__detect_changes",
}
SECURITY_TOOLS = {
    "mcp__code-graph__query_security_surfaces",
    "mcp__code-graph__trace_data_flow",
    "mcp__code-graph__query_stig_evidence",
}
LEXICAL_TOOLS = {
    "mcp__code-graph__search_code",
}
RETRIEVAL_TOOLS = SEMANTIC_TOOLS | GRAPH_TOOLS | SECURITY_TOOLS | LEXICAL_TOOLS


class BenchmarkError(ValueError):
    """The case or recorded-run file violates the benchmark schema."""


def load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BenchmarkError(f"{path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise BenchmarkError(f"{path}:{line_number}: record must be an object")
        records.append(record)
    if not records:
        raise BenchmarkError(f"{path}: no records")
    return records


def require_keys(record: dict, keys: tuple[str, ...], context: str) -> None:
    missing = [key for key in keys if key not in record]
    if missing:
        raise BenchmarkError(f"{context}: missing {', '.join(missing)}")
    if record.get("schema_version") != 1:
        raise BenchmarkError(f"{context}: schema_version must be 1")


def normalize_assertion(text: str) -> str:
    """Canonicalize adjudicated assertion whitespace, not answer semantics."""
    return " ".join(text.split())


def load_cases(path: Path) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for record in load_jsonl(path):
        require_keys(
            record,
            (
                "schema_version",
                "case_id",
                "category",
                "query",
                "expected_route",
                "expected_evidence",
                "expected_claims",
                "expected_index_error",
            ),
            str(path),
        )
        case_id = record["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in cases:
            raise BenchmarkError(f"{path}: duplicate/invalid case_id {case_id!r}")
        if not isinstance(record["expected_evidence"], list):
            raise BenchmarkError(f"{path}:{case_id}: expected_evidence must be a list")
        expected_evidence = record["expected_evidence"]
        if (
            not expected_evidence
            or any(
                not isinstance(evidence_id, str) or not evidence_id
                for evidence_id in expected_evidence
            )
            or len(set(expected_evidence)) != len(expected_evidence)
        ):
            raise BenchmarkError(
                f"{path}:{case_id}: expected_evidence must contain unique IDs"
            )
        expected_claims = record["expected_claims"]
        if not isinstance(expected_claims, list) or not expected_claims:
            raise BenchmarkError(
                f"{path}:{case_id}: expected_claims must be a non-empty list"
            )
        claim_ids: set[str] = set()
        for claim in expected_claims:
            if not isinstance(claim, dict):
                raise BenchmarkError(
                    f"{path}:{case_id}: expected claim must be an object"
                )
            missing_claim_keys = {
                "claim_id",
                "text",
                "required_evidence_ids",
            } - set(claim)
            if missing_claim_keys:
                raise BenchmarkError(
                    f"{path}:{case_id}: expected claim missing "
                    + ", ".join(sorted(missing_claim_keys))
                )
            claim_id = claim["claim_id"]
            text = claim["text"]
            required_evidence = claim["required_evidence_ids"]
            if (
                not isinstance(claim_id, str)
                or not claim_id
                or claim_id in claim_ids
            ):
                raise BenchmarkError(
                    f"{path}:{case_id}: duplicate/invalid expected claim_id "
                    f"{claim_id!r}"
                )
            claim_ids.add(claim_id)
            if (
                not isinstance(text, str)
                or not text
                or text != normalize_assertion(text)
            ):
                raise BenchmarkError(
                    f"{path}:{case_id}:{claim_id}: text must be a normalized "
                    "canonical assertion"
                )
            if (
                not isinstance(required_evidence, list)
                or not required_evidence
                or any(
                    not isinstance(evidence_id, str) or not evidence_id
                    for evidence_id in required_evidence
                )
                or len(set(required_evidence)) != len(required_evidence)
                or not set(required_evidence) <= set(expected_evidence)
            ):
                raise BenchmarkError(
                    f"{path}:{case_id}:{claim_id}: required_evidence_ids must "
                    "be unique IDs from expected_evidence"
                )
        cases[case_id] = record
    return cases


def load_runs(path: Path, cases: dict[str, dict]) -> tuple[str, str, dict[str, dict]]:
    runs: dict[str, dict] = {}
    modes: set[str] = set()
    run_ids: set[str] = set()
    for record in load_jsonl(path):
        require_keys(
            record,
            (
                "schema_version",
                "run_id",
                "run_mode",
                "case_id",
                "tool_calls",
                "evidence",
                "claims",
                "index_error",
                "latency_ms",
            ),
            str(path),
        )
        case_id = record["case_id"]
        if case_id not in cases:
            raise BenchmarkError(f"{path}: unknown case_id {case_id!r}")
        if case_id in runs:
            raise BenchmarkError(f"{path}: duplicate run for case_id {case_id!r}")
        if not isinstance(record["tool_calls"], list):
            raise BenchmarkError(f"{path}:{case_id}: tool_calls must be a list")
        if not isinstance(record["evidence"], list):
            raise BenchmarkError(f"{path}:{case_id}: evidence must be a list")
        if not isinstance(record["claims"], list):
            raise BenchmarkError(f"{path}:{case_id}: claims must be a list")
        if not isinstance(record["latency_ms"], (int, float)) or record["latency_ms"] < 0:
            raise BenchmarkError(f"{path}:{case_id}: latency_ms must be non-negative")
        for call in record["tool_calls"]:
            if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                raise BenchmarkError(f"{path}:{case_id}: malformed tool call")
        for claim in record["claims"]:
            if (
                not isinstance(claim, dict)
                or not isinstance(claim.get("claim_id"), str)
                or not claim["claim_id"]
                or not isinstance(claim.get("text"), str)
                or not isinstance(claim.get("evidence_ids"), list)
                or any(
                    not isinstance(evidence_id, str) or not evidence_id
                    for evidence_id in claim["evidence_ids"]
                )
            ):
                raise BenchmarkError(f"{path}:{case_id}: malformed claim")
        modes.add(record["run_mode"])
        run_ids.add(record["run_id"])
        runs[case_id] = record

    missing = set(cases) - set(runs)
    if missing:
        raise BenchmarkError(f"{path}: missing runs for {', '.join(sorted(missing))}")
    if len(modes) != 1 or not modes <= {"fixture", "live"}:
        raise BenchmarkError(f"{path}: records must have one run_mode: fixture or live")
    if len(run_ids) != 1:
        raise BenchmarkError(f"{path}: records must have one run_id")
    return next(iter(modes)), next(iter(run_ids)), runs


def derive_route(run: dict) -> str:
    calls = run["tool_calls"]
    names = {call["tool"] for call in calls}
    if run["index_error"] != "none" and not (names & RETRIEVAL_TOOLS):
        return "block_index"
    if names & SECURITY_TOOLS:
        return "security"
    lexical = any(
        call["tool"] in LEXICAL_TOOLS
        or (
            call["tool"] == "mcp__code-search__search_code"
            and call.get("arguments", {}).get("search_mode") == "keyword"
        )
        for call in calls
    )
    semantic = bool(names & SEMANTIC_TOOLS) and not lexical
    graph = bool(names & GRAPH_TOOLS)
    if semantic and graph:
        return "mixed"
    if lexical:
        return "lexical"
    if semantic:
        return "semantic"
    if graph:
        return "graph"
    return "none"


def percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return float(ordered[index])


def score(cases: dict[str, dict], runs: dict[str, dict]) -> dict:
    route_hits = 0
    expected_evidence_count = 0
    returned_evidence_count = 0
    true_evidence_count = 0
    unsupported_claims = 0
    claim_count = 0
    stale_index_errors = 0
    tool_counts: list[int] = []
    latencies: list[float] = []

    for case_id, case in cases.items():
        run = runs[case_id]
        if derive_route(run) == case["expected_route"]:
            route_hits += 1

        expected = set(case["expected_evidence"])
        returned = set(run["evidence"])
        expected_evidence_count += len(expected)
        returned_evidence_count += len(returned)
        true_evidence_count += len(expected & returned)

        expected_claims = {
            claim["claim_id"]: claim for claim in case["expected_claims"]
        }
        recorded_by_id: dict[str, list[dict]] = {}
        for claim in run["claims"]:
            recorded_by_id.setdefault(claim["claim_id"], []).append(claim)

        for claim_id, expected_claim in expected_claims.items():
            claim_count += 1
            candidates = recorded_by_id.get(claim_id, [])
            if len(candidates) != 1:
                unsupported_claims += 1
                if len(candidates) > 1:
                    duplicate_count = len(candidates) - 1
                    claim_count += duplicate_count
                    unsupported_claims += duplicate_count
                continue
            recorded_claim = candidates[0]
            required_evidence = set(expected_claim["required_evidence_ids"])
            cited_evidence = set(recorded_claim["evidence_ids"])
            exact_text = (
                normalize_assertion(recorded_claim["text"])
                == expected_claim["text"]
            )
            fully_cited = (
                required_evidence <= cited_evidence
                and required_evidence <= returned
            )
            if not exact_text or not fully_cited:
                unsupported_claims += 1

        for claim_id, unknown_claims in recorded_by_id.items():
            if claim_id in expected_claims:
                continue
            claim_count += len(unknown_claims)
            unsupported_claims += len(unknown_claims)

        expected_error = case["expected_index_error"]
        actual_error = run["index_error"]
        names = {call["tool"] for call in run["tool_calls"]}
        queried_while_blocked = expected_error != "none" and bool(
            names & RETRIEVAL_TOOLS
        )
        if actual_error != expected_error or queried_while_blocked:
            stale_index_errors += 1

        tool_counts.append(len(run["tool_calls"]))
        latencies.append(float(run["latency_ms"]))

    total_cases = len(cases)
    return {
        "case_count": total_cases,
        "routing_accuracy": route_hits / total_cases,
        "evidence_precision": (
            true_evidence_count / returned_evidence_count
            if returned_evidence_count
            else 0.0
        ),
        "evidence_recall": (
            true_evidence_count / expected_evidence_count
            if expected_evidence_count
            else 0.0
        ),
        "unsupported_claim_rate": (
            unsupported_claims / claim_count if claim_count else 0.0
        ),
        "tool_calls": {
            "total": sum(tool_counts),
            "mean_per_case": statistics.fmean(tool_counts),
        },
        "latency_ms": {
            "mean": statistics.fmean(latencies),
            "p95": percentile_95(latencies),
        },
        "stale_index_errors": stale_index_errors,
    }


def threshold_failures(metrics: dict, thresholds: dict) -> list[str]:
    checks = (
        (
            metrics["routing_accuracy"]
            >= thresholds["min_routing_accuracy"],
            "routing_accuracy",
        ),
        (
            metrics["evidence_precision"]
            >= thresholds["min_evidence_precision"],
            "evidence_precision",
        ),
        (
            metrics["evidence_recall"] >= thresholds["min_evidence_recall"],
            "evidence_recall",
        ),
        (
            metrics["unsupported_claim_rate"]
            <= thresholds["max_unsupported_claim_rate"],
            "unsupported_claim_rate",
        ),
        (
            metrics["tool_calls"]["mean_per_case"]
            <= thresholds["max_mean_tool_calls"],
            "tool_calls.mean_per_case",
        ),
        (
            metrics["latency_ms"]["p95"] <= thresholds["max_p95_latency_ms"],
            "latency_ms.p95",
        ),
        (
            metrics["stale_index_errors"]
            <= thresholds["max_stale_index_errors"],
            "stale_index_errors",
        ),
    )
    return [name for passed, name in checks if not passed]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score a recorded routing/evidence JSONL run"
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument(
        "--bom",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "component-bom.json",
        help="component BOM bound into live provenance",
    )
    parser.add_argument(
        "--provenance",
        type=Path,
        help="independent live-run provenance (required for run_mode=live)",
    )
    args = parser.parse_args()

    try:
        cases = load_cases(args.cases)
        mode, run_id, runs = load_runs(args.runs, cases)
        if mode == "live" and args.provenance is None:
            raise BenchmarkError("live runs require --provenance")
        if mode == "live":
            validate_live_provenance(
                provenance_path=args.provenance,
                run_id=run_id,
                cases_path=args.cases,
                runs_path=args.runs,
                thresholds_path=args.thresholds,
                bom_path=args.bom,
            )
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
        metrics = score(cases, runs)
        failures = threshold_failures(metrics, thresholds)
    except (
        BenchmarkError,
        ProvenanceError,
        OSError,
        json.JSONDecodeError,
        KeyError,
    ) as exc:
        print(f"Benchmark validation FAILED: {exc}", file=sys.stderr)
        return 2

    if mode == "fixture":
        print("FIXTURE VALIDATION — NOT A LIVE BENCHMARK RESULT")
    else:
        print("PROVENANCED LIVE MEASUREMENT — NO COMPARATIVE GRADE")
    print(f"run_id: {run_id}")
    print(json.dumps(metrics, indent=2, sort_keys=True))
    if failures:
        print("thresholds: FAIL (" + ", ".join(failures) + ")")
        return 1
    print("thresholds: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
