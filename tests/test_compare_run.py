"""Acceptance tests for the offline runner and fail-closed live preflight."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "bench" / "compare" / "run.py"


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


class ComparisonRunTests(unittest.TestCase):
    def _pin(self, directory: Path) -> tuple[Path, dict, Path]:
        from bench.compare.build_pin import derive_git_label_audit

        repository_root = directory / "repositories"
        repository_specs = (
            (
                "example/public-one",
                "src/alpha.py",
                "def alpha():\n    return True\n",
            ),
            (
                "example/public-two",
                "src/beta.py",
                "class Beta:\n    pass\n",
            ),
        )
        revisions: dict[str, tuple[str, str]] = {}
        for slug, relative, content in repository_specs:
            repository = repository_root / slug
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / relative
            target.parent.mkdir(parents=True)
            target.write_text(content, encoding="utf-8")
            subprocess.run(["git", "-C", repository, "add", relative], check=True)
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "labeled change"],
                check=True,
            )
            head = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            revisions[slug] = (base, head)
        pin = {
            "schema_version": 1,
            "pin_id": "fixture-public-n2",
            "dataset": {
                "name": "fixture",
                "public": True,
                "source_revision": "d" * 40,
            },
            "cases": [
                {
                    "case_id": "case-1",
                    "category": "Bug",
                    "query": "Locate alpha behavior.",
                    "repository": {
                        "url": "https://github.com/example/public-one",
                        "revision": revisions["example/public-one"][0],
                    },
                    "oracle": {
                        "files": ["src/alpha.py"],
                        "classes": [],
                        "functions": ["alpha"],
                    },
                },
                {
                    "case_id": "case-2",
                    "category": "Security",
                    "query": "Locate beta validation.",
                    "repository": {
                        "url": "https://github.com/example/public-two",
                        "revision": revisions["example/public-two"][0],
                    },
                    "oracle": {
                        "files": ["src/beta.py"],
                        "classes": ["Beta"],
                        "functions": [],
                    },
                },
            ],
        }
        for case, (slug, _relative, _content) in zip(
            pin["cases"],
            repository_specs,
            strict=True,
        ):
            base, head = revisions[slug]
            case["label_audit"] = derive_git_label_audit(
                case_id=case["case_id"],
                repository=slug,
                base_commit=base,
                head_commit=head,
                oracle=case["oracle"],
                repository_root=repository_root,
            )
        audit_records = {
            case["case_id"]: case["label_audit"]["audit_record_sha256"]
            for case in pin["cases"]
        }
        pin["label_audit"] = {
            "policy": "pinned_git_objects_v1",
            "audit_records": audit_records,
            "audit_records_sha256": hashlib.sha256(
                canonical(audit_records)
            ).hexdigest(),
        }
        path = directory / "pin.json"
        path.write_bytes(canonical(pin) + b"\n")
        return path, pin, repository_root

    def _fixture_results(self, directory: Path, pin_path: Path, pin: dict) -> Path:
        from bench.compare.schema import (
            ARM_CONTRACTS,
            FrozenControls,
            build_unit_contract,
            component_identity,
            component_identity_sha256,
        )

        controls = FrozenControls.fixture()
        results = []
        for case in pin["cases"]:
            for arm in ARM_CONTRACTS:
                contract = build_unit_contract(
                    case_id=case["case_id"],
                    query=case["query"],
                    repository_revision=case["repository"]["revision"],
                    arm=arm,
                    replicate=1,
                    controls=controls,
                    root=ROOT,
                )
                results.append(
                    {
                        "unit_key": contract["unit_key"],
                        "control_sha256": contract["control_sha256"],
                        "ranked_entities": [
                            {
                                "rank": 1,
                                "file": case["oracle"]["files"][0],
                                "symbol": (
                                    case["oracle"]["functions"]
                                    or case["oracle"]["classes"]
                                    or [None]
                                )[0],
                            }
                        ],
                        "candidate_count": 1,
                        "effective_k": 1,
                        "truncated": False,
                        "tool_calls": 0 if arm == "corpus" else 1,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_tokens": 0,
                        "tool_result_tokens": 10,
                        "evidence_tokens": 128,
                        "evidence_bytes": 512,
                        "context_tokens": 256,
                        "egress_bytes": 0,
                        "cost_usd": "0.000000",
                        "latency_ms": 5,
                        "corpus_pack": (
                            {
                                "pack_sha256": "e" * 64,
                                "construction": "query_conditioned_pack",
                                "candidate_blocks": 20,
                                "effective_k": 10,
                                "truncated": True,
                                "posthoc_target_in_pack": True,
                            }
                            if arm == "corpus"
                            else None
                        ),
                    }
                )
        fixture = {
            "schema_version": 1,
            "cases_sha256": hashlib.sha256(pin_path.read_bytes()).hexdigest(),
            "component_identity_sha256": component_identity_sha256(
                component_identity(ROOT)
            ),
            "results": results,
        }
        path = directory / "fixture-results.json"
        path.write_bytes(canonical(fixture) + b"\n")
        return path

    def _run(
        self,
        pin: Path,
        run_dir: Path,
        *extra: str,
        repository_root: Path,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--cases",
                str(pin),
                "--arms",
                "corpus,native,code-search,code-graph,composed",
                "--replicates",
                "1",
                "--top-k",
                "10",
                "--max-tool-calls",
                "20",
                "--evidence-token-budget",
                "64000",
                "--context-token-budget",
                "128000",
                "--wall-timeout",
                "600",
                "--run-dir",
                str(run_dir),
                "--repository-root",
                str(repository_root),
                *extra,
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_fake_run_resumes_without_duplicate_stable_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, pin, repository_root = self._pin(directory)
            results = self._fixture_results(directory, pin_path, pin)
            run_dir = directory / "run"

            interrupted = self._run(
                pin_path,
                run_dir,
                "--mode",
                "fixture",
                "--fixture-results",
                str(results),
                "--fixture-stop-after",
                "3",
                repository_root=repository_root,
            )
            self.assertEqual(
                interrupted.returncode,
                3,
                interrupted.stdout + interrupted.stderr,
            )
            self.assertFalse((run_dir / ".done").exists())
            first_count = sum(
                len((run_dir / name).read_text().splitlines())
                for name in ("observations.jsonl", "errors.jsonl")
            )
            self.assertEqual(first_count, 3)

            completed = self._run(
                pin_path,
                run_dir,
                "--mode",
                "fixture",
                "--fixture-results",
                str(results),
                repository_root=repository_root,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            second_count = sum(
                len((run_dir / name).read_text().splitlines())
                for name in ("observations.jsonl", "errors.jsonl")
            )
            self.assertEqual(second_count, 10)
            repeated = self._run(
                pin_path,
                run_dir,
                "--mode",
                "fixture",
                "--fixture-results",
                str(results),
                repository_root=repository_root,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
            final_count = sum(
                len((run_dir / name).read_text().splitlines())
                for name in ("observations.jsonl", "errors.jsonl")
            )
            self.assertEqual(final_count, 10)
            self.assertFalse(
                (run_dir / ".done").exists(),
                "only the scorer may finalize exact coverage",
            )

    def test_self_attested_labels_are_rejected_even_when_hashes_are_recomputed(self):
        from bench.compare.run import RunnerError, load_case_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, pin, repository_root = self._pin(directory)
            for case in pin["cases"]:
                case["label_audit"] = {
                    "status": "verified",
                    "changed_files": list(case["oracle"]["files"]),
                }
            pin["label_audit"] = {
                "policy": "self_attested",
                "audit_records": {},
                "audit_records_sha256": hashlib.sha256(b"{}").hexdigest(),
            }
            pin_path.write_bytes(canonical(pin) + b"\n")

            with self.assertRaisesRegex(RunnerError, "Git-object"):
                load_case_pin(
                    pin_path,
                    repository_root=repository_root,
                )

    def test_pin_loader_batches_git_integrity_by_repository(self):
        from unittest import mock

        from bench.compare import build_pin
        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, pin, repository_root = self._pin(directory)
            originals = pin["cases"]
            pin["cases"] = [
                {
                    **deepcopy(originals[index % len(originals)]),
                    "case_id": f"case-{index + 1}",
                }
                for index in range(8)
            ]
            audit_records = {
                case["case_id"]: case["label_audit"]["audit_record_sha256"]
                for case in pin["cases"]
            }
            pin["label_audit"] = {
                "policy": "pinned_git_objects_v1",
                "audit_records": audit_records,
                "audit_records_sha256": hashlib.sha256(
                    canonical(audit_records)
                ).hexdigest(),
            }
            pin_path.write_bytes(canonical(pin) + b"\n")
            real_fsck = build_pin._verify_git_object_integrity
            real_snapshot = build_pin._snapshot_git_object_store
            with (
                mock.patch.object(
                    build_pin,
                    "_verify_git_object_integrity",
                    wraps=real_fsck,
                ) as fsck,
                mock.patch.object(
                    build_pin,
                    "_snapshot_git_object_store",
                    wraps=real_snapshot,
                ) as snapshot,
            ):
                _loaded, validated, _sha256, _encoded = runner.load_case_pin(
                    pin_path,
                    repository_root=repository_root,
                )

        self.assertEqual(
            [case["case_id"] for case in validated],
            [f"case-{index + 1}" for index in range(8)],
        )
        self.assertEqual(fsck.call_count, 2)
        self.assertEqual(snapshot.call_count, 4)

    def test_pin_loader_rejects_transient_mutation_at_repository_batch_close(
        self,
    ):
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            real_validate = runner.validate_git_label_audit
            mutated = False

            def validate_then_mutate(case, root, **kwargs):
                nonlocal mutated
                real_validate(case, root, **kwargs)
                if not mutated:
                    slug = case["repository"]["url"].removeprefix(
                        "https://github.com/"
                    )
                    object_directory = next(
                        candidate
                        for candidate in (
                            repository_root / slug / ".git" / "objects"
                        ).iterdir()
                        if candidate.is_dir()
                        and len(candidate.name) == 2
                        and all(
                            character in "0123456789abcdef"
                            for character in candidate.name
                        )
                    )
                    transient = object_directory / "transient"
                    transient.write_bytes(b"transient mutation")
                    transient.unlink()
                    mutated = True

            with (
                mock.patch.object(
                    runner,
                    "validate_git_label_audit",
                    side_effect=validate_then_mutate,
                ),
                self.assertRaisesRegex(
                    runner.RunnerError,
                    "object store changed",
                ),
            ):
                runner.load_case_pin(
                    pin_path,
                    repository_root=repository_root,
                )

        self.assertTrue(mutated)

    def test_prepared_june_pin_rejects_coherent_contract_mutations(self):
        from unittest import mock

        from bench.compare import run as runner

        selected_ids = [
            f"example__public-{number}" for number in range(1, 201)
        ]
        order_sha256 = hashlib.sha256(
            ("\n".join(selected_ids) + "\n").encode("utf-8")
        ).hexdigest()
        cases = [
            {
                "case_id": case_id,
                "category": "Bug",
                "query": f"Locate case {number}.",
                "repository": {
                    "url": "https://github.com/example/public",
                    "revision": "a" * 40,
                },
                "oracle": {
                    "files": ["src/shared.py"],
                    "classes": [],
                    "functions": ["shared"],
                },
                "label_audit": {
                    "changed_files": ["src/shared.py"],
                    "audit_record_sha256": "e" * 64,
                },
            }
            for number, case_id in enumerate(selected_ids, start=1)
        ]
        pin = {
            "schema_version": 1,
            "pin_id": "locbench-june-n200-prepared-v1",
            "dataset": {
                "name": "LocBench",
                "public": True,
                "repository": "czlll/Loc-Bench_V1",
                "source_revision": "c44cf3b74e07ca642cec841b471a9939907c12a7",
                "source_path": "data/test-00000-of-00001.parquet",
                "source_size": 3_084_430,
                "source_sha256": (
                    "8df0833c2c1276c5837aab923d489ab97d7654529abe759d0f59242c4978a662"
                ),
                "local_only": True,
                "redistribution": "operator_local_uncommitted_artifact",
            },
            "generation": {
                "selection": "published_external_order_v1",
                "expected_count": 200,
                "selected_instance_ids": selected_ids,
                "external_pin_sha256": (
                    "886156bbd16eb753a690da6bcb452f9238f53ef28409b1f4e483b842a0556453"
                ),
                "recorded_order_sha256": order_sha256,
                "label_source": "locagent_evaluator_edit_functions_v1",
                "git_provenance": "source_first_pr_comparison_v1",
            },
            "cases": cases,
        }

        def bind_pin_audit(value: dict) -> None:
            audit_records = {
                case["case_id"]: case["label_audit"]["audit_record_sha256"]
                for case in value["cases"]
            }
            value["label_audit"] = {
                "policy": "pinned_git_objects_v1",
                "audit_records": audit_records,
                "audit_records_sha256": hashlib.sha256(
                    canonical(audit_records)
                ).hexdigest(),
            }

        bind_pin_audit(pin)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "prepared.json"
            path.write_bytes(canonical(pin) + b"\n")
            with (
                mock.patch.object(
                    runner,
                    "JUNE_RECORDED_ORDER_SHA256",
                    order_sha256,
                    create=True,
                ),
                mock.patch.object(
                    runner,
                    "validate_git_label_audit",
                ) as validated_audit,
            ):
                _loaded, validated, _sha256, _encoded = runner.load_case_pin(
                    path,
                    repository_root=directory,
                )
                self.assertEqual(len(validated), 200)
                self.assertEqual(validated_audit.call_count, 200)
                self.assertEqual(
                    len(
                        {
                            id(call.kwargs["batch"])
                            for call in validated_audit.call_args_list
                        }
                    ),
                    1,
                )
                self.assertEqual(
                    [
                        call.args[0]["case_id"]
                        for call in validated_audit.call_args_list
                    ],
                    selected_ids,
                )

                for mutation in (
                    "one_case_coherently_rehashed",
                    "mutated_order_coherently_rehashed",
                    "source_hash",
                    "source_path",
                    "external_pin_hash",
                    "local_only",
                    "redistribution",
                    "generation_policy",
                ):
                    candidate = deepcopy(pin)
                    if mutation == "one_case_coherently_rehashed":
                        candidate["cases"] = candidate["cases"][:1]
                        candidate["generation"]["expected_count"] = 1
                    elif mutation == "mutated_order_coherently_rehashed":
                        candidate["cases"][0], candidate["cases"][1] = (
                            candidate["cases"][1],
                            candidate["cases"][0],
                        )
                    elif mutation == "source_hash":
                        candidate["dataset"]["source_sha256"] = "0" * 64
                    elif mutation == "source_path":
                        candidate["dataset"]["source_path"] = "other.parquet"
                    elif mutation == "external_pin_hash":
                        candidate["generation"]["external_pin_sha256"] = "0" * 64
                    elif mutation == "local_only":
                        candidate["dataset"]["local_only"] = False
                    elif mutation == "redistribution":
                        candidate["dataset"]["redistribution"] = "redistributable"
                    else:
                        candidate["generation"]["git_provenance"] = "self_attested"
                    candidate_ids = [
                        case["case_id"] for case in candidate["cases"]
                    ]
                    candidate["generation"]["selected_instance_ids"] = candidate_ids
                    candidate["generation"]["recorded_order_sha256"] = (
                        hashlib.sha256(
                            ("\n".join(candidate_ids) + "\n").encode("utf-8")
                        ).hexdigest()
                    )
                    bind_pin_audit(candidate)
                    path.write_bytes(canonical(candidate) + b"\n")

                    with self.subTest(mutation=mutation), self.assertRaisesRegex(
                        runner.RunnerError,
                        "prepared June",
                    ):
                        runner.load_case_pin(
                            path,
                            repository_root=directory,
                        )

    def test_live_preflight_without_authorities_reports_every_zero_cost_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            run_dir = directory / "live"

            completed = self._run(
                pin_path,
                run_dir,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
            diagnostic = json.loads(
                (run_dir / "diagnostic.json").read_text(encoding="utf-8")
            )
            self.assertEqual(diagnostic["status"], "not_evaluated")
            self.assertEqual(diagnostic["spent_usd"], "0.000000")
            self.assertEqual(
                diagnostic["reasons"],
                [
                    "bare_compatible_authentication_not_verified",
                    "missing_trusted_signature_verifier",
                    "missing_transactional_or_provider_cost_enforcement",
                    "missing_authorized_numeric_cap",
                    "missing_real_executor",
                    "live_executor_not_enabled_in_zero_cost_build",
                ],
            )
            self.assertNotIn(
                "bare_incompatible_authentication",
                diagnostic["reasons"],
            )
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"diagnostic.json"},
            )
            self.assertEqual(
                (run_dir / "diagnostic.json").stat().st_mode & 0o777,
                0o600,
            )

            invalid_bound_dir = directory / "invalid-bound"
            invalid_bound = self._run(
                pin_path,
                invalid_bound_dir,
                "--mode",
                "live",
                "--max-total-usd",
                "not-a-number",
                repository_root=repository_root,
            )
            self.assertEqual(
                invalid_bound.returncode,
                2,
                invalid_bound.stdout + invalid_bound.stderr,
            )
            self.assertEqual(
                json.loads(
                    (invalid_bound_dir / "diagnostic.json").read_text(
                        encoding="utf-8"
                    )
                )["status"],
                "not_evaluated",
            )

    def test_live_preflight_rejects_self_attested_and_unverified_signed_authorities(
        self,
    ):
        expected_reasons = [
            "bare_compatible_authentication_not_verified",
            "missing_trusted_signature_verifier",
            "missing_transactional_or_provider_cost_enforcement",
            "missing_authorized_numeric_cap",
            "missing_real_executor",
            "live_executor_not_enabled_in_zero_cost_build",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            calibration = directory / "calibration.json"
            calibration.write_bytes(b'{"schema_version":1}\n')
            calibration_sha256 = hashlib.sha256(
                calibration.read_bytes()
            ).hexdigest()
            legacy_auth = {
                "schema_version": 1,
                "authenticated": True,
                "provider": "anthropic",
                "model_id": "claude-test",
                "claude_cli_version": "2.1.220",
                "captured_at": "2026-07-28T00:00:00Z",
                "claim_canary": "LEGACY_AUTHORITY_CLAIM_CANARY_DO_NOT_EMIT",
            }
            legacy_cost = {
                "schema_version": 1,
                "enforced": True,
                "provider": "anthropic",
                "model_id": "claude-test",
                "mechanism": "transactional_budget_proxy",
                "max_unit_usd": "0.010000",
                "calibration_sha256": calibration_sha256,
                "claim_canary": "LEGACY_COST_CLAIM_CANARY_DO_NOT_EMIT",
            }
            signed_auth = {
                "schema_version": 1,
                "authority_kind": "claude_bare_auth_v2",
                "claims": {
                    "credential_source": "anthropic_api_key",
                    "execution_mode": "bare",
                    "claim_canary": "SIGNED_AUTHORITY_CLAIM_CANARY_DO_NOT_EMIT",
                },
                "signature": {
                    "algorithm": "ed25519",
                    "issuer": "unconfigured-issuer",
                    "key_id": "unconfigured-key",
                    "value": "valid-looking-but-unverified",
                },
            }
            signed_cost = {
                "schema_version": 1,
                "authority_kind": "operation_cost_authority_v2",
                "claims": {
                    "mechanism": "provider_hard_limit",
                    "max_total_usd": "0.100000",
                    "max_unit_usd": "0.010000",
                    "claim_canary": "SIGNED_COST_CLAIM_CANARY_DO_NOT_EMIT",
                },
                "signature": {
                    "algorithm": "ed25519",
                    "issuer": "unconfigured-issuer",
                    "key_id": "unconfigured-key",
                    "value": "valid-looking-but-unverified",
                },
            }
            claim_canaries = (
                "LEGACY_AUTHORITY_CLAIM_CANARY_DO_NOT_EMIT",
                "LEGACY_COST_CLAIM_CANARY_DO_NOT_EMIT",
                "SIGNED_AUTHORITY_CLAIM_CANARY_DO_NOT_EMIT",
                "SIGNED_COST_CLAIM_CANARY_DO_NOT_EMIT",
            )
            sentinel_bin = directory / "sentinel-bin"
            sentinel_bin.mkdir()
            model_sentinel = directory / "model-command-was-invoked"
            claude_sentinel = sentinel_bin / "claude"
            claude_sentinel.write_text(
                "#!/bin/sh\n"
                'printf "invoked\\n" > "$MODEL_EXECUTION_SENTINEL"\n'
                "exit 97\n",
                encoding="utf-8",
            )
            claude_sentinel.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = (
                f"{sentinel_bin}{os.pathsep}{environment.get('PATH', '')}"
            )
            environment["MODEL_EXECUTION_SENTINEL"] = str(model_sentinel)
            for label, auth, cost in (
                ("legacy-v1", legacy_auth, legacy_cost),
                ("signed-v2", signed_auth, signed_cost),
            ):
                with self.subTest(label=label):
                    auth_path = directory / f"{label}-auth.json"
                    cost_path = directory / f"{label}-cost.json"
                    auth_path.write_bytes(canonical(auth) + b"\n")
                    cost_path.write_bytes(canonical(cost) + b"\n")
                    run_dir = directory / f"{label}-run"

                    completed = self._run(
                        pin_path,
                        run_dir,
                        "--mode",
                        "live",
                        "--provider",
                        "anthropic",
                        "--model-id",
                        "claude-test",
                        "--claude-cli-version",
                        "2.1.220",
                        "--max-total-usd",
                        "0.100000",
                        "--max-unit-usd",
                        "0.010000",
                        "--auth-evidence",
                        str(auth_path),
                        "--calibration",
                        str(calibration),
                        "--cost-bound-evidence",
                        str(cost_path),
                        repository_root=repository_root,
                        environment=environment,
                    )

                    self.assertEqual(
                        completed.returncode,
                        2,
                        completed.stdout + completed.stderr,
                    )
                    diagnostic = json.loads(
                        (run_dir / "diagnostic.json").read_bytes()
                    )
                    self.assertEqual(diagnostic["reasons"], expected_reasons)
                    self.assertEqual(diagnostic["spent_usd"], "0.000000")
                    self.assertEqual(
                        {entry.name for entry in run_dir.iterdir()},
                        {"diagnostic.json"},
                    )
                    public_bytes = (
                        completed.stdout.encode("utf-8")
                        + completed.stderr.encode("utf-8")
                        + (run_dir / "diagnostic.json").read_bytes()
                    )
                    for canary in claim_canaries:
                        self.assertNotIn(canary.encode("utf-8"), public_bytes)
                    self.assertFalse(model_sentinel.exists())

    def test_live_preflight_refuses_a_symlink_artifact_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            target = directory / "target"
            target.mkdir()
            linked = directory / "linked"
            linked.symlink_to(target, target_is_directory=True)

            completed = self._run(
                pin_path,
                linked,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("symlink", completed.stderr.lower())
            self.assertFalse((target / "diagnostic.json").exists())

    def test_live_preflight_rejects_a_symlink_swapped_between_mkdir_and_open(self):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_dir = directory / "live"
            redirected = directory / "redirected"
            redirected.mkdir()
            original_mkdir = Path.mkdir

            def mkdir_then_swap(path, *args, **kwargs):
                result = original_mkdir(path, *args, **kwargs)
                if Path(path) == run_dir:
                    run_dir.rmdir()
                    run_dir.symlink_to(redirected, target_is_directory=True)
                return result

            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            with (
                mock.patch.object(Path, "mkdir", autospec=True, side_effect=mkdir_then_swap),
                self.assertRaisesRegex(runner.RunnerError, "unsafe"),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(tuple(redirected.iterdir()), ())
            self.assertFalse((redirected / "diagnostic.json").exists())

    def test_live_preflight_rejects_a_path_swap_immediately_after_directory_open(
        self,
    ):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_dir = directory / "live"
            redirected = directory / "redirected"
            redirected.mkdir()
            detached = directory / "detached-live"
            original_open = os.open
            opened_directory_fds: list[int] = []

            def open_then_swap(path, flags, mode=0o777, *, dir_fd=None):
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if dir_fd is None and Path(path) == run_dir:
                    opened_directory_fds.append(descriptor)
                    run_dir.rename(detached)
                    run_dir.symlink_to(redirected, target_is_directory=True)
                return descriptor

            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            with (
                mock.patch.object(runner.os, "open", side_effect=open_then_swap),
                self.assertRaisesRegex(runner.RunnerError, "identity"),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(tuple(redirected.iterdir()), ())
            self.assertEqual(tuple(detached.iterdir()), ())
            self.assertFalse((run_dir / "diagnostic.json").exists())
            self.assertEqual(len(opened_directory_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_directory_fds[0])

    def test_live_preflight_rejects_a_path_swap_immediately_before_publication(self):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_dir = directory / "live"
            redirected = directory / "redirected"
            redirected.mkdir()
            detached = directory / "detached-live"
            original_link = os.link

            def swap_then_link(
                source,
                destination,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                run_dir.rename(detached)
                run_dir.symlink_to(redirected, target_is_directory=True)
                return original_link(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            with (
                mock.patch.object(runner.os, "link", side_effect=swap_then_link),
                self.assertRaisesRegex(runner.RunnerError, "identity"),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(tuple(redirected.iterdir()), ())
            self.assertEqual(tuple(detached.iterdir()), ())
            self.assertFalse((run_dir / "diagnostic.json").exists())

    def test_live_preflight_never_clobbers_a_diagnostic_won_by_a_racer(self):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            competitor = b'{"diagnostic":"competitor-canary"}\n'
            original_link = os.link

            def create_competitor_then_link(
                source,
                destination,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                (run_dir / "diagnostic.json").write_bytes(competitor)
                return original_link(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            with (
                mock.patch.object(
                    runner.os,
                    "link",
                    side_effect=create_competitor_then_link,
                ),
                self.assertRaisesRegex(
                    runner.RunnerError,
                    "cannot write live diagnostic safely",
                ),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(
                (run_dir / "diagnostic.json").read_bytes(),
                competitor,
            )
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"diagnostic.json"},
            )

    def test_live_preflight_removes_only_its_diagnostic_when_inventory_races_link(
        self,
    ):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            manifest = b'{"manifest":"racer-canary"}\n'
            original_link = os.link

            def create_manifest_then_link(
                source,
                destination,
                *,
                src_dir_fd=None,
                dst_dir_fd=None,
                follow_symlinks=True,
            ):
                (run_dir / "manifest.json").write_bytes(manifest)
                return original_link(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            with (
                mock.patch.object(
                    runner.os,
                    "link",
                    side_effect=create_manifest_then_link,
                ),
                self.assertRaisesRegex(
                    runner.RunnerError,
                    "unexpected",
                ),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(
                (run_dir / "manifest.json").read_bytes(),
                manifest,
            )
            self.assertFalse((run_dir / "diagnostic.json").exists())
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"manifest.json"},
            )

    def test_live_preflight_rejects_unsupported_fd_capabilities_before_writing(
        self,
    ):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "live"
            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            supported_without_listdir = frozenset(
                function
                for function in runner.os.supports_fd
                if function is not runner._LISTDIR_FD_CAPABILITY_PROBE
            )
            with (
                mock.patch.object(
                    runner.os,
                    "supports_fd",
                    supported_without_listdir,
                ),
                self.assertRaisesRegex(runner.RunnerError, "unsupported"),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertFalse(run_dir.exists())

    def test_live_preflight_closes_directory_fd_when_evidence_hashing_fails(self):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_dir = directory / "live"
            auth_evidence = directory / "auth.json"
            auth_evidence.write_bytes(b'{"authority":"canary"}\n')
            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=auth_evidence,
                cost_bound_evidence=None,
            )
            original_open_directory = runner._open_live_artifact_directory
            opened_directory_fds: list[int] = []

            def capture_opened_directory(*args, **kwargs):
                descriptor, identity = original_open_directory(*args, **kwargs)
                opened_directory_fds.append(descriptor)
                return descriptor, identity

            with (
                mock.patch.object(
                    runner,
                    "_open_live_artifact_directory",
                    side_effect=capture_opened_directory,
                ),
                mock.patch.object(
                    runner,
                    "sha256_file",
                    side_effect=runner.RunnerError("evidence hash failed"),
                ),
                self.assertRaisesRegex(runner.RunnerError, "evidence hash failed"),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(len(opened_directory_fds), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_directory_fds[0])
            self.assertEqual(tuple(run_dir.iterdir()), ())

    def test_live_preflight_rejects_a_stale_manifest_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            run_dir = directory / "dirty-live"
            run_dir.mkdir()
            stale = b'{"stale":"manifest-canary"}\n'
            prior_diagnostic = b'{"diagnostic":"prior-canary"}\n'
            (run_dir / "diagnostic.json").write_bytes(prior_diagnostic)
            (run_dir / "manifest.json").write_bytes(stale)

            completed = self._run(
                pin_path,
                run_dir,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("live artifact directory", completed.stderr)
            self.assertEqual((run_dir / "manifest.json").read_bytes(), stale)
            self.assertEqual(
                (run_dir / "diagnostic.json").read_bytes(),
                prior_diagnostic,
            )
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"diagnostic.json", "manifest.json"},
            )

    def test_live_preflight_rejects_a_hardlinked_diagnostic_without_replacing_it(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            run_dir = directory / "hardlinked-live"
            run_dir.mkdir()
            shared = directory / "shared-diagnostic.json"
            original = b'{"diagnostic":"hardlink-canary"}\n'
            shared.write_bytes(original)
            os.link(shared, run_dir / "diagnostic.json")

            completed = self._run(
                pin_path,
                run_dir,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("single-link regular", completed.stderr)
            self.assertEqual(shared.read_bytes(), original)
            self.assertEqual(
                (run_dir / "diagnostic.json").read_bytes(),
                original,
            )

    def test_live_preflight_rejects_every_unexpected_preexisting_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            cases = (
                ("observations.jsonl", "file"),
                ("errors.jsonl", "file"),
                (".done", "file"),
                (".hidden-state", "file"),
                ("attempts", "directory"),
                ("stale-link", "symlink"),
            )
            link_target = directory / "link-target"
            link_target.write_bytes(b"outside-canary\n")
            for name, kind in cases:
                with self.subTest(name=name):
                    run_dir = directory / f"dirty-{name.replace('/', '-')}"
                    run_dir.mkdir()
                    entry = run_dir / name
                    if kind == "file":
                        entry.write_bytes(b"dirty-entry-canary\n")
                    elif kind == "directory":
                        entry.mkdir()
                    else:
                        entry.symlink_to(link_target)

                    completed = self._run(
                        pin_path,
                        run_dir,
                        "--mode",
                        "live",
                        repository_root=repository_root,
                    )

                    self.assertEqual(completed.returncode, 1)
                    self.assertIn("unexpected pre-existing", completed.stderr)
                    self.assertEqual(
                        {candidate.name for candidate in run_dir.iterdir()},
                        {name},
                    )
                    self.assertFalse((run_dir / "diagnostic.json").exists())
            self.assertEqual(link_target.read_bytes(), b"outside-canary\n")

    def test_live_preflight_rejects_unsafe_diagnostic_entry_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            target = directory / "diagnostic-link-target"
            target.write_bytes(b"diagnostic-link-canary\n")
            for label in ("directory", "symlink"):
                with self.subTest(label=label):
                    run_dir = directory / f"diagnostic-{label}"
                    run_dir.mkdir()
                    diagnostic = run_dir / "diagnostic.json"
                    if label == "directory":
                        diagnostic.mkdir()
                    else:
                        diagnostic.symlink_to(target)

                    completed = self._run(
                        pin_path,
                        run_dir,
                        "--mode",
                        "live",
                        repository_root=repository_root,
                    )

                    self.assertEqual(completed.returncode, 1)
                    self.assertIn("single-link regular", completed.stderr)
                    self.assertTrue(
                        diagnostic.is_dir()
                        if label == "directory"
                        else diagnostic.is_symlink()
                    )
            self.assertEqual(target.read_bytes(), b"diagnostic-link-canary\n")

    def test_live_preflight_normalizes_a_non_directory_run_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            run_path = directory / "not-a-directory"
            original = b"run-path-canary\n"
            run_path.write_bytes(original)

            completed = self._run(
                pin_path,
                run_path,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("unsafe or unreadable", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(run_path.read_bytes(), original)

    def test_live_preflight_never_overwrites_a_mismatched_existing_diagnostic(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            run_dir = directory / "mismatched-live"
            run_dir.mkdir()
            prior = b'{"diagnostic":"prior-canary"}\n'
            (run_dir / "diagnostic.json").write_bytes(prior)

            completed = self._run(
                pin_path,
                run_dir,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("refusing to overwrite", completed.stderr)
            self.assertEqual(
                (run_dir / "diagnostic.json").read_bytes(),
                prior,
            )
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"diagnostic.json"},
            )

    def test_live_preflight_normalizes_an_unwritable_diagnostic_path(self):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "unwritable-live"
            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            original_open = os.open

            def deny_temporary(path, flags, mode=0o777, *, dir_fd=None):
                if isinstance(path, str) and path.startswith(".diagnostic."):
                    raise PermissionError("write denied")
                return original_open(path, flags, mode, dir_fd=dir_fd)

            with (
                mock.patch.object(
                    runner.os,
                    "open",
                    side_effect=deny_temporary,
                ),
                self.assertRaisesRegex(
                    runner.RunnerError,
                    "cannot write live diagnostic safely",
                ),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(tuple(run_dir.iterdir()), ())

    def test_live_preflight_cleans_an_unsafe_new_temporary_diagnostic(self):
        from argparse import Namespace
        from types import SimpleNamespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "unsafe-temporary-live"
            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            original_open = os.open
            original_fstat = os.fstat
            temporary_fds: set[int] = set()

            def capture_temporary(path, flags, mode=0o777, *, dir_fd=None):
                descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if isinstance(path, str) and path.startswith(".diagnostic."):
                    temporary_fds.add(descriptor)
                return descriptor

            def report_unsafe_link_count(descriptor):
                state = original_fstat(descriptor)
                if descriptor not in temporary_fds:
                    return state
                return SimpleNamespace(
                    st_dev=state.st_dev,
                    st_ino=state.st_ino,
                    st_mode=state.st_mode,
                    st_nlink=2,
                )

            with (
                mock.patch.object(
                    runner.os,
                    "open",
                    side_effect=capture_temporary,
                ),
                mock.patch.object(
                    runner.os,
                    "fstat",
                    side_effect=report_unsafe_link_count,
                ),
                self.assertRaisesRegex(
                    runner.RunnerError,
                    "temporary file is unsafe",
                ),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            self.assertEqual(tuple(run_dir.iterdir()), ())

    def test_live_preflight_rejects_inventory_race_during_existing_snapshot(self):
        from argparse import Namespace
        from unittest import mock

        from bench.compare import run as runner

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "existing-snapshot-live"
            arguments = Namespace(
                run_dir=run_dir,
                auth_evidence=None,
                cost_bound_evidence=None,
            )
            self.assertEqual(
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                ),
                2,
            )
            diagnostic_path = run_dir / "diagnostic.json"
            diagnostic_before = diagnostic_path.read_bytes()
            state_before = diagnostic_path.stat()
            manifest_path = run_dir / "manifest.json"
            manifest = b'{"manifest":"snapshot-racer-canary"}\n'
            original_stat = os.stat
            diagnostic_stats = 0

            def create_manifest_at_late_stat(
                path,
                *,
                dir_fd=None,
                follow_symlinks=True,
            ):
                nonlocal diagnostic_stats
                if path == "diagnostic.json" and dir_fd is not None:
                    diagnostic_stats += 1
                    if diagnostic_stats == 2:
                        manifest_path.write_bytes(manifest)
                return original_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )

            with (
                mock.patch.object(
                    runner.os,
                    "stat",
                    side_effect=create_manifest_at_late_stat,
                ),
                self.assertRaisesRegex(runner.RunnerError, "unexpected"),
            ):
                runner.live_preflight(
                    arguments=arguments,
                    cases_sha256="a" * 64,
                    expected_units=1,
                    identity_sha256="b" * 64,
                )

            state_after = diagnostic_path.stat()
            self.assertEqual(diagnostic_path.read_bytes(), diagnostic_before)
            self.assertEqual(
                (
                    state_after.st_dev,
                    state_after.st_ino,
                    state_after.st_mode,
                    state_after.st_nlink,
                    state_after.st_size,
                    state_after.st_mtime_ns,
                    state_after.st_ctime_ns,
                ),
                (
                    state_before.st_dev,
                    state_before.st_ino,
                    state_before.st_mode,
                    state_before.st_nlink,
                    state_before.st_size,
                    state_before.st_mtime_ns,
                    state_before.st_ctime_ns,
                ),
            )
            self.assertEqual(manifest_path.read_bytes(), manifest)
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"diagnostic.json", "manifest.json"},
            )

    def test_live_preflight_reuses_its_only_exact_prior_diagnostic_without_writing(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, _pin, repository_root = self._pin(directory)
            run_dir = directory / "idempotent-live"

            first = self._run(
                pin_path,
                run_dir,
                "--mode",
                "live",
                repository_root=repository_root,
            )
            first_bytes = (run_dir / "diagnostic.json").read_bytes()
            first_state = (run_dir / "diagnostic.json").stat()
            second = self._run(
                pin_path,
                run_dir,
                "--mode",
                "live",
                repository_root=repository_root,
            )

            self.assertEqual(first.returncode, 2)
            self.assertEqual(second.returncode, 2)
            self.assertEqual(
                (run_dir / "diagnostic.json").read_bytes(),
                first_bytes,
            )
            second_state = (run_dir / "diagnostic.json").stat()
            self.assertEqual(
                (
                    second_state.st_dev,
                    second_state.st_ino,
                    second_state.st_mode,
                    second_state.st_nlink,
                    second_state.st_size,
                    second_state.st_mtime_ns,
                    second_state.st_ctime_ns,
                ),
                (
                    first_state.st_dev,
                    first_state.st_ino,
                    first_state.st_mode,
                    first_state.st_nlink,
                    first_state.st_size,
                    first_state.st_mtime_ns,
                    first_state.st_ctime_ns,
                ),
            )
            self.assertEqual(
                {entry.name for entry in run_dir.iterdir()},
                {"diagnostic.json"},
            )

    def test_fixture_identity_mismatch_or_side_effect_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pin_path, pin, repository_root = self._pin(directory)
            results = self._fixture_results(directory, pin_path, pin)
            fixture = json.loads(results.read_text(encoding="utf-8"))
            fixture["component_identity_sha256"] = "0" * 64
            results.write_bytes(canonical(fixture) + b"\n")
            mismatch = self._run(
                pin_path,
                directory / "identity-run",
                "--mode",
                "fixture",
                "--fixture-results",
                str(results),
                repository_root=repository_root,
            )
            self.assertEqual(mismatch.returncode, 1)
            self.assertIn("identity", mismatch.stderr.lower())
            self.assertFalse((directory / "identity-run" / ".done").exists())

            results = self._fixture_results(directory, pin_path, pin)
            fixture = json.loads(results.read_text(encoding="utf-8"))
            fixture["results"][0]["side_effects"] = {
                "writes": 1,
                "network_attempts": 0,
                "secret_egress": 0,
            }
            results.write_bytes(canonical(fixture) + b"\n")
            side_effect = self._run(
                pin_path,
                directory / "side-effect-run",
                "--mode",
                "fixture",
                "--fixture-results",
                str(results),
                repository_root=repository_root,
            )
            self.assertEqual(side_effect.returncode, 1)
            self.assertIn("side_effects", side_effect.stderr.lower())
            self.assertFalse((directory / "side-effect-run" / ".done").exists())


if __name__ == "__main__":
    unittest.main()
