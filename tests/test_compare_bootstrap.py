"""Acceptance tests for pre-import benchmark source bootstrapping."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPARE = ROOT / "bench" / "compare"


class ComparisonBootstrapTests(unittest.TestCase):
    def test_dirty_initializer_cannot_execute_before_entrypoint_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            compare = directory / "bench" / "compare"
            compare.mkdir(parents=True)
            shutil.copyfile(COMPARE / "run.py", compare / "run.py")
            marker = directory / "initializer-side-effect"
            (compare / "__init__.py").write_text(
                "import os\n"
                "from pathlib import Path\n"
                "Path(os.environ['COMPARE_INIT_MARKER']).write_text('executed')\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [sys.executable, str(compare / "run.py"), "--help"],
                cwd=directory,
                env={**os.environ, "COMPARE_INIT_MARKER": str(marker)},
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(marker.exists())
            self.assertIn("initializer", completed.stderr.lower())

    def test_every_tracked_compare_python_source_is_bound_and_entrypoints_guarded(self):
        from bench.compare.schema import HARNESS_SOURCE_FILES

        tracked = {
            path
            for path in subprocess.check_output(
                ["git", "ls-files", "bench/compare/*.py"],
                cwd=ROOT,
                text=True,
            ).splitlines()
            if (ROOT / path).is_file()
        }
        on_disk = {
            path.relative_to(ROOT).as_posix()
            for path in COMPARE.glob("*.py")
            if path.is_file() and not path.is_symlink()
        }

        self.assertFalse((COMPARE / "__init__.py").exists())
        self.assertEqual(tracked, on_disk)
        self.assertEqual(set(HARNESS_SOURCE_FILES), tracked)
        for relative in sorted(tracked):
            source = (ROOT / relative).read_text(encoding="utf-8")
            if not source.startswith("#!/usr/bin/env python3"):
                continue
            guard = source.find("\n_reject_package_initializer()\n")
            package_import = source.find("\nfrom bench.compare")
            self.assertGreaterEqual(guard, 0, relative)
            self.assertGreater(package_import, guard, relative)


if __name__ == "__main__":
    unittest.main()
