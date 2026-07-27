"""Acceptance tests for the offline runner and fail-closed live preflight."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
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

    def test_live_preflight_without_auth_or_enforceable_cost_bound_is_not_evaluated(self):
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
            self.assertIn("missing_claude_auth_evidence", diagnostic["reasons"])
            self.assertIn("missing_enforceable_cost_bound", diagnostic["reasons"])
            self.assertIn("missing_model_identity", diagnostic["reasons"])
            self.assertIn("missing_claude_cli_identity", diagnostic["reasons"])
            self.assertFalse((run_dir / ".done").exists())
            self.assertFalse((run_dir / "observations.jsonl").exists())

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
