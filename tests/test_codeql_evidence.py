import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_proof_evaluator import INDEX_GENERATION, module


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codeql_evidence.py"
REPOSITORY_ID = "r" * 64
SOURCE_REVISION = "s" * 40


def _write_inputs(root: Path) -> tuple[Path, Path, Path]:
    database_manifest = root / "codeql-database-manifest.json"
    database_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_id": REPOSITORY_ID,
                "source_revision": SOURCE_REVISION,
                "language": "python",
                "codeql_cli_version": "2.23.1",
                "extractor_version": "codeql/python-all@4.0.0",
                "database_content_sha256": "d" * 64,
                "quality": {
                    "status": "pass",
                    "source_files": 18,
                    "baseline_lines": 640,
                    "extractor_errors": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    query_pack_manifest = root / "query-pack-lock.json"
    query_pack_manifest.write_text(
        '{"pack":"codeql/python-queries","version":"1.6.3"}\n',
        encoding="utf-8",
    )
    sarif = root / "results.sarif"
    sarif.write_text(
        json.dumps(
            {
                "version": "2.1.0",
                "runs": [
                    {
                        "tool": {
                            "driver": {
                                "name": "CodeQL",
                                "semanticVersion": "2.23.1",
                                "rules": [
                                    {
                                        "id": "py/sql-injection",
                                        "properties": {
                                            "precision": "high",
                                            "security-severity": "8.8",
                                        },
                                    }
                                ],
                            }
                        },
                        "results": [
                            {
                                "ruleId": "py/sql-injection",
                                "ruleIndex": 0,
                                "message": {"text": "User input reaches SQL execution."},
                                "codeFlows": [
                                    {
                                        "threadFlows": [
                                            {
                                                "locations": [
                                                    {
                                                        "location": {
                                                            "physicalLocation": {
                                                                "artifactLocation": {
                                                                    "uri": "src/api/reports.py"
                                                                },
                                                                "region": {
                                                                    "startLine": 4,
                                                                    "startColumn": 12,
                                                                    "endLine": 4,
                                                                    "endColumn": 24,
                                                                },
                                                            }
                                                        }
                                                    },
                                                    {
                                                        "location": {
                                                            "physicalLocation": {
                                                                "artifactLocation": {
                                                                    "uri": "src/db/execute.py"
                                                                },
                                                                "region": {
                                                                    "startLine": 9,
                                                                    "startColumn": 5,
                                                                    "endLine": 9,
                                                                    "endColumn": 20,
                                                                },
                                                            }
                                                        }
                                                    },
                                                ]
                                            }
                                        ]
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return database_manifest, query_pack_manifest, sarif


class CodeQLEvidenceAcceptanceTests(unittest.TestCase):
    def test_cli_projects_a_codeql_path_into_variable_level_taint_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_manifest, query_pack_manifest, sarif = _write_inputs(root)
            output = root / "observation.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "ingest",
                    str(sarif),
                    "--database-manifest",
                    str(database_manifest),
                    "--query-pack-manifest",
                    str(query_pack_manifest),
                    "--index-generation",
                    INDEX_GENERATION,
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            observation = json.loads(output.read_text(encoding="utf-8"))
            analysis = observation["evidence_ref"]["analysis_ref"]
            self.assertEqual(observation["source_engine"], "codeql")
            self.assertEqual(observation["derivation"], "codeql_path")
            self.assertEqual(analysis["analysis_kind"], "variable_level_taint")
            self.assertEqual(analysis["query_id"], "py/sql-injection")
            self.assertEqual(
                analysis["database_manifest_sha256"],
                hashlib.sha256(database_manifest.read_bytes()).hexdigest(),
            )
            self.assertEqual(analysis["database_content_sha256"], "d" * 64)
            self.assertEqual(analysis["database_quality"]["status"], "pass")
            self.assertEqual(
                [step["role"] for step in analysis["path_steps"]],
                ["source", "sink"],
            )

            bundle = {
                "schema_version": 1,
                "claim": {
                    "schema_version": 1,
                    "repository_id": REPOSITORY_ID,
                    "claim_kind": "variable_level_taint_path",
                    "claim_text": "Report owner input reaches SQL execution.",
                },
                "index_state": {
                    "coherent": True,
                    "freshness": "current",
                    "index_generation": INDEX_GENERATION,
                },
                "assurance_requirement": {
                    "required_capabilities": ["variable_level_taint"],
                },
                "observations": [observation],
                "contradiction_search": {
                    "performed": True,
                    "strategy": "codeql_source_sink_path_search",
                    "candidate_count": 1,
                },
                "coverage": {
                    "state": "complete",
                    "examined": 1,
                    "expected": 1,
                    "unresolved": 0,
                },
            }
            claim_payload = bundle["claim"]
            claim_payload["id"] = module._stable_id("claim", claim_payload)

            result = module.evaluate(bundle)

            self.assertEqual(result["verdict"], "verified")
            self.assertEqual(
                result["assurance_lattice"]["satisfied_by"],
                "support",
            )

    def test_cli_rejects_database_revision_mismatch_with_the_requested_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_manifest, query_pack_manifest, sarif = _write_inputs(root)
            output = root / "observation.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "ingest",
                    str(sarif),
                    "--database-manifest",
                    str(database_manifest),
                    "--query-pack-manifest",
                    str(query_pack_manifest),
                    "--repository-id",
                    REPOSITORY_ID,
                    "--source-revision",
                    "x" * 40,
                    "--index-generation",
                    INDEX_GENERATION,
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("source revision", completed.stderr)
            self.assertFalse(output.exists())

    def test_cli_rejects_a_result_without_a_code_flow(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_manifest, query_pack_manifest, sarif = _write_inputs(root)
            payload = json.loads(sarif.read_text(encoding="utf-8"))
            del payload["runs"][0]["results"][0]["codeFlows"]
            sarif.write_text(json.dumps(payload), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "ingest",
                    str(sarif),
                    "--database-manifest",
                    str(database_manifest),
                    "--query-pack-manifest",
                    str(query_pack_manifest),
                    "--repository-id",
                    REPOSITORY_ID,
                    "--source-revision",
                    SOURCE_REVISION,
                    "--index-generation",
                    INDEX_GENERATION,
                    "--output",
                    str(root / "observation.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("result.codeFlows", completed.stderr)

    def test_cli_rejects_a_database_without_passing_extraction_quality(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_manifest, query_pack_manifest, sarif = _write_inputs(root)
            database = json.loads(database_manifest.read_text(encoding="utf-8"))
            database["quality"]["status"] = "fail"
            database_manifest.write_text(json.dumps(database), encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "ingest",
                    str(sarif),
                    "--database-manifest",
                    str(database_manifest),
                    "--query-pack-manifest",
                    str(query_pack_manifest),
                    "--repository-id",
                    REPOSITORY_ID,
                    "--source-revision",
                    SOURCE_REVISION,
                    "--index-generation",
                    INDEX_GENERATION,
                    "--output",
                    str(root / "observation.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("database quality", completed.stderr)


if __name__ == "__main__":
    unittest.main()
