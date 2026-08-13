#!/usr/bin/env python3
"""Measure clean, no-op, and one-file-update index lifecycle costs."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.public_measure.run import (  # noqa: E402
    MCPClient,
    index_graph,
    index_search,
    isolated_environment,
    repository_stats,
    sha256_file,
)
from bench.scale_measure import (  # noqa: E402
    COMPONENTS,
    query_graph,
    query_search,
    selected_components,
)


class LifecycleError(RuntimeError):
    """A measurement precondition or observed lifecycle outcome failed."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def git(root: Path, *arguments: str, timeout: float = 900) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            check=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        diagnostic = getattr(exc, "stderr", "") or str(exc)
        raise LifecycleError(
            f"git {' '.join(arguments)} failed: {diagnostic.strip()}"
        ) from exc
    return completed.stdout.strip()


def validate_checkout(root: Path) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise LifecycleError("repository must be a real directory")
    revision = git(root, "rev-parse", "HEAD")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise LifecycleError("repository must be clean, including untracked files")
    try:
        origin = git(root, "remote", "get-url", "origin")
    except LifecycleError:
        origin = None
    return {
        "revision": revision,
        "origin": origin,
        **repository_stats(root),
    }


def copy_checkout(source: Path, destination: Path, expected: dict) -> None:
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
            capture_output=True,
            check=True,
            text=True,
            timeout=1800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        diagnostic = getattr(exc, "stderr", "") or str(exc)
        raise LifecycleError(f"copy checkout failed: {diagnostic.strip()}") from exc
    git(destination, "checkout", "--quiet", "--detach", expected["revision"])
    if expected.get("origin"):
        git(destination, "remote", "set-url", "origin", expected["origin"])
    observed = validate_checkout(destination)
    for field in (
        "revision",
        "tracked_files",
        "tracked_bytes",
        "utf8_text_bytes",
        "utf8_text_lines",
        "tracked_manifest_sha256",
    ):
        if observed[field] != expected[field]:
            raise LifecycleError(f"copied checkout differs at {field}")


def storage_usage(path: Path) -> dict[str, int]:
    """Return logical and allocated file bytes without following symlinks."""
    if not path.exists():
        return {"files": 0, "logical_bytes": 0, "allocated_bytes": 0}
    files = logical = allocated = 0
    for item in path.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        stat = item.stat()
        files += 1
        logical += stat.st_size
        allocated += getattr(stat, "st_blocks", 0) * 512
    return {
        "files": files,
        "logical_bytes": logical,
        "allocated_bytes": allocated,
    }


def component_storage_roots(component: str, runtime_root: Path) -> tuple[Path, Path]:
    """Return (index payload, total component runtime) storage roots."""
    if component == "code-search":
        runtime = runtime_root / "code-search-storage"
        return runtime / "projects", runtime
    if component == "code-graph":
        runtime = runtime_root / "home" / ".cache" / "codebase-memory-mcp"
        return runtime, runtime
    raise LifecycleError(f"unknown component: {component}")


def _comment_prefix(path: Path) -> bytes:
    if path.suffix.lower() in {
        ".py",
        ".pyi",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
    }:
        return b"#"
    if path.suffix.lower() in {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
    }:
        return b"//"
    raise LifecycleError(f"unsupported mutation file type: {path.suffix}")


@contextmanager
def temporary_mutation(
    root: Path,
    relative_path: str,
    marker: str,
) -> Iterator[dict[str, object]]:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise LifecycleError("mutation file must be a safe relative path")
    target = root / relative
    if target.is_symlink() or not target.is_file():
        raise LifecycleError("mutation file must be a regular tracked file")
    tracked = set(git(root, "ls-files", "--", relative.as_posix()).splitlines())
    if relative.as_posix() not in tracked:
        raise LifecycleError("mutation file must be tracked")
    original = target.read_bytes()
    separator = b"" if not original or original.endswith(b"\n") else b"\n"
    addition = separator + _comment_prefix(target) + b" " + marker.encode("ascii") + b"\n"
    modified = original + addition
    target.write_bytes(modified)
    metadata: dict[str, object] = {
        "relative_path": relative.as_posix(),
        "marker_sha256": hashlib.sha256(marker.encode("ascii")).hexdigest(),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "modified_sha256": hashlib.sha256(modified).hexdigest(),
        "bytes_added": len(addition),
    }
    try:
        yield metadata
    finally:
        target.write_bytes(original)
        if hashlib.sha256(target.read_bytes()).hexdigest() != metadata["original_sha256"]:
            raise LifecycleError("mutation file was not restored exactly")


def validate_phase_result(component: str, phase: str, result: dict) -> None:
    if component == "code-search":
        fields = tuple(result.get(name) for name in (
            "files_added", "files_modified", "files_removed"
        ))
        if not all(isinstance(value, int) for value in fields):
            raise LifecycleError("code-search omitted observed file deltas")
        added, modified, removed = fields
        if phase == "clean" and added <= 0:
            raise LifecycleError("code-search clean phase did not add files")
        if phase == "no_op" and (added, modified, removed) != (0, 0, 0):
            raise LifecycleError("code-search no-op phase observed source changes")
        if phase == "small_change" and (added, modified, removed) != (0, 1, 0):
            raise LifecycleError("code-search small-change phase was not one-file")
        return
    if component != "code-graph":
        raise LifecycleError(f"unknown component: {component}")
    delta = result.get("index_delta")
    if not isinstance(delta, dict):
        raise LifecycleError("code-graph omitted index_delta")
    mode = delta.get("mode")
    discovered = delta.get("files_discovered")
    changed = delta.get("files_changed")
    unchanged = delta.get("files_unchanged")
    if not all(isinstance(value, int) for value in (discovered, changed, unchanged)):
        raise LifecycleError("code-graph index_delta is incomplete")
    expected_mode = {"clean": "full", "no_op": "noop", "small_change": "incremental"}[phase]
    if mode != expected_mode:
        raise LifecycleError(
            f"code-graph {phase} selected {mode!r}, expected {expected_mode!r}"
        )
    if discovered <= 0 or discovered != changed + unchanged:
        raise LifecycleError("code-graph file delta totals are incoherent")
    if phase == "clean" and (changed <= 0 or unchanged != 0):
        raise LifecycleError("code-graph clean phase was not a full initial index")
    if phase == "no_op" and (changed != 0 or unchanged != discovered):
        raise LifecycleError("code-graph no-op phase observed source changes")
    if phase == "small_change" and (changed != 1 or unchanged <= 0):
        raise LifecycleError("code-graph small-change phase was not one-file")


def validate_comment_only_graph_cardinality(clean: dict, update: dict) -> None:
    expected = (clean.get("nodes"), clean.get("edges"))
    observed = (update.get("nodes"), update.get("edges"))
    if not all(isinstance(value, int) for value in (*expected, *observed)):
        raise LifecycleError("code-graph omitted cardinality observations")
    if observed != expected:
        raise LifecycleError(
            "code-graph comment-only update changed graph cardinality: "
            f"clean={expected}, update={observed}"
        )


def _uncapped_rows(result: dict, label: str) -> list[dict]:
    rows = result.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise LifecycleError(f"code-graph {label} query omitted rows")
    if result.get("capped") is True:
        raise LifecycleError(f"code-graph {label} query was capped")
    return rows


def graph_semantic_fingerprint(client: MCPClient, project: str) -> dict:
    """Hash canonical QN nodes and edges, excluding volatile properties."""
    schema = client.call_tool("get_graph_schema", {})
    projects = schema.get("projects")
    if not isinstance(projects, list):
        raise LifecycleError("code-graph schema omitted projects")
    selected = next(
        (
            item
            for item in projects
            if isinstance(item, dict) and item.get("project") == project
        ),
        None,
    )
    if not isinstance(selected, dict) or not isinstance(selected.get("schema"), dict):
        raise LifecycleError("code-graph schema omitted selected project")
    relationship_types = selected["schema"].get("relationship_types")
    if not isinstance(relationship_types, list):
        raise LifecycleError("code-graph schema omitted relationship types")
    edge_types = sorted(
        item["type"]
        for item in relationship_types
        if isinstance(item, dict)
        and isinstance(item.get("type"), str)
        and item.get("count", 0) > 0
    )

    node_rows = _uncapped_rows(
        client.call_tool(
            "query_graph",
            {
                "project": project,
                "query": (
                    "MATCH (n) RETURN labels(n), n.qualified_name "
                    "ORDER BY n.qualified_name LIMIT 10000"
                ),
                "max_rows": 10000,
            },
        ),
        "node fingerprint",
    )
    nodes = sorted(
        (
            tuple(row.get("LABELS(n)", [])),
            row.get("n.qualified_name"),
        )
        for row in node_rows
    )
    if any(not qn or not labels for labels, qn in nodes):
        raise LifecycleError("code-graph node fingerprint contained malformed rows")

    edges: list[tuple[str, str, str]] = []
    for edge_type in edge_types:
        result = client.call_tool(
            "query_graph",
            {
                "project": project,
                "query": (
                    f"MATCH (a)-[r:{edge_type}]->(b) "
                    "RETURN a.qualified_name, b.qualified_name "
                    "ORDER BY a.qualified_name LIMIT 10000"
                ),
                "max_rows": 10000,
            },
        )
        for row in _uncapped_rows(result, f"{edge_type} fingerprint"):
            source = row.get("a.qualified_name")
            target = row.get("b.qualified_name")
            if not isinstance(source, str) or not isinstance(target, str):
                raise LifecycleError("code-graph edge fingerprint contained malformed rows")
            edges.append((source, edge_type, target))
    edges.sort()
    payload = {"nodes": nodes, "edges": edges}
    return {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(canonical_json(payload)).hexdigest(),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "relationship_types": edge_types,
    }


def _identity(result: dict) -> dict:
    identity = result.get("index_identity")
    if not isinstance(identity, dict):
        raise LifecycleError("backend omitted index identity")
    required = {
        "repository_id",
        "checkout_id",
        "source_revision",
        "dirty_fingerprint",
        "index_generation",
    }
    if not required <= identity.keys():
        raise LifecycleError("backend index identity is incomplete")
    return {name: identity[name] for name in sorted(required)}


def _phase(
    component: str,
    client: MCPClient,
    repository: Path,
    index_storage: Path,
    runtime_storage: Path,
    phase: str,
) -> tuple[dict, dict]:
    if component == "code-search":
        result, elapsed, peak = index_search(client, repository, incremental=True)
    else:
        result, elapsed, peak = index_graph(client, repository)
    validate_phase_result(component, phase, result)
    observation = {
        "index_elapsed_ns": elapsed,
        "peak_rss_bytes": peak,
        "index_storage": storage_usage(index_storage),
        "runtime_storage": storage_usage(runtime_storage),
        "index_identity": _identity(result),
    }
    if component == "code-search":
        observation["index_delta"] = {
            name: result.get(name)
            for name in (
                "files_added",
                "files_modified",
                "files_removed",
                "chunks_added",
                "chunks_removed",
            )
        }
    else:
        observation["index_delta"] = result["index_delta"]
        observation["nodes"] = result.get("nodes")
        observation["edges"] = result.get("edges")
    return result, observation


def _query(
    component: str,
    client: MCPClient,
    project: str | None,
    query: str,
    repetitions: int,
) -> dict:
    if component == "code-search":
        return query_search(client, query, repetitions)
    if not project:
        raise LifecycleError("code-graph omitted project identity")
    return query_graph(client, project, query, repetitions)


def measure_component(
    component: str,
    *,
    server: Path,
    repository: Path,
    runtime_root: Path,
    model: Path,
    mutation_file: str,
    query: str,
    repetitions: int,
    timeout: float,
) -> dict:
    environment = isolated_environment(runtime_root, model)
    deadline = time.monotonic() + timeout
    client = MCPClient(component, str(server), deadline, environment, repository)
    index_storage, runtime_storage = component_storage_roots(component, runtime_root)
    try:
        required = (
            {"index_directory", "get_indexing_progress", "search_code_evidence"}
            if component == "code-search"
            else {"index_repository", "code_localize"}
        )
        missing = required - set(client.list_tools())
        if missing:
            raise LifecycleError(f"{component}: missing tools {sorted(missing)}")
        clean_result, clean = _phase(
            component, client, repository, index_storage, runtime_storage, "clean"
        )
        project = clean_result.get("project") if component == "code-graph" else None
        if component == "code-graph":
            if not project:
                raise LifecycleError("code-graph omitted project identity")
            clean["semantic_fingerprint"] = graph_semantic_fingerprint(
                client, project
            )
        clean["query"] = _query(component, client, project, query, repetitions)

        _no_op_result, no_op = _phase(
            component, client, repository, index_storage, runtime_storage, "no_op"
        )

        marker = "code_intel_lifecycle_probe_20260812"
        with temporary_mutation(repository, mutation_file, marker) as mutation:
            _update_result, update = _phase(
                component,
                client,
                repository,
                index_storage,
                runtime_storage,
                "small_change",
            )
            if update["index_identity"]["dirty_fingerprint"] == "clean":
                raise LifecycleError("small-change identity was incorrectly clean")
            update["query"] = _query(
                component, client, project, query, repetitions
            )
            if component == "code-graph":
                validate_comment_only_graph_cardinality(clean, update)
                update["semantic_fingerprint"] = graph_semantic_fingerprint(
                    client, project
                )
                if update["semantic_fingerprint"] != clean["semantic_fingerprint"]:
                    raise LifecycleError(
                        "code-graph comment-only update changed canonical graph fingerprint"
                    )
        if git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
            raise LifecycleError("measurement checkout was not restored")
        return {
            "status": "completed",
            "server_sha256": sha256_file(server),
            "mutation": mutation,
            "phases": {
                "clean": clean,
                "no_op": no_op,
                "small_change": update,
            },
        }
    finally:
        client.close()


def summarize_components(components: dict) -> dict:
    cells: list[dict] = []
    maintenance: list[dict] = []
    summaries: dict[str, dict] = {}
    for component, observation in components.items():
        if observation.get("status", "completed") != "completed":
            continue
        phases = observation["phases"]
        for phase, values in phases.items():
            cell = {
                "component": component,
                "phase": phase,
                "elapsed_ns": values["index_elapsed_ns"],
            }
            cells.append(cell)
            if phase != "clean":
                maintenance.append(cell)
        clean = phases["clean"]["index_elapsed_ns"]
        summaries[component] = {
            "no_op_to_clean_ratio": phases["no_op"]["index_elapsed_ns"] / clean,
            "update_to_clean_ratio": phases["small_change"]["index_elapsed_ns"] / clean,
            "allocated_index_storage_after_clean": phases["clean"]["index_storage"]["allocated_bytes"],
            "allocated_index_storage_after_update": phases["small_change"]["index_storage"]["allocated_bytes"],
            "index_storage_growth_after_one_update": (
                phases["small_change"]["index_storage"]["allocated_bytes"]
                - phases["clean"]["index_storage"]["allocated_bytes"]
            ),
        }
    if not cells:
        return {"components": summaries}
    return {
        "components": summaries,
        "dominant_index_time_cell": max(cells, key=lambda item: item["elapsed_ns"]),
        "dominant_maintenance_time_cell": max(
            maintenance, key=lambda item: item["elapsed_ns"]
        ),
    }


def write_checkpoint(path: Path, document: dict) -> None:
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--mutation-file", required=True)
    parser.add_argument("--search-server", type=Path)
    parser.add_argument("--graph-server", type=Path)
    parser.add_argument("--component", action="append", choices=COMPONENTS)
    parser.add_argument("--local-model", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--query", default="parse source file diagnostics")
    parser.add_argument("--warm-repetitions", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=7200)
    args = parser.parse_args()
    if args.warm_repetitions < 1 or args.timeout <= 0:
        parser.error("repetitions and timeout must be positive")
    components = selected_components(args.component)
    servers = {"code-search": args.search_server, "code-graph": args.graph_server}
    for component in components:
        server = servers[component]
        if server is None:
            parser.error(f"{component} server is required")
        if not server.is_file() or not os.access(server, os.X_OK):
            parser.error(f"server is missing or not executable: {server}")
    if args.workspace.exists() or args.output.exists():
        parser.error("workspace and output must both be initially absent")
    source = args.repository.resolve()
    expected = validate_checkout(source)
    args.workspace.mkdir(parents=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    repository = args.workspace / "repository"
    copy_checkout(source, repository, expected)
    result = {
        "schema_version": 1,
        "measurement_id": "bounded-index-lifecycle-v1",
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "language_model_calls": 0,
        "repository": expected,
        "mutation_file": args.mutation_file,
        "warm_query_repetitions": args.warm_repetitions,
        "components": {},
    }
    for component in components:
        server = servers[component]
        assert server is not None
        try:
            result["components"][component] = measure_component(
                component,
                server=server.resolve(),
                repository=repository,
                runtime_root=args.workspace / "runtime" / component,
                model=args.local_model.resolve(),
                mutation_file=args.mutation_file,
                query=args.query,
                repetitions=args.warm_repetitions,
                timeout=args.timeout,
            )
        except Exception as exc:
            result["components"][component] = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "server_sha256": sha256_file(server),
            }
        write_checkpoint(args.output, result)
    result["summary"] = summarize_components(result["components"])
    result["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result["status"] = (
        "completed"
        if all(
            result["components"].get(component, {}).get("status") == "completed"
            for component in components
        )
        else "partial"
    )
    result["result_sha256"] = hashlib.sha256(canonical_json(result)).hexdigest()
    write_checkpoint(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
