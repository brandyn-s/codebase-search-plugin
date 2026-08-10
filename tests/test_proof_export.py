import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_proof_evaluator import _bundle


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_proof.py"
CI_FIXTURE = ROOT / "bench" / "e2e" / "proof-fixture-v1.json"


class ProofExportTests(unittest.TestCase):
    def _write_bundle(self, root: Path) -> Path:
        path = root / "input-proof-bundle.json"
        path.write_text(json.dumps(_bundle()), encoding="utf-8")
        return path

    def test_exports_deterministic_self_verifying_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = self._write_bundle(root)
            packets = [root / "packet-a", root / "packet-b"]

            for packet in packets:
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "export",
                        str(bundle),
                        "--output-dir",
                        str(packet),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            expected_files = {
                "manifest.json",
                "proof-bundle.json",
                "proof-result.json",
                "proof.md",
            }
            self.assertEqual(
                {item.name for item in packets[0].iterdir()},
                expected_files,
            )
            for name in expected_files:
                self.assertEqual(
                    (packets[0] / name).read_bytes(),
                    (packets[1] / name).read_bytes(),
                )

            manifest = json.loads((packets[0] / "manifest.json").read_text())
            self.assertEqual(manifest["schema_version"], 1)
            self.assertTrue(manifest["package_id"].startswith("proof-package:v1:"))
            for artifact in manifest["artifacts"]:
                contents = (packets[0] / artifact["path"]).read_bytes()
                self.assertEqual(hashlib.sha256(contents).hexdigest(), artifact["sha256"])
                self.assertEqual(len(contents), artifact["bytes"])

            verified = subprocess.run(
                [sys.executable, str(SCRIPT), "verify", str(packets[0])],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            verification = json.loads(verified.stdout)
            self.assertEqual(verification["status"], "verified")
            self.assertEqual(verification["package_id"], manifest["package_id"])

    def test_verification_rejects_tampered_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = self._write_bundle(root)
            packet = root / "packet"
            exported = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "export",
                    str(bundle),
                    "--output-dir",
                    str(packet),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)

            result = packet / "proof-result.json"
            result.write_text(result.read_text() + " ", encoding="utf-8")
            verified = subprocess.run(
                [sys.executable, str(SCRIPT), "verify", str(packet)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(verified.returncode, 2)
            self.assertIn("sha256 mismatch", verified.stderr)

    def test_checked_in_ci_fixture_exports_and_verifies(self):
        self.assertTrue(CI_FIXTURE.is_file(), CI_FIXTURE)
        with tempfile.TemporaryDirectory() as temporary:
            packet = Path(temporary) / "packet"
            exported = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "export",
                    str(CI_FIXTURE),
                    "--output-dir",
                    str(packet),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            verified = subprocess.run(
                [sys.executable, str(SCRIPT), "verify", str(packet)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(json.loads(verified.stdout)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
