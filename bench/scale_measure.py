#!/usr/bin/env python3
"""Measure released code-intelligence backends on one large Git checkout."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.public_measure.run import (  # noqa: E402
    MCPClient,
    PeakRSS,
    directory_bytes,
    index_graph,
    index_search,
    isolated_environment,
    percentile,
    repository_stats,
    sha256_file,
)


class ScaleError(RuntimeError):
    """A measurement precondition or backend call failed."""


COMPONENTS = ("code-search", "code-graph")


def selected_components(requested: list[str] | None) -> tuple[str, ...]:
    """Return the requested components in canonical execution order."""
    if not requested:
        return COMPONENTS
    selected = set(requested)
    return tuple(component for component in COMPONENTS if component in selected)


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        check=True,
        text=True,
        timeout=600,
    )
    return completed.stdout.strip()


def validate_checkout(root: Path) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise ScaleError("repository must be a real directory")
    revision = git(root, "rev-parse", "HEAD")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ScaleError("repository must be clean")
    return {"revision": revision, **repository_stats(root)}


def query_search(client: MCPClient, query: str, repetitions: int) -> dict:
    latencies: list[int] = []
    first: dict | None = None
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        result = client.call_tool(
            "search_code_evidence",
            {
                "query": query,
                "k": 20,
                "search_mode": "hybrid",
                "include_context": False,
                "auto_reindex": False,
            },
        )
        latencies.append(time.perf_counter_ns() - started)
        if first is None:
            first = result
    assert first is not None
    return {
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "result_count": len(first.get("results", [])),
        "latency_p50_ns": percentile(latencies, 0.50),
        "latency_p95_ns": percentile(latencies, 0.95),
        "repetitions": repetitions,
    }


def query_graph(
    client: MCPClient,
    project: str,
    query: str,
    repetitions: int,
) -> dict:
    latencies: list[int] = []
    first: dict | None = None
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        result = client.call_tool(
            "code_localize",
            {
                "issue_description": query,
                "project": project,
                "top_k": 20,
                "depth": 3,
                "seed_strategy": "substring",
            },
        )
        latencies.append(time.perf_counter_ns() - started)
        if first is None:
            first = result
    assert first is not None
    return {
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "result_count": len(first.get("matches", [])),
        "latency_p50_ns": percentile(latencies, 0.50),
        "latency_p95_ns": percentile(latencies, 0.95),
        "repetitions": repetitions,
    }


def write_checkpoint(output: Path, document: dict) -> None:
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, output)


def measure_component(
    component: str,
    *,
    server: Path,
    repository: Path,
    runtime_root: Path,
    model: Path,
    query: str,
    repetitions: int,
    timeout: float,
    lines: int,
) -> dict:
    environment = isolated_environment(runtime_root, model)
    deadline = time.monotonic() + timeout
    client = MCPClient(component, str(server), deadline, environment, repository)
    try:
        required = (
            {"index_directory", "get_indexing_progress", "search_code_evidence"}
            if component == "code-search"
            else {"index_repository", "code_localize", "list_projects"}
        )
        missing = required - set(client.list_tools())
        if missing:
            raise ScaleError(f"{component}: missing tools {sorted(missing)}")
        if component == "code-search":
            index_result, cold_ns, peak_rss = index_search(
                client,
                repository,
                incremental=True,
            )
            query_result = query_search(client, query, repetitions)
            index_path = runtime_root / "code-search-storage"
            counts = {
                "chunks": index_result.get("index_stats", {}).get(
                    "chunks_indexed"
                )
            }
        else:
            index_result, cold_ns, peak_rss = index_graph(client, repository)
            project = index_result.get("project")
            if not isinstance(project, str) or not project:
                raise ScaleError("code-graph did not return a project identity")
            query_result = query_graph(client, project, query, repetitions)
            index_path = runtime_root / "home" / ".cache" / "codebase-memory-mcp"
            counts = {
                "nodes": index_result.get("nodes"),
                "edges": index_result.get("edges"),
            }
        persisted = directory_bytes(index_path)
        return {
            "status": "completed",
            "server_sha256": sha256_file(server),
            "cold_index_ns": cold_ns,
            "cold_peak_rss_bytes": peak_rss,
            "index_bytes": persisted,
            "index_bytes_per_utf8_line": persisted / lines if lines else None,
            "query": query_result,
            **counts,
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--search-server", type=Path)
    parser.add_argument("--graph-server", type=Path)
    parser.add_argument(
        "--component",
        action="append",
        choices=COMPONENTS,
        help="backend to measure; repeat for both (default: both)",
    )
    parser.add_argument("--local-model", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--query", default="parse source file diagnostics")
    parser.add_argument("--warm-repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=14400)
    args = parser.parse_args()
    if args.warm_repetitions < 1 or args.timeout <= 0:
        parser.error("repetitions and timeout must be positive")
    components = selected_components(args.component)
    servers = {
        "code-search": args.search_server,
        "code-graph": args.graph_server,
    }
    for component in components:
        path = servers[component]
        if path is None:
            option = "--search-server" if component == "code-search" else "--graph-server"
            parser.error(f"{option} is required when measuring {component}")
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f"server is missing or not executable: {path}")
    if args.runtime_root.exists() or args.output.exists():
        parser.error("runtime root and output must both be initially absent")
    args.runtime_root.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        repository = args.repository.resolve()
        stats = validate_checkout(repository)
        result = {
            "schema_version": 1,
            "measurement_id": "released-backends-very-large-repository-v1",
            "started_at": started,
            "repository": {
                "path": str(repository),
                **stats,
            },
            "components": {},
            "selected_components": list(components),
            "language_model_calls": 0,
        }
        for component in components:
            server = servers[component]
            assert server is not None
            server = server.resolve()
            try:
                result["components"][component] = measure_component(
                    component,
                    server=server,
                    repository=repository,
                    runtime_root=args.runtime_root / component,
                    model=args.local_model.resolve(),
                    query=args.query,
                    repetitions=args.warm_repetitions,
                    timeout=args.timeout,
                    lines=stats["utf8_text_lines"],
                )
            except Exception as exc:
                result["components"][component] = {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "server_sha256": sha256_file(server),
                }
            write_checkpoint(args.output, result)
        result["finished_at"] = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        completed = sum(
            value.get("status") == "completed"
            for value in result["components"].values()
        )
        result["status"] = (
            "completed" if completed == len(components) else "partial"
        )
        result["result_sha256"] = hashlib.sha256(canonical_json(result)).hexdigest()
        write_checkpoint(args.output, result)
        print(json.dumps(result, sort_keys=True))
        return 0 if completed == 2 else 2
    except Exception as exc:
        print(f"Scale measurement FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
