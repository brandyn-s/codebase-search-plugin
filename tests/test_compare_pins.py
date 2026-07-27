"""Tests for balanced calibration pins and the external June pin reference."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPARE = ROOT / "bench" / "compare"


class ComparisonPinTests(unittest.TestCase):
    def _git_backed_sources(
        self,
        directory: Path,
    ) -> tuple[dict, dict, Path]:
        categories = (
            "Bug Report",
            "Feature Request",
            "Performance Issue",
            "Security Vulnerability",
        )
        repository_root = directory / "repositories"
        cases = []
        audits = []
        for category_index, category in enumerate(categories):
            slug = f"example/public-{category_index}"
            repo = repository_root / slug
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            source_cases = []
            for index in range(12):
                case_id = f"case-{category_index}-{index:02d}"
                file_name = f"src/{case_id}.py"
                target = repo / file_name
                target.parent.mkdir(parents=True, exist_ok=True)
                content = f"def function_{index}():\n    return True\n"
                if case_id == "case-0-01":
                    content = "VALUE = True\n"
                target.write_text(content, encoding="utf-8")
                source_cases.append(
                    {
                        "instance_id": case_id,
                        "category": category,
                        "query": f"Locate {case_id}.",
                        "repo": slug,
                        "base_commit": base_commit,
                        "oracle": {
                            "files": [file_name],
                            "classes": [],
                            "functions": [f"function_{index}"],
                        },
                    }
                )
            subprocess.run(["git", "-C", repo, "add", "src"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-qm", "audited changes"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            cases.extend(source_cases)
            audits.extend(
                {
                    "instance_id": case["instance_id"],
                    "repository": slug,
                    "base_commit": base_commit,
                    "head_commit": head_commit,
                }
                for case in source_cases
            )
        cases[0]["oracle"]["files"] = ["src/disputed.py"]
        return (
            {"schema_version": 1, "cases": cases},
            {"schema_version": 1, "cases": audits},
            repository_root,
        )

    def test_n40_generation_is_balanced_deterministic_and_audited(self):
        from bench.compare.build_pin import build_balanced_pin

        with tempfile.TemporaryDirectory() as tmp:
            source, audit_evidence, repository_root = self._git_backed_sources(
                Path(tmp)
            )
            arguments = {
                "source_sha256": "a" * 64,
                "source_repository": "czlll/Loc-Bench_V1",
                "source_revision": "b" * 40,
                "source_path": "data/public-labels.json",
                "audit_evidence": audit_evidence,
                "audit_evidence_sha256": "c" * 64,
                "audit_evidence_path": "data/public-audit-evidence.json",
                "repository_root": repository_root,
                "seed": 42,
            }
            first = build_balanced_pin(source, **arguments)
            second = build_balanced_pin(source, **arguments)

        self.assertEqual(first, second)
        self.assertEqual(len(first["cases"]), 40)
        counts = {
            category: sum(
                case["category"] == category for case in first["cases"]
            )
            for category in ("Bug", "Feature", "Performance", "Security")
        }
        self.assertEqual(counts, {category: 10 for category in counts})
        self.assertEqual(first["generation"]["seed"], 42)
        self.assertEqual(first["generation"]["selection"], "sha256_priority_v1")
        self.assertTrue(
            {"case-0-00", "case-0-01"}
            <= {
                item["case_id"]
                for item in first["label_audit"]["quarantined"]
            },
        )
        for case in first["cases"]:
            self.assertEqual(case["label_audit"]["status"], "verified")
            self.assertEqual(
                case["label_audit"]["changed_files_source"],
                "git_diff_base_head_v1",
            )
            self.assertEqual(
                case["label_audit"]["symbol_verification"],
                "definition_pattern_v1",
            )
            self.assertEqual(
                case["label_audit"]["verifier"],
                "pinned_git_objects_v1",
            )
            self.assertRegex(
                case["label_audit"]["audit_record_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertTrue(
                set(case["oracle"]["files"])
                <= set(case["label_audit"]["changed_files"])
            )

    def test_n40_generation_refuses_incomplete_or_unpinned_source(self):
        from bench.compare.build_pin import PinError, build_balanced_pin

        with tempfile.TemporaryDirectory() as tmp:
            source, audit_evidence, repository_root = self._git_backed_sources(
                Path(tmp)
            )
            common = {
                "source_sha256": "a" * 64,
                "source_repository": "czlll/Loc-Bench_V1",
                "source_revision": "b" * 40,
                "source_path": "data/public-labels.json",
                "audit_evidence_sha256": "c" * 64,
                "audit_evidence_path": "data/public-audit-evidence.json",
                "repository_root": repository_root,
                "seed": 42,
            }
            incomplete = {"schema_version": 1, "cases": source["cases"][:9]}
            incomplete_audit = {
                "schema_version": 1,
                "cases": audit_evidence["cases"][:9],
            }
            with self.assertRaisesRegex(PinError, "eligible"):
                build_balanced_pin(
                    incomplete,
                    audit_evidence=incomplete_audit,
                    **common,
                )
            with self.assertRaisesRegex(PinError, "revision"):
                build_balanced_pin(
                    source,
                    audit_evidence=audit_evidence,
                    **{**common, "source_revision": "main"},
                )

            invented = json.loads(json.dumps(audit_evidence))
            invented["cases"][0]["head_commit"] = "f" * 40
            with self.assertRaisesRegex(PinError, "Git object"):
                build_balanced_pin(
                    source,
                    audit_evidence=invented,
                    **common,
                )

    def test_external_june_pin_is_verified_by_hash_without_copying_cases(self):
        from bench.compare.build_pin import verify_external_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            external = {
                "schema_version": 1,
                "n": 200,
                "score_depth": 10,
                "recorded_order_sha256": "c" * 64,
                "pinned_instance_ids": [
                    f"case-{index}" for index in range(200)
                ],
                "cases": [
                    {
                        "instance_id": f"case-{index}",
                        "repo": "example/public",
                        "base_commit": "d" * 40,
                        "category": "Bug Report",
                    }
                    for index in range(200)
                ],
            }
            external_path = directory / "locbench-n200-pin.json"
            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            reference = {
                "schema_version": 1,
                "kind": "external_content_address",
                "repository": "redacted-org/code-graph",
                "source_revision": "e" * 40,
                "path": "bench/accuracy/example.json",
                "sha256": hashlib.sha256(external_path.read_bytes()).hexdigest(),
                "expected_count": 200,
                "score_depth": 10,
                "recorded_order_sha256": "c" * 64,
                "availability": "published",
            }
            ordered = "\n".join(external["pinned_instance_ids"]) + "\n"
            external["recorded_order_sha256"] = hashlib.sha256(
                ordered.encode("utf-8")
            ).hexdigest()
            reference["recorded_order_sha256"] = external[
                "recorded_order_sha256"
            ]
            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            reference["sha256"] = hashlib.sha256(
                external_path.read_bytes()
            ).hexdigest()

            verified = verify_external_pin(reference, external_path)
            self.assertEqual(verified["verified_count"], 200)
            self.assertEqual(verified["sha256"], reference["sha256"])
            self.assertEqual(
                verified["status"],
                "address_verified_not_runnable",
            )
            self.assertFalse(verified["runnable"])
            self.assertEqual(
                verified["blockers"],
                ["missing_query_oracle_labels"],
            )

            invented = json.loads(json.dumps(external))
            for case in invented["cases"]:
                case["query"] = "Invented query"
                case["oracle"] = {
                    "files": ["src/invented.py"],
                    "classes": [],
                    "functions": ["invented"],
                }
                case["label_audit"] = {
                    "status": "verified",
                    "changed_files": ["src/invented.py"],
                }
            external_path.write_text(
                json.dumps(invented, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            invented_reference = {
                **reference,
                "availability": "published",
                "sha256": hashlib.sha256(external_path.read_bytes()).hexdigest(),
            }
            invented_result = verify_external_pin(
                invented_reference,
                external_path,
            )
            self.assertFalse(invented_result["runnable"])
            self.assertIn(
                "git_object_label_provenance_not_verified",
                invented_result["blockers"],
            )

            tampered = json.loads(json.dumps(external))
            tampered["recorded_order_sha256"] = "0" * 64
            external_path.write_text(
                json.dumps(tampered, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tampered_reference = dict(reference)
            tampered_reference["sha256"] = hashlib.sha256(
                external_path.read_bytes()
            ).hexdigest()
            tampered_reference["recorded_order_sha256"] = "0" * 64
            with self.assertRaisesRegex(Exception, "recorded order"):
                verify_external_pin(tampered_reference, external_path)

            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            external["cases"].pop()
            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "SHA-256"):
                verify_external_pin(reference, external_path)

    def test_checked_in_june_reference_contains_no_private_case_data(self):
        reference_path = (
            COMPARE / "pins" / "locbench-june-n200.external.json"
        )
        reference = json.loads(reference_path.read_text(encoding="utf-8"))

        self.assertEqual(reference["expected_count"], 200)
        self.assertEqual(
            reference["sha256"],
            "886156bbd16eb753a690da6bcb452f9238f53ef28409b1f4e483b842a0556453",
        )
        self.assertNotIn("cases", reference)
        self.assertNotIn("queries", reference)
        self.assertEqual(reference["availability"], "published")
        self.assertEqual(
            reference["source_revision"],
            "d7b93959dace3215cd096a13c1a27e259063dc95",
        )
        self.assertFalse(reference["runnable"])
        self.assertEqual(
            reference["blockers"],
            ["missing_query_oracle_labels"],
        )

    def test_ci_runs_good_and_bad_instrument_falsifiers_without_secrets(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Gate the five-arm comparison instrument", workflow)
        self.assertIn("five-arm-good.json", workflow)
        self.assertIn("five-arm-bad.json", workflow)
        compare_step = workflow.split(
            "- name: Gate the five-arm comparison instrument", 1
        )[1]
        self.assertNotIn("secrets.", compare_step)
        self.assertNotIn("continue-on-error", compare_step)

    def test_five_arm_fixture_has_seven_cases_and_a_content_addressed_canary(self):
        pin_path = COMPARE / "pins" / "fixture-public-n7.json"
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
        canary = pin["dataset"]["instrument_canary"]
        canary_path = ROOT / canary["path"]

        self.assertEqual(len(pin["cases"]), 7)
        self.assertEqual(
            sum(case.get("injection_canary") is True for case in pin["cases"]),
            1,
        )
        self.assertEqual(
            hashlib.sha256(canary_path.read_bytes()).hexdigest(),
            canary["sha256"],
        )
        manifest = json.loads(
            (ROOT / pin["dataset"]["instrument_fixture"]["manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        source_root = Path(
            pin["dataset"]["instrument_fixture"]["source_root"]
        )
        canary_relative = Path(canary["path"]).relative_to(source_root).as_posix()
        self.assertIn(canary_relative, manifest["files"])
        canary_text = canary_path.read_text(encoding="utf-8")
        self.assertIn(canary["write_path_environment"], canary_text)
        self.assertIn(canary["secret_environment"], canary_text)
        self.assertIn(canary["network_environment"], canary_text)

        for fixture_name in ("five-arm-good.json", "five-arm-bad.json"):
            fixture = json.loads(
                (COMPARE / "fixtures" / fixture_name).read_text(encoding="utf-8")
            )
            self.assertEqual(
                fixture["kind"],
                "deterministic_executor_fault_plan_v1",
            )
            self.assertEqual(
                fixture["cases_sha256"],
                hashlib.sha256(pin_path.read_bytes()).hexdigest(),
            )
            self.assertNotIn("results", fixture)
            self.assertNotIn("oracle", json.dumps(fixture))
            self.assertNotIn("evidence_bytes", json.dumps(fixture))
        good = json.loads(
            (COMPARE / "fixtures" / "five-arm-good.json").read_text(encoding="utf-8")
        )
        bad = json.loads(
            (COMPARE / "fixtures" / "five-arm-bad.json").read_text(encoding="utf-8")
        )
        self.assertEqual(good["faults"], [])
        self.assertEqual(len(bad["faults"]), 1)


if __name__ == "__main__":
    unittest.main()
