"""Enforce at-most-once successful traces and pilot-required exactly once."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MODES = {
    "pre-tool-use",
    "pre-terminal-output",
    "post-tool-use",
    "post-tool-failure",
    "stop",
}


def _load_input() -> dict[str, Any]:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise TypeError("hook input must be an object")
    session_id = value.get("session_id")
    if not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("hook input has an invalid session_id")
    return value


def _state_root() -> Path:
    configured = os.environ.get("CODE_INTEL_TRACE_GUARD_ROOT")
    root = (
        Path(configured)
        if configured
        else Path(tempfile.gettempdir()) / "code-intel-trace-guard"
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("trace guard state root is unsafe")
    return root


def _read_state(path: Path) -> dict[str, str] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("status") not in {"pending", "completed"}
        or not isinstance(value.get("tool_use_id"), str)
    ):
        raise ValueError("trace guard state is invalid")
    return value


def _write_state(
    path: Path,
    value: dict[str, str],
    *,
    create_only: bool = False,
) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if create_only:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            temporary.unlink()
            raise
        else:
            os.close(descriptor)
    os.replace(temporary, path)


def _block(reason: str) -> int:
    print(reason, file=sys.stderr)
    return 2


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in MODES:
        print(
            "usage: code_intel_trace_guard.py "
            "pre-tool-use|pre-terminal-output|post-tool-use|"
            "post-tool-failure|stop",
            file=sys.stderr,
        )
        return 2
    try:
        mode = sys.argv[1]
        value = _load_input()
        if mode == "stop" and value.get("stop_hook_active") is True:
            return 0
        current = value.get("tool_use_id")
        if mode != "stop" and (not isinstance(current, str) or not current):
            raise ValueError("hook input omitted tool_use_id")
        root = _state_root()
        state_path = root / f"{value['session_id']}.json"
        state = _read_state(state_path)
        if mode == "pre-terminal-output":
            if state is not None and state.get("status") == "completed":
                return 0
            return _block(
                "Call trace_call_path exactly once as required before returning "
                "structured output"
            )
        if mode == "pre-tool-use":
            if state is None:
                try:
                    _write_state(
                        state_path,
                        {"status": "pending", "tool_use_id": current},
                        create_only=True,
                    )
                    return 0
                except FileExistsError:
                    state = _read_state(state_path)
            if state == {"status": "pending", "tool_use_id": current}:
                return 0
            return _block(
                "trace_call_path is allowed exactly once; use the existing "
                "trace result instead of calling it again"
            )
        if mode == "post-tool-use":
            if state != {"status": "pending", "tool_use_id": current}:
                raise ValueError("successful trace has no matching pending state")
            _write_state(
                state_path,
                {"status": "completed", "tool_use_id": current},
            )
            return 0
        if mode == "post-tool-failure":
            if state == {"status": "pending", "tool_use_id": current}:
                state_path.unlink()
            return 0
        if state is None or state.get("status") != "completed":
            return _block(
                "Call trace_call_path exactly once as required before stopping, "
                "then finish from that trace and the pinned source evidence"
            )
        state_path.unlink()
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _block(f"code-intel trace guard failed closed: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
