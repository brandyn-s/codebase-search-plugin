#!/usr/bin/env python3
"""Line-delimited JSON-RPC MCP fixture backed by a compatibility snapshot."""

import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]


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
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {"tools": tools},
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
