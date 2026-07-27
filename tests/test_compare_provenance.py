"""Tests for append-only, content-addressed comparison run artifacts."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


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
