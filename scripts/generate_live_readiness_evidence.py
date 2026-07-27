#!/usr/bin/env python3
"""Generate fail-closed readiness evidence from two installed MCP servers.

The smoke test copies a small, committed fixture into a fresh Git checkout,
indexes that exact checkout with both servers over stdio MCP, verifies their
terminal readiness and v1 identities, and writes evidence only if neither
server changed the checkout.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time


IDENTITY_FIELDS = (
    "repository_id",
    "checkout_id",
    "source_revision",
    "dirty_fingerprint",
    "index_generation",
    "captured_at",
)
EQUAL_IDENTITY_FIELDS = IDENTITY_FIELDS[:-1]
SECRET_ENVIRONMENT_NAMES = (
    "CODE_INTEL_COMPONENT_TOKEN",
    "CODE_INTEL_LIVE_READINESS_EVIDENCE",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


class SmokeError(RuntimeError):
    """The installed components did not satisfy the readiness contract."""


def load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeError(f"{path}: expected a JSON object")
    return value


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in SECRET_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def run_git(checkout: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            env=sanitized_environment(),
            text=True,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        diagnostic = getattr(exc, "stderr", "") or str(exc)
        raise SmokeError(f"Git fixture setup failed: {diagnostic.strip()}") from exc
    return completed.stdout.strip()


def initialize_checkout(checkout: Path) -> None:
    run_git(checkout, "init", "--quiet")
    run_git(checkout, "config", "user.name", "Readiness Smoke")
    run_git(checkout, "config", "user.email", "readiness-smoke.invalid")
    run_git(checkout, "add", "--all")
    run_git(
        checkout,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "readiness smoke fixture",
    )


def working_tree_digest(checkout: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (
            candidate
            for candidate in checkout.rglob("*")
            if ".git" not in candidate.relative_to(checkout).parts
        ),
        key=lambda candidate: candidate.relative_to(checkout).as_posix(),
    ):
        relative = path.relative_to(checkout).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0")
            digest.update(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        elif path.is_dir():
            digest.update(b"directory\0")
        digest.update(b"\0")
    return digest.hexdigest()


def checkout_state(checkout: Path) -> tuple[str, str, str]:
    return (
        run_git(checkout, "rev-parse", "HEAD"),
        run_git(checkout, "status", "--porcelain=v1", "--untracked-files=all"),
        working_tree_digest(checkout),
    )


def _read_messages(stream, messages: queue.Queue, diagnostics: list[str]) -> None:
    for raw_line in iter(stream.readline, ""):
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            diagnostics.append(line)
            continue
        if isinstance(message, dict):
            messages.put(message)


class MCPClient:
    """Minimal persistent stdio MCP client for the readiness smoke."""

    def __init__(self, component: str, command: str, deadline: float):
        self.component = component
        self.deadline = deadline
        self.messages: queue.Queue = queue.Queue()
        self.diagnostics: list[str] = []
        self.stderr_lines: list[str] = []
        self.next_id = 1
        try:
            self.process = subprocess.Popen(
                [command],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=sanitized_environment(),
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise SmokeError(f"cannot start {component} server {command}: {exc}") from exc
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(
            target=_read_messages,
            args=(self.process.stdout, self.messages, self.diagnostics),
            daemon=True,
        ).start()
        threading.Thread(
            target=lambda: self.stderr_lines.extend(self.process.stderr.readlines()),
            daemon=True,
        ).start()
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"roots": {"listChanged": False}},
                "clientInfo": {
                    "name": "codebase-search-readiness-smoke",
                    "version": "1",
                },
            },
        )
        self.notify("notifications/initialized", {})

    def send(self, message: dict) -> None:
        if self.process.stdin is None:
            raise SmokeError(f"{self.component}: MCP stdin is unavailable")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        while time.monotonic() < self.deadline:
            if self.process.poll() is not None:
                raise SmokeError(
                    f"{self.component}: server exited with status "
                    f"{self.process.returncode} while waiting for {method}"
                )
            try:
                message = self.messages.get(
                    timeout=max(0.01, min(0.2, self.deadline - time.monotonic()))
                )
            except queue.Empty:
                continue
            if message.get("method") == "roots/list" and "id" in message:
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "result": {"roots": []},
                    }
                )
                continue
            if message.get("method") == "ping" and "id" in message:
                self.send(
                    {"jsonrpc": "2.0", "id": message["id"], "result": {}}
                )
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise SmokeError(
                    f"{self.component}: {method} failed: "
                    f"{json.dumps(message['error'], sort_keys=True)}"
                )
            result = message.get("result")
            if not isinstance(result, dict):
                raise SmokeError(f"{self.component}: {method} returned no object")
            return result
        raise SmokeError(f"{self.component}: timed out waiting for {method}")

    def list_tools(self) -> dict[str, dict]:
        result = self.request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise SmokeError(f"{self.component}: tools/list omitted its tools array")
        by_name: dict[str, dict] = {}
        for tool in tools:
            if not isinstance(tool, dict):
                raise SmokeError(f"{self.component}: malformed tools/list entry")
            name = tool.get("name")
            schema = tool.get("inputSchema")
            if not isinstance(name, str) or not isinstance(schema, dict):
                raise SmokeError(f"{self.component}: malformed tools/list entry")
            if name in by_name:
                raise SmokeError(f"{self.component}: duplicate tool {name}")
            by_name[name] = schema
        return by_name

    def call_tool(self, name: str, arguments: dict) -> dict:
        result = self.request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if result.get("isError") is True:
            raise SmokeError(f"{self.component}: tool {name} reported isError")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise SmokeError(
                f"{self.component}: tool {name} returned no unambiguous object"
            )
        item = content[0]
        if not isinstance(item, dict) or item.get("type") != "text":
            raise SmokeError(
                f"{self.component}: tool {name} returned unsupported content"
            )
        try:
            payload = json.loads(item.get("text", ""))
        except json.JSONDecodeError as exc:
            raise SmokeError(
                f"{self.component}: tool {name} returned non-JSON text"
            ) from exc
        if not isinstance(payload, dict):
            raise SmokeError(
                f"{self.component}: tool {name} returned non-object JSON"
            )
        return payload

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def empty_error(value) -> bool:
    return value in (None, "", [])


def require_binding(payload: dict, job_id: str, root: str, project: str) -> None:
    required = {
        "job_id": job_id,
        "directory": root,
        "project_name": project,
    }
    mismatches = [
        f"{field}={payload.get(field)!r}, expected {expected!r}"
        for field, expected in required.items()
        if payload.get(field) != expected
    ]
    if mismatches:
        raise SmokeError("semantic job binding mismatch: " + "; ".join(mismatches))


def validate_identity(component: str, payload: dict) -> dict:
    identity = payload.get("index_identity")
    if not isinstance(identity, dict) or identity.get("schema_version") != 1:
        raise SmokeError(f"{component}: missing v1 index_identity")
    for field in IDENTITY_FIELDS:
        if not isinstance(identity.get(field), str) or not identity[field]:
            raise SmokeError(f"{component}: invalid index_identity.{field}")
    expected_generation = hashlib.sha256(
        (
            identity["repository_id"]
            + "\0"
            + identity["source_revision"]
            + "\0"
            + identity["dirty_fingerprint"]
        ).encode("utf-8")
    ).hexdigest()
    if identity["index_generation"] != expected_generation:
        raise SmokeError(f"{component}: invalid index_generation")
    try:
        captured = datetime.fromisoformat(identity["captured_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise SmokeError(f"{component}: invalid captured_at timestamp") from exc
    if captured.utcoffset() is None or captured.utcoffset().total_seconds() != 0:
        raise SmokeError(f"{component}: captured_at is not UTC")
    return identity


def run_smoke(
    bom: dict,
    fixture: Path,
    servers: dict[str, str],
    output: Path,
    timeout: float,
) -> None:
    readiness = bom.get("integrated_readiness")
    if not isinstance(readiness, dict) or readiness.get("status") != "ready":
        raise SmokeError("component BOM integrated readiness is not ready")
    components = bom.get("components")
    if not isinstance(components, dict):
        raise SmokeError("component BOM components are missing")
    try:
        search_version = components["code-search"]["install"]["revision"]
        graph_version = components["code-graph"]["install"]["tag"]
    except (KeyError, TypeError) as exc:
        raise SmokeError("component BOM versions are malformed") from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="code-intel-readiness-", dir=output.parent
    ) as temporary:
        checkout = Path(temporary) / "target-repo"
        shutil.copytree(fixture, checkout)
        initialize_checkout(checkout)
        before = checkout_state(checkout)
        if before[1]:
            raise SmokeError("fresh readiness fixture checkout is dirty")

        deadline = time.monotonic() + timeout
        clients: dict[str, MCPClient] = {}
        try:
            for component in ("code-search", "code-graph"):
                clients[component] = MCPClient(
                    component, servers[component], deadline
                )
            search = clients["code-search"]
            graph = clients["code-graph"]
            search_tools = search.list_tools()
            graph_tools = graph.list_tools()
            for tool in (
                "index_directory",
                "get_indexing_progress",
                "get_index_status",
            ):
                if tool not in search_tools:
                    raise SmokeError(f"code-search: required tool {tool} is absent")
            status_schema = search_tools["get_index_status"]
            project_path_schema = status_schema.get("properties", {}).get(
                "project_path"
            )
            status_required = status_schema.get("required", [])
            if (
                not isinstance(project_path_schema, dict)
                or project_path_schema.get("type") != "string"
                or not isinstance(status_required, list)
                or "project_path" in status_required
            ):
                raise SmokeError(
                    "code-search: live get_index_status schema lacks optional "
                    "string project_path"
                )
            for tool in ("index_repository", "index_status"):
                if tool not in graph_tools:
                    raise SmokeError(f"code-graph: required tool {tool} is absent")
            skip_report = (
                graph_tools["index_repository"]
                .get("properties", {})
                .get("skip_report")
            )
            if not isinstance(skip_report, dict) or skip_report.get("type") != "boolean":
                raise SmokeError(
                    "code-graph: live index_repository schema lacks boolean skip_report"
                )

            root = str(checkout.resolve())
            project = checkout.name
            started = search.call_tool(
                "index_directory", {"directory_path": root}
            )
            if (
                started.get("status") != "indexing"
                or not isinstance(started.get("job_id"), str)
                or not started["job_id"]
                or started.get("directory") != root
                or started.get("project_name") != project
                or started.get("index_ready") is not False
                or started.get("message")
                != (
                    "Indexing started in background. Use "
                    "get_indexing_progress to check status."
                )
                or "requested_directory" in started
                or "indexing_conflict" in started
            ):
                raise SmokeError("code-search: incompatible new-job response")
            job_id = started["job_id"]

            completion: dict | None = None
            while time.monotonic() < deadline:
                progress = search.call_tool("get_indexing_progress", {})
                require_binding(progress, job_id, root, project)
                status = progress.get("status")
                if status == "indexing":
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                if status != "completed":
                    raise SmokeError(
                        f"code-search: unexpected terminal status {status!r}"
                    )
                result = progress.get("result")
                if (
                    not isinstance(result, dict)
                    or result.get("success") is not True
                    or progress.get("index_ready") is not True
                    or result.get("index_ready") is not True
                    or not empty_error(progress.get("error"))
                    or not empty_error(result.get("error"))
                ):
                    raise SmokeError(
                        "code-search: completed job failed readiness gates"
                    )
                completion = result
                break
            if completion is None:
                raise SmokeError("code-search: indexing timed out")

            graph_result = graph.call_tool(
                "index_repository",
                {"repo_path": root, "skip_report": True},
            )
            if (
                not empty_error(graph_result.get("error"))
                or graph_result.get("status") == "degraded"
                or graph_result.get("identity_status") != "captured"
            ):
                raise SmokeError(
                    "code-graph: index_repository returned a degraded result"
                )
            graph_project = graph_result.get("project")
            if not isinstance(graph_project, str) or not graph_project:
                raise SmokeError("code-graph: index_repository omitted project")
            graph_completion_identity = validate_identity(
                "code-graph index_repository", graph_result
            )

            search_status = search.call_tool(
                "get_index_status", {"project_path": root}
            )
            if (
                search_status.get("index_ready") is not True
                or search_status.get("index_identity_status") != "ready"
                or not empty_error(search_status.get("error"))
            ):
                raise SmokeError("code-search: final status is not exactly ready")
            graph_status = graph.call_tool(
                "index_status", {"project": graph_project}
            )
            if (
                graph_status.get("status") != "ready"
                or graph_status.get("identity_status") != "captured"
                or not empty_error(graph_status.get("error"))
            ):
                raise SmokeError("code-graph: final status is not exactly ready")
            search_identity = validate_identity("code-search", search_status)
            graph_identity = validate_identity("code-graph", graph_status)
            if any(
                graph_completion_identity[field] != graph_identity[field]
                for field in EQUAL_IDENTITY_FIELDS
            ):
                raise SmokeError(
                    "code-graph: completion and final identities differ"
                )
            mismatches = [
                field
                for field in EQUAL_IDENTITY_FIELDS
                if search_identity[field] != graph_identity[field]
            ]
            if mismatches:
                raise SmokeError(
                    "component index identities differ: " + ", ".join(mismatches)
                )
        finally:
            for client in clients.values():
                client.close()

        after = checkout_state(checkout)
        if after != before:
            raise SmokeError("installed servers changed the readiness checkout")
        evidence = {
            "schema_version": 1,
            "producer": "scripts/generate_live_readiness_evidence.py:v1",
            "components": {
                "code-search": {
                    "version": search_version,
                    "completion": completion,
                    "index_ready": True,
                    "index_identity": search_identity,
                },
                "code-graph": {
                    "version": graph_version,
                    "status": "ready",
                    "index_identity": graph_identity,
                },
            },
            "checkout_unchanged": True,
        }
        output.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def parse_servers(values: list[str]) -> dict[str, str]:
    servers: dict[str, str] = {}
    for value in values:
        component, separator, command = value.partition("=")
        if not separator or not component or not command:
            raise SmokeError(
                f"invalid --server {value!r}; expected COMPONENT=EXECUTABLE"
            )
        if component in servers:
            raise SmokeError(f"duplicate server component {component}")
        servers[component] = command
    expected = {"code-search", "code-graph"}
    if set(servers) != expected:
        raise SmokeError(
            "servers must exactly match components: " + ", ".join(sorted(expected))
        )
    return servers


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate integrated readiness evidence from installed MCPs"
    )
    parser.add_argument("--component-bom", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--server", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()
    try:
        if args.timeout <= 0:
            raise SmokeError("--timeout must be positive")
        if not args.fixture.is_dir():
            raise SmokeError(f"fixture directory is missing: {args.fixture}")
        run_smoke(
            load_json(args.component_bom),
            args.fixture.resolve(),
            parse_servers(args.server),
            args.output.resolve(),
            args.timeout,
        )
    except (OSError, SmokeError) as exc:
        print(f"Readiness smoke FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Readiness smoke passed; evidence written to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
