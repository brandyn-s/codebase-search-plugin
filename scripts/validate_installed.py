#!/usr/bin/env python3
"""Fail-closed validation of installed stdio MCP tool contracts.

The validator performs a real MCP initialize + tools/list handshake for each
installed server, then compares every supported tool's canonical inputSchema
fingerprint with the tested component snapshot. It also scans plugin skills so
an unavailable tool can never be documented as callable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parent.parent
TOOL_REFERENCE = re.compile(r"mcp__code-(search|graph)__([A-Za-z0-9_]+)")
COMPONENT_FOR_PREFIX = {"search": "code-search", "graph": "code-graph"}


class ContractError(RuntimeError):
    """An installed MCP does not satisfy the tested plugin contract."""


def canonical_schema_fingerprint(schema: dict) -> str:
    try:
        canonical = json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError(f"inputSchema is not valid JSON: {exc}") from exc
    return hashlib.sha256(canonical).hexdigest()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{path}: cannot load JSON: {exc}") from exc


def load_contract() -> tuple[dict, dict[str, dict]]:
    bom = load_json(ROOT / "component-bom.json")
    components = bom.get("components")
    if bom.get("schema_version") != 1 or not isinstance(components, dict):
        raise ContractError("component-bom.json: unsupported or malformed BOM")

    snapshots: dict[str, dict] = {}
    for component, details in components.items():
        rel = details.get("schema_snapshot")
        if not isinstance(rel, str):
            raise ContractError(f"BOM component {component}: missing schema_snapshot")
        snapshot = load_json(ROOT / rel)
        if snapshot.get("component") != component:
            raise ContractError(f"{rel}: component does not match BOM key {component}")
        snapshots[component] = snapshot
    return bom, snapshots


def referenced_tools() -> dict[str, set[str]]:
    references = {"code-search": set(), "code-graph": set()}
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        for prefix, tool_name in TOOL_REFERENCE.findall(
            skill.read_text(encoding="utf-8")
        ):
            references[COMPONENT_FOR_PREFIX[prefix]].add(tool_name)
    return references


def validate_skill_references(snapshots: dict[str, dict]) -> None:
    errors: list[str] = []
    for component, names in referenced_tools().items():
        supported = set(snapshots[component].get("tools", {}))
        for missing in sorted(names - supported):
            errors.append(
                f"{component}: skill references unavailable tested tool '{missing}'"
            )
    if errors:
        raise ContractError("; ".join(errors))


def _reader(stream, messages: queue.Queue, diagnostics: list[str]) -> None:
    for raw in iter(stream.readline, ""):
        line = raw.strip()
        if not line:
            continue
        try:
            messages.put(json.loads(line))
        except json.JSONDecodeError:
            diagnostics.append(line)


def _send(process: subprocess.Popen, message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def _await_response(
    process: subprocess.Popen,
    messages: queue.Queue,
    request_id: int,
    deadline: float,
) -> dict:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ContractError(
                f"server exited with status {process.returncode} before response "
                f"to request {request_id}"
            )
        try:
            message = messages.get(timeout=min(0.2, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if message.get("method") == "roots/list" and "id" in message:
            _send(
                process,
                {"jsonrpc": "2.0", "id": message["id"], "result": {"roots": []}},
            )
            continue
        if message.get("method") == "ping" and "id" in message:
            _send(process, {"jsonrpc": "2.0", "id": message["id"], "result": {}})
            continue
        if message.get("id") == request_id:
            if "error" in message:
                raise ContractError(
                    f"request {request_id} failed: {json.dumps(message['error'])}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise ContractError(f"request {request_id}: malformed result")
            return result
    raise ContractError(f"timeout waiting for MCP response to request {request_id}")


def list_tools(
    command: str, timeout: float, env: dict[str, str] | None = None
) -> list[dict]:
    try:
        process = subprocess.Popen(
            [command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        raise ContractError(f"cannot start {command}: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    messages: queue.Queue = queue.Queue()
    diagnostics: list[str] = []
    stdout_thread = threading.Thread(
        target=_reader, args=(process.stdout, messages, diagnostics), daemon=True
    )
    stderr_lines: list[str] = []
    stderr_thread = threading.Thread(
        target=lambda: stderr_lines.extend(process.stderr.readlines()), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + timeout
    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {
                        "name": "codebase-search-contract-validator",
                        "version": "1",
                    },
                },
            },
        )
        _await_response(process, messages, 1, deadline)
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        tools: list[dict] = []
        cursor = None
        seen_cursors: set[str] = set()
        request_id = 2
        while True:
            params = {} if cursor is None else {"cursor": cursor}
            _send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/list",
                    "params": params,
                },
            )
            result = _await_response(process, messages, request_id, deadline)
            page = result.get("tools")
            if not isinstance(page, list):
                raise ContractError(
                    "tools/list result does not contain a tools array"
                )
            tools.extend(page)
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tools
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise ContractError("tools/list returned an invalid pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            request_id += 1
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def validate_server(
    component: str, command: str, snapshot: dict, timeout: float
) -> None:
    actual_list = list_tools(command, timeout)
    actual: dict[str, dict] = {}
    for tool in actual_list:
        name = tool.get("name")
        schema = tool.get("inputSchema")
        if not isinstance(name, str) or not isinstance(schema, dict):
            raise ContractError(f"{component}: malformed tools/list entry")
        if name in actual:
            raise ContractError(f"{component}: duplicate tool name '{name}'")
        actual[name] = schema

    expected_tools = snapshot.get("tools")
    if not isinstance(expected_tools, dict) or not expected_tools:
        raise ContractError(f"{component}: tested snapshot has no tools")

    errors: list[str] = []
    for name, expected in expected_tools.items():
        if name not in actual:
            errors.append(f"missing tool '{name}'")
            continue
        fingerprint = canonical_schema_fingerprint(actual[name])
        if fingerprint != expected.get("input_schema_sha256"):
            errors.append(
                f"schema mismatch for '{name}' "
                f"(expected {expected.get('input_schema_sha256')}, got {fingerprint})"
            )
    for name in sorted(set(actual) - set(expected_tools)):
        errors.append(f"unexpected tool '{name}'")
    if errors:
        raise ContractError(f"{component}: " + "; ".join(errors))


def parse_servers(values: list[str]) -> dict[str, str]:
    servers: dict[str, str] = {}
    for value in values:
        component, separator, command = value.partition("=")
        if not separator or not component or not command:
            raise ContractError(
                f"invalid --server '{value}'; expected COMPONENT=/path/to/executable"
            )
        if component in servers:
            raise ContractError(f"duplicate --server component '{component}'")
        servers[component] = command
    return servers


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate installed MCP tools against the tested component BOM"
    )
    parser.add_argument(
        "--server",
        action="append",
        default=[],
        metavar="COMPONENT=EXECUTABLE",
        help="installed stdio MCP executable (repeat for each BOM component)",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    try:
        _, snapshots = load_contract()
        validate_skill_references(snapshots)
        servers = parse_servers(args.server)
        expected_components = set(snapshots)
        if set(servers) != expected_components:
            raise ContractError(
                "servers must exactly match BOM components: "
                + ", ".join(sorted(expected_components))
            )
        for component in sorted(servers):
            validate_server(
                component, servers[component], snapshots[component], args.timeout
            )
    except ContractError as exc:
        print(f"Installed MCP contract validation FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        "Installed MCP contract validation passed "
        f"({len(snapshots)} component(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
