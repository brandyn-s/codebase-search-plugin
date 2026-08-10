import subprocess
import sys
from pathlib import Path
import hashlib
import json
import os
import tempfile
from types import SimpleNamespace
import unittest

from bench.e2e.pilot.run import (
    _claude_command,
    _claude_environment,
    _prompt,
    _route_satisfies,
    _routing_contract_satisfies,
    project_transcript,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bench" / "e2e" / "pilot" / "run.py"


class OperatorPilotAcceptanceTests(unittest.TestCase):
    def test_v2_preregistration_binds_expanded_repeated_corpus(self):
        preregistration = json.loads(
            (ROOT / "bench" / "e2e" / "pilot" / "preregistration-v2.json").read_text()
        )
        cases = [
            json.loads(line)
            for line in (
                ROOT / "bench" / "e2e" / "pilot" / "cases-v2.jsonl"
            ).read_text().splitlines()
            if line.strip()
        ]

        self.assertEqual(preregistration["controls"]["repetitions"], 2)
        self.assertEqual(len(cases), 8)
        self.assertEqual(
            preregistration["controls"]["case_ids"],
            [case["case_id"] for case in cases],
        )
        self.assertEqual(
            preregistration["components"]["code-graph"]["version"],
            "v0.8.0-redacted.2",
        )
        self.assertTrue(
            any(case.get("expected_disposition") == "not_supported" for case in cases)
        )
        self.assertEqual(
            sum(isinstance(case.get("routing_contract"), dict) for case in cases),
            4,
        )

    def test_help_exposes_bounded_operator_authorized_surface(self):
        completed = subprocess.run(
            [sys.executable, str(RUNNER), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("bounded operator-authorized", completed.stdout)
        self.assertIn("--arms", completed.stdout)
        self.assertIn("--output-dir", completed.stdout)
        self.assertIn("--repetitions", completed.stdout)

    def test_literal_command_records_and_scores_one_fake_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_claude = root / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "events = [\n"
                "  {'type':'system','subtype':'init','model':'claude-sonnet-5'},\n"
                "  {'type':'assistant','message':{'content':[{'type':'tool_use','name':'mcp__code-search__search_code','input':{'query':'bearer validation'}}]}},\n"
                "  {'type':'result','is_error':False,'duration_ms':321,'total_cost_usd':0.01,'structured_output':{'disposition':'supported','claim_text':'Bearer tokens are verified before middleware forwards the request.','evidence_ids':['src/auth/token.py:1-8','src/auth/middleware.py:1-6'],'answer':'supported','canary_violation':False}}\n"
                "]\n"
                "for event in events: print(json.dumps(event, separators=(',', ':')))\n",
                encoding="utf-8",
            )
            os.chmod(fake_claude, 0o755)
            output = root / "recording"
            storage = root / "storage"
            storage.mkdir()
            model = root / "model"
            model.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--arms",
                    "composed",
                    "--case-ids",
                    "semantic-auth",
                    "--repetitions",
                    "2",
                    "--output-dir",
                    str(output),
                    "--claude",
                    str(fake_claude),
                    "--code-search",
                    "/usr/bin/true",
                    "--code-graph",
                    "/usr/bin/true",
                    "--code-search-storage",
                    str(storage),
                    "--local-model",
                    str(model),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["arms"]["composed"]["case_count"], 2)
            self.assertEqual(summary["arms"]["composed"]["unique_case_count"], 1)
            self.assertEqual(summary["arms"]["composed"]["repetitions"], 2)
            self.assertEqual(summary["arms"]["composed"]["evidence_precision"], 1.0)
            self.assertEqual(summary["arms"]["composed"]["evidence_recall"], 1.0)
            self.assertEqual(summary["arms"]["composed"]["routing_accuracy"], 1.0)
            self.assertEqual(summary["arms"]["composed"]["unsupported_claim_rate"], 0.0)
            self.assertTrue(
                (output / "raw" / "composed" / "r01" / "semantic-auth.jsonl").is_file()
            )
            self.assertTrue(
                (output / "raw" / "composed" / "r02" / "semantic-auth.jsonl").is_file()
            )
            self.assertTrue((output / "records.jsonl").is_file())
            self.assertTrue((output / "preregistration-v2.json").is_file())
            records = [
                json.loads(line)
                for line in (output / "records.jsonl").read_text().splitlines()
            ]
            self.assertEqual([record["repetition"] for record in records], [1, 2])
            manifest = json.loads((output / "manifest.json").read_text())
            raw_path = output / "raw" / "composed" / "r01" / "semantic-auth.jsonl"
            self.assertEqual(
                manifest["artifacts"]["raw/composed/r01/semantic-auth.jsonl"],
                hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            )
            self.assertIn("preregistration-v2.json", manifest["artifacts"])

    def test_composed_invocation_does_not_disable_explicit_mcp(self):
        arguments = SimpleNamespace(
            claude=Path("/usr/bin/claude"),
            code_search=Path("/usr/bin/code-search"),
            code_graph=Path("/usr/bin/code-graph"),
            code_search_storage=Path("/tmp/search"),
            local_model=Path("/tmp/model"),
            model="sonnet",
            max_turns=8,
            max_budget_usd=0.5,
        )
        case = {
            "query": "What calls process_order?",
            "expected_claims": [{"text": "The orders API calls process_order."}],
        }

        composed = _claude_command(arguments, arm="composed", case=case)
        native = _claude_command(arguments, arm="native", case=case)

        self.assertNotIn("--safe-mode", composed)
        self.assertIn("--safe-mode", native)
        self.assertIn("--strict-mcp-config", composed)

    def test_operator_environment_disables_incompatible_scrub_for_child_only(self):
        ambient = {
            "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
            "PRESERVE_ME": "yes",
        }

        child = _claude_environment(
            ambient,
            sentinel="sentinel",
            write_canary=Path("/tmp/canary"),
        )

        self.assertEqual(child["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "0")
        self.assertEqual(child["PRESERVE_ME"], "yes")
        self.assertEqual(ambient["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")

    def test_composed_prompt_makes_route_precedence_explicit(self):
        case = {
            "query": "Explain login and its callers",
            "expected_claims": [{"text": "Session creation calls login."}],
        }

        prompt = _prompt(case, "composed")

        self.assertIn("explicit source-to-sink", prompt)
        self.assertIn('search_mode="keyword"', prompt)
        self.assertIn("semantic search first, then a graph relationship tool", prompt)
        self.assertIn("An explicit symbol does not waive this mixed route", prompt)
        self.assertIn("Do not substitute graph text search", prompt)
        self.assertIn('trace_call_path once with direction="inbound"', prompt)
        self.assertIn("use Read to corroborate", prompt)

    def test_explain_symbol_counts_as_graph_relationship_work(self):
        tool_calls = [
            {
                "tool": "mcp__code-search__search_code",
                "arguments": {"query": "login flow"},
            },
            {
                "tool": "mcp__code-graph__explain_symbol",
                "arguments": {"name": "login"},
            },
        ]

        self.assertTrue(_route_satisfies("mixed", tool_calls))

    def test_exact_callers_contract_allows_one_inbound_trace_and_read(self):
        case = {
            "routing_contract": {
                "trace_call_path": {"count": 1, "direction": "inbound"},
                "forbidden_tools": ["mcp__code-graph__search_graph"],
            }
        }
        efficient = [
            {
                "tool": "mcp__code-graph__trace_call_path",
                "arguments": {"function_name": "login", "direction": "inbound"},
            },
            {"tool": "Read", "arguments": {"file_path": "src/api/session.py"}},
        ]
        redundant = [
            {
                "tool": "mcp__code-graph__trace_call_path",
                "arguments": {"function_name": "login", "direction": "both"},
            },
            {"tool": "mcp__code-graph__search_graph", "arguments": {"name": "login"}},
        ]

        self.assertTrue(_routing_contract_satisfies(case, efficient))
        self.assertFalse(_routing_contract_satisfies(case, redundant))


class TranscriptProjectionTests(unittest.TestCase):
    def test_projects_schema_valid_stream_into_objective_case_record(self):
        case = {
            "case_id": "semantic-auth",
            "expected_route": "semantic",
            "expected_evidence": [
                "src/auth/token.py:1-8",
                "src/auth/middleware.py:1-6",
            ],
            "expected_claims": [
                {
                    "claim_id": "semantic-auth:token-verification-order",
                    "text": "Bearer tokens are verified before middleware forwards the request.",
                    "required_evidence_ids": [
                        "src/auth/token.py:1-8",
                        "src/auth/middleware.py:1-6",
                    ],
                }
            ],
            "expected_index_error": "none",
        }
        transcript = [
            {
                "type": "system",
                "subtype": "init",
                "model": "claude-sonnet-5",
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__code-search__search_code",
                            "input": {"query": "bearer validation"},
                        }
                    ]
                },
            },
            {
                "type": "result",
                "is_error": False,
                "duration_ms": 321,
                "total_cost_usd": 0.01,
                "structured_output": {
                    "disposition": "supported",
                    "claim_text": "Bearer tokens are verified before middleware forwards the request.",
                    "evidence_ids": [
                        "src/auth/middleware.py:1-6",
                        "src/auth/token.py:1-8",
                    ],
                    "answer": "The source supports the assertion.",
                    "canary_violation": False,
                },
            },
        ]

        record = project_transcript(
            transcript,
            case=case,
            arm="composed",
            run_id="pilot-1",
        )

        self.assertEqual(record["status"], "success")
        self.assertEqual(record["model"], "claude-sonnet-5")
        self.assertEqual(record["derived_route"], "semantic")
        self.assertEqual(record["evidence_true_positives"], 2)
        self.assertEqual(record["evidence_false_positives"], 0)
        self.assertEqual(record["evidence_false_negatives"], 0)
        self.assertEqual(record["unsupported_claim_count"], 0)
        self.assertEqual(record["tool_calls"][0]["tool"], "mcp__code-search__search_code")

    def test_scores_correct_rejection_of_false_candidate_as_supported_adjudication(self):
        case = {
            "case_id": "negative-auth-bypass",
            "expected_route": "semantic",
            "expected_disposition": "not_supported",
            "expected_evidence": ["src/auth/token.py:7-8"],
            "expected_claims": [
                {
                    "claim_id": "negative-auth-bypass:signature-skip",
                    "text": "Bearer tokens are forwarded without signature verification.",
                    "required_evidence_ids": ["src/auth/token.py:7-8"],
                }
            ],
            "expected_index_error": "none",
        }
        transcript = [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__code-search__search_code",
                            "input": {"query": "bearer signature verification"},
                        }
                    ]
                },
            },
            {
                "type": "result",
                "is_error": False,
                "duration_ms": 100,
                "total_cost_usd": 0.01,
                "structured_output": {
                    "disposition": "not_supported",
                    "claim_text": None,
                    "evidence_ids": ["src/auth/token.py:7-8"],
                    "answer": "The implementation calls signature verification.",
                    "canary_violation": False,
                },
            },
        ]

        record = project_transcript(
            transcript,
            case=case,
            arm="composed",
            run_id="pilot-negative",
            repetition=2,
        )

        self.assertEqual(record["repetition"], 2)
        self.assertEqual(record["unsupported_claim_count"], 0)
        self.assertEqual(record["evidence_true_positives"], 1)

    def test_accepts_narrower_evidence_and_expected_route_with_corroboration(self):
        case = {
            "case_id": "semantic-auth",
            "expected_route": "semantic",
            "expected_evidence": [
                "src/auth/token.py:1-8",
                "src/auth/middleware.py:1-6",
            ],
            "expected_claims": [
                {
                    "claim_id": "semantic-auth:token-verification-order",
                    "text": "Bearer tokens are verified before middleware forwards the request.",
                    "required_evidence_ids": [
                        "src/auth/token.py:1-8",
                        "src/auth/middleware.py:1-6",
                    ],
                }
            ],
            "expected_index_error": "none",
        }
        transcript = [
            {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__code-search__search_code",
                            "input": {"query": "bearer validation"},
                        },
                        {
                            "type": "tool_use",
                            "name": "mcp__code-graph__query_security_surfaces",
                            "input": {"role": "auth_boundary"},
                        },
                        {
                            "type": "tool_use",
                            "name": "mcp__code-graph__search_code",
                            "input": {"pattern": "bearer"},
                        },
                    ]
                },
            },
            {
                "type": "result",
                "is_error": False,
                "duration_ms": 321,
                "total_cost_usd": 0.01,
                "structured_output": {
                    "disposition": "supported",
                    "claim_text": "Bearer tokens are verified before middleware forwards the request.",
                    "evidence_ids": [
                        "src/auth/middleware.py:4-6",
                        "src/auth/token.py:1-8",
                    ],
                    "answer": "supported",
                    "canary_violation": False,
                },
            },
        ]

        record = project_transcript(
            transcript,
            case=case,
            arm="composed",
            run_id="pilot-1",
        )

        self.assertEqual(record["derived_route"], "security")
        self.assertTrue(record["route_correct"])
        self.assertEqual(record["evidence_true_positives"], 2)
        self.assertEqual(record["evidence_false_positives"], 0)
        self.assertEqual(record["evidence_false_negatives"], 0)
        self.assertEqual(record["unsupported_claim_count"], 0)


if __name__ == "__main__":
    unittest.main()
