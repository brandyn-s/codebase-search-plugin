"""Acceptance tests for provenance-bound live benchmark recordings."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "e2e"
RECORDER = BENCH / "record_live.py"
SCORER = BENCH / "score.py"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
        + "\n",
        encoding="utf-8",
    )


def identity(captured_at: str) -> dict:
    repository_id = "a" * 64
    source_revision = "c" * 40
    dirty_fingerprint = "clean"
    generation = hashlib.sha256(
        (
            repository_id
            + "\0"
            + source_revision
            + "\0"
            + dirty_fingerprint
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "repository_id": repository_id,
        "checkout_id": "b" * 64,
        "source_revision": source_revision,
        "dirty_fingerprint": dirty_fingerprint,
        "index_generation": generation,
        "captured_at": captured_at,
    }


class LiveBenchmarkProvenanceTests(unittest.TestCase):
    def _prepare_bundle(self, bundle: Path) -> dict[str, Path]:
        cases = bundle / "cases.jsonl"
        thresholds = bundle / "thresholds.json"
        runs = bundle / "runs.jsonl"
        transcript = bundle / "raw-mcp-transcript.jsonl"
        answers = bundle / "final-answers.jsonl"
        extraction = bundle / "claim-extraction.jsonl"
        evidence = bundle / "component-evidence.json"
        bom_path = bundle / "component-bom.json"
        target_manifest = bundle / "target-repo-manifest.json"
        provenance = bundle / "provenance.json"

        shutil.copy2(BENCH / "cases.jsonl", cases)
        shutil.copy2(BENCH / "thresholds.json", thresholds)
        shutil.copy2(BENCH / "target-repo-manifest.json", target_manifest)
        shutil.copytree(BENCH / "target-repo", bundle / "target-repo")

        run_records = [
            json.loads(line)
            for line in (BENCH / "runs" / "fixture-good.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line
        ]
        run_id = "live-provenance-test-v1"
        for record in run_records:
            record["run_id"] = run_id
            record["run_mode"] = "live"
        write_jsonl(runs, run_records)

        transcript_records = []
        answer_records = []
        extraction_records = []
        for record in run_records:
            raw_calls = [
                {
                    **call,
                    "response": {
                        "content": f"recorded fixture response for {record['case_id']}"
                    },
                }
                for call in record["tool_calls"]
            ]
            transcript_records.append(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "case_id": record["case_id"],
                    "tool_calls": raw_calls,
                    "evidence": record["evidence"],
                    "index_error": record["index_error"],
                    "latency_ms": record["latency_ms"],
                }
            )
            answer = " ".join(claim["text"] for claim in record["claims"])
            answer_records.append(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "case_id": record["case_id"],
                    "answer": answer,
                }
            )
            extraction_records.append(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "case_id": record["case_id"],
                    "answer_sha256": sha256_bytes(answer.encode("utf-8")),
                    "claims": record["claims"],
                }
            )
        write_jsonl(transcript, transcript_records)
        write_jsonl(answers, answer_records)
        write_jsonl(extraction, extraction_records)

        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        bom["integrated_readiness"]["status"] = "ready"
        bom_path.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
        component_evidence = {
            "schema_version": 1,
            "checkout_unchanged": True,
            "components": {
                "code-search": {
                    "version": bom["components"]["code-search"]["install"]["revision"],
                    "index_ready": True,
                    "index_identity": identity("2026-07-26T20:00:00Z"),
                },
                "code-graph": {
                    "version": bom["components"]["code-graph"]["install"]["tag"],
                    "status": "ready",
                    "index_identity": identity("2026-07-26T20:00:01Z"),
                },
            },
        }
        evidence.write_text(
            json.dumps(component_evidence, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "cases": cases,
            "thresholds": thresholds,
            "runs": runs,
            "transcript": transcript,
            "answers": answers,
            "extraction": extraction,
            "evidence": evidence,
            "bom": bom_path,
            "target_manifest": target_manifest,
            "provenance": provenance,
        }

    def _record(self, paths: dict[str, Path]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(RECORDER),
                "--cases",
                str(paths["cases"]),
                "--runs",
                str(paths["runs"]),
                "--thresholds",
                str(paths["thresholds"]),
                "--component-bom",
                str(paths["bom"]),
                "--component-evidence",
                str(paths["evidence"]),
                "--target-manifest",
                str(paths["target_manifest"]),
                "--raw-transcript",
                str(paths["transcript"]),
                "--final-answers",
                str(paths["answers"]),
                "--claim-extraction",
                str(paths["extraction"]),
                "--output",
                str(paths["provenance"]),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def _score(self, paths: dict[str, Path]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(SCORER),
                "--cases",
                str(paths["cases"]),
                "--runs",
                str(paths["runs"]),
                "--thresholds",
                str(paths["thresholds"]),
                "--bom",
                str(paths["bom"]),
                "--provenance",
                str(paths["provenance"]),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_recorder_binds_live_artifacts_before_scoring(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._prepare_bundle(Path(tmp))
            recorded = self._record(paths)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            scored = self._score(paths)

        self.assertEqual(scored.returncode, 0, scored.stdout + scored.stderr)
        self.assertIn(
            "PROVENANCED LIVE MEASUREMENT — NO COMPARATIVE GRADE",
            scored.stdout,
        )

    def test_unmanifested_target_file_invalidates_live_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            paths = self._prepare_bundle(bundle)
            recorded = self._record(paths)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            (bundle / "target-repo" / "src" / "unmanifested.py").write_text(
                "UNRECORDED = True\n",
                encoding="utf-8",
            )
            scored = self._score(paths)

        self.assertEqual(scored.returncode, 2, scored.stdout + scored.stderr)
        self.assertIn("unmanifested target fixture file", scored.stderr)
        self.assertNotIn("PROVENANCED LIVE MEASUREMENT", scored.stdout)

    def test_missing_raw_artifact_invalidates_live_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._prepare_bundle(Path(tmp))
            recorded = self._record(paths)
            self.assertEqual(
                recorded.returncode,
                0,
                recorded.stdout + recorded.stderr,
            )
            provenance = json.loads(
                paths["provenance"].read_text(encoding="utf-8")
            )
            provenance["artifacts"]["raw_mcp_transcript"]["path"] = (
                "missing-transcript.jsonl"
            )
            paths["provenance"].write_text(
                json.dumps(provenance, indent=2) + "\n",
                encoding="utf-8",
            )
            scored = self._score(paths)

        self.assertEqual(scored.returncode, 2, scored.stdout + scored.stderr)
        self.assertIn("artifact raw_mcp_transcript: file is missing", scored.stderr)
        self.assertNotIn("PROVENANCED LIVE MEASUREMENT", scored.stdout)

    def test_live_recording_workflow_is_documented_without_a_grade_claim(self):
        documentation = (BENCH / "README.md").read_text(encoding="utf-8")
        root_documentation = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("record_live.py", documentation)
        self.assertIn("--provenance", documentation)
        self.assertIn("target-repo-manifest.json", documentation)
        self.assertIn("raw MCP transcript", documentation)
        self.assertIn("final answers", documentation)
        self.assertIn("claim extraction", documentation)
        self.assertIn("component evidence", documentation)
        self.assertIn("NO COMPARATIVE GRADE", documentation)
        self.assertIn("Historical component-only measurements", root_documentation)
        self.assertIn(
            "not an integrated E2E comparative grade",
            root_documentation,
        )


if __name__ == "__main__":
    unittest.main()
