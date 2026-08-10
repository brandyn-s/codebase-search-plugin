#!/usr/bin/env python3
"""Aggregate code-intelligence routing traces without storing query text.

Input is JSONL with one object per completed request. Only categorical routing
and numeric operational fields are accepted. Unknown fields are ignored so raw
queries, paths, snippets, and evidence cannot be copied into the output.
"""

from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import statistics

ALLOWED_ROUTES = {"semantic", "lexical", "graph", "mixed", "security", "block_index"}
ALLOWED_BLOCK_REASONS = {"none", "stale", "identity_mismatch", "indexing", "unavailable"}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def aggregate(records: list[dict]) -> dict:
    routes: Counter[str] = Counter()
    blocks: Counter[str] = Counter()
    calls: list[int] = []
    latencies: list[float] = []
    fallback_count = 0
    incoherent_attempts = 0

    for record in records:
        route = record.get("route")
        if route not in ALLOWED_ROUTES:
            raise ValueError(f"invalid route: {route!r}")
        block_reason = record.get("block_reason", "none")
        if block_reason not in ALLOWED_BLOCK_REASONS:
            raise ValueError(f"invalid block_reason: {block_reason!r}")
        tool_calls = record.get("tool_calls", 0)
        latency_ms = record.get("latency_ms", 0.0)
        if isinstance(tool_calls, bool) or not isinstance(tool_calls, int) or tool_calls < 0:
            raise ValueError("tool_calls must be a nonnegative integer")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)) or latency_ms < 0:
            raise ValueError("latency_ms must be a nonnegative number")

        routes[route] += 1
        blocks[block_reason] += 1
        calls.append(tool_calls)
        latencies.append(float(latency_ms))
        fallback_count += 1 if record.get("fallback_used") is True else 0
        incoherent_attempts += 1 if record.get("cross_engine_incoherent_attempt") is True else 0

    total = len(records)
    return {
        "schema_version": 1,
        "requests": total,
        "routes": dict(sorted(routes.items())),
        "block_reasons": dict(sorted(blocks.items())),
        "mean_tool_calls": round(statistics.fmean(calls), 3) if calls else 0.0,
        "p95_latency_ms": round(_percentile(latencies, 0.95), 3),
        "fallback_rate": round(fallback_count / total, 6) if total else 0.0,
        "cross_engine_incoherent_attempts": incoherent_attempts,
        "privacy": {
            "query_text_stored": False,
            "repository_path_stored": False,
            "evidence_text_stored": False
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    records = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = aggregate(records)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
