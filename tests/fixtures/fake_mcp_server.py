#!/usr/bin/env python3
"""Line-delimited JSON-RPC MCP fixture backed by a compatibility snapshot."""

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SECRET_SENTINEL = "capture-secret-sentinel"


def send(message: dict) -> None:
    print(json.dumps(message, separators=(",", ":")), flush=True)


def main() -> int:
    component = sys.argv[1]
    snapshot_path = ROOT / "compatibility" / f"{component}-tools.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    tools = [
        {
            "name": name,
            "description": f"Fixture for {name}",
            "inputSchema": details["input_schema"],
        }
        for name, details in snapshot["tools"].items()
        if name != os.environ.get("FAKE_MCP_DROP_TOOL")
    ]
    mutate = os.environ.get("FAKE_MCP_MUTATE_SCHEMA")
    for tool in tools:
        if tool["name"] == mutate:
            tool["inputSchema"] = {
                **tool["inputSchema"],
                "properties": {
                    **tool["inputSchema"].get("properties", {}),
                    "fixture_drift": {"type": "boolean"},
                },
            }

    fixture_mode = (
        sys.argv[2] if len(sys.argv) > 2 else os.environ.get("FAKE_MCP_MODE")
    )
    if fixture_mode == "empty":
        tools = []
    elif fixture_mode == "duplicate":
        tools.append(dict(tools[0]))
    elif fixture_mode == "malformed":
        tools[0] = {**tools[0], "inputSchema": []}
    elif fixture_mode == "empty-name":
        tools[0] = {**tools[0], "name": ""}
    elif fixture_mode == "extra":
        tools.append(
            {
                "name": "unreviewed_extra_tool",
                "description": "Tool absent from the tested snapshot",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )
    elif fixture_mode == "nonobject-inputs":
        target = (
            "get_index_status"
            if component == "code-search"
            else "index_repository"
        )
        property_name = (
            "project_path" if component == "code-search" else "skip_report"
        )
        property_type = "string" if component == "code-search" else "boolean"
        for tool in tools:
            if tool["name"] == target:
                tool["inputSchema"] = {
                    **tool["inputSchema"],
                    "type": "string",
                    "properties": {
                        **tool["inputSchema"].get("properties", {}),
                        property_name: {"type": property_type},
                    },
                }

    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": component, "version": "fixture"},
                    },
                }
            )
        elif method == "tools/list":
            if SECRET_SENTINEL in os.environ.values():
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {
                            "code": -32000,
                            "message": "capture subprocess inherited a secret",
                        },
                    }
                )
                continue
            listed_tools = tools
            next_cursor = None
            if fixture_mode == "paginated":
                cursor = request.get("params", {}).get("cursor")
                midpoint = len(tools) // 2
                if cursor is None:
                    listed_tools = tools[:midpoint]
                    next_cursor = "fixture-page-2"
                elif cursor == "fixture-page-2":
                    listed_tools = tools[midpoint:]
                else:
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "error": {"code": -32602, "message": "invalid cursor"},
                        }
                    )
                    continue
            result = {"tools": listed_tools}
            if next_cursor is not None:
                result["nextCursor"] = next_cursor
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": result,
                }
            )
        elif method == "roots/list":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"roots": []},
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
