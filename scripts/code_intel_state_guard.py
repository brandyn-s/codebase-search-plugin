#!/usr/bin/env python3
"""Host-owned route and backend-evidence state for code-intelligence runs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from proof_evaluator import ProofInputError, _validate_evidence_ref


SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EVIDENCE_ID = re.compile(r"^ev:v1:[0-9a-f]{64}$")
VALID_ROUTES = {"semantic", "lexical", "graph", "mixed", "security"}
MODES = {
    "pre-tool-use",
    "post-tool-use",
    "post-tool-failure",
    "pre-terminal-output",
    "cleanup",
}
TRACE_DIRECTIONS = {"none", "inbound", "outbound", "both"}
ALLOWED_ROUTE_CAPABILITIES = {
    "semantic": {"semantic"},
    "lexical": {"lexical"},
    "graph": {"graph"},
    "mixed": {"semantic", "graph"},
    "security": {"security", "graph"},
}
REQUIRED_ROUTE_CAPABILITIES = {
    "semantic": {"semantic"},
    "lexical": {"lexical"},
    "graph": {"graph"},
    "mixed": {"semantic", "graph"},
    "security": {"security"},
}
GRAPH_TOOL_SUFFIXES = {
    "__explain_symbol",
    "__search_graph",
    "__trace_call_path",
    "__query_graph",
    "__get_code_snippet",
    "__get_architecture",
    "__detect_changes",
    "__get_review_context",
    "__get_relationship_evidence",
}
SECURITY_TOOL_SUFFIXES = {
    "__query_security_surfaces",
    "__trace_data_flow",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("--expected-route", choices=sorted(VALID_ROUTES), required=True)
    parser.add_argument(
        "--trace-direction",
        choices=sorted(TRACE_DIRECTIONS),
        default="none",
    )
    return parser.parse_args()


def _load_input() -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise TypeError("hook input must be an object")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("hook input has an invalid session_id")
    return value


def _state_root() -> Path:
    configured = os.environ.get("CODE_INTEL_STATE_GUARD_ROOT")
    root = (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "code-intel-state-guard"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("state guard root is unsafe")
    return root


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "routes": [],
        "trace": None,
        "evidence": {},
    }


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("routes"), list)
        or not all(item in VALID_ROUTES for item in value["routes"])
        or not isinstance(value.get("evidence"), dict)
    ):
        raise ValueError("state guard state is invalid")
    trace = value.get("trace")
    if trace is not None and (
        not isinstance(trace, dict)
        or trace.get("status") not in {"pending", "completed"}
        or not isinstance(trace.get("tool_use_id"), str)
        or trace.get("direction") not in TRACE_DIRECTIONS - {"none"}
    ):
        raise ValueError("state guard trace state is invalid")
    return value


def _write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(state, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


@contextmanager
def _locked_state(session_id: str) -> Iterator[tuple[Path, dict[str, Any]]]:
    root = _state_root()
    state_path = root / f"{session_id}.json"
    lock_path = root / f"{session_id}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield state_path, _read_state(state_path)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _route_capabilities(tool_name: str, tool_input: dict[str, Any]) -> set[str]:
    if any(tool_name.endswith(suffix) for suffix in SECURITY_TOOL_SUFFIXES):
        return {"security"}
    if tool_name.endswith("__search_code"):
        if tool_input.get("search_mode") == "keyword" or "code-graph" in tool_name:
            return {"lexical"}
        return {"semantic"}
    if tool_name.endswith("__search_code_evidence"):
        if tool_input.get("search_mode") == "keyword":
            return {"lexical"}
        return {"semantic"}
    if tool_name.endswith("__code_localize"):
        return {"semantic"}
    if any(tool_name.endswith(suffix) for suffix in GRAPH_TOOL_SUFFIXES):
        return {"graph"}
    return set()


def _derived_route(routes: set[str]) -> str:
    if "security" in routes:
        return "security"
    if {"semantic", "graph"}.issubset(routes):
        return "mixed"
    if "lexical" in routes:
        return "lexical"
    if "semantic" in routes:
        return "semantic"
    if "graph" in routes:
        return "graph"
    return "native"


def _walk(value: object) -> Iterator[dict[str, Any]]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return
        yield from _walk(decoded)
    elif isinstance(value, dict):
        evidence = value.get("evidence_ref")
        if isinstance(evidence, dict):
            yield evidence
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _pre_tool(
    value: dict[str, Any], expected_route: str, trace_direction: str
) -> int:
    tool_name = value.get("tool_name")
    tool_input = value.get("tool_input")
    tool_use_id = value.get("tool_use_id")
    if (
        not isinstance(tool_name, str)
        or not isinstance(tool_input, dict)
        or not isinstance(tool_use_id, str)
        or not tool_use_id
    ):
        raise ValueError("tool request omitted tool name, input, or ID")
    with _locked_state(value["session_id"]) as (_, state):
        routes = set(state["routes"])
    if tool_name == "Read":
        missing = REQUIRED_ROUTE_CAPABILITIES[expected_route] - routes
        if missing:
            raise ValueError(
                "complete the required route before Read; missing: "
                + ", ".join(sorted(missing))
            )
        return 0
    capabilities = _route_capabilities(tool_name, tool_input)
    if capabilities and not capabilities <= ALLOWED_ROUTE_CAPABILITIES[expected_route]:
        raise ValueError(f"tool is not allowed for the required {expected_route} route")
    if not tool_name.endswith("__trace_call_path"):
        return 0
    if trace_direction == "none":
        raise ValueError("trace_call_path is not allowed for this contract")
    if tool_input.get("direction") != trace_direction:
        raise ValueError(f"trace_call_path direction must be {trace_direction}")
    requested = {
        "status": "pending",
        "tool_use_id": tool_use_id,
        "direction": trace_direction,
    }
    with _locked_state(value["session_id"]) as (state_path, state):
        if state["trace"] is None:
            state["trace"] = requested
            _write_state(state_path, state)
            return 0
        if state["trace"] == requested:
            return 0
        raise ValueError("trace_call_path is allowed exactly once")


def _record_failure(value: dict[str, Any]) -> int:
    tool_name = value.get("tool_name")
    tool_use_id = value.get("tool_use_id")
    if not isinstance(tool_name, str) or not isinstance(tool_use_id, str):
        raise ValueError("tool failure omitted tool name or ID")
    if not tool_name.endswith("__trace_call_path"):
        return 0
    with _locked_state(value["session_id"]) as (state_path, state):
        trace = state["trace"]
        if (
            isinstance(trace, dict)
            and trace.get("status") == "pending"
            and trace.get("tool_use_id") == tool_use_id
        ):
            state["trace"] = None
            _write_state(state_path, state)
    return 0


def _record_tool(value: dict[str, Any]) -> int:
    tool_name = value.get("tool_name")
    tool_input = value.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise ValueError("tool receipt omitted tool name or input")
    references: dict[str, dict[str, Any]] = {}
    for position, raw in enumerate(_walk(value.get("tool_response"))):
        reference = _validate_evidence_ref(raw, f"tool_response.evidence[{position}]")
        references[reference["id"]] = reference
    with _locked_state(value["session_id"]) as (state_path, state):
        if tool_name.endswith("__trace_call_path"):
            pending = state["trace"]
            if (
                not isinstance(pending, dict)
                or pending.get("status") != "pending"
                or pending.get("tool_use_id") != value.get("tool_use_id")
            ):
                raise ValueError("successful trace has no matching pending state")
            state["trace"] = {**pending, "status": "completed"}
        state["routes"] = sorted(
            set(state["routes"]) | _route_capabilities(tool_name, tool_input)
        )
        state["evidence"].update(references)
        _write_state(state_path, state)
    return 0


def _validate_terminal(
    value: dict[str, Any], expected_route: str, trace_direction: str
) -> int:
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("structured output omitted tool input")
    with _locked_state(value["session_id"]) as (_, state):
        observed_route = _derived_route(set(state["routes"]))
        if observed_route != expected_route:
            raise ValueError(
                f"required route is {expected_route}; successful route is {observed_route}"
            )
        if trace_direction != "none" and state["trace"] != {
            "status": "completed",
            "tool_use_id": state["trace"].get("tool_use_id")
            if isinstance(state["trace"], dict)
            else None,
            "direction": trace_direction,
        }:
            raise ValueError(
                f"complete exactly one {trace_direction} trace before terminal output"
            )
        evidence_ids = tool_input.get("evidence_ids")
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(item, str) and EVIDENCE_ID.fullmatch(item) for item in evidence_ids)
        ):
            raise ValueError("select at least one canonical backend evidence ID")
        unseen = [item for item in evidence_ids if item not in state["evidence"]]
        if unseen:
            raise ValueError("structured output selected unseen evidence IDs")
        answer = tool_input.get("answer")
        if not isinstance(answer, str):
            raise ValueError("structured output answer must be a string")
        if any(item not in answer for item in evidence_ids):
            raise ValueError("every selected evidence ID must be cited in the answer")
    return 0


def _cleanup(value: dict[str, Any]) -> int:
    with _locked_state(value["session_id"]) as (state_path, _):
        state_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    try:
        args = _parse_args()
        value = _load_input()
        if args.mode == "pre-tool-use":
            return _pre_tool(value, args.expected_route, args.trace_direction)
        if args.mode == "post-tool-use":
            return _record_tool(value)
        if args.mode == "post-tool-failure":
            return _record_failure(value)
        if args.mode == "cleanup":
            return _cleanup(value)
        return _validate_terminal(value, args.expected_route, args.trace_direction)
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError, ProofInputError) as exc:
        print(f"code-intel state guard failed closed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
