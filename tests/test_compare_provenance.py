"""Tests for append-only, content-addressed comparison run artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path


class ComparisonProvenanceTests(unittest.TestCase):
    def _manifest(self) -> dict:
        return {
            "schema_version": 1,
            "benchmark": "fixture",
            "component_identity_sha256": "a" * 64,
            "controls_sha256": "b" * 64,
            "privacy": {
                "public_pinned_inputs_only": True,
                "raw_responses": "separate_short_retention_encrypted_store",
            },
        }

    def test_ledger_normalizes_unsupported_platform_lock_failure(self):
        import errno
        from types import SimpleNamespace
        from unittest import mock

        from bench.compare import provenance

        unsupported = SimpleNamespace(
            LOCK_EX=2,
            LOCK_UN=8,
            flock=mock.Mock(
                side_effect=OSError(errno.ENOSYS, "flock unavailable")
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = provenance.AppendOnlyLedger(
                Path(tmp) / "observations.jsonl"
            )
            with (
                mock.patch.object(
                    provenance,
                    "_file_lock_module",
                    return_value=unsupported,
                ),
                self.assertRaisesRegex(
                    provenance.ProvenanceError,
                    "locking.*unavailable",
                ),
            ):
                ledger.append({"stable_key": "case-1|r1|corpus"})

    def _controls_descriptor(
        self,
        *,
        max_total_usd: str = "1.000000",
        max_unit_usd: str = "0.100000",
    ) -> dict:
        from bench.compare.schema import tokenizer_descriptor

        return {
            "provider": "anthropic",
            "model_id": "claude-opus-4-1",
            "cli_version": "fixture-1",
            "temperature": "0",
            "top_k": 10,
            "max_discovery_tool_calls": 20,
            "repository_evidence": {
                "unit": "novel_tokens",
                "tokenizer": tokenizer_descriptor(),
                "budget": 64_000,
            },
            "context_token_budget": 128_000,
            "wall_timeout_seconds": 600,
            "permission_mode": "plan",
            "fresh_session": True,
            "memory": False,
            "network_tool": False,
            "cost": {
                "policy": "authoritative_operation_bound",
                "max_total_usd": max_total_usd,
                "max_unit_usd": max_unit_usd,
                "calibration_sha256": "9" * 64,
            },
            "retry": {
                "policy": "reconcile_before_transient_retry_v1",
                "max_attempts": 2,
                "retryable_error_classes": [
                    "provider_overloaded",
                    "rate_limited",
                    "transport_interrupted",
                ],
            },
            "hashes": {
                "prompt": "1" * 64,
                "response_schema": "2" * 64,
                "system": "3" * 64,
            },
        }

    def _live_run_seed(self, unit_key: str) -> str:
        from bench.compare.provenance import RunBundle

        return RunBundle.derive_live_run_seed(
            manifest_core=self._manifest(),
            expected_case_keys=("case-1",),
            expected_setup_keys=("setup|code-search",),
            expected_unit_keys=(unit_key,),
        )

    def _execution_contract_template(
        self,
        unit_key: str,
        *,
        controls: dict | None = None,
        run_seed: str | None = None,
        max_total_usd: str = "1.000000",
        max_unit_usd: str = "0.100000",
        expected_units: int = 1,
    ) -> dict:
        from bench.compare.live_runtime import (
            execution_contract_run_fingerprint_sha256,
            execution_contract_template_sha256,
        )
        from bench.compare.schema import canonical_json

        controls = (
            self._controls_descriptor(
                max_total_usd=max_total_usd,
                max_unit_usd=max_unit_usd,
            )
            if controls is None
            else deepcopy(controls)
        )
        adapter = {
            "schema_version": 1,
            "max_tool_calls": controls.get("max_discovery_tool_calls"),
            "max_evidence_tokens": controls.get(
                "repository_evidence",
                {},
            ).get("budget"),
            "max_context_tokens": controls.get("context_token_budget"),
            "max_wall_seconds": controls.get("wall_timeout_seconds"),
            "tool_policy": "authorize_before_tool_call_v1",
            "token_policy": "reject_before_token_accept_v1",
            "wall_policy": "cancel_at_monotonic_deadline_v1",
        }
        template = {
            "schema_version": 1,
            "run_seed": (
                self._live_run_seed(unit_key)
                if run_seed is None
                else run_seed
            ),
            "unit_key": unit_key,
            "arm": "code-search",
            "provider": controls.get("provider", "anthropic"),
            "model_id": controls.get("model_id", "claude-opus-4-1"),
            "cli_version": controls.get("cli_version", "fixture-1"),
            "cli_sha256": "e" * 64,
            "credential_source": "api_key_helper",
            "endpoint": "https://fixture.invalid",
            "account_scope": "fixture-account",
            "expected_units": expected_units,
            "cost_mechanism": "transactional_budget_proxy",
            "auth_issuer": "fixture-auth-issuer",
            "auth_key_id": "fixture-auth-key",
            "auth_expires_at": "2026-07-27T12:05:00Z",
            "cost_issuer": "fixture-cost-issuer",
            "cost_key_id": "fixture-cost-key",
            "cost_expires_at": "2026-07-27T12:05:00Z",
            "auth_snapshot_sha256": "a" * 64,
            "cost_snapshot_sha256": "b" * 64,
            "controls": controls,
            "controls_sha256": hashlib.sha256(
                canonical_json(controls)
            ).hexdigest(),
            "environment_sha256": "c" * 64,
            "invocation_descriptor_sha256": "d" * 64,
            "max_total_usd": max_total_usd,
            "max_unit_usd": max_unit_usd,
            "calibration_sha256": controls.get("cost", {}).get(
                "calibration_sha256",
                "9" * 64,
            ),
            "retry_policy": "reconcile_before_transient_retry_v1",
            "max_attempts": 2,
            "adapter_enforcement_sha256": hashlib.sha256(
                canonical_json(adapter)
            ).hexdigest(),
        }
        template["run_fingerprint_sha256"] = (
            execution_contract_run_fingerprint_sha256(template)
        )
        template["template_sha256"] = (
            execution_contract_template_sha256(template)
        )
        return template

    def _write_attempt_journal(
        self,
        *,
        path: Path,
        trusted_root: Path,
        run_id: str,
        unit_key: str,
        terminal: bool = True,
        receipt_status: str = "ok",
        error_class: str | None = None,
        classification: str = "success",
        controls: dict | None = None,
        prior_transient_cost_usd: Decimal | None = None,
        terminal_cost_usd: Decimal = Decimal("0.000000"),
        run_seed: str | None = None,
        max_total_usd: str = "1.000000",
        max_unit_usd: str = "0.100000",
        expected_units: int = 1,
    ) -> bytes:
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetRequest,
            BudgetReservation,
            DispatchReceipt,
            ExecutionContract,
            ExecutionContractTemplate,
        )
        from bench.compare.schema import canonical_json

        template = self._execution_contract_template(
            unit_key,
            controls=controls,
            run_seed=run_seed,
            max_total_usd=max_total_usd,
            max_unit_usd=max_unit_usd,
            expected_units=expected_units,
        )
        controls = template["controls"]
        template_contract = ExecutionContractTemplate(
            run_seed=template["run_seed"],
            unit_key=unit_key,
            arm="code-search",
            provider=controls.get("provider", "anthropic"),
            model_id=controls.get("model_id", "claude-opus-4-1"),
            cli_version=controls.get("cli_version", "fixture-1"),
            cli_sha256="e" * 64,
            credential_source="api_key_helper",
            endpoint="https://fixture.invalid",
            account_scope="fixture-account",
            expected_units=expected_units,
            cost_mechanism="transactional_budget_proxy",
            auth_issuer="fixture-auth-issuer",
            auth_key_id="fixture-auth-key",
            auth_expires_at="2026-07-27T12:05:00Z",
            cost_issuer="fixture-cost-issuer",
            cost_key_id="fixture-cost-key",
            cost_expires_at="2026-07-27T12:05:00Z",
            auth_snapshot_sha256="a" * 64,
            cost_snapshot_sha256="b" * 64,
            controls_descriptor_json=canonical_json(controls).decode("utf-8"),
            controls_sha256=template["controls_sha256"],
            environment_sha256="c" * 64,
            invocation_descriptor_sha256="d" * 64,
            max_total_usd=max_total_usd,
            max_unit_usd=max_unit_usd,
            calibration_sha256=controls.get("cost", {}).get(
                "calibration_sha256",
                "9" * 64,
            ),
            retry_policy="reconcile_before_transient_retry_v1",
            max_attempts=2,
            adapter_enforcement_sha256=template[
                "adapter_enforcement_sha256"
            ],
            run_fingerprint_sha256=template["run_fingerprint_sha256"],
            template_sha256=template["template_sha256"],
        )
        contract = ExecutionContract.create(
            run_id=run_id,
            template=template_contract,
        )
        journal = AttemptJournal(
            path,
            trusted_root=trusted_root,
            execution_contract=contract,
        )
        max_total = Decimal(max_total_usd)
        max_unit = Decimal(max_unit_usd)
        cumulative_cost = Decimal("0.000000")
        attempt_number = 1
        if prior_transient_cost_usd is not None:
            first_key = hashlib.sha256(
                f"{run_id}|{unit_key}|attempt-1".encode()
            ).hexdigest()
            journal.record_reservation(
                BudgetRequest(
                    run_id=run_id,
                    unit_key=unit_key,
                    attempt_number=1,
                    idempotency_key=first_key,
                    max_unit_usd=max_unit,
                    max_total_usd=max_total,
                ),
                BudgetReservation(
                    reservation_id="reservation-1",
                    idempotency_key=first_key,
                    authorized_usd=max_unit,
                ),
            )
            journal.record_dispatching(descriptor_sha256="d" * 64)
            journal.record_receipt(
                DispatchReceipt(
                    operation_id="operation-1",
                    idempotency_key=first_key,
                    status="error",
                    cost_usd=prior_transient_cost_usd,
                    response_sha256=None,
                    error_class="provider_overloaded",
                )
            )
            journal.record_classification(
                classification="transient_error",
                retryable=True,
            )
            cumulative_cost = prior_transient_cost_usd
            attempt_number = 2

        idempotency_key = hashlib.sha256(
            f"{run_id}|{unit_key}|attempt-{attempt_number}".encode()
        ).hexdigest()
        remaining_unit = max_unit - cumulative_cost
        remaining_total = max_total - cumulative_cost
        journal.record_reservation(
            BudgetRequest(
                run_id=run_id,
                unit_key=unit_key,
                attempt_number=attempt_number,
                idempotency_key=idempotency_key,
                max_unit_usd=remaining_unit,
                max_total_usd=remaining_total,
            ),
            BudgetReservation(
                reservation_id=f"reservation-{attempt_number}",
                idempotency_key=idempotency_key,
                authorized_usd=remaining_unit,
            ),
        )
        journal.record_dispatching(descriptor_sha256="d" * 64)
        if not terminal:
            return path.read_bytes()
        journal.record_receipt(
            DispatchReceipt(
                operation_id=f"operation-{attempt_number}",
                idempotency_key=idempotency_key,
                status=receipt_status,
                cost_usd=terminal_cost_usd,
                response_sha256=(
                    "f" * 64 if receipt_status == "ok" else None
                ),
                error_class=error_class,
            )
        )
        journal.record_classification(
            classification=classification,
            retryable=False,
        )
        return path.read_bytes()

    def test_ledger_fsyncs_records_and_resumes_without_duplicates(self):
        from bench.compare.provenance import AppendOnlyLedger, ProvenanceError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            ledger = AppendOnlyLedger(path)
            record = {"stable_key": "case-1|r1|corpus", "status": "ok"}
            self.assertTrue(ledger.append(record))
            self.assertFalse(AppendOnlyLedger(path).append(record))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

            conflict = deepcopy(record)
            conflict["status"] = "different"
            with self.assertRaisesRegex(ProvenanceError, "conflicting"):
                AppendOnlyLedger(path).append(conflict)

            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["status"] = "tampered"
            path.write_text(json.dumps(stored) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ProvenanceError, "record SHA"):
                AppendOnlyLedger(path)

    def test_sigkill_torn_tail_is_truncated_to_last_durable_record(self):
        from bench.compare.provenance import AppendOnlyLedger

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            first = {"stable_key": "case-1|r1|corpus", "status": "ok"}
            interrupted = {"stable_key": "case-2|r1|corpus", "status": "ok"}
            AppendOnlyLedger(path).append(first)
            durable = path.read_bytes()
            script = (
                "from pathlib import Path\n"
                "from bench.compare.provenance import AppendOnlyLedger\n"
                f"AppendOnlyLedger(Path({str(path)!r})).append("
                f"{interrupted!r}, _fault_after_bytes=17)\n"
            )

            killed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                check=False,
            )

            self.assertEqual(killed.returncode, -signal.SIGKILL)
            self.assertGreater(len(path.read_bytes()), len(durable))
            pending = path.with_name(f".{path.name}.pending")
            self.assertTrue(pending.is_file())
            resumed = AppendOnlyLedger(path)
            self.assertEqual(set(resumed.records), {first["stable_key"]})
            self.assertEqual(path.read_bytes(), durable)
            self.assertFalse(pending.exists())
            self.assertTrue(resumed.append(interrupted))
            self.assertEqual(
                set(AppendOnlyLedger(path).records),
                {first["stable_key"], interrupted["stable_key"]},
            )

    def test_tail_recovery_never_masks_completed_record_corruption(self):
        from bench.compare.provenance import AppendOnlyLedger, ProvenanceError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            AppendOnlyLedger(path).append(
                {"stable_key": "case-1|r1|corpus", "status": "ok"}
            )
            corrupted = path.read_bytes().replace(b'"status":"ok"', b'"status":"xx"')
            path.write_bytes(corrupted + b'{"stable_key":"torn')
            before = path.read_bytes()

            with self.assertRaisesRegex(ProvenanceError, "record SHA"):
                AppendOnlyLedger(path)

            self.assertEqual(path.read_bytes(), before)

    def test_ambiguous_corrupt_final_record_is_never_erased_as_a_torn_write(self):
        from bench.compare.provenance import AppendOnlyLedger, ProvenanceError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            AppendOnlyLedger(path).append(
                {"stable_key": "case-1|r1|corpus", "status": "ok"}
            )
            AppendOnlyLedger(path).append(
                {"stable_key": "case-2|r1|corpus", "status": "ok"}
            )
            lines = path.read_bytes().splitlines(keepends=True)
            corrupted = lines[0] + lines[1].removesuffix(b"\n")[:-1] + b"!"
            path.write_bytes(corrupted)
            before = path.read_bytes()

            with self.assertRaisesRegex(ProvenanceError, "ambiguous"):
                AppendOnlyLedger(path)

            self.assertEqual(path.read_bytes(), before)

    def test_complete_final_record_without_newline_is_validated_not_discarded(self):
        from bench.compare.provenance import AppendOnlyLedger, ProvenanceError

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "observations.jsonl"
            record = {"stable_key": "case-1|r1|corpus", "status": "ok"}
            AppendOnlyLedger(path).append(record)
            valid_without_newline = path.read_bytes().removesuffix(b"\n")
            path.write_bytes(valid_without_newline)

            resumed = AppendOnlyLedger(path)

            self.assertEqual(set(resumed.records), {record["stable_key"]})
            self.assertTrue(path.read_bytes().endswith(b"\n"))

            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["status"] = "tampered"
            path.write_bytes(json.dumps(stored).encode("utf-8"))
            before = path.read_bytes()
            with self.assertRaisesRegex(ProvenanceError, "record SHA"):
                AppendOnlyLedger(path)
            self.assertEqual(path.read_bytes(), before)

    def test_finalized_ledger_never_repairs_a_torn_tail(self):
        from bench.compare.provenance import AppendOnlyLedger, ProvenanceError

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "observations.jsonl"
            seal = directory / ".done"
            AppendOnlyLedger(path, seal_path=seal).append(
                {"stable_key": "case-1|r1|corpus", "status": "ok"}
            )
            path.write_bytes(path.read_bytes() + b'{"stable_key":"torn')
            seal.write_text("{}\n", encoding="utf-8")
            before = path.read_bytes()

            with self.assertRaisesRegex(ProvenanceError, "interrupted"):
                AppendOnlyLedger(path, seal_path=seal)

            self.assertEqual(path.read_bytes(), before)

    def test_complete_bundle_writes_done_only_after_exact_itt_coverage(self):
        from bench.compare.provenance import RunBundle

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            bundle = RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|corpus", "setup|native"),
                expected_unit_keys=(
                    "case-1|r1|corpus",
                    "case-1|r1|native",
                ),
            )
            bundle.cases.append(
                {"stable_key": "case-1", "case_id": "case-1", "public": True}
            )
            bundle.setup.append(
                {"stable_key": "setup|corpus", "status": "ok", "cost_usd": "0"}
            )
            bundle.setup.append(
                {"stable_key": "setup|native", "status": "ok", "cost_usd": "0"}
            )
            bundle.observations.append(
                {
                    "stable_key": "case-1|r1|corpus",
                    "unit_key": "case-1|r1|corpus",
                    "status": "ok",
                }
            )
            bundle.errors.append(
                {
                    "stable_key": "case-1|r1|native",
                    "unit_key": "case-1|r1|native",
                    "status": "error",
                    "error_class": "timeout",
                }
            )
            provenance = bundle.finalize(
                {
                    "schema_version": 1,
                    "intent_to_treat": True,
                    "expected_units": 2,
                    "accounted_units": 2,
                }
            )

            self.assertTrue((run_dir / ".done").is_file())
            self.assertEqual(provenance["run_id"], bundle.manifest["run_id"])
            self.assertEqual(
                provenance["result_id"],
                json.loads((run_dir / ".done").read_text(encoding="utf-8"))[
                    "result_id"
                ],
            )
            self.assertEqual(
                set(provenance["artifacts"]),
                {
                    "cases.jsonl",
                    "errors.jsonl",
                    "manifest.json",
                    "observations.jsonl",
                    "setup.jsonl",
                    "summary.json",
                },
            )
            reopened = RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|corpus", "setup|native"),
                expected_unit_keys=(
                    "case-1|r1|corpus",
                    "case-1|r1|native",
                ),
            )
            self.assertFalse(
                reopened.observations.append(
                    {
                        "stable_key": "case-1|r1|corpus",
                        "unit_key": "case-1|r1|corpus",
                        "status": "ok",
                    }
                )
            )
            self.assertEqual(
                len((run_dir / "observations.jsonl").read_text().splitlines()),
                1,
            )

    def test_incomplete_or_overlapping_outcomes_never_write_done(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            bundle = RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|corpus",),
                expected_unit_keys=("case-1|r1|corpus",),
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|corpus"})
            with self.assertRaisesRegex(ProvenanceError, "coverage"):
                bundle.finalize(
                    {
                        "schema_version": 1,
                        "intent_to_treat": True,
                        "expected_units": 1,
                        "accounted_units": 0,
                    }
                )
            self.assertFalse((run_dir / ".done").exists())

            outcome = {
                "stable_key": "case-1|r1|corpus",
                "unit_key": "case-1|r1|corpus",
            }
            bundle.observations.append(outcome)
            bundle.errors.append(outcome)
            with self.assertRaisesRegex(ProvenanceError, "both"):
                bundle.finalize(
                    {
                        "schema_version": 1,
                        "intent_to_treat": True,
                        "expected_units": 1,
                        "accounted_units": 1,
                    }
                )
            self.assertFalse((run_dir / ".done").exists())

    def test_live_bundle_requires_every_bound_attempt_journal_to_be_resolved(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            bundle = RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            bundle.observations.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "ok",
                    "operation_id": "operation-1",
                    "raw_response_sha256": "f" * 64,
                    "cost_usd": "0.000000",
                }
            )

            with self.assertRaisesRegex(ProvenanceError, "attempt journal"):
                bundle.finalize(summary)

            journal_path = bundle.attempt_journal_path(unit_key)
            relative_path = journal_path.relative_to(bundle.run_dir).as_posix()
            journal_path.parent.mkdir()
            self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
                terminal=False,
            )
            with self.assertRaisesRegex(ProvenanceError, "unresolved"):
                bundle.finalize(summary)

            journal_path.unlink()
            encoded = self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
            )

            provenance = bundle.finalize(summary)

            journal_descriptor = provenance["attempt_journals"][unit_key]
            self.assertEqual(journal_descriptor["relative_path"], relative_path)
            self.assertEqual(
                journal_descriptor["execution_contract_sha256"],
                json.loads(encoded)["execution_contract_sha256"],
            )
            self.assertEqual(
                journal_descriptor["run_fingerprint_sha256"],
                bundle.manifest["attempt_journal_contract"][
                    "run_fingerprint_sha256"
                ],
            )
            self.assertEqual(
                journal_descriptor["sha256"],
                hashlib.sha256(encoded).hexdigest(),
            )
            self.assertIn(relative_path, provenance["artifacts"])
            self.assertEqual(
                RunBundle.open_existing(bundle.run_dir).manifest["run_id"],
                bundle.manifest["run_id"],
            )

    def test_success_terminal_requires_matching_observation_identity(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            bundle.observations.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "ok",
                    "operation_id": "operation-1",
                    "raw_response_sha256": "0" * 64,
                    "cost_usd": "0.000000",
                }
            )
            journal_path = bundle.attempt_journal_path(unit_key)
            journal_path.parent.mkdir()
            self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
            )

            with self.assertRaisesRegex(ProvenanceError, "result binding"):
                bundle.finalize(summary)

            self.assertFalse((bundle.run_dir / ".done").exists())

    def test_measured_terminal_requires_matching_error_identity(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }

        def create_bundle(run_dir):
            bundle = RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            journal_path = bundle.attempt_journal_path(unit_key)
            journal_path.parent.mkdir()
            self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
                receipt_status="error",
                error_class="timeout",
                classification="measured_error",
            )
            return bundle

        with tempfile.TemporaryDirectory() as tmp:
            mismatched = create_bundle(Path(tmp) / "mismatched")
            mismatched.observations.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "ok",
                    "operation_id": "operation-1",
                    "raw_response_sha256": "f" * 64,
                    "cost_usd": "0.000000",
                }
            )
            with self.assertRaisesRegex(ProvenanceError, "result binding"):
                mismatched.finalize(summary)
            self.assertFalse((mismatched.run_dir / ".done").exists())

        with tempfile.TemporaryDirectory() as tmp:
            matched = create_bundle(Path(tmp) / "matched")
            matched.errors.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "error",
                    "operation_id": "operation-1",
                    "error_class": "timeout",
                    "cost_usd": "0.000000",
                }
            )
            matched.finalize(summary)

            self.assertEqual(
                RunBundle.open_existing(matched.run_dir).manifest["run_id"],
                matched.manifest["run_id"],
            )

    def test_retry_terminal_binds_scorer_record_to_cumulative_cost(self):
        from bench.compare.provenance import RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        for receipt_status, error_class, classification in (
            ("ok", None, "success"),
            ("error", "timeout", "measured_error"),
        ):
            with self.subTest(classification=classification):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    bundle = RunBundle.create(
                        Path(tmp) / "run",
                        manifest_core=self._manifest(),
                        expected_case_keys=("case-1",),
                        expected_setup_keys=("setup|code-search",),
                        expected_unit_keys=(unit_key,),
                        expected_attempt_journal_keys=(unit_key,),
                        expected_live_run_seed=self._live_run_seed(unit_key),
                        expected_execution_contract_templates={
                            unit_key: self._execution_contract_template(
                                unit_key
                            ),
                        },
                    )
                    bundle.cases.append({"stable_key": "case-1"})
                    bundle.setup.append(
                        {"stable_key": "setup|code-search"}
                    )
                    journal_path = bundle.attempt_journal_path(unit_key)
                    journal_path.parent.mkdir()
                    self._write_attempt_journal(
                        path=journal_path,
                        trusted_root=bundle.run_dir,
                        run_id=bundle.manifest["run_id"],
                        unit_key=unit_key,
                        receipt_status=receipt_status,
                        error_class=error_class,
                        classification=classification,
                        prior_transient_cost_usd=Decimal("0.004000"),
                        terminal_cost_usd=Decimal("0.003000"),
                    )
                    scorer_record = {
                        "stable_key": unit_key,
                        "unit_key": unit_key,
                        "status": receipt_status,
                        "operation_id": "operation-2",
                        "cost_usd": "0.007000",
                    }
                    if receipt_status == "ok":
                        scorer_record["raw_response_sha256"] = "f" * 64
                        bundle.observations.append(scorer_record)
                    else:
                        scorer_record["error_class"] = error_class
                        bundle.errors.append(scorer_record)

                    provenance = bundle.finalize(summary)

                    terminal = provenance["attempt_journals"][unit_key][
                        "terminal_result"
                    ]
                    self.assertEqual(terminal["cost_usd"], "0.007000")
                    self.assertEqual(
                        terminal["operation_id"],
                        "operation-2",
                    )
                    self.assertEqual(
                        terminal["classification"],
                        classification,
                    )

    def test_multi_unit_finalization_is_exact_at_low_decimal_precision(self):
        from bench.compare.provenance import RunBundle

        unit_keys = (
            "case-1|r1|code-search",
            "case-2|r1|code-search",
        )
        case_keys = ("case-1", "case-2")
        setup_keys = ("setup|code-search",)
        run_seed = RunBundle.derive_live_run_seed(
            manifest_core=self._manifest(),
            expected_case_keys=case_keys,
            expected_setup_keys=setup_keys,
            expected_unit_keys=unit_keys,
        )
        max_unit_usd = "1000000000000.000002"
        max_total_usd = "2000000000000.000004"
        templates = {
            unit_key: self._execution_contract_template(
                unit_key,
                run_seed=run_seed,
                max_total_usd=max_total_usd,
                max_unit_usd=max_unit_usd,
                expected_units=2,
            )
            for unit_key in unit_keys
        }
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 2,
            "accounted_units": 2,
        }

        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=case_keys,
                expected_setup_keys=setup_keys,
                expected_unit_keys=unit_keys,
                expected_attempt_journal_keys=unit_keys,
                expected_live_run_seed=run_seed,
                expected_execution_contract_templates=templates,
            )
            for case_key in case_keys:
                bundle.cases.append({"stable_key": case_key})
            bundle.setup.append({"stable_key": setup_keys[0]})
            for unit_key in unit_keys:
                journal_path = bundle.attempt_journal_path(unit_key)
                journal_path.parent.mkdir(exist_ok=True)
                self._write_attempt_journal(
                    path=journal_path,
                    trusted_root=bundle.run_dir,
                    run_id=bundle.manifest["run_id"],
                    unit_key=unit_key,
                    run_seed=run_seed,
                    prior_transient_cost_usd=Decimal(
                        "1000000000000.000001"
                    ),
                    terminal_cost_usd=Decimal("0.000001"),
                    max_total_usd=max_total_usd,
                    max_unit_usd=max_unit_usd,
                    expected_units=2,
                )
                bundle.observations.append(
                    {
                        "stable_key": unit_key,
                        "unit_key": unit_key,
                        "status": "ok",
                        "operation_id": "operation-2",
                        "raw_response_sha256": "f" * 64,
                        "cost_usd": max_unit_usd,
                    }
                )

            with localcontext() as context:
                context.prec = 6
                provenance = bundle.finalize(summary)

            self.assertTrue((bundle.run_dir / ".done").is_file())
        self.assertEqual(
            {
                descriptor["terminal_result"]["cost_usd"]
                for descriptor in provenance["attempt_journals"].values()
            },
            {max_unit_usd},
        )

    def test_multi_unit_manifest_requires_total_capacity_for_every_unit(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_keys = (
            "case-1|r1|code-search",
            "case-2|r1|code-search",
        )
        case_keys = ("case-1", "case-2")
        setup_keys = ("setup|code-search",)
        run_seed = RunBundle.derive_live_run_seed(
            manifest_core=self._manifest(),
            expected_case_keys=case_keys,
            expected_setup_keys=setup_keys,
            expected_unit_keys=unit_keys,
        )
        templates = {
            unit_key: self._execution_contract_template(
                unit_key,
                run_seed=run_seed,
                max_total_usd="0.150000",
                max_unit_usd="0.100000",
                expected_units=2,
            )
            for unit_key in unit_keys
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                ProvenanceError,
                "template is invalid",
            ):
                RunBundle.create(
                    Path(tmp) / "run",
                    manifest_core=self._manifest(),
                    expected_case_keys=case_keys,
                    expected_setup_keys=setup_keys,
                    expected_unit_keys=unit_keys,
                    expected_attempt_journal_keys=unit_keys,
                    expected_live_run_seed=run_seed,
                    expected_execution_contract_templates=templates,
                )

            self.assertFalse((Path(tmp) / "run" / ".done").exists())

    def test_live_manifest_requires_one_run_scoped_execution_fingerprint(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_keys = (
            "case-1|r1|code-search",
            "case-2|r1|code-search",
        )
        run_seed = RunBundle.derive_live_run_seed(
            manifest_core=self._manifest(),
            expected_case_keys=("case-1", "case-2"),
            expected_setup_keys=("setup|code-search",),
            expected_unit_keys=unit_keys,
        )
        mutations = {}

        mixed_model_controls = self._controls_descriptor()
        mixed_model_controls["model_id"] = "claude-sonnet-4-1"
        mutations["model"] = self._execution_contract_template(
            unit_keys[1],
            controls=mixed_model_controls,
            run_seed=run_seed,
            expected_units=2,
        )

        mixed_cap_controls = self._controls_descriptor(
            max_total_usd="2.000000",
        )
        mutations["controls"] = self._execution_contract_template(
            unit_keys[1],
            controls=mixed_cap_controls,
            run_seed=run_seed,
            max_total_usd="2.000000",
            expected_units=2,
        )

        for label, second_template in mutations.items():
            with self.subTest(label=label):
                templates = {
                    unit_keys[0]: self._execution_contract_template(
                        unit_keys[0],
                        run_seed=run_seed,
                        expected_units=2,
                    ),
                    unit_keys[1]: second_template,
                }
                with tempfile.TemporaryDirectory() as tmp:  # noqa: SIM117
                    with self.assertRaisesRegex(
                        ProvenanceError,
                        "run-scoped fingerprint",
                    ):
                        RunBundle.create(
                            Path(tmp) / "run",
                            manifest_core=self._manifest(),
                            expected_case_keys=("case-1", "case-2"),
                            expected_setup_keys=("setup|code-search",),
                            expected_unit_keys=unit_keys,
                            expected_attempt_journal_keys=unit_keys,
                            expected_live_run_seed=run_seed,
                            expected_execution_contract_templates=templates,
                        )

    def test_multi_unit_live_manifest_rejects_signed_unit_count_drift(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_keys = (
            "case-1|r1|code-search",
            "case-2|r1|code-search",
        )
        case_keys = ("case-1", "case-2")
        setup_keys = ("setup|code-search",)
        run_seed = RunBundle.derive_live_run_seed(
            manifest_core=self._manifest(),
            expected_case_keys=case_keys,
            expected_setup_keys=setup_keys,
            expected_unit_keys=unit_keys,
        )
        templates = {
            unit_key: self._execution_contract_template(
                unit_key,
                run_seed=run_seed,
                expected_units=1,
            )
            for unit_key in unit_keys
        }

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            ProvenanceError,
            "expected unit count",
        ):
            RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=case_keys,
                expected_setup_keys=setup_keys,
                expected_unit_keys=unit_keys,
                expected_attempt_journal_keys=unit_keys,
                expected_live_run_seed=run_seed,
                expected_execution_contract_templates=templates,
            )

    def test_multi_unit_finalization_rejects_signed_unit_count_drift(self):
        from bench.compare.live_runtime import (
            execution_contract_identity_sha256,
        )
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_keys = (
            "case-1|r1|code-search",
            "case-2|r1|code-search",
        )
        case_keys = ("case-1", "case-2")
        setup_keys = ("setup|code-search",)
        run_seed = RunBundle.derive_live_run_seed(
            manifest_core=self._manifest(),
            expected_case_keys=case_keys,
            expected_setup_keys=setup_keys,
            expected_unit_keys=unit_keys,
        )
        valid_templates = {
            unit_key: self._execution_contract_template(
                unit_key,
                run_seed=run_seed,
                expected_units=2,
            )
            for unit_key in unit_keys
        }
        wrong_templates = {
            unit_key: self._execution_contract_template(
                unit_key,
                run_seed=run_seed,
                expected_units=1,
            )
            for unit_key in unit_keys
        }
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 2,
            "accounted_units": 2,
        }

        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=case_keys,
                expected_setup_keys=setup_keys,
                expected_unit_keys=unit_keys,
                expected_attempt_journal_keys=unit_keys,
                expected_live_run_seed=run_seed,
                expected_execution_contract_templates=valid_templates,
            )
            journal_contract = bundle.manifest["attempt_journal_contract"]
            for unit_key, template in wrong_templates.items():
                template_sha256 = template["template_sha256"]
                journal_contract[
                    "execution_contract_template_sha256_by_unit_key"
                ][unit_key] = template_sha256
                journal_contract[
                    "execution_contract_sha256_by_unit_key"
                ][unit_key] = execution_contract_identity_sha256(
                    run_id=bundle.manifest["run_id"],
                    run_seed=run_seed,
                    unit_key=unit_key,
                    template_sha256=template_sha256,
                )

            for case_key in case_keys:
                bundle.cases.append({"stable_key": case_key})
            bundle.setup.append({"stable_key": setup_keys[0]})
            for unit_key in unit_keys:
                journal_path = bundle.attempt_journal_path(unit_key)
                journal_path.parent.mkdir(exist_ok=True)
                self._write_attempt_journal(
                    path=journal_path,
                    trusted_root=bundle.run_dir,
                    run_id=bundle.manifest["run_id"],
                    unit_key=unit_key,
                    run_seed=run_seed,
                    expected_units=1,
                )
                bundle.observations.append(
                    {
                        "stable_key": unit_key,
                        "unit_key": unit_key,
                        "status": "ok",
                        "operation_id": "operation-1",
                        "raw_response_sha256": "f" * 64,
                        "cost_usd": "0.000000",
                    }
                )

            with self.assertRaisesRegex(
                ProvenanceError,
                "expected unit count",
            ):
                bundle.finalize(summary)

            self.assertFalse((bundle.run_dir / ".done").exists())

    def test_finalization_rejects_a_hardlinked_attempt_journal(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            bundle.observations.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "ok",
                    "operation_id": "operation-1",
                    "raw_response_sha256": "f" * 64,
                    "cost_usd": "0.000000",
                }
            )
            journal_path = bundle.attempt_journal_path(unit_key)
            journal_path.parent.mkdir()
            self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
            )
            os.link(journal_path, bundle.run_dir / "journal-hardlink.json")

            with self.assertRaisesRegex(
                ProvenanceError,
                "single-link regular file",
            ):
                bundle.finalize(summary)

            self.assertFalse((bundle.run_dir / ".done").exists())

    def test_live_bundle_rejects_forged_terminal_journal_event_chain(self):
        from bench.compare.provenance import ProvenanceError, RunBundle
        from bench.compare.schema import canonical_json

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            bundle.observations.append(
                {"stable_key": unit_key, "unit_key": unit_key}
            )
            journal_path = bundle.attempt_journal_path(unit_key)
            journal_path.parent.mkdir()
            self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
            )
            forged = json.loads(journal_path.read_bytes())
            forged["events"][-1]["previous_sha256"] = "0" * 64
            journal_path.write_bytes(canonical_json(forged) + b"\n")

            with self.assertRaisesRegex(ProvenanceError, "integrity"):
                bundle.finalize(summary)

    def test_fatal_terminal_journal_cannot_finalize_as_ok_observation(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            bundle.observations.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "ok",
                    "operation_id": "operation-1",
                    "raw_response_sha256": "f" * 64,
                    "cost_usd": "0.000000",
                }
            )
            journal_path = bundle.attempt_journal_path(unit_key)
            journal_path.parent.mkdir()
            self._write_attempt_journal(
                path=journal_path,
                trusted_root=bundle.run_dir,
                run_id=bundle.manifest["run_id"],
                unit_key=unit_key,
                receipt_status="error",
                error_class="authentication",
                classification="fatal_error",
            )

            with self.assertRaisesRegex(ProvenanceError, "fatal"):
                bundle.finalize(summary)

            self.assertFalse((bundle.run_dir / ".done").exists())

    def test_live_manifest_precommits_contract_without_a_run_id_hash_cycle(self):
        from bench.compare.provenance import RunBundle

        unit_key = "case-1|r1|code-search"
        template = self._execution_contract_template(unit_key)
        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: template,
                },
            )

        contract = bundle.manifest["attempt_journal_contract"]
        self.assertEqual(
            set(contract),
            {
                "schema_version",
                "policy",
                "run_seed",
                "paths_by_unit_key",
                "identity_sha256_by_unit_key",
                "execution_contract_template_sha256_by_unit_key",
                "controls_sha256_by_unit_key",
                "run_fingerprint_sha256",
                "execution_contract_sha256_by_unit_key",
            },
        )
        self.assertEqual(
            contract["run_seed"],
            self._live_run_seed(unit_key),
        )
        self.assertEqual(
            contract["controls_sha256_by_unit_key"][unit_key],
            template["controls_sha256"],
        )
        self.assertEqual(
            contract["run_fingerprint_sha256"],
            template["run_fingerprint_sha256"],
        )
        self.assertRegex(contract["run_seed"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            contract["execution_contract_template_sha256_by_unit_key"][
                unit_key
            ],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            contract["execution_contract_template_sha256_by_unit_key"][
                unit_key
            ],
            template["template_sha256"],
        )
        self.assertRegex(
            contract["execution_contract_sha256_by_unit_key"][unit_key],
            r"^[0-9a-f]{64}$",
        )

    def test_live_manifest_rejects_rehashed_semantic_template_drift(self):
        from bench.compare.live_runtime import (
            execution_contract_template_sha256,
        )
        from bench.compare.provenance import ProvenanceError, RunBundle
        from bench.compare.schema import canonical_json

        unit_key = "case-1|r1|code-search"
        mutations = {}

        provider = self._execution_contract_template(unit_key)
        provider["controls"]["provider"] = "different-provider"
        mutations["provider"] = provider

        model = self._execution_contract_template(unit_key)
        model["model_id"] = "different-model"
        mutations["model"] = model

        cli_version = self._execution_contract_template(unit_key)
        cli_version["cli_version"] = "different-cli"
        mutations["CLI version"] = cli_version

        nested_cli_version = self._execution_contract_template(unit_key)
        nested_cli_version["controls"]["cli_version"] = "different-cli"
        mutations["nested CLI version"] = nested_cli_version

        malformed_cli_hash = self._execution_contract_template(unit_key)
        malformed_cli_hash["cli_sha256"] = "not-a-sha256"
        mutations["CLI hash"] = malformed_cli_hash

        missing_credential_source = self._execution_contract_template(unit_key)
        missing_credential_source["credential_source"] = ""
        mutations["credential source"] = missing_credential_source

        incompatible_credential_source = self._execution_contract_template(
            unit_key
        )
        incompatible_credential_source["provider"] = "aws-bedrock"
        incompatible_credential_source["controls"]["provider"] = "aws-bedrock"
        mutations["provider credential compatibility"] = (
            incompatible_credential_source
        )

        cap = self._execution_contract_template(unit_key)
        cap["max_unit_usd"] = "0.900000"
        mutations["cap"] = cap

        nested_cap = self._execution_contract_template(unit_key)
        nested_cap["controls"]["cost"]["max_unit_usd"] = "0.900000"
        mutations["nested cap"] = nested_cap

        calibration = self._execution_contract_template(unit_key)
        calibration["calibration_sha256"] = "8" * 64
        mutations["calibration"] = calibration

        nested_calibration = self._execution_contract_template(unit_key)
        nested_calibration["controls"]["cost"][
            "calibration_sha256"
        ] = "8" * 64
        mutations["nested calibration"] = nested_calibration

        retry = self._execution_contract_template(unit_key)
        retry["controls"]["retry"]["max_attempts"] = 1
        mutations["retry"] = retry

        adapter_limit = self._execution_contract_template(unit_key)
        adapter_limit["controls"]["max_discovery_tool_calls"] = 21
        mutations["adapter limit"] = adapter_limit

        inverted_caps = self._execution_contract_template(unit_key)
        inverted_caps["max_unit_usd"] = "2.000000"
        mutations["unit above total"] = inverted_caps

        insufficient_total = self._execution_contract_template(
            unit_key,
            max_total_usd="0.100000",
            max_unit_usd="0.100000",
            expected_units=2,
        )
        mutations["total cannot cover expected units"] = insufficient_total

        for label, template in mutations.items():
            with self.subTest(label=label):
                template["controls_sha256"] = hashlib.sha256(
                    canonical_json(template["controls"])
                ).hexdigest()
                template["template_sha256"] = (
                    execution_contract_template_sha256(template)
                )
                with tempfile.TemporaryDirectory() as tmp:  # noqa: SIM117
                    with self.assertRaisesRegex(
                        ProvenanceError,
                        "template is invalid",
                    ):
                        RunBundle.create(
                            Path(tmp) / "run",
                            manifest_core=self._manifest(),
                            expected_case_keys=("case-1",),
                            expected_setup_keys=("setup|code-search",),
                            expected_unit_keys=(unit_key,),
                            expected_attempt_journal_keys=(unit_key,),
                            expected_live_run_seed=self._live_run_seed(
                                unit_key
                            ),
                            expected_execution_contract_templates={
                                unit_key: template,
                            },
                        )

    def test_live_manifest_rejects_rehashed_template_identity_and_type_drift(
        self,
    ):
        from bench.compare.live_runtime import (
            execution_contract_template_sha256,
        )
        from bench.compare.provenance import ProvenanceError, RunBundle
        from bench.compare.schema import canonical_json

        unit_key = "case-1|r1|code-search"
        mutations = {}

        wrong_arm = self._execution_contract_template(unit_key)
        wrong_arm["arm"] = "code-graph"
        mutations["unit arm suffix"] = wrong_arm

        unsupported_arm = self._execution_contract_template(unit_key)
        unsupported_arm["arm"] = "unsupported"
        mutations["supported arm"] = unsupported_arm

        endpoint = self._execution_contract_template(unit_key)
        endpoint["endpoint"] = 7
        mutations["endpoint type"] = endpoint

        account_scope = self._execution_contract_template(unit_key)
        account_scope["account_scope"] = " fixture-account"
        mutations["account scope normalization"] = account_scope

        provider = self._execution_contract_template(unit_key)
        provider["provider"] = " fixture"
        provider["controls"]["provider"] = " fixture"
        mutations["provider normalization"] = provider

        model = self._execution_contract_template(unit_key)
        model["model_id"] = "fixture-model\n"
        model["controls"]["model_id"] = "fixture-model\n"
        mutations["model normalization"] = model

        issuer = self._execution_contract_template(unit_key)
        issuer["auth_issuer"] = " fixture-auth-issuer"
        mutations["issuer normalization"] = issuer

        scalar_type = self._execution_contract_template(unit_key)
        scalar_type["max_attempts"] = 2.0
        scalar_type["controls"]["retry"]["max_attempts"] = 2.0
        mutations["retry scalar type"] = scalar_type

        nested_scalar_type = self._execution_contract_template(unit_key)
        nested_scalar_type["controls"]["top_k"] = 10.0
        mutations["controls scalar type"] = nested_scalar_type

        for label, template in mutations.items():
            with self.subTest(label=label):
                template["controls_sha256"] = hashlib.sha256(
                    canonical_json(template["controls"])
                ).hexdigest()
                template["template_sha256"] = (
                    execution_contract_template_sha256(template)
                )
                with tempfile.TemporaryDirectory() as tmp:  # noqa: SIM117
                    with self.assertRaisesRegex(
                        ProvenanceError,
                        "template is invalid",
                    ):
                        RunBundle.create(
                            Path(tmp) / "run",
                            manifest_core=self._manifest(),
                            expected_case_keys=("case-1",),
                            expected_setup_keys=("setup|code-search",),
                            expected_unit_keys=(unit_key,),
                            expected_attempt_journal_keys=(unit_key,),
                            expected_live_run_seed=self._live_run_seed(
                                unit_key
                            ),
                            expected_execution_contract_templates={
                                unit_key: template,
                            },
                        )

    def test_live_manifest_rejects_self_consistent_forged_contract_controls(self):
        from bench.compare.live_runtime import LiveControlError
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle = RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: self._execution_contract_template(unit_key),
                },
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|code-search"})
            bundle.observations.append(
                {
                    "stable_key": unit_key,
                    "unit_key": unit_key,
                    "status": "ok",
                    "operation_id": "operation-1",
                    "raw_response_sha256": "f" * 64,
                    "cost_usd": "0.000000",
                }
            )
            journal_path = bundle.attempt_journal_path(unit_key)
            journal_path.parent.mkdir()
            with self.assertRaisesRegex(LiveControlError, "controls"):
                self._write_attempt_journal(
                    path=journal_path,
                    trusted_root=bundle.run_dir,
                    run_id=bundle.manifest["run_id"],
                    unit_key=unit_key,
                    controls={
                        "schema_version": 1,
                        "forged_after_manifest": True,
                    },
                )

            with self.assertRaisesRegex(
                ProvenanceError,
                "missing",
            ):
                bundle.finalize(summary)

            self.assertFalse((bundle.run_dir / ".done").exists())

    def test_live_manifest_rejects_template_bound_to_another_run_seed(self):
        from bench.compare.live_runtime import (
            execution_contract_run_fingerprint_sha256,
            execution_contract_template_sha256,
        )
        from bench.compare.provenance import ProvenanceError, RunBundle

        unit_key = "case-1|r1|code-search"
        template = self._execution_contract_template(unit_key)
        template["run_seed"] = "9" * 64
        template["run_fingerprint_sha256"] = (
            execution_contract_run_fingerprint_sha256(template)
        )
        template["template_sha256"] = (
            execution_contract_template_sha256(template)
        )

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            ProvenanceError,
            "template identity mismatch",
        ):
            RunBundle.create(
                Path(tmp) / "run",
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|code-search",),
                expected_unit_keys=(unit_key,),
                expected_attempt_journal_keys=(unit_key,),
                expected_live_run_seed=self._live_run_seed(unit_key),
                expected_execution_contract_templates={
                    unit_key: template,
                },
            )

    def test_existing_run_refuses_changed_manifest_or_expected_keys(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|corpus",),
                expected_unit_keys=("case-1|r1|corpus",),
            )
            changed = self._manifest()
            changed["component_identity_sha256"] = "c" * 64
            with self.assertRaisesRegex(ProvenanceError, "manifest"):
                RunBundle.create(
                    run_dir,
                    manifest_core=changed,
                    expected_case_keys=("case-1",),
                    expected_setup_keys=("setup|corpus",),
                    expected_unit_keys=("case-1|r1|corpus",),
                )
            with self.assertRaisesRegex(ProvenanceError, "manifest"):
                RunBundle.create(
                    run_dir,
                    manifest_core=self._manifest(),
                    expected_case_keys=("case-1",),
                    expected_setup_keys=("setup|corpus",),
                    expected_unit_keys=("case-1|r1|native",),
                )

    def test_existing_run_normalizes_malformed_utf8_manifest(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_bytes(b'{"schema_version":1}\xff')

            with self.assertRaisesRegex(
                ProvenanceError,
                "manifest.*UTF-8",
            ):
                RunBundle.open_existing(run_dir)

    def test_existing_run_rejects_utf16_and_utf32_manifest_snapshots(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for encoding in ("utf-16", "utf-32"):
                with self.subTest(encoding=encoding):
                    run_dir = directory / encoding
                    RunBundle.create(
                        run_dir,
                        manifest_core=self._manifest(),
                        expected_case_keys=("case-1",),
                        expected_setup_keys=("setup|corpus",),
                        expected_unit_keys=("case-1|r1|corpus",),
                    )
                    manifest_path = run_dir / "manifest.json"
                    manifest_text = manifest_path.read_text(encoding="utf-8")
                    manifest_path.write_bytes(manifest_text.encode(encoding))

                    with self.assertRaisesRegex(
                        ProvenanceError,
                        "manifest.*UTF-8",
                    ):
                        RunBundle.open_existing(run_dir)

    def test_finalized_bundle_rejects_utf16_and_utf32_json_artifacts(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for artifact in (".done", "provenance.json", "summary.json"):
                for encoding in ("utf-16", "utf-32"):
                    with self.subTest(artifact=artifact, encoding=encoding):
                        run_dir = directory / (
                            f"{artifact.removeprefix('.')}-{encoding}"
                        )
                        bundle = RunBundle.create(
                            run_dir,
                            manifest_core=self._manifest(),
                            expected_case_keys=("case-1",),
                            expected_setup_keys=("setup|corpus",),
                            expected_unit_keys=("case-1|r1|corpus",),
                        )
                        bundle.cases.append({"stable_key": "case-1"})
                        bundle.setup.append({"stable_key": "setup|corpus"})
                        bundle.observations.append(
                            {
                                "stable_key": "case-1|r1|corpus",
                                "unit_key": "case-1|r1|corpus",
                            }
                        )
                        bundle.finalize(summary)
                        artifact_path = run_dir / artifact
                        artifact_text = artifact_path.read_text(
                            encoding="utf-8"
                        )
                        artifact_path.write_bytes(
                            artifact_text.encode(encoding)
                        )

                        with self.assertRaisesRegex(
                            ProvenanceError,
                            "finalized artifact.*UTF-8",
                        ):
                            RunBundle.open_existing(run_dir)

    def test_done_marker_seals_ledgers_and_verifies_every_final_artifact(self):
        from bench.compare.provenance import ProvenanceError, RunBundle

        summary = {
            "schema_version": 1,
            "intent_to_treat": True,
            "expected_units": 1,
            "accounted_units": 1,
            "scoring": {"bootstrap": 100, "seed": 42},
        }
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            bundle = RunBundle.create(
                run_dir,
                manifest_core=self._manifest(),
                expected_case_keys=("case-1",),
                expected_setup_keys=("setup|corpus",),
                expected_unit_keys=("case-1|r1|corpus",),
            )
            bundle.cases.append({"stable_key": "case-1"})
            bundle.setup.append({"stable_key": "setup|corpus"})
            outcome = {
                "stable_key": "case-1|r1|corpus",
                "unit_key": "case-1|r1|corpus",
            }
            bundle.observations.append(outcome)
            bundle.finalize(summary)

            self.assertFalse(bundle.observations.append(outcome))
            with self.assertRaisesRegex(ProvenanceError, "finalized"):
                bundle.observations.append(
                    {
                        "stable_key": "unexpected",
                        "unit_key": "unexpected",
                    }
                )
            changed_summary = deepcopy(summary)
            changed_summary["scoring"]["seed"] = 7
            with self.assertRaisesRegex(ProvenanceError, "finalized"):
                bundle.finalize(changed_summary)

            stored = (run_dir / "observations.jsonl").read_text(encoding="utf-8")
            (run_dir / "observations.jsonl").write_text(
                stored.replace("corpus", "native"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ProvenanceError, "finalized artifact"):
                RunBundle.open_existing(run_dir)


if __name__ == "__main__":
    unittest.main()
