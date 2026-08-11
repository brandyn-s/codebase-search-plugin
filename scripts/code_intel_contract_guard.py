#!/usr/bin/env python3
"""Enforce route completion and source-pinned final evidence for pilot runs."""

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

SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EVIDENCE_LOCATION = re.compile(r"^(.+):(\d+)-(\d+)$")
VALID_ROUTES = {"semantic", "lexical", "graph", "mixed", "security"}
MODES = {
    "pre-route-tool",
    "pre-read",
    "record-route",
    "record-read",
    "pre-terminal-output",
    "cleanup",
}
SEMANTIC_TOOLS = {
    "mcp__code-search__search_code_evidence",
    "mcp__code-search__code_localize",
}
GRAPH_TOOLS = {
    "mcp__code-graph__explain_symbol",
    "mcp__code-graph__search_graph",
    "mcp__code-graph__trace_call_path",
    "mcp__code-graph__query_graph",
    "mcp__code-graph__get_code_snippet",
    "mcp__code-graph__get_architecture",
    "mcp__code-graph__detect_changes",
    "mcp__code-graph__get_review_context",
    "mcp__code-graph__get_relationship_evidence",
}
SECURITY_TOOLS = {"mcp__code-graph__query_security_surfaces"}
ALLOWED_ROUTE_CAPABILITIES = {
    "semantic": {"semantic"},
    "lexical": {"lexical"},
    "graph": {"graph"},
    "mixed": {"semantic", "graph"},
    "security": {"security", "graph"},
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("--expected-route", choices=sorted(VALID_ROUTES), required=True)
    parser.add_argument("--target", type=Path, required=True)
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
    configured = os.environ.get("CODE_INTEL_CONTRACT_GUARD_ROOT")
    root = (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "code-intel-contract-guard"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("contract guard state root is unsafe")
    return root


def _empty_state() -> dict[str, Any]:
    return {"schema_version": 1, "routes": [], "pins": []}


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("routes"), list)
        or not all(item in VALID_ROUTES for item in value["routes"])
        or not isinstance(value.get("pins"), list)
        or not all(
            isinstance(item, str) and EVIDENCE_LOCATION.fullmatch(item)
            for item in value["pins"]
        )
    ):
        raise ValueError("contract guard state is invalid")
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


def _block(reason: str) -> int:
    print(reason, file=sys.stderr)
    return 2


def _post_context(message: str) -> int:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": message,
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


def _route_capabilities(tool_name: str, tool_input: dict[str, Any]) -> set[str]:
    if tool_name in SECURITY_TOOLS:
        return {"security"}
    if tool_name == "mcp__code-search__search_code":
        if tool_input.get("search_mode") == "keyword":
            return {"lexical"}
        return {"semantic"}
    if tool_name == "mcp__code-graph__search_code":
        return {"lexical"}
    if tool_name in SEMANTIC_TOOLS:
        return {"semantic"}
    if tool_name in GRAPH_TOOLS:
        return {"graph"}
    return set()


def _derived_route(routes: set[str]) -> str:
    if "security" in routes:
        return "security"
    if "semantic" in routes and "graph" in routes:
        return "mixed"
    if "lexical" in routes:
        return "lexical"
    if "semantic" in routes:
        return "semantic"
    if "graph" in routes:
        return "graph"
    return "native"


def _record_route(value: dict[str, Any]) -> int:
    tool_name = value.get("tool_name")
    tool_input = value.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise ValueError("route receipt omitted tool name or input")
    capabilities = _route_capabilities(tool_name, tool_input)
    if not capabilities:
        return 0
    with _locked_state(value["session_id"]) as (state_path, state):
        state["routes"] = sorted(set(state["routes"]) | capabilities)
        _write_state(state_path, state)
    return 0


def _pre_route_tool(value: dict[str, Any], expected_route: str) -> int:
    tool_name = value.get("tool_name")
    tool_input = value.get("tool_input")
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        raise ValueError("route request omitted tool name or input")
    capabilities = _route_capabilities(tool_name, tool_input)
    if not capabilities:
        return 0
    if capabilities <= ALLOWED_ROUTE_CAPABILITIES[expected_route]:
        return 0
    return _block(
        f"{tool_name} is not allowed for the required {expected_route} route. "
        "Use only that route's required tool family, then continue from its result."
    )


def _target_relative(file_path: object, target: Path) -> tuple[Path, str]:
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("Read pin omitted file_path")
    candidate = Path(file_path)
    if not candidate.is_absolute():
        raise ValueError("Read pin file_path must be absolute")
    resolved = candidate.resolve(strict=True)
    exact_target = target.resolve(strict=True)
    try:
        relative = resolved.relative_to(exact_target)
    except ValueError as exc:
        raise ValueError("Read pin is outside the exact target") from exc
    if not resolved.is_file():
        raise ValueError("Read pin source is not a regular file")
    return resolved, relative.as_posix()


def _record_read(value: dict[str, Any], target: Path) -> int:
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("Read receipt omitted tool input")
    offset = tool_input.get("offset")
    limit = tool_input.get("limit")
    if offset is None or limit is None:
        return _post_context(
            "This Read is inspection-only. Before structured output, make one "
            "successful exact Read pin for every final evidence location using "
            "offset=start and limit=end-start+1 after applying the deletion test."
        )
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 1
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError("Read pin offset and limit must be positive integers")
    resolved, relative = _target_relative(tool_input.get("file_path"), target)
    line_count = len(resolved.read_text(encoding="utf-8").splitlines())
    end = offset + limit - 1
    if end > line_count:
        raise ValueError("Read pin range exceeds the source")
    evidence_id = f"{relative}:{offset}-{end}"
    with _locked_state(value["session_id"]) as (state_path, state):
        state["pins"] = sorted(set(state["pins"]) | {evidence_id})
        _write_state(state_path, state)
    return _post_context(
        f"Evidence pin recorded for {evidence_id}. Cite it only if deletion would "
        "leave an atomic clause or named endpoint unsupported."
    )


def _pre_read(value: dict[str, Any], target: Path) -> int:
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("Read request omitted tool input")
    _target_relative(tool_input.get("file_path"), target)
    return 0


def _validate_output(
    value: dict[str, Any],
    *,
    expected_route: str,
) -> int:
    tool_input = value.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError("structured output omitted tool input")
    with _locked_state(value["session_id"]) as (_, state):
        observed_route = _derived_route(set(state["routes"]))
        if observed_route != expected_route:
            return _block(
                f"Complete the required {expected_route} route before structured "
                f"output; the successful tool route is currently {observed_route}"
            )
        evidence_ids = tool_input.get("evidence_ids")
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or not all(isinstance(item, str) for item in evidence_ids)
        ):
            return _block("Return at least one well-formed final evidence location")
        malformed = [
            item for item in evidence_ids if EVIDENCE_LOCATION.fullmatch(item) is None
        ]
        if malformed:
            return _block(
                "Final evidence locations must use repo-relative path:start-end: "
                + ", ".join(malformed)
            )
        pins = set(state["pins"])
        missing = [item for item in evidence_ids if item not in pins]
        if missing:
            return _block(
                "Before structured output, create an exact successful Read pin "
                "with offset=start and limit=end-start+1 for: "
                + ", ".join(missing)
                + ". Reapply the deletion test and pin only the minimal final ranges."
            )
        answer = tool_input.get("answer")
        if not isinstance(answer, str):
            return _block("Structured output answer must be a string")
        orphaned = [item for item in evidence_ids if item not in answer]
        if orphaned:
            return _block(
                "Every final evidence ID must be cited verbatim in the answer; not "
                "cited verbatim: " + ", ".join(orphaned)
            )
    return 0


def _cleanup(value: dict[str, Any]) -> int:
    with _locked_state(value["session_id"]) as (state_path, _):
        state_path.unlink(missing_ok=True)
    return 0


def main() -> int:
    try:
        args = _parse_args()
        value = _load_input()
        if args.mode == "pre-route-tool":
            return _pre_route_tool(value, args.expected_route)
        if args.mode == "pre-read":
            return _pre_read(value, args.target)
        if args.mode == "record-route":
            return _record_route(value)
        if args.mode == "record-read":
            return _record_read(value, args.target)
        if args.mode == "pre-terminal-output":
            return _validate_output(value, expected_route=args.expected_route)
        return _cleanup(value)
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return _block(f"code-intel contract guard failed closed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
