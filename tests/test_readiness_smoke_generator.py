"""End-to-end contract for live readiness evidence generation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_live_readiness_evidence.py"
FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_readiness_mcp.py"


class ReadinessSmokeGeneratorTests(unittest.TestCase):
    def _wrapper(self, directory: Path, component: str) -> Path:
        wrapper = directory / component
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{FAKE_SERVER}" "{component}"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_generator_indexes_fixture_with_both_just_installed_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fixture = directory / "fixture"
            shutil.copytree(ROOT / "bench" / "e2e" / "target-repo", fixture)
            bom_path = directory / "component-bom.json"
            bom = json.loads(
                (ROOT / "component-bom.json").read_text(encoding="utf-8")
            )
            bom["integrated_readiness"]["status"] = "ready"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")
            code_search = self._wrapper(directory, "code-search")
            code_graph = self._wrapper(directory, "code-graph")
            output = directory / "live-readiness-evidence.json"

            completed = subprocess.run(
                [
                    sys.executable,
                    str(GENERATOR),
                    "--component-bom",
                    str(bom_path),
                    "--fixture",
                    str(fixture),
                    "--server",
                    f"code-search={code_search}",
                    "--server",
                    f"code-graph={code_graph}",
                    "--output",
                    str(output),
                    "--timeout",
                    "10",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PYTHONUNBUFFERED": "1",
                    "GH_TOKEN": "must-not-reach-smoke",
                },
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            evidence = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(
            evidence["producer"],
            "scripts/generate_live_readiness_evidence.py:v1",
        )
        self.assertTrue(evidence["checkout_unchanged"])
        search = evidence["components"]["code-search"]
        graph = evidence["components"]["code-graph"]
        self.assertEqual(
            search["version"],
            bom["components"]["code-search"]["install"]["revision"],
        )
        self.assertEqual(
            graph["version"],
            bom["components"]["code-graph"]["install"]["tag"],
        )
        self.assertTrue(search["completion"]["success"])
        self.assertTrue(search["index_ready"])
        self.assertEqual(graph["status"], "ready")
        for field in (
            "repository_id",
            "checkout_id",
            "source_revision",
            "dirty_fingerprint",
            "index_generation",
        ):
            self.assertEqual(
                search["index_identity"][field],
                graph["index_identity"][field],
            )
        self.assertNotIn("must-not-reach-smoke", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
