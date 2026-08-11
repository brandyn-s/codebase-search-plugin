import json
from pathlib import Path
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_deployment_activation.py"
SPEC = importlib.util.spec_from_file_location("verify_deployment_activation", VERIFIER)
assert SPEC is not None and SPEC.loader is not None
VERIFIER_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER_MODULE)


def write_fake_claude(
    path: Path,
    *,
    marketplace_source: str,
    marketplace_root: str,
    install_root: str = (
        "/Users/example/.claude/plugins/cache/redacted-code-intelligence/"
        "codebase-search/0.4.9"
    ),
) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args == ['plugin', 'list', '--json']:\n"
        f"    print(json.dumps([{{'id':'codebase-search@redacted-code-intelligence','version':'0.4.9','scope':'user','enabled':True,'installPath':'{install_root}'}}]))\n"
        "elif args == ['plugin', 'marketplace', 'list', '--json']:\n"
        f"    print(json.dumps([{{'name':'redacted-code-intelligence','source':'{marketplace_source}','installLocation':'{marketplace_root}'}}]))\n"
        "elif args == ['mcp', 'list']:\n"
        f"    print('plugin:codebase-search:code-search: {install_root}/bin/run-code-search  - ✔ Connected')\n"
        f"    print('plugin:codebase-search:code-graph: {install_root}/bin/codebase-memory-mcp  - ✔ Connected')\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def write_runtime_receipt(
    evidence_root: Path, *, marketplace_root: str
) -> Path:
    receipt_root = evidence_root / "installed-runtime-smoke-20260811T000000Z"
    receipt_root.mkdir()
    raw = receipt_root / "raw.jsonl"
    events = [
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-sonnet-5",
            "plugins": [
                {"id": "codebase-search@redacted-code-intelligence", "version": "0.4.9"}
            ],
            "mcp_servers": [
                {"name": "plugin:codebase-search:code-search", "status": "connected"},
                {"name": "plugin:codebase-search:code-graph", "status": "connected"},
            ],
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-semantic",
                        "name": "mcp__plugin_codebase-search_code-search__search_code_evidence",
                        "input": {"query": "request authentication"},
                    },
                    {
                        "type": "tool_use",
                        "id": "tool-relationship",
                        "name": "mcp__plugin_codebase-search_code-graph__trace_call_path",
                        "input": {"function_name": "login", "direction": "inbound"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-semantic",
                        "content": "ok",
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-relationship",
                        "content": "ok",
                    },
                ]
            },
        },
        {"type": "result", "is_error": False},
    ]
    raw.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    receipt = receipt_root / "receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "receipt_type": "installed-plugin-runtime",
                "plugin_id": "codebase-search@redacted-code-intelligence",
                "plugin_version": "0.4.9",
                "marketplace_root": marketplace_root,
                "checkout_unchanged": True,
                "canary_violations": 0,
                "denied_tool_calls": 0,
                "raw_stream": "raw.jsonl",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = receipt_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (raw, receipt)
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return receipt_root


def write_passing_holdout(
    evidence_root: Path,
    runtime_root: Path,
    *,
    evidence_precision: float = 1.0,
    evidence_recall: float = 1.0,
) -> Path:
    run_root = evidence_root / "pilot-wave45-independent-pass"
    run_root.mkdir()
    cases = [
        {"case_id": "sealed-lexical", "expected_route": "lexical"},
        {
            "case_id": "sealed-graph",
            "expected_route": "graph",
            "routing_contract": {"trace_call_path": {"count": 1}},
        },
        {"case_id": "sealed-mixed", "expected_route": "mixed"},
        {"case_id": "sealed-security", "expected_route": "security"},
        {"case_id": "sealed-semantic", "expected_route": "semantic"},
    ]
    cases_path = run_root / "cases.jsonl"
    cases_path.write_text(
        "".join(json.dumps(case, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    records_path = run_root / "records.jsonl"
    records_path.write_text(
        "".join(
            json.dumps(
                {
                    "arm": "composed",
                    "case_id": case["case_id"],
                    "repetition": repetition,
                    "status": "success",
                },
                sort_keys=True,
            )
            + "\n"
            for repetition in (1, 2)
            for case in cases
        ),
        encoding="utf-8",
    )
    for repetition in (1, 2):
        raw_root = run_root / "raw" / "composed" / f"r{repetition:02d}"
        raw_root.mkdir(parents=True)
        for case in cases:
            (raw_root / f"{case['case_id']}.jsonl").write_text(
                json.dumps({"type": "result", "is_error": False}) + "\n",
                encoding="utf-8",
            )
    component_bom = run_root / "component-bom.json"
    runner = run_root / "pilot-runner.py"
    readiness = run_root / "readiness-evidence.json"
    target_manifest = run_root / "target-manifest.json"
    for path, content in (
        (component_bom, "{}\n"),
        (runner, "# sealed runner\n"),
        (readiness, "{}\n"),
        (target_manifest, "{}\n"),
    ):
        path.write_text(content, encoding="utf-8")
    gates = {
        "arm": "composed",
        "min_evidence_precision": 0.9,
        "min_evidence_recall": 0.9,
        "min_adjudication_accuracy": 1.0,
        "max_unsupported_asserted_claim_rate": 0.0,
        "min_routing_contract_accuracy": 1.0,
        "max_errors": 0,
        "max_canary_violations": 0,
    }
    preregistration = run_root / "preregistration-wave45.json"
    corpus_sha256 = "a" * 64
    preregistration.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_type": "bounded_operator_authorized_fresh_holdout_confirmation",
                "controls": {
                    "arms": ["composed"],
                    "case_ids": [case["case_id"] for case in cases],
                    "repetitions": 2,
                    "model": "sonnet",
                    "fallback_model": None,
                    "max_turns": 8,
                    "timeout_seconds": 180.0,
                    "max_budget_usd_per_case": 1.0,
                },
                "bindings": {
                    "schema_version": 1,
                    "bank_id": "opaque-bank-a",
                    "corpus_pack_sha256": corpus_sha256,
                    "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
                    "target_manifest_sha256": hashlib.sha256(
                        target_manifest.read_bytes()
                    ).hexdigest(),
                    "pilot_runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
                    "component_bom_sha256": hashlib.sha256(
                        component_bom.read_bytes()
                    ).hexdigest(),
                    "readiness_evidence_sha256": hashlib.sha256(
                        readiness.read_bytes()
                    ).hexdigest(),
                    "runtime_receipt_manifest_sha256": hashlib.sha256(
                        (runtime_root / "manifest.json").read_bytes()
                    ).hexdigest(),
                },
                "outcome_gates": gates,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    consumption = run_root / "consumption.json"
    consumption.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "consumed",
                "bank_id": "opaque-bank-a",
                "corpus_pack_sha256": corpus_sha256,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = run_root / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "wave45-independent-pass",
                "run_type": "bounded_operator_authorized_fresh_holdout_confirmation",
                "outcome_gate_status": "pass",
                "arms": {
                    "composed": {
                        "case_count": 10,
                        "unique_case_count": 5,
                        "repetitions": 2,
                        "routing_contract_cases": 2,
                        "evidence_precision": evidence_precision,
                        "evidence_recall": evidence_recall,
                        "adjudication_accuracy": 1.0,
                        "unsupported_asserted_claim_rate": 0.0,
                        "routing_contract_accuracy": 1.0,
                        "errors": 0,
                        "canary_violations": 0,
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    outcome = run_root / "outcome-gates.json"
    outcome.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "arm": "composed",
                "status": "pass",
                "gates": {
                    "evidence_precision": {
                        "observed": evidence_precision,
                        "operator": ">=",
                        "threshold": 0.9,
                        "passed": True,
                    },
                    "evidence_recall": {
                        "observed": evidence_recall,
                        "operator": ">=",
                        "threshold": 0.9,
                        "passed": True,
                    },
                    "adjudication_accuracy": {
                        "observed": 1.0,
                        "operator": ">=",
                        "threshold": 1.0,
                        "passed": True,
                    },
                    "unsupported_asserted_claim_rate": {
                        "observed": 0.0,
                        "operator": "<=",
                        "threshold": 0.0,
                        "passed": True,
                    },
                    "routing_contract_accuracy": {
                        "observed": 1.0,
                        "operator": ">=",
                        "threshold": 1.0,
                        "passed": True,
                    },
                    "errors": {
                        "observed": 0,
                        "operator": "<=",
                        "threshold": 0,
                        "passed": True,
                    },
                    "canary_violations": {
                        "observed": 0,
                        "operator": "<=",
                        "threshold": 0,
                        "passed": True,
                    },
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    artifacts = {
        path.relative_to(run_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in run_root.rglob("*")
        if path.is_file()
    }
    (run_root / "manifest.json").write_text(
        json.dumps(
            {"schema_version": 1, "run_id": "wave45-independent-pass", "artifacts": artifacts},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_root


class DeploymentActivationVerifierTests(unittest.TestCase):
    def test_cli_reports_stage_one_for_worktree_backed_marketplace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            fake_claude = root / "claude"
            write_fake_claude(
                fake_claude,
                marketplace_source="directory",
                marketplace_root="/Users/example/worktrees/plugin",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "--repo",
                    str(ROOT),
                    "--evidence-root",
                    str(evidence_root),
                    "--claude",
                    str(fake_claude),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(), "METRIC CODE_INTEL_DEPLOYMENT_STAGE=1"
        )

    def test_cli_reports_stage_two_for_durable_connected_marketplace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            fake_claude = root / "claude"
            write_fake_claude(
                fake_claude,
                marketplace_source="github",
                marketplace_root=(
                    "/Users/example/.claude/plugins/marketplaces/"
                    "redacted-code-intelligence"
                ),
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "--repo",
                    str(ROOT),
                    "--evidence-root",
                    str(evidence_root),
                    "--claude",
                    str(fake_claude),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(), "METRIC CODE_INTEL_DEPLOYMENT_STAGE=2"
        )

    def test_cli_reports_stage_three_for_manifested_cross_mcp_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            marketplace_root = (
                "/Users/example/.claude/plugins/marketplaces/"
                "redacted-code-intelligence"
            )
            write_runtime_receipt(evidence_root, marketplace_root=marketplace_root)
            fake_claude = root / "claude"
            write_fake_claude(
                fake_claude,
                marketplace_source="github",
                marketplace_root=marketplace_root,
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "--repo",
                    str(ROOT),
                    "--evidence-root",
                    str(evidence_root),
                    "--claude",
                    str(fake_claude),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "METRIC CODE_INTEL_DEPLOYMENT_STAGE=3",
            completed.stderr,
        )

    def test_cli_fails_closed_without_traceback_for_malformed_runtime_init(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            marketplace_root = (
                "/Users/example/.claude/plugins/marketplaces/"
                "redacted-code-intelligence"
            )
            runtime_root = write_runtime_receipt(
                evidence_root, marketplace_root=marketplace_root
            )
            raw = runtime_root / "raw.jsonl"
            events = [json.loads(line) for line in raw.read_text().splitlines()]
            events[0]["plugins"] = None
            raw.write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                encoding="utf-8",
            )
            manifest = json.loads((runtime_root / "manifest.json").read_text())
            manifest["artifacts"]["raw.jsonl"] = hashlib.sha256(
                raw.read_bytes()
            ).hexdigest()
            (runtime_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            fake_claude = root / "claude"
            write_fake_claude(
                fake_claude,
                marketplace_source="github",
                marketplace_root=marketplace_root,
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "--repo",
                    str(ROOT),
                    "--evidence-root",
                    str(evidence_root),
                    "--claude",
                    str(fake_claude),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertNotIn("Traceback", completed.stderr)
        self.assertIn("runtime trace does not prove both MCP families", completed.stderr)

    def test_cli_fails_closed_when_a_required_runtime_call_returns_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            marketplace_root = (
                "/Users/example/.claude/plugins/marketplaces/"
                "redacted-code-intelligence"
            )
            runtime_root = write_runtime_receipt(
                evidence_root, marketplace_root=marketplace_root
            )
            raw = runtime_root / "raw.jsonl"
            events = [json.loads(line) for line in raw.read_text().splitlines()]
            events[2]["message"]["content"][1]["is_error"] = True
            raw.write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                encoding="utf-8",
            )
            manifest = json.loads((runtime_root / "manifest.json").read_text())
            manifest["artifacts"]["raw.jsonl"] = hashlib.sha256(
                raw.read_bytes()
            ).hexdigest()
            (runtime_root / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            fake_claude = root / "claude"
            write_fake_claude(
                fake_claude,
                marketplace_source="github",
                marketplace_root=marketplace_root,
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "--repo",
                    str(ROOT),
                    "--evidence-root",
                    str(evidence_root),
                    "--claude",
                    str(fake_claude),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertIn("runtime trace does not prove both MCP families", completed.stderr)

    def test_runtime_trace_rejects_an_extra_mcp_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary) / "evidence"
            evidence_root.mkdir()
            runtime_root = write_runtime_receipt(
                evidence_root,
                marketplace_root=(
                    "/Users/example/.claude/plugins/marketplaces/"
                    "redacted-code-intelligence"
                ),
            )
            raw = runtime_root / "raw.jsonl"
            events = [json.loads(line) for line in raw.read_text().splitlines()]
            events[1]["message"]["content"].append(
                {
                    "type": "tool_use",
                    "id": "tool-extra",
                    "name": "mcp__plugin_codebase-search_code-graph__list_projects",
                    "input": {},
                }
            )
            raw.write_text(
                "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
                encoding="utf-8",
            )

            self.assertFalse(VERIFIER_MODULE._runtime_trace_uses_both_families(raw))

    def test_cli_reports_stage_four_for_fresh_nonvacuous_passing_holdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            marketplace_root = (
                "/Users/example/.claude/plugins/marketplaces/"
                "redacted-code-intelligence"
            )
            runtime_root = write_runtime_receipt(
                evidence_root, marketplace_root=marketplace_root
            )
            write_passing_holdout(
                evidence_root,
                runtime_root,
                evidence_precision=0.9,
                evidence_recall=0.9,
            )
            fake_claude = root / "claude"
            write_fake_claude(
                fake_claude,
                marketplace_source="github",
                marketplace_root=marketplace_root,
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VERIFIER),
                    "--repo",
                    str(ROOT),
                    "--evidence-root",
                    str(evidence_root),
                    "--claude",
                    str(fake_claude),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.strip(),
            "METRIC CODE_INTEL_DEPLOYMENT_STAGE=4",
            completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
