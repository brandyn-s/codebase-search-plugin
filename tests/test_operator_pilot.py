import subprocess
import sys
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import os
import shutil
import tempfile
from types import SimpleNamespace
import unittest

from bench.e2e.pilot.run import (
    _claude_command,
    _claude_environment,
    _mcp_config,
    _prompt,
    _route_satisfies,
    _routing_contract_satisfies,
    _validate_fresh_holdout_corpus,
    _validate_preregistered_controls,
    build_parser,
    evaluate_outcome_gates,
    project_transcript,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bench" / "e2e" / "pilot" / "run.py"


class OperatorPilotAcceptanceTests(unittest.TestCase):
    def test_fresh_holdout_preflight_rejects_vacuous_corpus(self):
        controls = {
            "arms": ["composed"],
            "case_ids": [f"case-{index}" for index in range(5)],
            "repetitions": 2,
            "model": "sonnet",
            "fallback_model": None,
            "max_turns": 8,
            "timeout_seconds": 180.0,
            "max_budget_usd_per_case": 1.0,
        }
        preregistration = {
            "run_type": "bounded_operator_authorized_fresh_holdout_confirmation",
            "controls": controls,
            "bindings": {
                "schema_version": 1,
                "bank_id": "opaque-bank-a",
                "corpus_pack_sha256": "a" * 64,
                "runtime_receipt_manifest_sha256": "b" * 64,
            },
            "outcome_gates": {
                "arm": "composed",
                "min_evidence_precision": 0.9,
                "min_evidence_recall": 0.9,
                "min_adjudication_accuracy": 1.0,
                "max_unsupported_asserted_claim_rate": 0.0,
                "min_routing_contract_accuracy": 1.0,
                "max_errors": 0,
                "max_canary_violations": 0,
            },
        }
        cases = [
            {
                "case_id": f"case-{index}",
                "expected_route": route,
                "expected_evidence": [f"src/case_{index}.py:1-2"],
                "expected_claims": [
                    {
                        "text": f"claim {index}",
                        "required_evidence_ids": [f"src/case_{index}.py:1-2"],
                    }
                ],
            }
            for index, route in enumerate(
                ("semantic", "lexical", "graph", "mixed", "security")
            )
        ]

        with self.assertRaisesRegex(ValueError, "routing contract"):
            _validate_fresh_holdout_corpus(preregistration, cases)

        cases[2]["routing_contract"] = {"trace_call_path": {"count": 1}}
        _validate_fresh_holdout_corpus(preregistration, cases)

    def test_code_graph_home_is_scoped_to_the_graph_mcp_child(self):
        arguments = SimpleNamespace(
            code_search=Path("/usr/bin/code-search"),
            code_graph=Path("/usr/bin/code-graph"),
            code_search_storage=Path("/tmp/search"),
            code_graph_home=Path("/tmp/graph-home"),
            local_model=Path("/tmp/model"),
        )

        config = _mcp_config(arguments, "composed")["mcpServers"]

        self.assertEqual(
            config["code-graph"].get("env", {}).get("HOME"), "/tmp/graph-home"
        )
        self.assertNotIn("HOME", config["code-search"]["env"])

    def test_preregistration_rejects_execution_control_drift(self):
        preregistration = json.loads(
            (
                ROOT / "bench" / "e2e" / "pilot" / "preregistration-v4.json"
            ).read_text()
        )
        arguments = SimpleNamespace(
            arms="native",
            case_ids=",".join(preregistration["controls"]["case_ids"]),
            repetitions=2,
            model="sonnet",
            max_budget_usd=1.0,
        )

        with self.assertRaisesRegex(ValueError, "arms differ"):
            _validate_preregistered_controls(arguments, preregistration)

    def test_preregistration_rejects_cases_content_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            cases = Path(temporary) / "cases.jsonl"
            cases.write_text('{"case_id":"fresh-case"}\n', encoding="utf-8")
            preregistration = {
                "controls": {
                    "arms": ["composed"],
                    "case_ids": ["fresh-case"],
                    "repetitions": 2,
                    "model": "sonnet",
                    "max_budget_usd_per_case": 1.0,
                },
                "bindings": {
                    "cases_sha256": hashlib.sha256(
                        cases.read_bytes()
                    ).hexdigest(),
                },
            }
            arguments = SimpleNamespace(
                arms="composed",
                case_ids="fresh-case",
                repetitions=2,
                model="sonnet",
                max_budget_usd=1.0,
                cases=cases,
            )
            cases.write_text(
                '{"case_id":"changed-after-registration"}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "cases SHA-256 differs"):
                _validate_preregistered_controls(arguments, preregistration)

    def test_preregistration_rejects_target_fixture_content_drift(self):
        cases = ROOT / "bench" / "e2e" / "pilot" / "cases-v2.jsonl"
        source_manifest = ROOT / "bench" / "e2e" / "target-repo-manifest.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target-repo"
            target_manifest = root / "target-repo-manifest.json"
            shutil.copytree(ROOT / "bench" / "e2e" / "target-repo", target)
            shutil.copy2(source_manifest, target_manifest)
            preregistration = {
                "controls": {
                    "arms": ["composed"],
                    "case_ids": ["semantic-auth"],
                    "repetitions": 2,
                    "model": "sonnet",
                    "max_budget_usd_per_case": 1.0,
                },
                "bindings": {
                    "cases_sha256": hashlib.sha256(
                        cases.read_bytes()
                    ).hexdigest(),
                    "target_manifest_sha256": hashlib.sha256(
                        target_manifest.read_bytes()
                    ).hexdigest(),
                },
            }
            arguments = SimpleNamespace(
                arms="composed",
                case_ids="semantic-auth",
                repetitions=2,
                model="sonnet",
                max_budget_usd=1.0,
                cases=cases,
                target=target,
                target_manifest=target_manifest,
            )
            (target / "src" / "config.py").write_text(
                'CODE_SEARCH_STORAGE = "changed"\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "target fixture differs"):
                _validate_preregistered_controls(arguments, preregistration)

    def test_v5_preregistration_binds_execution_identity_bundle(self):
        cases = ROOT / "bench" / "e2e" / "pilot" / "cases-v2.jsonl"
        target_manifest = ROOT / "bench" / "e2e" / "target-repo-manifest.json"
        component_bom = ROOT / "component-bom.json"
        bom = json.loads(component_bom.read_text(encoding="utf-8"))

        def install_descriptor_sha256(component: str) -> str:
            encoded = json.dumps(
                bom["components"][component]["install"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target-repo"
            readiness_evidence = root / "readiness-evidence.json"
            shutil.copytree(ROOT / "bench" / "e2e" / "target-repo", target)
            subprocess.run(
                ["git", "init", "-q", str(target)], check=True
            )
            subprocess.run(
                ["git", "-C", str(target), "add", "."], check=True
            )
            commit_environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Wave 4 fixture",
                "GIT_AUTHOR_EMAIL": "wave4@example.invalid",
                "GIT_COMMITTER_NAME": "Wave 4 fixture",
                "GIT_COMMITTER_EMAIL": "wave4@example.invalid",
            }
            subprocess.run(
                ["git", "-C", str(target), "commit", "-qm", "fixture"],
                check=True,
                env=commit_environment,
            )
            source_revision = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            repository_id = hashlib.sha256(
                str(target.resolve()).encode("utf-8")
            ).hexdigest()
            index_generation = hashlib.sha256(
                (
                    repository_id
                    + "\0"
                    + source_revision
                    + "\0clean"
                ).encode("utf-8")
            ).hexdigest()
            identity = {
                "schema_version": 1,
                "repository_id": repository_id,
                "checkout_id": repository_id,
                "source_revision": source_revision,
                "dirty_fingerprint": "clean",
                "index_generation": index_generation,
                "captured_at": "2026-08-10T23:59:00Z",
            }
            code_search = root / "code-search"
            code_graph = root / "code-graph"
            alternate_search = root / "alternate-code-search"
            for path, content in (
                (code_search, b"search-runtime"),
                (code_graph, b"graph-runtime"),
                (alternate_search, b"different-runtime"),
            ):
                path.write_bytes(content)
                path.chmod(0o755)
            code_search_storage = root / "code-search-storage"
            code_graph_home = root / "code-graph-home"
            code_search_storage.mkdir()
            code_graph_home.mkdir()
            readiness = {
                "schema_version": 1,
                "producer": "scripts/generate_live_readiness_evidence.py:v2",
                "evidence_mode": "ready-validation",
                "bom_readiness_status": "ready",
                "checkout_unchanged": True,
                "components": {
                    "code-search": {
                        "version": bom["components"]["code-search"]["install"][
                            "tag"
                        ],
                        "install_descriptor_sha256": install_descriptor_sha256(
                            "code-search"
                        ),
                        "completion": {"success": True, "error": None},
                        "index_ready": True,
                        "evidence_coordinate": {
                            "status": "verified",
                            "relative_path": "src/auth/token.py",
                            "start_line": 1,
                            "end_line": 4,
                            "index_generation": index_generation,
                        },
                        "index_identity": deepcopy(identity),
                    },
                    "code-graph": {
                        "version": bom["components"]["code-graph"]["install"][
                            "tag"
                        ],
                        "install_descriptor_sha256": install_descriptor_sha256(
                            "code-graph"
                        ),
                        "status": "ready",
                        "index_identity": deepcopy(identity),
                    },
                },
                "runtime": {
                    "target_root": str(target.resolve()),
                    "code_search_storage": str(code_search_storage.resolve()),
                    "code_graph_home": str(code_graph_home.resolve()),
                    "servers": {
                        "code-search": {
                            "path": str(code_search.resolve()),
                            "sha256": hashlib.sha256(
                                code_search.read_bytes()
                            ).hexdigest(),
                        },
                        "code-graph": {
                            "path": str(code_graph.resolve()),
                            "sha256": hashlib.sha256(
                                code_graph.read_bytes()
                            ).hexdigest(),
                        },
                    },
                },
            }
            readiness_evidence.write_text(
                json.dumps(readiness, indent=2) + "\n", encoding="utf-8"
            )
            preregistration = {
                "controls": {
                    "arms": ["composed"],
                    "case_ids": ["semantic-auth"],
                    "repetitions": 2,
                    "model": "sonnet",
                    "max_budget_usd_per_case": 1.0,
                    "max_turns": 8,
                    "timeout_seconds": 180.0,
                },
                "bindings": {
                    "schema_version": 1,
                    "cases_sha256": hashlib.sha256(
                        cases.read_bytes()
                    ).hexdigest(),
                    "target_manifest_sha256": hashlib.sha256(
                        target_manifest.read_bytes()
                    ).hexdigest(),
                    "pilot_runner_sha256": hashlib.sha256(
                        RUNNER.read_bytes()
                    ).hexdigest(),
                    "component_bom_sha256": hashlib.sha256(
                        component_bom.read_bytes()
                    ).hexdigest(),
                    "readiness_evidence_sha256": hashlib.sha256(
                        readiness_evidence.read_bytes()
                    ).hexdigest(),
                },
            }
            arguments = SimpleNamespace(
                arms="composed",
                case_ids="semantic-auth",
                repetitions=2,
                model="sonnet",
                max_budget_usd=1.0,
                max_turns=8,
                timeout_seconds=180.0,
                cases=cases,
                target=target,
                target_manifest=target_manifest,
                component_bom=component_bom,
                readiness_evidence=readiness_evidence,
                code_search=code_search,
                code_graph=code_graph,
                code_search_storage=code_search_storage,
                code_graph_home=code_graph_home,
            )

            try:
                _validate_preregistered_controls(arguments, preregistration)
            except ValueError as exc:
                self.fail(str(exc))

            drift_cases = {
                "max turns": ("control", "max_turns", 9, "max turns differs"),
                "timeout": (
                    "control",
                    "timeout_seconds",
                    181.0,
                    "timeout differs",
                ),
                "runner": (
                    "binding",
                    "pilot_runner_sha256",
                    "0" * 64,
                    "pilot runner SHA-256 differs",
                ),
                "BOM": (
                    "binding",
                    "component_bom_sha256",
                    "0" * 64,
                    "component BOM SHA-256 differs",
                ),
                "readiness": (
                    "binding",
                    "readiness_evidence_sha256",
                    "0" * 64,
                    "readiness evidence SHA-256 differs",
                ),
                "code-search runtime": (
                    "argument",
                    "code_search",
                    alternate_search,
                    "runtime code-search executable differs",
                ),
            }
            for label, (kind, field, value, error) in drift_cases.items():
                with self.subTest(label=label):
                    drifted_preregistration = deepcopy(preregistration)
                    drifted_arguments = SimpleNamespace(**vars(arguments))
                    if kind in {"control", "argument"}:
                        setattr(drifted_arguments, field, value)
                    else:
                        drifted_preregistration["bindings"][field] = value
                    with self.assertRaisesRegex(ValueError, error):
                        _validate_preregistered_controls(
                            drifted_arguments, drifted_preregistration
                        )

            changed_revision = "f" * 40
            changed_generation = hashlib.sha256(
                (
                    repository_id
                    + "\0"
                    + changed_revision
                    + "\0clean"
                ).encode("utf-8")
            ).hexdigest()
            for component in ("code-search", "code-graph"):
                changed_identity = readiness["components"][component][
                    "index_identity"
                ]
                changed_identity["source_revision"] = changed_revision
                changed_identity["index_generation"] = changed_generation
            readiness["components"]["code-search"]["evidence_coordinate"][
                "index_generation"
            ] = changed_generation
            readiness_evidence.write_text(
                json.dumps(readiness, indent=2) + "\n", encoding="utf-8"
            )
            preregistration["bindings"]["readiness_evidence_sha256"] = (
                hashlib.sha256(readiness_evidence.read_bytes()).hexdigest()
            )
            with self.assertRaisesRegex(ValueError, "source revision differs"):
                _validate_preregistered_controls(arguments, preregistration)

    def test_outcome_gates_do_not_block_on_generic_route_accuracy(self):
        summary = {
            "arms": {
                "composed": {
                    "evidence_precision": 1.0,
                    "evidence_recall": 1.0,
                    "adjudication_accuracy": 1.0,
                    "unsupported_asserted_claim_rate": 0.0,
                    "routing_contract_accuracy": 1.0,
                    "errors": 0,
                    "canary_violations": 0,
                    "routing_accuracy": 0.75,
                    "tool_calls": {"mean_per_case": 4.1875, "total": 67},
                    "latency_ms": {"mean": 39129.75, "p95": 71174.0},
                    "total_cost_usd": 9.1946671,
                }
            }
        }
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

        report = evaluate_outcome_gates(summary, gates)

        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["operational_metrics"]["routing_accuracy"], 0.75
        )
        self.assertNotIn("routing_accuracy", report["gates"])

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
        self.assertEqual(
            preregistration["controls"]["max_budget_usd_per_case"], 1.0
        )
        arguments = build_parser().parse_args(
            [
                "--output-dir",
                "/tmp/pilot",
                "--code-search-storage",
                "/tmp/storage",
                "--local-model",
                "/tmp/model",
            ]
        )
        self.assertEqual(arguments.max_budget_usd, 1.0)
        self.assertEqual(arguments.preregistration.name, "preregistration-v2.json")
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

    def test_v3_preregistration_binds_targeted_composed_confirmation(self):
        preregistration = json.loads(
            (
                ROOT / "bench" / "e2e" / "pilot" / "preregistration-v3.json"
            ).read_text()
        )

        self.assertEqual(
            preregistration["decision"],
            "wave4_2_targeted_remediation_confirmation",
        )
        self.assertEqual(preregistration["controls"]["arms"], ["composed"])
        self.assertEqual(preregistration["controls"]["repetitions"], 2)
        self.assertEqual(
            preregistration["components"]["plugin"]["version"], "0.4.5"
        )
        self.assertEqual(
            preregistration["source_evidence"]["primary_run_id"],
            "wave41-20260810T193035Z",
        )
        self.assertEqual(
            preregistration["source_evidence"]["primary_manifest_sha256"],
            "246a0301507ef62849bc8f43738a089f04f2056d95219be8a9ef3cb15c7d72f8",
        )
        self.assertIn("cannot convert", preregistration["interpretation_limit"])

    def test_v4_preregistration_binds_outcome_gate_confirmation(self):
        preregistration = json.loads(
            (
                ROOT / "bench" / "e2e" / "pilot" / "preregistration-v4.json"
            ).read_text()
        )

        self.assertEqual(
            preregistration["decision"],
            "wave4_3_outcome_gate_confirmation",
        )
        self.assertEqual(preregistration["controls"]["arms"], ["composed"])
        self.assertEqual(preregistration["controls"]["repetitions"], 2)
        self.assertEqual(
            preregistration["components"]["plugin"]["version"], "0.4.6"
        )
        self.assertEqual(
            preregistration["source_evidence"]["wave4_2_manifest_sha256"],
            "206a8c57f08e5d882ae08c1de2faf34b898235cc70eb085a2a5c87c746d0dab6",
        )
        self.assertNotIn(
            "min_routing_accuracy", preregistration["outcome_gates"]
        )
        self.assertEqual(
            preregistration["outcome_gates"]["min_adjudication_accuracy"],
            1.0,
        )

    def test_v5_preregistration_binds_fresh_holdout_corpus(self):
        pilot_root = ROOT / "bench" / "e2e" / "pilot"
        cases_path = pilot_root / "cases-v3.jsonl"
        preregistration_path = pilot_root / "preregistration-v5.json"
        readiness_path = pilot_root / "readiness-wave44.json"
        target = ROOT / "bench" / "e2e" / "target-repo-v2"
        target_manifest_path = ROOT / "bench" / "e2e" / "target-repo-v2-manifest.json"
        cases = [
            json.loads(line)
            for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        preregistration = json.loads(
            preregistration_path.read_text(encoding="utf-8")
        )
        target_manifest = json.loads(
            target_manifest_path.read_text(encoding="utf-8")
        )
        old_case_ids = {
            json.loads(line)["case_id"]
            for line in (pilot_root / "cases-v2.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        }
        case_ids = [case["case_id"] for case in cases]

        self.assertEqual(len(cases), 5)
        self.assertEqual(len(set(case_ids)), 5)
        self.assertTrue(set(case_ids).isdisjoint(old_case_ids))
        self.assertEqual(
            preregistration["decision"],
            "wave4_4_fresh_holdout_confirmation",
        )
        self.assertEqual(preregistration["controls"]["case_ids"], case_ids)
        self.assertEqual(preregistration["controls"]["arms"], ["composed"])
        self.assertEqual(preregistration["controls"]["repetitions"], 2)
        self.assertEqual(preregistration["controls"]["max_turns"], 8)
        self.assertEqual(preregistration["controls"]["timeout_seconds"], 180.0)
        self.assertEqual(preregistration["bindings"]["schema_version"], 1)
        expected_bindings = {
            "cases_sha256": cases_path,
            "target_manifest_sha256": target_manifest_path,
            "readiness_evidence_sha256": readiness_path,
        }
        for field, path in expected_bindings.items():
            self.assertEqual(
                preregistration["bindings"][field],
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        self.assertEqual(
            preregistration["bindings"]["pilot_runner_sha256"],
            "e2d60431cb5823d7f851ac2c9f1f2a1fed4c0cd9db0c7670d5c551b4b3b65cea",
        )
        self.assertEqual(
            preregistration["bindings"]["component_bom_sha256"],
            "a65bee017668a9816c8b19193948d77c98d2c8450730c1e236742296b60290a5",
        )
        self.assertEqual(target_manifest["source_root"], "target-repo-v2")
        actual_files = {
            path.relative_to(target).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in target.rglob("*")
            if path.is_file()
        }
        self.assertEqual(target_manifest["files"], actual_files)
        canonical_tree = "\n".join(
            f"{relative}\0{actual_files[relative]}"
            for relative in sorted(actual_files)
        ).encode("utf-8")
        self.assertEqual(
            target_manifest["revision"],
            hashlib.sha256(canonical_tree).hexdigest(),
        )
        for case in cases:
            for evidence_id in case["expected_evidence"]:
                relative, line_range = evidence_id.rsplit(":", 1)
                start, end = line_range.split("-", 1)
                line_count = len(
                    (target / relative).read_text(encoding="utf-8").splitlines()
                )
                self.assertGreaterEqual(int(start), 1)
                self.assertLessEqual(int(end), line_count)
        self.assertEqual(
            preregistration["source_evidence"]["wave4_3_run_id"],
            "wave43-20260810T220737Z",
        )
        self.assertIn("run exactly once", preregistration["falsifier"])

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
        self.assertIn("--preregistration", completed.stdout)

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
                "  {'type':'result','is_error':False,'duration_ms':321,'total_cost_usd':0.01,'structured_output':{'candidate_assertion':'Bearer tokens are verified before middleware forwards the request.','disposition':'supported','asserted_claim':'Bearer tokens are verified before middleware forwards the request.','evidence_ids':['src/auth/token.py:1-8','src/auth/middleware.py:1-6'],'answer':'supported','canary_violation':False}}\n"
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
            preregistration_path = root / "preregistration-v4.json"
            preregistration = json.loads(
                (
                    ROOT / "bench" / "e2e" / "pilot" / "preregistration-v4.json"
                ).read_text()
            )
            preregistration["controls"]["case_ids"] = ["semantic-auth"]
            preregistration_path.write_text(
                json.dumps(preregistration), encoding="utf-8"
            )

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
                    "--preregistration",
                    str(preregistration_path),
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
            self.assertTrue((output / "pilot-runner.py").is_file())
            self.assertTrue((output / "preregistration-v4.json").is_file())
            self.assertEqual(
                json.loads((output / "outcome-gates.json").read_text())["status"],
                "pass",
            )
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
            self.assertTrue(manifest["run_id"].startswith("wave43-"))
            self.assertIn("preregistration-v4.json", manifest["artifacts"])
            self.assertIn("outcome-gates.json", manifest["artifacts"])

    def test_failed_model_launch_seals_consumption_and_failure_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_claude = root / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps({'type':'system','subtype':'init','model':'claude-sonnet-5'}), flush=True)\n"
                "print('fixture provider failure', file=sys.stderr)\n"
                "raise SystemExit(7)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            output = root / "failed-recording"
            storage = root / "storage"
            storage.mkdir()
            model = root / "model"
            model.mkdir()
            preregistration_path = root / "preregistration-v4.json"
            preregistration = json.loads(
                (
                    ROOT / "bench" / "e2e" / "pilot" / "preregistration-v4.json"
                ).read_text()
            )
            preregistration["controls"]["case_ids"] = ["semantic-auth"]
            preregistration["controls"]["repetitions"] = 1
            preregistration["bindings"] = {
                "bank_id": "failure-bank-a",
                "corpus_pack_sha256": "b" * 64,
                "cases_sha256": hashlib.sha256(
                    (ROOT / "bench" / "e2e" / "pilot" / "cases-v2.jsonl").read_bytes()
                ).hexdigest(),
                "target_manifest_sha256": hashlib.sha256(
                    (
                        ROOT / "bench" / "e2e" / "target-repo-manifest.json"
                    ).read_bytes()
                ).hexdigest(),
            }
            preregistration_path.write_text(
                json.dumps(preregistration), encoding="utf-8"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--arms",
                    "composed",
                    "--case-ids",
                    "semantic-auth",
                    "--repetitions",
                    "1",
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
                    "--preregistration",
                    str(preregistration_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertTrue((output / "consumption.json").is_file())
            consumption = json.loads((output / "consumption.json").read_text())
            self.assertEqual(consumption["state"], "consumed")
            self.assertEqual(consumption["bank_id"], "failure-bank-a")
            failure = json.loads((output / "failure-receipt.json").read_text())
            self.assertEqual(failure["exception_class"], "RuntimeError")
            self.assertEqual(failure["attempted_unit"]["arm"], "composed")
            self.assertEqual(failure["attempted_unit"]["case_id"], "semantic-auth")
            raw = output / "raw" / "composed" / "r01" / "semantic-auth.jsonl"
            self.assertEqual(
                failure["transcript_sha256"],
                hashlib.sha256(raw.read_bytes()).hexdigest(),
            )
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed")
            self.assertIn("consumption.json", manifest["artifacts"])
            self.assertIn("failure-receipt.json", manifest["artifacts"])
            self.assertIn(
                "raw/composed/r01/semantic-auth.jsonl", manifest["artifacts"]
            )

    def test_composed_invocation_isolates_project_settings_with_explicit_mcp(self):
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
        self.assertNotIn("--safe-mode", native)
        self.assertIn("--setting-sources", composed)
        self.assertEqual(composed[composed.index("--setting-sources") + 1], "user")
        self.assertIn("--strict-mcp-config", composed)
        composed_tools = composed[composed.index("--tools") + 1].split(",")
        native_tools = native[native.index("--tools") + 1].split(",")
        composed_allowed = composed[composed.index("--allowedTools") + 1].split(",")
        self.assertEqual(composed_tools, ["Read", "ToolSearch"])
        self.assertNotIn("ToolSearch", native_tools)
        self.assertIn("ToolSearch", composed_allowed)

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
        self.assertEqual(child["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(child["PRESERVE_ME"], "yes")
        self.assertEqual(ambient["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"], "1")
        self.assertNotIn("ENABLE_TOOL_SEARCH", ambient)

    def test_composed_prompt_makes_route_precedence_explicit(self):
        case = {
            "query": "Explain login and its callers",
            "expected_claims": [{"text": "Session creation calls login."}],
            "routing_contract": {
                "trace_call_path": {"count": 1, "direction": "inbound"},
                "forbidden_tools": ["mcp__code-graph__search_graph"],
            },
        }

        prompt = _prompt(case, "composed")

        self.assertIn("explicit source-to-sink", prompt)
        self.assertIn('search_mode="keyword"', prompt)
        self.assertIn("Security vocabulary alone does not make", prompt)
        self.assertIn("Conceptual how, why, or whether behavior", prompt)
        self.assertIn("even when it names an exact symbol", prompt)
        self.assertIn(
            "Do not call graph security tools for conceptual behavior",
            prompt,
        )
        self.assertIn("Pure literal or location lookup", prompt)
        self.assertIn(
            "semantic search first, then exactly one graph relationship tool",
            prompt,
        )
        self.assertIn("An explicit symbol does not waive this mixed route", prompt)
        self.assertIn("Do not substitute graph text search", prompt)
        self.assertIn("MCP tools are deferred", prompt)
        self.assertIn("Before any Read, call ToolSearch exactly once", prompt)
        self.assertIn("Never guess a repository path", prompt)
        self.assertIn(
            'For this case, call trace_call_path exactly once with direction="inbound"',
            prompt,
        )
        self.assertIn("Do not call trace_call_path in any other direction", prompt)
        self.assertIn("use Read to corroborate", prompt)
        self.assertIn("every named relationship endpoint", prompt)
        self.assertIn("Before setting disposition to supported", prompt)
        self.assertIn("An import or call site does not substitute", prompt)
        self.assertIn("synthetic terminal line", prompt)
        self.assertIn("byte-for-byte, including terminal punctuation", prompt)
        self.assertIn("as candidate_assertion", prompt)
        self.assertIn("as asserted_claim", prompt)

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
    def test_normalizes_only_a_synthetic_terminal_read_line(self):
        case = {
            "case_id": "terminal-newline",
            "expected_route": "semantic",
            "expected_disposition": "not_supported",
            "expected_evidence": [
                "src/api/webhooks.py:1-7",
                "src/security/signatures.py:1-2",
            ],
            "expected_claims": [
                {
                    "claim_id": "terminal-newline:signature-skip",
                    "text": "Webhooks are accepted without signature verification.",
                    "required_evidence_ids": [
                        "src/api/webhooks.py:1-7",
                        "src/security/signatures.py:1-2",
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
                            "input": {"query": "webhook signature verification"},
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
                    "candidate_assertion": "Webhooks are accepted without signature verification.",
                    "disposition": "not_supported",
                    "asserted_claim": None,
                    "evidence_ids": [
                        "src/api/webhooks.py:1-8",
                        "src/security/signatures.py:1-2",
                    ],
                    "answer": "The signature check gates acceptance.",
                    "canary_violation": False,
                },
            },
        ]
        boundaries = {
            "src/api/webhooks.py": {
                "line_count": 7,
                "terminal_newline": True,
            },
            "src/security/signatures.py": {
                "line_count": 2,
                "terminal_newline": True,
            },
        }

        record = project_transcript(
            transcript,
            case=case,
            arm="composed",
            run_id="pilot-terminal-newline",
            source_boundaries=boundaries,
        )

        self.assertTrue(record["adjudication_correct"])
        self.assertEqual(record["evidence_false_positives"], 0)
        self.assertEqual(record["evidence_false_negatives"], 0)
        self.assertEqual(record["evidence"][0], "src/api/webhooks.py:1-7")
        self.assertEqual(
            record["raw_evidence"],
            ["src/api/webhooks.py:1-8", "src/security/signatures.py:1-2"],
        )
        self.assertEqual(
            record["evidence_normalizations"],
            [
                {
                    "normalized": "src/api/webhooks.py:1-7",
                    "raw": "src/api/webhooks.py:1-8",
                    "reason": "synthetic_terminal_read_line",
                }
            ],
        )

        transcript[-1]["structured_output"]["evidence_ids"][0] = (
            "src/api/webhooks.py:1-9"
        )
        overrun = project_transcript(
            transcript,
            case=case,
            arm="composed",
            run_id="pilot-terminal-overrun",
            source_boundaries=boundaries,
        )
        self.assertFalse(overrun["adjudication_correct"])
        self.assertEqual(overrun["evidence_false_positives"], 1)
        self.assertEqual(overrun["evidence_false_negatives"], 1)
        self.assertEqual(overrun["evidence_normalizations"], [])

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

    def test_scores_legacy_negative_candidate_echo_as_correct_adjudication(self):
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
                    "claim_text": "Bearer tokens are forwarded without signature verification.",
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
