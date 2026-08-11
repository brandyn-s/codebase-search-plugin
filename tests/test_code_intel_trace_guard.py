import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "code_intel_trace_guard.py"
TRACE_TOOL = "mcp__code-graph__trace_call_path"


class TraceGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = os.environ.copy()
        self.environment["CODE_INTEL_TRACE_GUARD_ROOT"] = self.temporary.name

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, mode: str, **overrides: object):
        event_names = {
            "pre-tool-use": "PreToolUse",
            "pre-terminal-output": "PreToolUse",
            "post-tool-use": "PostToolUse",
            "post-tool-failure": "PostToolUseFailure",
            "stop": "Stop",
        }
        payload = {
            "hook_event_name": event_names[mode],
            "session_id": "session-a",
            "tool_name": TRACE_TOOL,
            "tool_use_id": "trace-1",
            "stop_hook_active": False,
        }
        payload.update(overrides)
        return subprocess.run(
            [sys.executable, str(GUARD), mode],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def test_successful_trace_is_exactly_once_and_stop_cleans_state(self):
        first = self._run("pre-tool-use")
        success = self._run("post-tool-use")
        second = self._run("pre-tool-use", tool_use_id="trace-2")
        stop = self._run("stop", tool_use_id=None)
        next_session = self._run(
            "pre-tool-use",
            session_id="session-b",
            tool_use_id="trace-3",
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(second.returncode, 2)
        self.assertIn("exactly once", second.stderr)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(next_session.returncode, 0, next_session.stderr)

    def test_failed_trace_can_be_retried(self):
        first = self._run("pre-tool-use")
        failure = self._run("post-tool-failure")
        retry = self._run("pre-tool-use", tool_use_id="trace-2")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(failure.returncode, 0, failure.stderr)
        self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_stop_requires_one_trace_and_allows_hook_reentry(self):
        missing = self._run("stop", tool_use_id=None)
        self.assertEqual(self._run("pre-tool-use").returncode, 0)
        self.assertEqual(self._run("post-tool-use").returncode, 0)
        complete = self._run("stop", tool_use_id=None)
        reentry = self._run(
            "stop",
            tool_use_id=None,
            stop_hook_active=True,
        )

        self.assertEqual(missing.returncode, 2)
        self.assertIn("before stopping", missing.stderr)
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertEqual(reentry.returncode, 0, reentry.stderr)

    def test_terminal_output_requires_completed_trace(self):
        missing = self._run(
            "pre-terminal-output",
            tool_name="StructuredOutput",
            tool_use_id="output-1",
        )
        self.assertEqual(self._run("pre-tool-use").returncode, 0)
        pending = self._run(
            "pre-terminal-output",
            tool_name="StructuredOutput",
            tool_use_id="output-2",
        )
        self.assertEqual(self._run("post-tool-use").returncode, 0)
        complete = self._run(
            "pre-terminal-output",
            tool_name="StructuredOutput",
            tool_use_id="output-3",
        )

        self.assertEqual(missing.returncode, 2)
        self.assertIn("before returning structured output", missing.stderr)
        self.assertEqual(pending.returncode, 2)
        self.assertIn("before returning structured output", pending.stderr)
        self.assertEqual(complete.returncode, 0, complete.stderr)


if __name__ == "__main__":
    unittest.main()
