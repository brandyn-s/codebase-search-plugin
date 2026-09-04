"""The promotion gate decides whether trusted validation may touch a release."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts" / "promotion_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))
import promotion_gate  # noqa: E402


def _bom(repositories: dict[str, str]) -> dict:
    return {
        "components": {
            name: {"install": {"repository": repository, "tag": "v1.0.0"}}
            for name, repository in repositories.items()
        }
    }


class PromotionGateTests(unittest.TestCase):
    def test_third_party_pinned_bom_is_gated(self):
        result = promotion_gate.evaluate(
            _bom(
                {
                    "code-graph": "example-org/code-graph",
                    "code-search": "example-org/code-search",
                }
            )
        )
        self.assertFalse(result["run"])
        self.assertEqual(
            result["pinned"],
            {
                "code-graph": "example-org/code-graph",
                "code-search": "example-org/code-search",
            },
        )

    def test_mixed_pins_are_gated(self):
        result = promotion_gate.evaluate(
            _bom(
                {
                    "code-graph": "brandyn-s/code-graph",
                    "code-search": "example-org/code-search",
                }
            )
        )
        self.assertFalse(result["run"])

    def test_pending_first_release_is_gated_even_with_public_pins(self):
        bom = _bom(
            {
                "code-graph": "brandyn-s/code-graph",
                "code-search": "brandyn-s/code-search",
            }
        )
        bom["promotion_state"] = "pending-first-release"
        result = promotion_gate.evaluate(bom)
        self.assertFalse(result["run"])
        self.assertEqual(result["promotion_state"], "pending-first-release")

    def test_cli_names_the_promotion_state_in_its_notice(self):
        bom = _bom({"code-graph": "brandyn-s/code-graph"})
        bom["promotion_state"] = "pending-first-release"
        with tempfile.TemporaryDirectory() as tmp:
            bom_path = Path(tmp) / "bom.json"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(GATE), "--component-bom", str(bom_path)],
                text=True,
                capture_output=True,
                env={**os.environ, "GITHUB_OUTPUT": ""},
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["run"], False)
        self.assertIn("::notice::", completed.stderr)
        self.assertIn("pending-first-release", completed.stderr)

    def test_public_pins_run(self):
        result = promotion_gate.evaluate(
            _bom(
                {
                    "code-graph": "brandyn-s/code-graph",
                    "code-search": "brandyn-s/code-search",
                }
            )
        )
        self.assertTrue(result["run"])

    def test_malformed_bom_is_rejected(self):
        with self.assertRaises(ValueError):
            promotion_gate.evaluate({"components": {}})
        with self.assertRaises(ValueError):
            promotion_gate.evaluate({"components": {"code-graph": {"install": {}}}})

    def test_cli_writes_github_output_and_notice_when_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            bom_path = Path(tmp) / "bom.json"
            bom_path.write_text(
                json.dumps(_bom({"code-graph": "example-org/code-graph"})),
                encoding="utf-8",
            )
            output_path = Path(tmp) / "github-output"
            stdout, stderr = io.StringIO(), io.StringIO()
            previous = os.environ.get("GITHUB_OUTPUT")
            os.environ["GITHUB_OUTPUT"] = str(output_path)
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = promotion_gate.main(["--component-bom", str(bom_path)])
            finally:
                if previous is None:
                    os.environ.pop("GITHUB_OUTPUT", None)
                else:
                    os.environ["GITHUB_OUTPUT"] = previous
            github_output = output_path.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["run"], False)
        self.assertIn("::notice::", stderr.getvalue())
        self.assertIn("example-org/code-graph", stderr.getvalue())
        self.assertEqual(github_output, "run=false\n")

    def test_cli_reports_public_pins_without_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            bom_path = Path(tmp) / "bom.json"
            bom_path.write_text(
                json.dumps(_bom({"code-graph": "brandyn-s/code-graph"})),
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, str(GATE), "--component-bom", str(bom_path)],
                text=True,
                capture_output=True,
                env={**os.environ, "GITHUB_OUTPUT": ""},
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["run"], True)
        self.assertNotIn("::notice::", completed.stderr)

    def test_checked_in_bom_is_currently_gated_and_cli_exits_zero(self):
        completed = subprocess.run(
            [sys.executable, str(GATE), "--component-bom", str(ROOT / "component-bom.json")],
            text=True,
            capture_output=True,
            env={**os.environ, "GITHUB_OUTPUT": ""},
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertIn("run", result)
        self.assertEqual(set(result["pinned"]), {"code-graph", "code-search"})


if __name__ == "__main__":
    unittest.main()
