import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "capture_installed_runtime_smoke.py"


class InstalledRuntimeSmokeTests(unittest.TestCase):
    def test_capture_seals_a_fresh_cross_mcp_installed_plugin_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            (target / "app.py").write_text("def login():\n    return True\n")
            subprocess.run(["git", "init", "-q", str(target)], check=True)
            subprocess.run(["git", "-C", str(target), "add", "app.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target),
                    "-c",
                    "user.name=Runtime Smoke",
                    "-c",
                    "user.email=runtime@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            marketplace = root / ".claude" / "plugins" / "marketplaces" / "plugin"
            marketplace.mkdir(parents=True)
            fake_claude = root / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                "required = ['-p', '--output-format', 'stream-json', '--verbose', "
                "'--setting-sources', 'user', '--no-session-persistence']\n"
                "if any(item not in args for item in required): raise SystemExit(9)\n"
                "events = [\n"
                " {'type':'system','subtype':'init','model':'claude-sonnet-5',"
                "'plugins':[{'name':'codebase-search',"
                "'source':'codebase-search@redacted-code-intelligence',"
                "'version':'0.4.9'}],'mcp_servers':["
                "{'name':'plugin:codebase-search:code-search','status':'connected'},"
                "{'name':'plugin:codebase-search:code-graph','status':'connected'}]},\n"
                " {'type':'assistant','message':{'content':["
                "{'type':'tool_use','name':"
                "'mcp__plugin_codebase-search_code-search__search_code_evidence',"
                "'input':{'query':'request authentication'}},"
                "{'type':'tool_use','name':"
                "'mcp__plugin_codebase-search_code-graph__trace_call_path',"
                "'input':{'function_name':'login','direction':'inbound'}}]}},\n"
                " {'type':'result','is_error':False}\n"
                "]\n"
                "for event in events: print(json.dumps(event), flush=True)\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)
            evidence = root / "evidence"
            evidence.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    str(CAPTURE),
                    "--claude",
                    str(fake_claude),
                    "--target",
                    str(target),
                    "--evidence-root",
                    str(evidence),
                    "--marketplace-root",
                    str(marketplace),
                    "--plugin-version",
                    "0.4.9",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            roots = list(evidence.glob("installed-runtime-smoke-*"))
            self.assertEqual(len(roots), 1)
            receipt_root = roots[0]
            receipt = json.loads((receipt_root / "receipt.json").read_text())
            self.assertEqual(
                receipt["plugin_id"],
                "codebase-search@redacted-code-intelligence",
            )
            self.assertEqual(receipt["plugin_version"], "0.4.9")
            self.assertEqual(receipt["marketplace_root"], str(marketplace.resolve()))
            self.assertTrue(receipt["checkout_unchanged"])
            self.assertEqual(receipt["denied_tool_calls"], 0)
            manifest = json.loads((receipt_root / "manifest.json").read_text())
            self.assertEqual(
                set(manifest["artifacts"]),
                {"raw.jsonl", "receipt.json", "stderr.txt"},
            )
            for relative, expected in manifest["artifacts"].items():
                self.assertEqual(
                    hashlib.sha256((receipt_root / relative).read_bytes()).hexdigest(),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
