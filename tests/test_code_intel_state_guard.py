import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "code_intel_state_guard.py"


def canonical_evidence_ref() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "repository_id": "repo-1",
        "source_revision": "a" * 40,
        "index_generation": "b" * 64,
        "relative_path": "src/auth.py",
        "start_line": 7,
        "end_line": 9,
        "evidence_type": "semantic_match",
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        "id": "ev:v1:" + hashlib.sha256(encoded).hexdigest(),
        **payload,
    }


class CodeIntelStateGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.environment = dict(os.environ)
        self.environment["CODE_INTEL_STATE_GUARD_ROOT"] = str(
            Path(self.temporary.name) / "state"
        )

    def run_guard(
        self,
        mode: str,
        payload: dict[str, object],
        *,
        expected_route: str = "semantic",
        trace_direction: str = "none",
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(GUARD),
            mode,
            "--expected-route",
            expected_route,
        ]
        if trace_direction != "none":
            command.extend(["--trace-direction", trace_direction])
        return subprocess.run(
            command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=self.environment,
            check=False,
        )

    def test_backend_evidence_id_reaches_terminal_selection(self) -> None:
        evidence = canonical_evidence_ref()
        recorded = self.run_guard(
            "post-tool-use",
            {
                "session_id": "session-a",
                "tool_use_id": "tool-a",
                "tool_name": "mcp__code-search__search_code_evidence",
                "tool_input": {"query": "authentication"},
                "tool_response": {
                    "results": [
                        {
                            "span_role": "retrieval_context",
                            "context_span": {
                                "relative_path": "src/auth.py",
                                "start_line": 1,
                                "end_line": 20,
                            },
                            "evidence_candidates": [
                                {
                                    "role": "atomic_source_line",
                                    "evidence_ref": evidence,
                                }
                            ],
                        }
                    ]
                },
            },
        )
        terminal = self.run_guard(
            "pre-terminal-output",
            {
                "session_id": "session-a",
                "tool_use_id": "terminal-a",
                "tool_name": "StructuredOutput",
                "tool_input": {
                    "evidence_ids": [evidence["id"]],
                    "answer": f"Supported by {evidence['id']}",
                },
            },
        )

        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(terminal.returncode, 0, terminal.stderr)

    def test_required_trace_and_evidence_share_one_terminal_state(self) -> None:
        trace_input = {
            "session_id": "session-trace",
            "tool_use_id": "trace-a",
            "tool_name": "mcp__code-graph__trace_call_path",
            "tool_input": {"function_name": "authenticate", "direction": "inbound"},
        }
        trace_pre = self.run_guard(
            "pre-tool-use",
            trace_input,
            expected_route="graph",
            trace_direction="inbound",
        )
        trace_post = self.run_guard(
            "post-tool-use",
            {**trace_input, "tool_response": {"paths": []}},
            expected_route="graph",
            trace_direction="inbound",
        )
        evidence = canonical_evidence_ref()
        evidence_post = self.run_guard(
            "post-tool-use",
            {
                "session_id": "session-trace",
                "tool_use_id": "evidence-a",
                "tool_name": "mcp__code-graph__get_relationship_evidence",
                "tool_input": {"qualified_name": "authenticate"},
                "tool_response": {"relationships": [{"evidence_ref": evidence}]},
            },
            expected_route="graph",
            trace_direction="inbound",
        )
        terminal = self.run_guard(
            "pre-terminal-output",
            {
                "session_id": "session-trace",
                "tool_use_id": "terminal-trace",
                "tool_name": "StructuredOutput",
                "tool_input": {
                    "evidence_ids": [evidence["id"]],
                    "answer": f"Supported by {evidence['id']}",
                },
            },
            expected_route="graph",
            trace_direction="inbound",
        )

        self.assertEqual(trace_pre.returncode, 0, trace_pre.stderr)
        self.assertEqual(trace_post.returncode, 0, trace_post.stderr)
        self.assertEqual(evidence_post.returncode, 0, evidence_post.stderr)
        self.assertEqual(terminal.returncode, 0, terminal.stderr)

    def test_keyword_evidence_search_completes_the_lexical_route(self) -> None:
        evidence = canonical_evidence_ref()
        recorded = self.run_guard(
            "post-tool-use",
            {
                "session_id": "session-keyword",
                "tool_use_id": "tool-keyword",
                "tool_name": "mcp__code-search__search_code_evidence",
                "tool_input": {
                    "query": "CODE_SEARCH_STORAGE",
                    "search_mode": "keyword",
                },
                "tool_response": {"results": [{"evidence_ref": evidence}]},
            },
            expected_route="lexical",
        )
        terminal = self.run_guard(
            "pre-terminal-output",
            {
                "session_id": "session-keyword",
                "tool_use_id": "terminal-keyword",
                "tool_name": "StructuredOutput",
                "tool_input": {
                    "evidence_ids": [evidence["id"]],
                    "answer": f"Supported by {evidence['id']}",
                },
            },
            expected_route="lexical",
        )

        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(terminal.returncode, 0, terminal.stderr)

    def test_data_flow_evidence_completes_the_security_route(self) -> None:
        evidence = canonical_evidence_ref()
        recorded = self.run_guard(
            "post-tool-use",
            {
                "session_id": "session-data-flow",
                "tool_use_id": "tool-data-flow",
                "tool_name": "mcp__code-graph__trace_data_flow",
                "tool_input": {"source": "request.body", "max_depth": 6},
                "tool_response": {"flows": [{"evidence_ref": evidence}]},
            },
            expected_route="security",
        )
        terminal = self.run_guard(
            "pre-terminal-output",
            {
                "session_id": "session-data-flow",
                "tool_use_id": "terminal-data-flow",
                "tool_name": "StructuredOutput",
                "tool_input": {
                    "evidence_ids": [evidence["id"]],
                    "answer": f"Supported by {evidence['id']}",
                },
            },
            expected_route="security",
        )

        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        self.assertEqual(terminal.returncode, 0, terminal.stderr)


if __name__ == "__main__":
    unittest.main()
