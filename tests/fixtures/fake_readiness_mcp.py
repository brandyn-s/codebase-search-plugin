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
START_DIRECTORY = Path.cwd().resolve()
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
        "properties": {
            "project_path": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            }
        },
    },
    "search_code_evidence": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer"},
            "search_mode": {"type": "string"},
            "file_pattern": {"type": "string"},
            "include_context": {"type": "boolean"},
            "auto_reindex": {"type": "boolean"},
        },
        "required": ["query"],
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
BEHAVIOR = os.environ.get("FAKE_READINESS_BEHAVIOR", "")
PROBE_PATH = os.environ.get("FAKE_READINESS_PROBE_PATH", "src/config.py")
PROBE_QUERY = os.environ.get("FAKE_READINESS_PROBE_QUERY", "CODE_SEARCH_STORAGE")
PROBE_START = int(os.environ.get("FAKE_READINESS_PROBE_START", "1"))
PROBE_END = int(os.environ.get("FAKE_READINESS_PROBE_END", "3"))


def isolated_runtime_is_valid() -> bool:
    for name in (
        "GH_TOKEN",
        "CODE_INTEL_COMPONENT_TOKEN",
        "VOYAGE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        if os.environ.get(name):
            return False
    expected_values = {
        "EMBEDDING_PROVIDER": "local",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "RERANKER": "off",
        "QUANTIZATION": "float32",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
        "TOKENIZERS_PARALLELISM": "false",
    }
    if any(os.environ.get(name) != value for name, value in expected_values.items()):
        return False
    try:
        home = Path(os.environ["HOME"]).resolve()
        user_profile = Path(os.environ["USERPROFILE"]).resolve()
        model = Path(os.environ["LOCAL_EMBEDDING_MODEL"]).resolve()
        isolated_paths = [
            home,
            Path(os.environ["XDG_CONFIG_HOME"]).resolve(),
            Path(os.environ["XDG_CACHE_HOME"]).resolve(),
            Path(os.environ["XDG_DATA_HOME"]).resolve(),
            Path(os.environ["CODE_SEARCH_STORAGE"]).resolve(),
            Path(os.environ["HF_HOME"]).resolve(),
            Path(os.environ["TORCH_HOME"]).resolve(),
            Path(os.environ["TMPDIR"]).resolve(),
        ]
    except (KeyError, OSError):
        return False
    if home != user_profile:
        return False
    if any(not path.is_dir() for path in isolated_paths):
        return False
    return all(
        path.is_file()
        for path in (
            model / "modules.json",
            model / "config_sentence_transformers.json",
            model / "0_BoW" / "config.json",
        )
    )


def response(request_id, result: dict) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}),
        flush=True,
    )


def tool_result(request_id, payload: dict) -> None:
    result = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, sort_keys=True),
            }
        ],
        "isError": False,
    }
    if COMPONENT == "code-search":
        result["structuredContent"] = {
            "result": json.dumps(payload, sort_keys=True)
        }
    response(
        request_id,
        result,
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


def stable_reference_id(prefix: str, reference: dict) -> str:
    payload = {key: value for key, value in reference.items() if key != "id"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return (
        f"{prefix}:v{payload['schema_version']}:"
        + hashlib.sha256(encoded).hexdigest()
    )


def call_tool(request_id, name: str, arguments: dict) -> None:
    global repository
    if COMPONENT == "code-search" and name == "index_directory":
        repository = Path(arguments["directory_path"])
        if START_DIRECTORY != repository.resolve():
            raise RuntimeError("server cwd must equal the indexed checkout")
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
                    "directory": str(repository.resolve()),
                    "storage_target": str(
                        Path(os.environ["CODE_SEARCH_STORAGE"]).resolve()
                    ),
                    "files_added": 10,
                    "chunks_added": 10,
                    "pipeline_version": "fixture-pipeline-v1",
                    "index_identity_status": "ready",
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
                "project_path": (
                    "/wrong/search/project"
                    if BEHAVIOR == "search-status-wrong-project"
                    else str(repository.resolve())
                ),
                "index_ready": True,
                "index_identity_status": "ready",
                "error": None,
                "index_identity": identity(),
            },
        )
    elif COMPONENT == "code-search" and name == "search_code_evidence":
        assert repository is not None
        if arguments != {
            "query": PROBE_QUERY,
            "k": 5,
            "search_mode": "keyword",
            "file_pattern": PROBE_PATH,
            "include_context": False,
            "auto_reindex": False,
        }:
            raise RuntimeError("unexpected evidence-coordinate query")
        search_identity = identity()
        source_lines = (repository / PROBE_PATH).read_text(
            encoding="utf-8"
        ).splitlines()
        matching_lines = [
            line_number
            for line_number in range(PROBE_START, PROBE_END + 1)
            if source_lines[line_number - 1].strip()
            and PROBE_QUERY in source_lines[line_number - 1]
        ]
        if len(matching_lines) != 1:
            raise RuntimeError("fixture probe must identify one source line")
        candidate_line = (
            PROBE_END + 1
            if BEHAVIOR == "search-evidence-past-eof"
            else matching_lines[0]
        )
        candidate_snippet = (
            "out-of-range " + PROBE_QUERY
            if candidate_line > len(source_lines)
            else source_lines[candidate_line - 1].strip()
        )
        evidence_ref = {
            "schema_version": 1,
            "repository_id": search_identity["repository_id"],
            "source_revision": search_identity["source_revision"],
            "index_generation": search_identity["index_generation"],
            "relative_path": PROBE_PATH,
            "start_line": candidate_line,
            "end_line": candidate_line,
            "evidence_type": "lexical_match",
        }
        evidence_ref["id"] = stable_reference_id("ev", evidence_ref)
        if BEHAVIOR == "search-evidence-invalid-id":
            evidence_ref["id"] = "ev:v1:" + "0" * 64
        context_end = max(PROBE_END, candidate_line)
        tool_result(
            request_id,
            {
                "query": PROBE_QUERY,
                "results": [
                    {
                        "file": PROBE_PATH,
                        "lines": f"{PROBE_START}-{context_end}",
                        "span_role": "retrieval_context",
                        "context_span": {
                            "relative_path": PROBE_PATH,
                            "start_line": PROBE_START,
                            "end_line": context_end,
                        },
                        "evidence_candidates": [
                            {
                                "role": "atomic_source_line",
                                "lines": f"{candidate_line}-{candidate_line}",
                                "snippet": candidate_snippet,
                                "evidence_ref": evidence_ref,
                            }
                        ],
                    }
                ],
                "_metadata": {
                    "evidence_refs": {
                        "schema_version": 2,
                        "emitted": True,
                        "count": 1,
                        "result_count": 1,
                        "symbol_count": 0,
                        "index_generation": search_identity[
                            "index_generation"
                        ],
                        "candidate_policy": "atomic_nonblank_source_line",
                        "symbol_ref_policy": "canonical_qualified_name_only",
                    }
                },
            },
        )
    elif COMPONENT == "code-graph" and name == "index_repository":
        if arguments.get("skip_report") is not True:
            raise RuntimeError("skip_report must be true")
        repository = Path(arguments["repo_path"])
        if START_DIRECTORY != repository.resolve():
            raise RuntimeError("server cwd must equal the indexed checkout")
        tool_result(
            request_id,
            {
                "error": None,
                "project": repository.name,
                **(
                    {"status": "failed"}
                    if BEHAVIOR == "graph-completion-failed"
                    else {"status": None}
                    if BEHAVIOR == "graph-completion-null"
                    else {}
                ),
                "identity_status": "captured",
                "index_identity": identity(),
            },
        )
    elif COMPONENT == "code-graph" and name == "index_status":
        assert repository is not None
        tool_result(
            request_id,
            {
                "status": "ready",
                "error": None,
                "project": (
                    "wrong-graph-project"
                    if BEHAVIOR == "graph-status-wrong-project"
                    else repository.name
                ),
                "root_path": (
                    "/wrong/graph/root"
                    if BEHAVIOR == "graph-status-wrong-root"
                    else str(repository.resolve())
                ),
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
    if not isolated_runtime_is_valid():
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
