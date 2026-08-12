import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WRITER = ROOT / "scripts" / "write_deployment_receipt.py"


class DeploymentReceiptWriterTests(unittest.TestCase):
    def test_cli_writes_one_explicit_runtime_and_holdout_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary) / "evidence"
            runtime_manifest = evidence_root / "runtime" / "manifest.json"
            holdout_manifest = evidence_root / "holdout" / "manifest.json"
            runtime_manifest.parent.mkdir(parents=True)
            holdout_manifest.parent.mkdir(parents=True)
            runtime_manifest.write_text(
                '{"schema_version":1,"artifacts":{}}\n', encoding="utf-8"
            )
            holdout_manifest.write_text(
                '{"schema_version":1,"artifacts":{}}\n', encoding="utf-8"
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRITER),
                    "--evidence-root",
                    str(evidence_root),
                    "--plugin-version",
                    "0.4.22",
                    "--runtime-manifest",
                    str(runtime_manifest),
                    "--holdout-manifest",
                    str(holdout_manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            receipt_path = evidence_root / "deployment-receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            runtime_sha256 = hashlib.sha256(runtime_manifest.read_bytes()).hexdigest()
            holdout_sha256 = hashlib.sha256(holdout_manifest.read_bytes()).hexdigest()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), str(receipt_path))
        self.assertEqual(
            receipt,
            {
                "schema_version": 1,
                "receipt_type": "code-intelligence-deployment",
                "plugin_version": "0.4.22",
                "runtime_manifest": {
                    "path": "runtime/manifest.json",
                    "sha256": runtime_sha256,
                },
                "holdout_manifest": {
                    "path": "holdout/manifest.json",
                    "sha256": holdout_sha256,
                },
            },
        )

    def test_cli_binds_schema_two_holdout_with_explicit_artifact_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence_root = Path(temporary) / "evidence"
            runtime_manifest = evidence_root / "runtime" / "manifest.json"
            holdout_manifest = evidence_root / "holdout" / "manifest.json"
            runtime_manifest.parent.mkdir(parents=True)
            holdout_manifest.parent.mkdir(parents=True)
            runtime_manifest.write_text(
                '{"schema_version":1,"artifacts":{}}\n', encoding="utf-8"
            )
            holdout_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "artifacts": {"summary.json": "a" * 64},
                        "artifact_roles": {"summary": "summary.json"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(WRITER),
                    "--evidence-root",
                    str(evidence_root),
                    "--plugin-version",
                    "0.4.22",
                    "--runtime-manifest",
                    str(runtime_manifest),
                    "--holdout-manifest",
                    str(holdout_manifest),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
