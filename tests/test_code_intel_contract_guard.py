import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "code_intel_contract_guard.py"


class ContractGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "target"
        source = self.target / "src" / "example.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "def endpoint():\n"
            "    value = helper()\n"
            "    return value\n"
            "\n"
            "def helper():\n"
            "    return 1\n",
            encoding="utf-8",
        )
        self.source = source
        self.environment = os.environ.copy()
        self.environment["CODE_INTEL_CONTRACT_GUARD_ROOT"] = str(self.root / "state")

    def tearDown(self):
        self.temporary.cleanup()

    def _run(
        self,
        mode: str,
        *,
        expected_route: str = "semantic",
        tool_name: str = "StructuredOutput",
        tool_input: dict | None = None,
        session_id: str = "session-a",
    ) -> subprocess.CompletedProcess[str]:
        event_names = {
            "pre-route-tool": "PreToolUse",
            "pre-read": "PreToolUse",
            "record-route": "PostToolUse",
            "record-read": "PostToolUse",
            "pre-terminal-output": "PreToolUse",
            "cleanup": "PostToolUse",
        }
        payload = {
            "hook_event_name": event_names[mode],
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "tool_response": {"success": True},
            "tool_use_id": f"{mode}-1",
        }
        return subprocess.run(
            [
                sys.executable,
                str(GUARD),
                mode,
                "--expected-route",
                expected_route,
                "--target",
                str(self.target),
            ],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.environment,
            check=False,
        )

    def _record_semantic_route(self, *, session_id: str = "session-a"):
        return self._run(
            "record-route",
            tool_name="mcp__code-search__search_code",
            tool_input={"query": "how endpoint works"},
            session_id=session_id,
        )

    def _pin(
        self,
        start: int,
        end: int,
        *,
        session_id: str = "session-a",
    ):
        return self._run(
            "record-read",
            tool_name="Read",
            tool_input={
                "file_path": str(self.source),
                "offset": start,
                "limit": end - start + 1,
            },
            session_id=session_id,
        )

    def _output(
        self,
        evidence_ids: list[str],
        *,
        answer: str | None = None,
        expected_route: str = "semantic",
        session_id: str = "session-a",
    ):
        return self._run(
            "pre-terminal-output",
            expected_route=expected_route,
            tool_name="StructuredOutput",
            tool_input={
                "candidate_assertion": "endpoint calls helper.",
                "disposition": "supported",
                "asserted_claim": "endpoint calls helper.",
                "evidence_ids": evidence_ids,
                "answer": answer
                if answer is not None
                else "Supported by " + ", ".join(evidence_ids),
                "canary_violation": False,
            },
            session_id=session_id,
        )

    def test_requires_successful_route_and_exact_source_pins(self):
        missing_route = self._output(["src/example.py:1-2"])
        route = self._record_semantic_route()
        missing_pin = self._output(["src/example.py:1-2"])
        pin = self._pin(1, 2)
        complete = self._output(["src/example.py:1-2"])

        self.assertEqual(missing_route.returncode, 2)
        self.assertIn("required semantic route", missing_route.stderr)
        self.assertEqual(route.returncode, 0, route.stderr)
        self.assertEqual(missing_pin.returncode, 2)
        self.assertIn("exact successful Read pin", missing_pin.stderr)
        self.assertEqual(pin.returncode, 0, pin.stderr)
        self.assertIn("src/example.py:1-2", pin.stdout)
        self.assertEqual(complete.returncode, 0, complete.stderr)

    def test_whole_file_inspection_is_not_an_evidence_pin(self):
        self.assertEqual(self._record_semantic_route().returncode, 0)
        inspection = self._run(
            "record-read",
            tool_name="Read",
            tool_input={"file_path": str(self.source)},
        )
        output = self._output(["src/example.py:1-6"])

        self.assertEqual(inspection.returncode, 0, inspection.stderr)
        self.assertIn("inspection-only", inspection.stdout)
        self.assertEqual(output.returncode, 2)
        self.assertIn("src/example.py:1-6", output.stderr)

    def test_pin_must_match_the_final_range_exactly(self):
        self.assertEqual(self._record_semantic_route().returncode, 0)
        self.assertEqual(self._pin(1, 6).returncode, 0)

        output = self._output(["src/example.py:1-2"])

        self.assertEqual(output.returncode, 2)
        self.assertIn("src/example.py:1-2", output.stderr)

    def test_every_evidence_id_must_be_cited_in_the_answer(self):
        self.assertEqual(self._record_semantic_route().returncode, 0)
        self.assertEqual(self._pin(1, 2).returncode, 0)

        output = self._output(
            ["src/example.py:1-2"],
            answer="The endpoint calls the helper.",
        )

        self.assertEqual(output.returncode, 2)
        self.assertIn("not cited verbatim", output.stderr)

    def test_semantic_route_rejects_graph_substitution_or_corroboration(self):
        graph = self._run(
            "pre-route-tool",
            tool_name="mcp__code-graph__search_code",
            tool_input={"query": "endpoint"},
        )
        semantic = self._run(
            "pre-route-tool",
            tool_name="mcp__code-search__search_code",
            tool_input={"query": "how endpoint works"},
        )

        self.assertEqual(graph.returncode, 2)
        self.assertIn("not allowed for the required semantic route", graph.stderr)
        self.assertEqual(semantic.returncode, 0, semantic.stderr)

    def test_mixed_route_allows_semantic_and_graph_but_not_lexical(self):
        semantic = self._run(
            "pre-route-tool",
            expected_route="mixed",
            tool_name="mcp__code-search__search_code_evidence",
            tool_input={"query": "endpoint callers"},
        )
        graph = self._run(
            "pre-route-tool",
            expected_route="mixed",
            tool_name="mcp__code-graph__trace_call_path",
            tool_input={"function_name": "endpoint", "direction": "inbound"},
        )
        lexical = self._run(
            "pre-route-tool",
            expected_route="mixed",
            tool_name="mcp__code-search__search_code",
            tool_input={"query": "endpoint", "search_mode": "keyword"},
        )

        self.assertEqual(semantic.returncode, 0, semantic.stderr)
        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertEqual(lexical.returncode, 2)
        self.assertIn("required mixed route", lexical.stderr)

    def test_mixed_route_requires_semantic_and_graph_success(self):
        semantic = self._run(
            "record-route",
            expected_route="mixed",
            tool_name="mcp__code-search__search_code_evidence",
            tool_input={"query": "endpoint callers"},
        )
        self.assertEqual(semantic.returncode, 0, semantic.stderr)
        self.assertEqual(
            self._pin(1, 2, session_id="session-a").returncode,
            0,
        )
        incomplete = self._output(["src/example.py:1-2"], expected_route="mixed")
        graph = self._run(
            "record-route",
            expected_route="mixed",
            tool_name="mcp__code-graph__trace_call_path",
            tool_input={"function_name": "endpoint", "direction": "inbound"},
        )
        complete = self._output(["src/example.py:1-2"], expected_route="mixed")

        self.assertEqual(incomplete.returncode, 2)
        self.assertIn("required mixed route", incomplete.stderr)
        self.assertEqual(graph.returncode, 0, graph.stderr)
        self.assertEqual(complete.returncode, 0, complete.stderr)

    def test_keyword_search_is_lexical_not_semantic(self):
        keyword = self._run(
            "record-route",
            tool_name="mcp__code-search__search_code",
            tool_input={"query": "APP_MODE", "search_mode": "keyword"},
        )
        self.assertEqual(keyword.returncode, 0, keyword.stderr)
        self.assertEqual(self._pin(1, 2).returncode, 0)

        semantic = self._output(["src/example.py:1-2"])
        lexical = self._output(["src/example.py:1-2"], expected_route="lexical")

        self.assertEqual(semantic.returncode, 2)
        self.assertEqual(lexical.returncode, 0, lexical.stderr)

    def test_rejects_out_of_target_and_out_of_bounds_pins(self):
        outside = self.root / "outside.py"
        outside.write_text("secret = 1\n", encoding="utf-8")

        escaped = self._run(
            "record-read",
            tool_name="Read",
            tool_input={"file_path": str(outside), "offset": 1, "limit": 1},
        )
        out_of_bounds = self._run(
            "record-read",
            tool_name="Read",
            tool_input={
                "file_path": str(self.source),
                "offset": 6,
                "limit": 2,
            },
        )

        self.assertEqual(escaped.returncode, 2)
        self.assertIn("outside the exact target", escaped.stderr)
        self.assertEqual(out_of_bounds.returncode, 2)
        self.assertIn("exceeds the source", out_of_bounds.stderr)

    def test_pre_read_blocks_files_outside_the_exact_target(self):
        outside = self.root / "outside.py"
        outside.write_text("secret = 1\n", encoding="utf-8")

        escaped = self._run(
            "pre-read",
            tool_name="Read",
            tool_input={"file_path": str(outside)},
        )
        target = self._run(
            "pre-read",
            tool_name="Read",
            tool_input={"file_path": str(self.source)},
        )

        self.assertEqual(escaped.returncode, 2)
        self.assertIn("outside the exact target", escaped.stderr)
        self.assertEqual(target.returncode, 0, target.stderr)

    def test_cleanup_removes_session_state(self):
        self.assertEqual(self._record_semantic_route().returncode, 0)
        cleanup = self._run("cleanup")
        output = self._output(["src/example.py:1-2"])

        self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
        self.assertEqual(output.returncode, 2)
        self.assertIn("required semantic route", output.stderr)


if __name__ == "__main__":
    unittest.main()
