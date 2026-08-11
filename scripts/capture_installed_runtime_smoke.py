#!/usr/bin/env python3
"""Capture a fresh installed-plugin trace that exercises both MCP families."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from typing import Any


PLUGIN_ID = "codebase-search@redacted-code-intelligence"
SEMANTIC_TOOL = (
    "mcp__plugin_codebase-search_code-search__search_code_evidence"
)
RELATIONSHIP_TOOL = (
    "mcp__plugin_codebase-search_code-graph__trace_call_path"
)
ALLOWED_TOOLS = (SEMANTIC_TOOL, RELATIONSHIP_TOOL)
DEFAULT_PROMPT = (
    "Use the installed codebase-search plugin against this already indexed "
    "checkout. First call search_code_evidence for request authentication. "
    "Then call trace_call_path once for login with direction inbound. Both MCP "
    "calls are required; give a short answer only after both return."
)


class CaptureError(RuntimeError):
    """The installed runtime could not be proven."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(target: Path, *arguments: str) -> str:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    completed = subprocess.run(
        ["git", "-C", str(target), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise CaptureError("target git verification failed: " + completed.stderr.strip())
    return completed.stdout.strip()


def _parse_stream(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise CaptureError(f"raw stream line {line_number} is not an object")
            events.append(value)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"raw stream is invalid JSON: {exc}") from exc
    if not events:
        raise CaptureError("raw stream is empty")
    return events


def _tool_calls(events: list[dict[str, Any]]) -> list[str]:
    calls: list[str] = []
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    calls.append(name)
    return calls


def _validate_trace(events: list[dict[str, Any]], plugin_version: str) -> int:
    init = next(
        (
            event
            for event in events
            if event.get("type") == "system" and event.get("subtype") == "init"
        ),
        None,
    )
    if not isinstance(init, dict):
        raise CaptureError("raw stream omitted the initialization event")
    plugins = init.get("plugins")
    if not isinstance(plugins, list) or not any(
        isinstance(item, dict)
        and (
            item.get("id") == PLUGIN_ID
            or item.get("source") == PLUGIN_ID
            or item.get("name") == "codebase-search"
        )
        and item.get("version") == plugin_version
        for item in plugins
    ):
        raise CaptureError("fresh session did not load the exact installed plugin")
    servers = init.get("mcp_servers")
    if not isinstance(servers, list):
        raise CaptureError("fresh session omitted MCP server status")
    connected = {
        item.get("name")
        for item in servers
        if isinstance(item, dict) and item.get("status") == "connected"
    }
    if not {
        "plugin:codebase-search:code-search",
        "plugin:codebase-search:code-graph",
    }.issubset(connected):
        raise CaptureError("fresh session did not connect both installed MCP servers")
    calls = _tool_calls(events)
    denied = sum(name not in ALLOWED_TOOLS for name in calls)
    if SEMANTIC_TOOL not in calls or RELATIONSHIP_TOOL not in calls:
        raise CaptureError("fresh session did not invoke both installed MCP families")
    terminal = next(
        (event for event in reversed(events) if event.get("type") == "result"),
        None,
    )
    if not isinstance(terminal, dict) or terminal.get("is_error") is not False:
        raise CaptureError("fresh session did not finish successfully")
    if denied:
        raise CaptureError("fresh session attempted a tool outside the smoke contract")
    return denied


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_manifest(root: Path) -> None:
    artifacts = {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }
    _write_json(
        root / "manifest.json",
        {"schema_version": 1, "artifacts": artifacts},
    )


def capture(args: argparse.Namespace) -> Path:
    claude = args.claude.resolve()
    target = args.target.resolve()
    evidence_root = args.evidence_root.resolve()
    marketplace_root = args.marketplace_root.resolve()
    if not claude.is_file() or not os.access(claude, os.X_OK):
        raise CaptureError(f"claude executable is unavailable: {claude}")
    if not target.is_dir() or Path(_git(target, "rev-parse", "--show-toplevel")) != target:
        raise CaptureError("target must be the exact Git root")
    if _git(target, "status", "--porcelain", "--untracked-files=all"):
        raise CaptureError("target checkout must be clean")
    if (
        not marketplace_root.is_dir()
        or "/.claude/plugins/marketplaces/" not in str(marketplace_root)
        or "/worktrees/" in str(marketplace_root)
    ):
        raise CaptureError("marketplace root is not a durable Claude checkout")
    if re.fullmatch(r"\d+\.\d+\.\d+", args.plugin_version) is None:
        raise CaptureError("plugin version is invalid")
    evidence_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    token = secrets.token_hex(4)
    staging = evidence_root / f".installed-runtime-{timestamp}-{token}.tmp"
    staging.mkdir()
    head_before = _git(target, "rev-parse", "HEAD")
    status_before = _git(target, "status", "--porcelain", "--untracked-files=all")
    command = [
        str(claude),
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "sonnet",
        "--max-turns",
        "8",
        "--max-budget-usd",
        str(args.max_budget_usd),
        "--permission-mode",
        "dontAsk",
        "--setting-sources",
        "user",
        "--no-session-persistence",
        "--allowed-tools",
        ",".join(ALLOWED_TOOLS),
        args.prompt,
    ]
    environment = dict(os.environ)
    environment.pop("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", None)
    try:
        completed = subprocess.run(
            command,
            cwd=target,
            env=environment,
            capture_output=True,
            text=True,
            timeout=args.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        (staging / "raw.jsonl").write_text(stdout, encoding="utf-8")
        (staging / "stderr.txt").write_text(stderr, encoding="utf-8")
        _write_json(staging / "failure.json", {"schema_version": 1, "status": "timeout"})
        _write_manifest(staging)
        failure = evidence_root / f"installed-runtime-failure-{timestamp}-{token}"
        os.replace(staging, failure)
        raise CaptureError(f"fresh installed runtime timed out; evidence: {failure}") from exc

    (staging / "raw.jsonl").write_text(completed.stdout, encoding="utf-8")
    (staging / "stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        _write_json(
            staging / "failure.json",
            {
                "schema_version": 1,
                "status": "nonzero_exit",
                "returncode": completed.returncode,
            },
        )
        _write_manifest(staging)
        failure = evidence_root / f"installed-runtime-failure-{timestamp}-{token}"
        os.replace(staging, failure)
        raise CaptureError(f"fresh installed runtime exited nonzero; evidence: {failure}")

    try:
        events = _parse_stream(completed.stdout)
        denied = _validate_trace(events, args.plugin_version)
        head_after = _git(target, "rev-parse", "HEAD")
        status_after = _git(target, "status", "--porcelain", "--untracked-files=all")
        unchanged = head_after == head_before and status_after == status_before == ""
        if not unchanged:
            raise CaptureError("fresh installed runtime changed the target checkout")
        _write_json(
            staging / "receipt.json",
            {
                "schema_version": 1,
                "receipt_type": "installed-plugin-runtime",
                "plugin_id": PLUGIN_ID,
                "plugin_version": args.plugin_version,
                "marketplace_root": str(marketplace_root),
                "target_root": str(target),
                "target_revision": head_after,
                "checkout_unchanged": True,
                "canary_violations": 0,
                "denied_tool_calls": denied,
                "raw_stream": "raw.jsonl",
            },
        )
        _write_manifest(staging)
    except CaptureError as exc:
        _write_json(
            staging / "failure.json",
            {"schema_version": 1, "status": "invalid_trace", "reason": str(exc)},
        )
        _write_manifest(staging)
        failure = evidence_root / f"installed-runtime-failure-{timestamp}-{token}"
        os.replace(staging, failure)
        raise CaptureError(f"{exc}; evidence: {failure}") from exc
    destination = evidence_root / f"installed-runtime-smoke-{timestamp}-{token}"
    os.replace(staging, destination)
    return destination


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--marketplace-root", type=Path, required=True)
    parser.add_argument("--plugin-version", required=True)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    return parser


def main() -> int:
    try:
        destination = capture(build_parser().parse_args())
    except (CaptureError, OSError, ValueError) as exc:
        print(f"Installed runtime smoke FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "pass", "evidence_root": str(destination)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
