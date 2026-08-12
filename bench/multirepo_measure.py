#!/usr/bin/env python3
"""Measure direct cross-project discovery over multiple immutable Git checkouts."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
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
    isolated_environment,
    percentile,
    repository_stats,
    sha256_file,
)


class MultiRepoError(RuntimeError):
    """A cross-project measurement precondition or backend call failed."""


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


def load_object(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise MultiRepoError(f"missing or unsafe JSON input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MultiRepoError(f"{path}: expected a JSON object")
    return value


def parse_checkout(value: str) -> tuple[str, Path]:
    case_id, separator, raw_path = value.partition("=")
    if not separator or not case_id or not raw_path:
        raise argparse.ArgumentTypeError("checkout must be CASE_ID=/absolute/path")
    path = Path(raw_path)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("checkout paths must be absolute")
    return case_id, path


def normalize_file(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/").lstrip("./")
    return normalized or None


def first_rank(values: list[str], expected: set[str]) -> int | None:
    for rank, value in enumerate(values, 1):
        if value in expected:
            return rank
    return None


def timed_call(
    client: MCPClient,
    tool: str,
    arguments: dict,
    repetitions: int,
) -> tuple[dict, list[int], bool]:
    first: dict | None = None
    first_bytes: bytes | None = None
    latencies: list[int] = []
    stable = True
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        result = client.call_tool(tool, arguments)
        latencies.append(time.perf_counter_ns() - started)
        encoded = canonical_json(result)
        if first is None:
            first = result
            first_bytes = encoded
        else:
            stable = stable and encoded == first_bytes
    assert first is not None
    return first, latencies, stable


def wait_for_search_index(client: MCPClient, root: Path) -> tuple[dict, int, int]:
    started = time.perf_counter_ns()
    with PeakRSS(client.process.pid) as sampler:
        response = client.call_tool(
            "index_directory",
            {"directory_path": str(root), "incremental": True},
        )
        if response.get("status") != "indexing":
            raise MultiRepoError(f"code-search did not start indexing {root.name}")
        while True:
            progress = client.call_tool("get_indexing_progress", {})
            if progress.get("status") == "indexing":
                time.sleep(0.1)
                continue
            result = progress.get("result")
            if (
                progress.get("status") != "completed"
                or not isinstance(result, dict)
                or result.get("success") is not True
            ):
                raise MultiRepoError(f"code-search failed to index {root.name}")
            break
    return result, time.perf_counter_ns() - started, sampler.peak


def index_graph(client: MCPClient, root: Path) -> tuple[dict, int, int]:
    started = time.perf_counter_ns()
    with PeakRSS(client.process.pid) as sampler:
        result = client.call_tool(
            "index_repository",
            {"repo_path": str(root), "skip_report": True},
        )
    if result.get("identity_status") != "captured":
        raise MultiRepoError(f"code-graph failed to index {root.name}")
    return result, time.perf_counter_ns() - started, sampler.peak


def search_observation(
    result: dict,
    *,
    expected_root: Path,
    expected_files: set[str],
) -> dict:
    expected_root = expected_root.resolve()
    rows = result.get("results", [])
    if not isinstance(rows, list):
        raise MultiRepoError("search_all_projects returned malformed results")
    project_order: list[str] = []
    expected_project_files: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_root = row.get("project_path")
        if not isinstance(raw_root, str):
            continue
        resolved = str(Path(raw_root).resolve())
        if resolved not in project_order:
            project_order.append(resolved)
        if Path(raw_root).resolve() == expected_root:
            relative = normalize_file(row.get("file_path"))
            if relative:
                expected_project_files.append(relative)
    expected_root_text = str(expected_root)
    return {
        "projects_attempted": result.get("projects_attempted"),
        "projects_with_matches": result.get("projects_with_matches"),
        "project_rank": first_rank(project_order, {expected_root_text}),
        "expected_file_rank_within_project": first_rank(
            expected_project_files, expected_files
        ),
        "result_count": len(rows),
        "project_errors": result.get("project_errors", {}),
    }


def graph_observation(
    result: dict,
    *,
    expected_project: str,
    expected_files: set[str],
) -> dict:
    rows = result.get("results", [])
    if not isinstance(rows, list):
        raise MultiRepoError("localize_across_projects returned malformed results")
    project_order: list[str] = []
    expected_project_files: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        project = row.get("project")
        if not isinstance(project, str):
            continue
        if project not in project_order:
            project_order.append(project)
        if project == expected_project:
            relative = normalize_file(row.get("file_path"))
            if relative:
                expected_project_files.append(relative)
    return {
        "projects_attempted": result.get("projects_attempted"),
        "projects_with_matches": result.get("projects_with_matches"),
        "project_rank": first_rank(project_order, {expected_project}),
        "expected_file_rank_within_project": first_rank(
            expected_project_files, expected_files
        ),
        "result_count": len(rows),
        "project_errors": result.get("project_errors", {}),
    }


def validate_cases(
    case_set: dict,
    oracle_set: dict,
    requested: list[tuple[str, Path]],
) -> list[dict]:
    cases = {
        item.get("case_id"): item
        for item in case_set.get("cases", [])
        if isinstance(item, dict)
    }
    oracles = {
        item.get("case_id"): item
        for item in oracle_set.get("cases", [])
        if isinstance(item, dict)
    }
    if len(requested) < 3 or len({case_id for case_id, _ in requested}) != len(requested):
        raise MultiRepoError("at least three unique case checkouts are required")
    prepared: list[dict] = []
    roots: set[Path] = set()
    for case_id, raw_root in requested:
        case = cases.get(case_id)
        oracle = oracles.get(case_id)
        root = raw_root.resolve()
        if not isinstance(case, dict) or not isinstance(oracle, dict):
            raise MultiRepoError(f"unknown case or oracle: {case_id}")
        if not root.is_dir() or root.is_symlink() or root in roots:
            raise MultiRepoError(f"unsafe or duplicate checkout: {root}")
        roots.add(root)
        if git(root, "rev-parse", "HEAD") != case.get("revision"):
            raise MultiRepoError(f"{case_id}: checkout revision mismatch")
        if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
            raise MultiRepoError(f"{case_id}: checkout is dirty")
        expected_files = oracle.get("expected_files")
        if not isinstance(expected_files, list) or not expected_files:
            raise MultiRepoError(f"{case_id}: expected files are missing")
        prepared.append(
            {
                "case_id": case_id,
                "query": case["query"],
                "revision": case["revision"],
                "root": root,
                "expected_files": set(expected_files),
                "stats": repository_stats(root),
            }
        )
    return prepared


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--oracle", required=True, type=Path)
    parser.add_argument("--checkout", action="append", required=True, type=parse_checkout)
    parser.add_argument("--search-server", required=True, type=Path)
    parser.add_argument("--graph-server", required=True, type=Path)
    parser.add_argument("--local-model", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--warm-repetitions", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=7200)
    args = parser.parse_args()
    if args.warm_repetitions < 1 or args.timeout <= 0:
        parser.error("repetitions and timeout must be positive")
    for path in (args.search_server, args.graph_server):
        if not path.is_file() or not os.access(path, os.X_OK):
            parser.error(f"server is missing or not executable: {path}")
    if args.runtime_root.exists() or args.output.exists():
        parser.error("runtime root and output must both be initially absent")

    args.runtime_root.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = validate_cases(
        load_object(args.cases), load_object(args.oracle), args.checkout
    )
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    search_environment = isolated_environment(
        args.runtime_root / "code-search", args.local_model.resolve()
    )
    graph_environment = isolated_environment(
        args.runtime_root / "code-graph", args.local_model.resolve()
    )
    deadline = time.monotonic() + args.timeout
    search = MCPClient(
        "code-search",
        str(args.search_server.resolve()),
        deadline,
        search_environment,
        cases[0]["root"],
    )
    graph = MCPClient(
        "code-graph",
        str(args.graph_server.resolve()),
        deadline,
        graph_environment,
        cases[0]["root"],
    )
    try:
        required_search = {
            "index_directory",
            "get_indexing_progress",
            "list_projects",
            "search_all_projects",
        }
        required_graph = {
            "index_repository",
            "list_projects",
            "localize_across_projects",
        }
        if not required_search <= set(search.list_tools()):
            raise MultiRepoError("installed code-search lacks cross-project tools")
        if not required_graph <= set(graph.list_tools()):
            raise MultiRepoError("installed code-graph lacks cross-project tools")

        indexing: dict[str, dict] = {}
        graph_projects_by_root: dict[Path, str] = {}
        for case in cases:
            search_index, search_ns, search_rss = wait_for_search_index(
                search, case["root"]
            )
            graph_index, graph_ns, graph_rss = index_graph(graph, case["root"])
            graph_project = graph_index.get("project")
            if not isinstance(graph_project, str) or not graph_project:
                raise MultiRepoError(f"{case['case_id']}: missing graph project")
            graph_projects_by_root[case["root"]] = graph_project
            indexing[case["case_id"]] = {
                "revision": case["revision"],
                "source": case["stats"],
                "search_cold_index_ns": search_ns,
                "search_peak_rss_bytes": search_rss,
                "search_chunks": search_index.get("index_stats", {}).get(
                    "chunks_indexed"
                ),
                "graph_cold_index_ns": graph_ns,
                "graph_peak_rss_bytes": graph_rss,
                "graph_nodes": graph_index.get("nodes"),
                "graph_edges": graph_index.get("edges"),
                "graph_project": graph_project,
            }

        search_projects = search.call_tool("list_projects", {})
        if search_projects.get("count") != len(cases):
            raise MultiRepoError("code-search project inventory count mismatch")
        if (
            len(graph_projects_by_root) != len(cases)
            or len(set(graph_projects_by_root.values())) != len(cases)
        ):
            raise MultiRepoError("code-graph project inventory count mismatch")

        observations: list[dict] = []
        graph_names = sorted(graph_projects_by_root.values())
        for case in cases:
            search_result, search_latencies, search_stable = timed_call(
                search,
                "search_all_projects",
                {"query": case["query"], "k": 10, "top_k": 30},
                args.warm_repetitions,
            )
            graph_result, graph_latencies, graph_stable = timed_call(
                graph,
                "localize_across_projects",
                {
                    "query": case["query"],
                    "projects": graph_names,
                    "seed_strategy": "substring",
                    "depth": 3,
                    "per_project_top_k": 10,
                    "top_k": 30,
                },
                args.warm_repetitions,
            )
            observations.append(
                {
                    "case_id": case["case_id"],
                    "query_sha256": hashlib.sha256(
                        case["query"].encode("utf-8")
                    ).hexdigest(),
                    "search": {
                        **search_observation(
                            search_result,
                            expected_root=case["root"],
                            expected_files=case["expected_files"],
                        ),
                        "latency_p50_ns": percentile(search_latencies, 0.50),
                        "latency_p95_ns": percentile(search_latencies, 0.95),
                        "stable_across_repetitions": search_stable,
                    },
                    "graph": {
                        **graph_observation(
                            graph_result,
                            expected_project=graph_projects_by_root[case["root"]],
                            expected_files=case["expected_files"],
                        ),
                        "latency_p50_ns": percentile(graph_latencies, 0.50),
                        "latency_p95_ns": percentile(graph_latencies, 0.95),
                        "stable_across_repetitions": graph_stable,
                    },
                }
            )

        search_bytes = directory_bytes(
            args.runtime_root / "code-search" / "code-search-storage"
        )
        graph_bytes = directory_bytes(
            args.runtime_root
            / "code-graph"
            / "home"
            / ".cache"
            / "codebase-memory-mcp"
        )
        total_lines = sum(case["stats"]["utf8_text_lines"] for case in cases)
        result = {
            "schema_version": 1,
            "status": "completed",
            "measurement_id": "released-backends-multi-repository-v1",
            "started_at": started,
            "finished_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "case_count": len(cases),
            "language_model_calls": 0,
            "provenance": {
                "plugin_revision": git(ROOT, "rev-parse", "HEAD"),
                "runner_sha256": sha256_file(Path(__file__).resolve()),
                "cases_sha256": sha256_file(args.cases.resolve()),
                "oracle_sha256": sha256_file(args.oracle.resolve()),
            },
            "servers": {
                "code-search_sha256": sha256_file(args.search_server),
                "code-graph_sha256": sha256_file(args.graph_server),
            },
            "indexing": indexing,
            "observations": observations,
            "storage": {
                "source_utf8_text_lines": total_lines,
                "search_index_bytes": search_bytes,
                "graph_index_bytes": graph_bytes,
                "combined_index_bytes": search_bytes + graph_bytes,
                "combined_index_bytes_per_utf8_line": (
                    (search_bytes + graph_bytes) / total_lines
                    if total_lines
                    else None
                ),
            },
            "interpretation": {
                "multi_repository_direct_query_demonstrated": True,
                "organization_fleet_or_acl_model_demonstrated": False,
                "result_scope": (
                    "direct discovery across isolated local indexes; claims still "
                    "require project-bound evidence"
                ),
            },
        }
        result["result_sha256"] = hashlib.sha256(canonical_json(result)).hexdigest()
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        for case in cases:
            if git(
                case["root"], "status", "--porcelain=v1", "--untracked-files=all"
            ):
                raise MultiRepoError(f"{case['case_id']}: measurement dirtied checkout")
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"Multi-repository measurement FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        search.close()
        graph.close()


if __name__ == "__main__":
    raise SystemExit(main())
