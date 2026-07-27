#!/usr/bin/env python3
"""Create a content-addressed provenance envelope for a captured live run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from live_provenance import (
    ProvenanceError,
    build_provenance,
    load_json,
    load_jsonl,
    validate_component_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind raw live benchmark artifacts into provenance"
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--component-bom", type=Path, required=True)
    parser.add_argument("--component-evidence", type=Path, required=True)
    parser.add_argument("--target-manifest", type=Path, required=True)
    parser.add_argument("--raw-transcript", type=Path, required=True)
    parser.add_argument("--final-answers", type=Path, required=True)
    parser.add_argument("--claim-extraction", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        run_records = load_jsonl(args.runs)
        run_ids = {record.get("run_id") for record in run_records}
        run_modes = {record.get("run_mode") for record in run_records}
        if len(run_ids) != 1 or None in run_ids or run_modes != {"live"}:
            raise ProvenanceError("runs must contain one live run_id")
        run_id = next(iter(run_ids))
        target_manifest = load_json(args.target_manifest)
        target = {
            "repository": target_manifest.get("repository"),
            "revision": target_manifest.get("revision"),
        }
        validate_component_evidence(
            args.component_bom,
            args.component_evidence,
        )
        provenance = build_provenance(
            output=args.output,
            run_id=run_id,
            target=target,
            paths={
                "cases": args.cases,
                "runs": args.runs,
                "thresholds": args.thresholds,
                "component_bom": args.component_bom,
                "component_evidence": args.component_evidence,
                "target_manifest": args.target_manifest,
                "raw_mcp_transcript": args.raw_transcript,
                "final_answers": args.final_answers,
                "claim_extraction": args.claim_extraction,
            },
        )
        args.output.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ProvenanceError) as exc:
        print(f"Live recording FAILED: {exc}", file=sys.stderr)
        return 2

    print(f"Live provenance recorded: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
