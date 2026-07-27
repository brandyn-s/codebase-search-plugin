"""CLI behavior tests for trusted exact-component readiness validation."""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "validate_real_installed.py"


class ValidateRealInstalledCLITests(unittest.TestCase):
    def test_help_documents_readiness_evidence_output(self):
        completed = subprocess.run(
            [sys.executable, str(HELPER), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--readiness-evidence-output", completed.stdout)


if __name__ == "__main__":
    unittest.main()
