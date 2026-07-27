#!/usr/bin/env python3
"""Deterministic stdio MCP fixture for readiness-smoke acceptance tests."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


COMPONENT = sys.argv[1]
INDEX_REPOSITORY_SCHEMA = {
    "type": "object",
    "properties": {
        "repo_path": {"type": "string"},
        "skip_report": {"type": "boolean"},
    },
}
SEARCH_SCHEMAS = {
    "index_directory": {
        "type": "object",
        "properties": {"directory_path": {"type": "string"}},
        "required": ["directory_path"],
    },
    "get_indexing_progress": {"type": "object", "properties": {}},
    "get_index_status": {
        "type": "object",
        "properties": {"project_path": {"type": "string"}},
    },
}
GRAPH_SCHEMAS = {
    "index_repository": INDEX_REPOSITORY_SCHEMA,
    "index_status": {
        "type": "object",
        "properties": {"project": {"type": "string"}},
        "required": ["project"],
    },
}
repository: Path | None = None
job_id = "readiness-smoke-job"


def response(request_id, result: dict) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        flush=True,
    )


def tool_result(request_id, payload: dict) -> None:
    response(
        request_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, sort_keys=True),
                }
            ],
            "isError": False,
        },
    )


def identity() -> dict:
    assert repository is not None
    resolved = str(repository.resolve())
    repository_id = hashlib.sha256(f"path:{resolved}".encode()).hexdigest()
    source_revision = subprocess.run(
        ["git", "-C", resolved, "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    generation = hashlib.sha256(
        f"{repository_id}\0{source_revision}\0clean".encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "repository_id": repository_id,
        "checkout_id": repository_id,
        "source_revision": source_revision,
        "dirty_fingerprint": "clean",
        "index_generation": generation,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def call_tool(request_id, name: str, arguments: dict) -> None:
    global repository
    if COMPONENT == "code-search" and name == "index_directory":
        repository = Path(arguments["directory_path"])
        tool_result(
            request_id,
            {
                "status": "indexing",
                "job_id": job_id,
                "directory": str(repository.resolve()),
                "project_name": repository.name,
                "index_ready": False,
                "message": (
                    "Indexing started in background. Use "
                    "get_indexing_progress to check status."
                ),
            },
        )
    elif COMPONENT == "code-search" and name == "get_indexing_progress":
        assert repository is not None
        tool_result(
            request_id,
            {
                "status": "completed",
                "job_id": job_id,
                "directory": str(repository.resolve()),
                "project_name": repository.name,
                "index_ready": True,
                "error": None,
                "result": {
                    "success": True,
                    "index_ready": True,
                    "error": None,
                },
            },
        )
    elif COMPONENT == "code-search" and name == "get_index_status":
        if repository is None or arguments.get("project_path") != str(
            repository.resolve()
        ):
            raise RuntimeError("get_index_status must bind the canonical project_path")
        tool_result(
            request_id,
            {
                "index_ready": True,
                "index_identity_status": "ready",
                "error": None,
                "index_identity": identity(),
            },
        )
    elif COMPONENT == "code-graph" and name == "index_repository":
        if arguments.get("skip_report") is not True:
            raise RuntimeError("skip_report must be true")
        repository = Path(arguments["repo_path"])
        tool_result(
            request_id,
            {
                "error": None,
                "project": repository.name,
                "identity_status": "captured",
                "index_identity": identity(),
            },
        )
    elif COMPONENT == "code-graph" and name == "index_status":
        tool_result(
            request_id,
            {
                "status": "ready",
                "error": None,
                "identity_status": "captured",
                "index_identity": identity(),
            },
        )
    else:
        response(
            request_id,
            {
                "content": [
                    {"type": "text", "text": f"unknown fixture tool {name}"}
                ],
                "isError": True,
            },
        )


def main() -> int:
    if COMPONENT not in {"code-search", "code-graph"}:
        return 2
    if os.environ.get("GH_TOKEN") or os.environ.get("CODE_INTEL_COMPONENT_TOKEN"):
        return 3
    for raw_line in sys.stdin:
        message = json.loads(raw_line)
        request_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            response(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": COMPONENT, "version": "fixture"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            schemas = SEARCH_SCHEMAS if COMPONENT == "code-search" else GRAPH_SCHEMAS
            response(
                request_id,
                {
                    "tools": [
                        {"name": name, "inputSchema": schema}
                        for name, schema in schemas.items()
                    ]
                },
            )
        elif method == "tools/call":
            params = message.get("params", {})
            call_tool(
                request_id,
                params.get("name"),
                params.get("arguments", {}),
            )
        elif request_id is not None:
            response(
                request_id,
                {"error": {"code": -32601, "message": f"unknown method {method}"}},
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
