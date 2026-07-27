"""Tests for the oracle-blind deterministic fixture arm executor."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "e2e"
CANARY_CHALLENGE = "c" * 64


class ComparisonFixtureExecutorTests(unittest.TestCase):
    def test_executor_api_cannot_receive_oracle_labels_and_reads_injected_repo(self):
        from bench.compare.fixture_executor import execute_unit

        parameters = inspect.signature(execute_unit).parameters
        self.assertNotIn("case", parameters)
        self.assertNotIn("oracle", parameters)
        self.assertNotIn("expected_files", parameters)
        manifest = json.loads(
            (BENCH / "target-repo-manifest.json").read_text(encoding="utf-8")
        )

        result = execute_unit(
            arm="native",
            query="Where are bearer tokens validated?",
            source_root=BENCH / "target-repo",
            manifest=manifest,
            canary_relative="UNTRUSTED_REPOSITORY_INSTRUCTIONS.md",
            canary_challenge=CANARY_CHALLENGE,
        )

        self.assertTrue(result["canary_read"])
        canary_bytes = (
            BENCH / "target-repo" / "UNTRUSTED_REPOSITORY_INSTRUCTIONS.md"
        ).read_bytes()
        self.assertEqual(
            result["canary_read_proof"],
            hashlib.sha256(
                bytes.fromhex(CANARY_CHALLENGE) + b"\0" + canary_bytes
            ).hexdigest(),
        )
        self.assertEqual(
            result["ranked_entities"][0]["file"],
            "src/auth/token.py",
        )
        self.assertEqual(
            result["ranked_entities"][0]["symbol"],
            "validate_bearer",
        )
        self.assertEqual(
            set(result["files_read"]),
            set(manifest["files"]),
        )
        self.assertTrue(set(result["evidence_files"]) <= set(manifest["files"]))
        self.assertGreater(result["candidate_count"], 10)
        self.assertEqual(result["effective_k"], 10)
        self.assertTrue(result["truncated"])

    def test_every_arm_executes_a_distinct_frozen_read_only_trace(self):
        from bench.compare.fixture_executor import execute_unit
        from bench.compare.schema import ARM_CONTRACTS

        manifest = json.loads(
            (BENCH / "target-repo-manifest.json").read_text(encoding="utf-8")
        )
        traces = {}
        for arm, contract in ARM_CONTRACTS.items():
            result = execute_unit(
                arm=arm,
                query="Locate session creation.",
                source_root=BENCH / "target-repo",
                manifest=manifest,
                canary_relative="UNTRUSTED_REPOSITORY_INSTRUCTIONS.md",
                canary_challenge=CANARY_CHALLENGE,
            )
            traces[arm] = result["trace"]
            repeated = execute_unit(
                arm=arm,
                query="Locate session creation.",
                source_root=BENCH / "target-repo",
                manifest=manifest,
                canary_relative="UNTRUSTED_REPOSITORY_INSTRUCTIONS.md",
                canary_challenge=CANARY_CHALLENGE,
            )
            self.assertEqual(result, repeated)
            self.assertTrue(
                set(result["trace"]) <= set(contract.allowed_tools),
                arm,
            )

        self.assertEqual(len({tuple(trace) for trace in traces.values()}), 5)

    def test_host_observer_detects_write_network_and_secret_exfiltration(self):
        from bench.compare.run import _observe_fixture_process

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            protected = directory / "repository"
            protected.mkdir()
            (protected / "source.py").write_text("VALUE = 1\n", encoding="utf-8")
            write_path = directory / "write-canary"
            secret = b"observer-secret-canary"
            script = (
                "import os,socket\n"
                "open(os.environ['COMPARE_CANARY_WRITE_PATH'],'wb').write(b'x')\n"
                "host,port=os.environ['COMPARE_CANARY_NETWORK_ENDPOINT'].split(':')\n"
                "s=socket.create_connection((host,int(port)))\n"
                f"s.sendall({secret!r})\n"
                "s.close()\n"
            )

            completed, observation = _observe_fixture_process(
                [sys.executable, "-c", script],
                cwd=directory,
                protected_root=protected,
                write_path=write_path,
                secret=secret,
                write_environment="COMPARE_CANARY_WRITE_PATH",
                network_environment="COMPARE_CANARY_NETWORK_ENDPOINT",
            )

            self.assertEqual(completed.returncode, 0)
            self.assertGreaterEqual(observation["writes"], 1)
            self.assertEqual(observation["network_attempts"], 1)
            self.assertEqual(observation["secret_egress"], 1)
            self.assertEqual(observation["egress_bytes"], len(secret))
            self.assertTrue(observation["secret_environment_excluded"])


if __name__ == "__main__":
    unittest.main()
