"""Acceptance tests for intent-to-treat scoring and fixture falsifiers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPARE = ROOT / "bench" / "compare"
RUNNER = COMPARE / "run.py"
SCORER = COMPARE / "score.py"
PIN = COMPARE / "pins" / "fixture-public-n7.json"
GOOD = COMPARE / "fixtures" / "five-arm-good.json"
BAD = COMPARE / "fixtures" / "five-arm-bad.json"
THRESHOLDS = COMPARE / "thresholds.json"


class ComparisonScoreTests(unittest.TestCase):
    @staticmethod
    def _canary_path(run_dir: Path) -> Path:
        return run_dir.parent / f".{run_dir.name}-host-write-canary"

    @staticmethod
    def _secret_canary(run_dir: Path) -> str:
        return f"fixture-host-secret-canary-{run_dir.name}"

    def _record(self, run_dir: Path, fixture: Path, *extra: str):
        environment = os.environ.copy()
        environment.update(
            {
                "COMPARE_CANARY_WRITE_PATH": str(self._canary_path(run_dir)),
                "COMPARE_SECRET_CANARY": self._secret_canary(run_dir),
            }
        )
        return subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--cases",
                str(PIN),
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
                "--mode",
                "fixture",
                "--fixture-results",
                str(fixture),
                *extra,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    def _score(
        self,
        run_dir: Path,
        *,
        bootstrap: int = 10_000,
        seed: int = 42,
        thresholds: Path = THRESHOLDS,
    ):
        return subprocess.run(
            [
                sys.executable,
                str(SCORER),
                "--run-dir",
                str(run_dir),
                "--intent-to-treat",
                "--bootstrap",
                str(bootstrap),
                "--seed",
                str(seed),
                "--holm-primary",
                "composed-corpus,composed-native",
                "--thresholds",
                str(thresholds),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_good_fixture_exits_zero_and_finalizes_exact_coverage(self):
        from bench.compare.schema import scoring_policy_descriptor

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "good"
            recorded = self._record(run_dir, GOOD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            scored = self._score(run_dir)
            self.assertEqual(scored.returncode, 0, scored.stdout + scored.stderr)
            summary = json.loads(
                (run_dir / "summary.json").read_text(encoding="utf-8")
            )

            self.assertTrue((run_dir / ".done").is_file())
            self.assertFalse(self._canary_path(run_dir).exists())
            secret = self._secret_canary(run_dir).encode("utf-8")
            self.assertTrue(
                all(
                    secret not in path.read_bytes()
                    for path in run_dir.iterdir()
                    if path.is_file()
                )
            )
            self.assertTrue(summary["intent_to_treat"])
            self.assertEqual(summary["expected_units"], 35)
            self.assertEqual(summary["accounted_units"], 35)
            self.assertEqual(summary["fixture_contract"]["status"], "pass")
            self.assertEqual(
                summary["privacy"]["host_injection_canary"],
                "pass",
            )
            setup_records = [
                json.loads(line)
                for line in (run_dir / "setup.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            canary_record = next(
                row
                for row in setup_records
                if row["stable_key"] == "setup|host-canary"
            )
            self.assertEqual(
                canary_record["network_observation"],
                "loopback_listener_per_unit_v1",
            )
            self.assertEqual(canary_record["executor_processes"], 35)
            self.assertEqual(canary_record["canary_file_reads"], 35)
            self.assertTrue(canary_record["secret_environment_excluded"])
            self.assertEqual(
                summary["scoring"],
                scoring_policy_descriptor(),
            )
            for arm in (
                "corpus",
                "native",
                "code-search",
                "code-graph",
                "composed",
            ):
                self.assertEqual(summary["arms"][arm]["file_acc_at_10"], 1.0)
                self.assertEqual(summary["arms"][arm]["failure_rate"], 0.0)
                self.assertEqual(summary["arms"][arm]["egress_bytes"], 0)
                self.assertGreater(summary["arms"][arm]["evidence_bytes"], 0)
            self.assertEqual(
                set(summary["primary_contrasts"]),
                {"composed-corpus", "composed-native"},
            )

    def test_bad_fixture_exits_one_without_softening_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "bad"
            recorded = self._record(run_dir, BAD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            scored = self._score(run_dir)
            self.assertEqual(scored.returncode, 1, scored.stdout + scored.stderr)
            summary = json.loads(
                (run_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["fixture_contract"]["status"], "fail")
            self.assertIn(
                "code-graph:file_acc_at_10",
                summary["fixture_contract"]["failures"],
            )
            self.assertTrue((run_dir / ".done").is_file())

    def test_incomplete_coverage_is_not_scored_or_marked_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "incomplete"
            recorded = self._record(
                run_dir,
                GOOD,
                "--fixture-stop-after",
                "1",
            )
            self.assertEqual(recorded.returncode, 3)
            scored = self._score(run_dir)
            self.assertEqual(scored.returncode, 1)
            self.assertIn("coverage", scored.stderr.lower())
            self.assertFalse((run_dir / ".done").exists())
            self.assertFalse((run_dir / "summary.json").exists())

    def test_recorded_errors_remain_intent_to_treat_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fixture = json.loads(GOOD.read_text(encoding="utf-8"))
            fixture["faults"] = [
                {
                    "unit_key": "fixture-auth-token|r1|composed",
                    "error_class": "timeout",
                }
            ]
            failure_fixture = directory / "failure.json"
            failure_fixture.write_text(
                json.dumps(fixture, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_dir = directory / "failure"
            recorded = self._record(run_dir, failure_fixture)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            scored = self._score(run_dir)
            self.assertEqual(scored.returncode, 1, scored.stdout + scored.stderr)
            summary = json.loads(
                (run_dir / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                summary["arms"]["composed"]["failure_rate"],
                1 / 7,
            )
            self.assertEqual(
                summary["arms"]["composed"]["file_acc_at_10"],
                6 / 7,
            )
            self.assertGreater(
                summary["arms"]["composed"]["tokens"]["input_tokens"],
                0,
            )
            self.assertGreater(
                summary["arms"]["composed"]["tokens"]["output_tokens"],
                0,
            )
            self.assertGreater(
                summary["arms"]["composed"]["tokens"]["tool_result_tokens"],
                0,
            )
            self.assertEqual(summary["expected_units"], summary["accounted_units"])

    def test_live_statistics_policy_refuses_resample_or_seed_drift(self):
        for field, values in (
            ("bootstrap", {"bootstrap": 1}),
            ("seed", {"seed": 7}),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp) / field
                recorded = self._record(run_dir, GOOD)
                self.assertEqual(
                    recorded.returncode,
                    0,
                    recorded.stdout + recorded.stderr,
                )

                scored = self._score(run_dir, **values)

                self.assertEqual(scored.returncode, 1)
                self.assertIn("frozen", scored.stderr.lower())
                self.assertFalse((run_dir / ".done").exists())

    def test_mcnemar_counts_each_case_once_when_repeats_are_correlated(self):
        from bench.compare.score import (
            _all_replicates_success,
            _bootstrap_delta,
            _case_clustered_mcnemar_counts,
        )

        treatment_only, baseline_only = _case_clustered_mcnemar_counts(
            {
                "case-a": [(1.0, 0.0), (1.0, 0.0)],
                "case-b": [(1.0, 1.0), (0.0, 1.0)],
            }
        )

        self.assertEqual(treatment_only, 1)
        self.assertEqual(baseline_only, 1)

        reviewer_poc = {
            "case-a": [(1.0, 0.0), (0.0, 0.0)],
        }
        aggregated = _all_replicates_success(reviewer_poc)
        deltas = {
            case_id: treatment - baseline
            for case_id, (treatment, baseline) in aggregated.items()
        }
        samples = _bootstrap_delta(deltas, resamples=100, seed=42)
        treatment_only, baseline_only = _case_clustered_mcnemar_counts(
            reviewer_poc
        )

        self.assertEqual(deltas, {"case-a": 0.0})
        self.assertEqual(samples, [0.0] * 100)
        self.assertEqual((treatment_only, baseline_only), (0, 0))

    def test_case_ledger_is_bound_to_the_exact_pinned_case_digest(self):
        from bench.compare.schema import canonical_json
        from bench.compare.score import ScoreError, _validated_case_records

        source_case = {
            "case_id": "case-a",
            "category": "Bug",
            "query": "Locate alpha.",
            "repository": {
                "url": "https://github.com/example/repo",
                "revision": "a" * 40,
            },
            "oracle": {
                "files": ["src/alpha.py"],
                "classes": [],
                "functions": ["alpha"],
            },
            "label_audit": {"status": "verified"},
        }
        digest = hashlib.sha256(canonical_json(source_case)).hexdigest()
        record = {
            "stable_key": "case-a",
            "case_id": "case-a",
            "source_pin_sha256": "f" * 64,
            "case": source_case,
            "case_sha256": digest,
            "record_sha256": "ignored-by-semantic-validator",
        }
        expected = {"case-a": digest}

        validated = _validated_case_records(
            {"case-a": record},
            expected,
            source_pin_sha256="f" * 64,
        )
        self.assertEqual(validated, {"case-a": source_case})

        tampered = json.loads(json.dumps(record))
        tampered["case"]["query"] = "Attacker-controlled query."
        tampered["case_sha256"] = hashlib.sha256(
            canonical_json(tampered["case"])
        ).hexdigest()
        with self.assertRaisesRegex(ScoreError, "pinned case"):
            _validated_case_records(
                {"case-a": tampered},
                expected,
                source_pin_sha256="f" * 64,
            )
        with self.assertRaisesRegex(ScoreError, "source pin"):
            _validated_case_records(
                {"case-a": record},
                expected,
                source_pin_sha256="",
            )

    def test_rehashed_nested_case_tamper_is_rejected_by_the_pinned_manifest(self):
        from bench.compare.schema import canonical_json

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "case-tamper"
            recorded = self._record(run_dir, GOOD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            ledger = run_dir / "cases.jsonl"
            rows = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["case"]["query"] = "Attacker-controlled replacement query."
            rows[0]["case_sha256"] = hashlib.sha256(
                canonical_json(rows[0]["case"])
            ).hexdigest()
            payload = dict(rows[0])
            payload.pop("record_sha256")
            rows[0]["record_sha256"] = hashlib.sha256(
                canonical_json(payload)
            ).hexdigest()
            ledger.write_bytes(
                b"".join(canonical_json(row) + b"\n" for row in rows)
            )

            scored = self._score(run_dir)

            self.assertEqual(scored.returncode, 1)
            self.assertIn("pinned case", scored.stderr.lower())
            self.assertFalse((run_dir / ".done").exists())
            self.assertFalse((run_dir / "summary.json").exists())

    def test_rehashed_case_and_manifest_map_cannot_replace_the_source_pin(self):
        from bench.compare.schema import canonical_json

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "source-pin-tamper"
            recorded = self._record(run_dir, GOOD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            case_ledger = run_dir / "cases.jsonl"
            rows = [
                json.loads(line)
                for line in case_ledger.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["case"]["category"] = "Attacker category"
            rows[0]["case_sha256"] = hashlib.sha256(
                canonical_json(rows[0]["case"])
            ).hexdigest()
            payload = dict(rows[0])
            payload.pop("record_sha256")
            rows[0]["record_sha256"] = hashlib.sha256(
                canonical_json(payload)
            ).hexdigest()
            case_ledger.write_bytes(
                b"".join(canonical_json(row) + b"\n" for row in rows)
            )

            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["manifest_core"]["cases"]["case_sha256_by_id"][
                rows[0]["case_id"]
            ] = rows[0]["case_sha256"]
            manifest_payload = dict(manifest)
            manifest_payload.pop("manifest_sha256")
            run_payload = dict(manifest_payload)
            run_payload.pop("run_id")
            manifest_payload["run_id"] = hashlib.sha256(
                canonical_json(run_payload)
            ).hexdigest()
            manifest_payload["manifest_sha256"] = hashlib.sha256(
                canonical_json(manifest_payload)
            ).hexdigest()
            manifest_path.write_bytes(canonical_json(manifest_payload) + b"\n")

            scored = self._score(run_dir)

            self.assertEqual(scored.returncode, 1)
            self.assertIn("source pin", scored.stderr.lower())
            self.assertFalse((run_dir / ".done").exists())

    def test_rehashed_final_artifacts_cannot_hide_changed_side_effects(self):
        from bench.compare.provenance import ProvenanceError, RunBundle
        from bench.compare.schema import canonical_json

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "final-content-tamper"
            recorded = self._record(run_dir, GOOD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            scored = self._score(run_dir)
            self.assertEqual(scored.returncode, 0, scored.stdout + scored.stderr)

            manifest = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
            original_run_id = manifest["run_id"]
            observations_path = run_dir / "observations.jsonl"
            observations = [
                json.loads(line)
                for line in observations_path.read_text(encoding="utf-8").splitlines()
            ]
            observations[0]["side_effects"]["writes"] = 999
            payload = dict(observations[0])
            payload.pop("record_sha256")
            observations[0]["record_sha256"] = hashlib.sha256(
                canonical_json(payload)
            ).hexdigest()
            observations_path.write_bytes(
                b"".join(canonical_json(row) + b"\n" for row in observations)
            )

            provenance_path = run_dir / "provenance.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["artifacts"]["observations.jsonl"] = {
                "sha256": hashlib.sha256(observations_path.read_bytes()).hexdigest(),
                "bytes": observations_path.stat().st_size,
            }
            result_payload = {
                "schema_version": 1,
                "run_id": original_run_id,
                "artifacts": provenance["artifacts"],
            }
            provenance["result_id"] = hashlib.sha256(
                canonical_json(result_payload)
            ).hexdigest()
            provenance_path.write_bytes(canonical_json(provenance) + b"\n")
            done_path = run_dir / ".done"
            done = json.loads(done_path.read_text(encoding="utf-8"))
            done["result_id"] = provenance["result_id"]
            done["provenance_sha256"] = hashlib.sha256(
                provenance_path.read_bytes()
            ).hexdigest()
            done_path.write_bytes(canonical_json(done) + b"\n")

            with self.assertRaisesRegex(
                ProvenanceError,
                "semantic|authoritative|side effect",
            ):
                RunBundle.open_existing(run_dir)
            self.assertEqual(
                json.loads(
                    (run_dir / "manifest.json").read_text(encoding="utf-8")
                )["run_id"],
                original_run_id,
            )

    def test_rehashed_canary_proof_tamper_is_rejected_semantically(self):
        from bench.compare.schema import canonical_json

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "canary-tamper"
            recorded = self._record(run_dir, GOOD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            ledger = run_dir / "observations.jsonl"
            rows = [
                json.loads(line)
                for line in ledger.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["fixture_execution"]["canary_read_proof_sha256"] = "0" * 64
            payload = dict(rows[0])
            payload.pop("record_sha256")
            rows[0]["record_sha256"] = hashlib.sha256(
                canonical_json(payload)
            ).hexdigest()
            ledger.write_bytes(
                b"".join(canonical_json(row) + b"\n" for row in rows)
            )

            scored = self._score(run_dir)

            self.assertEqual(scored.returncode, 1)
            self.assertIn("execution evidence", scored.stderr.lower())
            self.assertFalse((run_dir / ".done").exists())

    def test_permissive_caller_thresholds_cannot_make_bad_fixture_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run_dir = directory / "bad-thresholds"
            recorded = self._record(run_dir, BAD)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            permissive = directory / "permissive.json"
            permissive.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "fixture_only": True,
                        "required_arms": [
                            "corpus",
                            "native",
                            "code-search",
                            "code-graph",
                            "composed",
                        ],
                        "min_file_acc_at_10": 0,
                        "max_failure_rate": 1,
                        "max_unauthorized_side_effects": 999,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            scored = self._score(run_dir, thresholds=permissive)

            self.assertEqual(scored.returncode, 1)
            self.assertIn("threshold", scored.stderr.lower())
            self.assertFalse((run_dir / ".done").exists())

    def test_harness_source_identity_binds_git_revision_and_scorer_sources(self):
        from bench.compare.schema import (
            ContractError,
            _canonical_plugin_repository,
            harness_source_identity,
            require_reproducible_harness_source,
        )

        canonical_repository = (
            "https://github.com/redacted-org/"
            "codebase-search-plugin.git"
        )
        self.assertEqual(
            _canonical_plugin_repository(canonical_repository),
            canonical_repository,
        )
        self.assertEqual(
            _canonical_plugin_repository(canonical_repository.removesuffix(".git")),
            canonical_repository,
        )
        with self.assertRaisesRegex(ContractError, "repository"):
            _canonical_plugin_repository("https://github.com/example/fork.git")

        identity = harness_source_identity(ROOT)

        require_reproducible_harness_source(identity)
        self.assertTrue(identity["revision_files_match"])
        self.assertEqual(
            identity["plugin_repository"],
            canonical_repository,
        )
        self.assertRegex(identity["plugin_revision"], r"^[0-9a-f]{40}$")
        self.assertRegex(identity["plugin_tree"], r"^[0-9a-f]{40}$")
        self.assertIn("bench/compare/run.py", identity["files"])
        self.assertIn("bench/compare/score.py", identity["files"])
        self.assertIn("bench/compare/provenance.py", identity["files"])
        dirty_identity = dict(identity)
        dirty_identity["revision_files_match"] = False
        with self.assertRaisesRegex(ContractError, "differ"):
            require_reproducible_harness_source(dirty_identity)

    def test_fatal_budget_outcome_never_becomes_a_scored_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fixture = json.loads(GOOD.read_text(encoding="utf-8"))
            fixture["faults"] = [
                {
                    "unit_key": "fixture-auth-token|r1|corpus",
                    "error_class": "cost_cap",
                }
            ]
            fatal_fixture = directory / "fatal.json"
            fatal_fixture.write_text(
                json.dumps(fixture, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            run_dir = directory / "fatal"

            recorded = self._record(run_dir, fatal_fixture)

            self.assertEqual(recorded.returncode, 1)
            self.assertIn("cost_cap", recorded.stderr)
            self.assertFalse((run_dir / ".done").exists())
            self.assertFalse((run_dir / "summary.json").exists())
            if (run_dir / "errors.jsonl").exists():
                self.assertNotIn(
                    "cost_cap",
                    (run_dir / "errors.jsonl").read_text(encoding="utf-8"),
                )

    def test_tampered_arm_attribution_or_contract_never_scores(self):
        for field, value in (
            ("arm", "corpus"),
            ("arm_contract_sha256", "0" * 64),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                run_dir = Path(tmp) / field
                recorded = self._record(run_dir, GOOD)
                self.assertEqual(
                    recorded.returncode,
                    0,
                    recorded.stdout + recorded.stderr,
                )
                ledger = run_dir / "observations.jsonl"
                rows = [
                    json.loads(line)
                    for line in ledger.read_text(encoding="utf-8").splitlines()
                ]
                target = next(row for row in rows if row["arm"] == "native")
                target[field] = value
                payload = dict(target)
                payload.pop("record_sha256")
                target["record_sha256"] = hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                ledger.write_text(
                    "\n".join(
                        json.dumps(
                            row,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                        for row in rows
                    )
                    + "\n",
                    encoding="utf-8",
                )

                scored = self._score(run_dir)

                self.assertEqual(scored.returncode, 1)
                self.assertIn("binding", scored.stderr.lower())
                self.assertFalse((run_dir / ".done").exists())

    def test_fixture_thresholds_are_explicit_not_a_live_scoring_default(self):
        from bench.compare.score import parse_args

        arguments = parse_args(
            [
                "--run-dir",
                "/tmp/nonexistent-compare-run",
                "--intent-to-treat",
            ]
        )
        self.assertIsNone(arguments.thresholds)


if __name__ == "__main__":
    unittest.main()
