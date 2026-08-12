#!/usr/bin/env python3
"""Run one bounded public retrieval comparison and real-repository scale check."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_live_readiness_evidence import (  # noqa: E402
    MCPClient,
    SmokeError,
    initialize_checkout,
    isolated_environment,
)


ARMS = ("native", "sourcegraph", "code-search", "code-graph", "composed")
CATEGORIES = {
    "Bug Report",
    "Feature Request",
    "Performance Issue",
    "Security Vulnerability",
}
STOP_WORDS = {
    "about",
    "after",
    "again",
    "against",
    "also",
    "been",
    "before",
    "being",
    "below",
    "bug",
    "cannot",
    "could",
    "current",
    "does",
    "feature",
    "from",
    "have",
    "here",
    "into",
    "least",
    "limit",
    "more",
    "newer",
    "not",
    "only",
    "performance",
    "security",
    "should",
    "shown",
    "summary",
    "specified",
    "takes",
    "than",
    "that",
    "their",
    "the",
    "then",
    "there",
    "these",
    "this",
    "through",
    "version",
    "when",
    "where",
    "which",
    "with",
    "worked",
    "would",
}


class MeasurementError(RuntimeError):
    """The frozen measurement could not be executed faithfully."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_object(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise MeasurementError(f"missing or unsafe JSON input: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MeasurementError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MeasurementError(f"{path}: expected a JSON object")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 900,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise MeasurementError(
            f"command failed: {arguments[0]}: {stderr.strip() or exc}"
        ) from exc


def git(checkout: Path, *arguments: str, timeout: float = 900) -> str:
    return run_command(
        ["git", "-C", str(checkout), *arguments], timeout=timeout
    ).stdout.strip()


def normalize_query(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n\n", 1)[0].strip()


def _clean_anchor(value: str) -> str | None:
    candidate = value.strip().strip("`'\"()[]{}<>.,:;!?*")
    candidate = candidate.removesuffix("()")
    if not candidate or candidate.startswith(("http://", "https://")):
        return None
    if "/blob/" in candidate or len(candidate) > 80:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", candidate):
        return None
    if len(candidate) < 3 or candidate.lower() in STOP_WORDS:
        return None
    return candidate


def query_anchors(query: str, limit: int = 8) -> list[str]:
    """Derive one oracle-blind identifier/title query for lexical interfaces."""
    normalized = normalize_query(query)
    values: list[str] = []

    def add(raw: str) -> None:
        candidate = _clean_anchor(raw)
        if candidate and candidate.casefold() not in {
            item.casefold() for item in values
        }:
            values.append(candidate)

    for match in re.finditer(r"(?<!`)`([^`\n]+)`(?!`)", normalized):
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", match.group(1)):
            add(token)
    for match in re.finditer(
        r"https://github\.com/[^\s)]+/blob/[0-9a-f]{40}/([^#\s)]+)", normalized
    ):
        path = match.group(1).strip(".,:;!?")
        if path and path.casefold() not in {item.casefold() for item in values}:
            values.append(path)
    without_urls = re.sub(r"https?://\S+", " ", normalized)
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", without_urls)
    for token in tokens:
        cleaned = _clean_anchor(token)
        if (
            cleaned
            and (
                "_" in cleaned
                or "." in cleaned
                or cleaned.isupper()
                or any(character.isupper() for character in cleaned[1:])
            )
        ):
            add(cleaned)
    title = normalized.splitlines()[0] if normalized else ""
    if len(values) < 4:
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", title):
            add(token)
    if len(values) < 6:
        for token in tokens:
            if len(token) >= 5:
                add(token)
            if len(values) >= 6:
                break
    return values[:limit]


def validate_inputs(
    contract: dict,
    case_set: dict,
    oracle_set: dict,
    selection_pin: dict,
) -> tuple[list[dict], dict[str, dict]]:
    if contract.get("schema_version") != 1 or contract.get("language_model_calls") != 0:
        raise MeasurementError("unsupported or non-zero-LLM measurement contract")
    cases = case_set.get("cases")
    oracles = oracle_set.get("cases")
    if not isinstance(cases, list) or not isinstance(oracles, list):
        raise MeasurementError("cases and oracle must contain arrays")
    by_id = {
        item.get("case_id"): item
        for item in oracles
        if isinstance(item, dict) and isinstance(item.get("case_id"), str)
    }
    if len(cases) != 4 or len(by_id) != 4:
        raise MeasurementError("the frozen pilot must contain exactly four cases")
    if {case.get("category") for case in cases} != CATEGORIES:
        raise MeasurementError("the frozen pilot is not balanced across four categories")
    if len({case.get("case_id") for case in cases}) != len(cases):
        raise MeasurementError("duplicate case identity")

    pin_cases = selection_pin.get("cases")
    if not isinstance(pin_cases, list) or selection_pin.get("n") != 200:
        raise MeasurementError("selection pin is not the recorded n=200 pin")
    first_by_category: dict[str, str] = {}
    for item in pin_cases:
        if isinstance(item, dict) and item.get("category") not in first_by_category:
            first_by_category[item.get("category")] = item.get("instance_id")
    expected_selection = {
        first_by_category[category] for category in sorted(CATEGORIES)
    }
    if {case.get("case_id") for case in cases} != expected_selection:
        raise MeasurementError("cases do not match the pre-existing selection rule")

    for case in cases:
        case_id = case.get("case_id")
        oracle = by_id.get(case_id)
        if oracle is None:
            raise MeasurementError(f"{case_id}: missing oracle")
        if not re.fullmatch(r"[0-9a-f]{40}", str(case.get("revision"))):
            raise MeasurementError(f"{case_id}: invalid revision")
        if normalize_query(case.get("query", "")) != case.get("query"):
            raise MeasurementError(f"{case_id}: query is not canonical")
        expected_functions = oracle.get("expected_functions")
        hunk_functions = oracle.get("github_pr", {}).get("hunk_functions")
        if (
            not isinstance(expected_functions, list)
            or not expected_functions
            or expected_functions != hunk_functions
        ):
            raise MeasurementError(f"{case_id}: two-source function labels disagree")
        derived_files = sorted(
            {item.split(":", 1)[0] for item in expected_functions}
        )
        if derived_files != sorted(oracle.get("expected_files", [])):
            raise MeasurementError(f"{case_id}: file labels disagree with functions")
        pr = oracle.get("github_pr", {})
        if pr.get("base_revision") != case.get("revision"):
            raise MeasurementError(f"{case_id}: GitHub base revision mismatch")
    return cases, by_id


def verify_github_oracle(case: dict, oracle: dict) -> dict:
    repository = case["repository"]
    pr = oracle["github_pr"]
    number = pr["number"]
    summary = json.loads(
        run_command(["gh", "api", f"repos/{repository}/pulls/{number}"]).stdout
    )
    files = json.loads(
        run_command(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repository}/pulls/{number}/files?per_page=100",
            ]
        ).stdout
    )
    observed_files = {
        item.get("filename"): item.get("patch", "")
        for item in files
        if isinstance(item, dict) and isinstance(item.get("filename"), str)
    }
    if (
        summary.get("merged_at") != pr["merged_at"]
        or summary.get("base", {}).get("sha") != pr["base_revision"]
        or summary.get("head", {}).get("sha") != pr["head_revision"]
    ):
        raise MeasurementError(f"{case['case_id']}: live GitHub PR identity differs")
    for label in oracle["expected_functions"]:
        path, symbol = label.split(":", 1)
        terminal = symbol.rsplit(".", 1)[-1]
        if path not in observed_files or terminal not in observed_files[path]:
            raise MeasurementError(
                f"{case['case_id']}: GitHub diff no longer corroborates {label}"
            )
    return {
        "repository": repository,
        "number": number,
        "base_revision": summary["base"]["sha"],
        "head_revision": summary["head"]["sha"],
        "merged_at": summary["merged_at"],
        "changed_paths": sorted(observed_files),
    }


def ensure_checkout(case: dict, destination: Path) -> None:
    revision = case["revision"]
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        run_command(["git", "init", "--quiet", str(destination)])
        git(
            destination,
            "remote",
            "add",
            "origin",
            f"https://github.com/{case['repository']}.git",
        )
        git(
            destination,
            "-c",
            "protocol.version=2",
            "fetch",
            "--quiet",
            "--depth=1",
            "--filter=blob:none",
            "origin",
            revision,
            timeout=1800,
        )
        git(destination, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    if git(destination, "rev-parse", "HEAD") != revision:
        raise MeasurementError(f"{case['case_id']}: checkout revision mismatch")
    if git(destination, "status", "--porcelain=v1", "--untracked-files=all"):
        raise MeasurementError(f"{case['case_id']}: checkout is dirty")


def tracked_paths(checkout: Path) -> list[str]:
    raw = run_command(
        ["git", "-C", str(checkout), "ls-files", "-z"]
    ).stdout
    return [item for item in raw.split("\0") if item]


def repository_stats(checkout: Path) -> dict:
    paths = tracked_paths(checkout)
    total_bytes = 0
    text_bytes = 0
    text_lines = 0
    digest = hashlib.sha256()
    for relative in paths:
        path = checkout / relative
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        total_bytes += len(data)
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        text_bytes += len(data)
        text_lines += len(text.splitlines())
    return {
        "tracked_files": len(paths),
        "tracked_bytes": total_bytes,
        "utf8_text_bytes": text_bytes,
        "utf8_text_lines": text_lines,
        "tracked_manifest_sha256": digest.hexdigest(),
    }


def distinct_files(items: list[dict], key: str, limit: int) -> list[str]:
    files: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if isinstance(value, str):
            normalized = value.replace("\\", "/").lstrip("./")
            if normalized and normalized not in files:
                files.append(normalized)
        if len(files) >= limit:
            break
    return files


def native_lexical(checkout: Path, anchors: list[str], limit: int) -> tuple[list[str], int]:
    start = time.perf_counter_ns()
    lowered = [anchor.casefold() for anchor in anchors]
    scored: list[tuple[int, int, str]] = []
    for relative in tracked_paths(checkout):
        path = checkout / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            content = path.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeDecodeError):
            continue
        haystack = relative.casefold() + "\n" + content
        counts = [haystack.count(anchor) for anchor in lowered]
        unique = sum(value > 0 for value in counts)
        if unique:
            scored.append((unique, sum(min(value, 100) for value in counts), relative))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[2] for item in scored[:limit]], time.perf_counter_ns() - start


def parse_sse(body: str) -> tuple[list[dict], dict | None, list[dict]]:
    matches: list[dict] = []
    progress: dict | None = None
    alerts: list[dict] = []
    for block in body.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MeasurementError("Sourcegraph returned malformed SSE JSON") from exc
        if event == "matches" and isinstance(payload, list):
            matches.extend(item for item in payload if isinstance(item, dict))
        elif event == "progress" and isinstance(payload, dict) and payload.get("done"):
            progress = payload
        elif event == "alert" and isinstance(payload, dict):
            alerts.append(payload)
    return matches, progress, alerts


def sourcegraph_search(
    contract: dict,
    case: dict,
    anchors: list[str],
) -> tuple[list[str], int, dict]:
    escaped_repository = re.escape(f"github.com/{case['repository']}")
    path_anchors = [
        anchor for anchor in anchors if "/" in anchor or anchor.endswith(".py")
    ]
    symbol_anchors = [
        anchor.rsplit(".", 1)[-1]
        for anchor in anchors
        if anchor not in path_anchors and ("_" in anchor or "." in anchor)
    ]
    if path_anchors:
        expression = f"file:{re.escape(path_anchors[0])}"
        result_type = "file"
        adapter_mode = "path"
    elif symbol_anchors:
        expression = "(" + " OR ".join(symbol_anchors) + ")"
        result_type = "symbol"
        adapter_mode = "symbol"
    else:
        acronym = next((anchor for anchor in anchors if anchor.isupper()), None)
        words = [anchor for anchor in anchors if not anchor.isupper()]
        clauses = [f'"{acronym}"'] if acronym else []
        if len(words) >= 2:
            clauses.append(f'"{words[0]} {words[1]}"')
        elif words:
            clauses.append(f'"{words[0]}"')
        expression = "(" + " OR ".join(clauses) + ")"
        result_type = "file"
        adapter_mode = "lexical"
    query = (
        f"repo:^{escaped_repository}$ rev:{case['revision']} "
        f"{expression} type:{result_type} patternType:keyword "
        f"count:{contract['sourcegraph']['requested_matches']}"
    )
    parameters = urllib.parse.urlencode(
        {
            "q": query,
            "v": contract["sourcegraph"]["query_version"],
            "cm": "true",
            "display": str(contract["sourcegraph"]["requested_matches"]),
        }
    )
    request = urllib.request.Request(
        contract["sourcegraph"]["endpoint"] + "?" + parameters,
        headers={
            "Accept": "text/event-stream",
            "User-Agent": "code-intel-bounded-benchmark/1",
        },
    )
    start = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(
            request, timeout=contract["sourcegraph"]["timeout_seconds"]
        ) as response:
            body = response.read().decode("utf-8", errors="strict")
    except Exception as exc:
        raise MeasurementError(f"Sourcegraph request failed: {exc}") from exc
    elapsed = time.perf_counter_ns() - start
    matches, progress, alerts = parse_sse(body)
    for item in matches:
        commit = item.get("commit")
        if commit is not None and commit != case["revision"]:
            raise MeasurementError(f"{case['case_id']}: Sourcegraph revision mismatch")
    files = distinct_files(matches, "path", contract["top_k_files"])
    return files, elapsed, {
        "query": query,
        "adapter_mode": adapter_mode,
        "match_count": progress.get("matchCount") if progress else None,
        "duration_ms_reported": progress.get("durationMs") if progress else None,
        "skipped": progress.get("skipped", []) if progress else [],
        "alerts": alerts,
    }


def _process_tree_rss_bytes(root_pid: int) -> int:
    try:
        output = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,rss="],
            text=True,
            capture_output=True,
            check=True,
            timeout=2,
        ).stdout
    except Exception:
        return 0
    rows: dict[int, tuple[int, int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, rss = map(int, parts)
        except ValueError:
            continue
        rows[pid] = (ppid, rss)
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, _) in rows.items():
            if ppid in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return sum(rows.get(pid, (0, 0))[1] for pid in descendants) * 1024


class PeakRSS:
    def __init__(self, pid: int):
        self.pid = pid
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, _process_tree_rss_bytes(self.pid))
            self._stop.wait(0.2)

    def __enter__(self) -> "PeakRSS":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.peak = max(self.peak, _process_tree_rss_bytes(self.pid))


def index_search(client: MCPClient, root: Path, incremental: bool = True) -> tuple[dict, int, int]:
    start = time.perf_counter_ns()
    with PeakRSS(client.process.pid) as sampler:
        started = client.call_tool(
            "index_directory",
            {"directory_path": str(root.resolve()), "incremental": incremental},
        )
        if started.get("status") != "indexing":
            raise MeasurementError("code-search did not start an indexing job")
        while True:
            progress = client.call_tool("get_indexing_progress", {})
            if progress.get("status") == "indexing":
                time.sleep(0.1)
                continue
            if progress.get("status") != "completed":
                raise MeasurementError("code-search indexing did not complete")
            result = progress.get("result")
            if not isinstance(result, dict) or result.get("success") is not True:
                raise MeasurementError("code-search indexing failed")
            break
    return result, time.perf_counter_ns() - start, sampler.peak


def index_graph(client: MCPClient, root: Path) -> tuple[dict, int, int]:
    start = time.perf_counter_ns()
    with PeakRSS(client.process.pid) as sampler:
        result = client.call_tool(
            "index_repository", {"repo_path": str(root.resolve()), "skip_report": True}
        )
    if result.get("identity_status") != "captured":
        raise MeasurementError("code-graph indexing did not capture an identity")
    return result, time.perf_counter_ns() - start, sampler.peak


def require_coherent_identities(search: dict, graph: dict, revision: str) -> dict:
    search_identity = search.get("index_identity")
    graph_identity = graph.get("index_identity")
    fields = (
        "repository_id",
        "checkout_id",
        "source_revision",
        "dirty_fingerprint",
        "index_generation",
    )
    if not isinstance(search_identity, dict) or not isinstance(graph_identity, dict):
        raise MeasurementError("an index identity is missing")
    if any(search_identity.get(field) != graph_identity.get(field) for field in fields):
        raise MeasurementError("search and graph indexed different source states")
    if search_identity.get("source_revision") != revision:
        raise MeasurementError("index source revision differs from the case pin")
    return {field: search_identity[field] for field in fields}


def timed_search(
    client: MCPClient,
    query: str,
    contract: dict,
) -> tuple[list[str], int, dict]:
    start = time.perf_counter_ns()
    result = client.call_tool(
        "search_code_evidence",
        {
            "query": query,
            "k": contract["search"]["requested_chunks"],
            "search_mode": contract["search"]["search_mode"],
            "include_context": contract["search"]["include_context"],
            "auto_reindex": contract["search"]["auto_reindex"],
        },
    )
    elapsed = time.perf_counter_ns() - start
    metadata = result.get("_metadata", {})
    refs = metadata.get("evidence_refs", {}) if isinstance(metadata, dict) else {}
    return (
        distinct_files(result.get("results", []), "file", contract["top_k_files"]),
        elapsed,
        {
            "freshness": metadata.get("freshness") if isinstance(metadata, dict) else None,
            "evidence_refs": refs,
        },
    )


def timed_graph(
    client: MCPClient,
    project: str,
    anchors: list[str],
    contract: dict,
) -> tuple[list[str], int, dict]:
    query = " ".join(anchors)
    start = time.perf_counter_ns()
    result = client.call_tool(
        "code_localize",
        {
            "issue_description": query,
            "project": project,
            "top_k": contract["graph"]["requested_entities"],
            "depth": contract["graph"]["depth"],
            "seed_strategy": contract["graph"]["seed_strategy"],
        },
    )
    elapsed = time.perf_counter_ns() - start
    return (
        distinct_files(result.get("matches", []), "file_path", contract["top_k_files"]),
        elapsed,
        {"query": query, "returned_entities": result.get("total_returned")},
    )


def rrf(rankings: list[list[str]], k: int, limit: int) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, path in enumerate(ranking, 1):
            scores[path] = scores.get(path, 0.0) + 1.0 / (k + rank)
    return [
        path
        for path, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[
            :limit
        ]
    ]


def score_ranking(files: list[str], expected: set[str]) -> dict:
    rank = next((index for index, path in enumerate(files[:10], 1) if path in expected), None)
    return {
        "rank": rank,
        "file_acc_at_1": rank == 1,
        "file_acc_at_3": rank is not None and rank <= 3,
        "file_acc_at_10": rank is not None and rank <= 10,
        "file_mrr_at_10": 0.0 if rank is None else 1.0 / rank,
    }


def directory_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        item.stat().st_size
        for item in path.rglob("*")
        if item.is_file() and not item.is_symlink()
    )


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def incremental_measurement(
    case: dict,
    oracle: dict,
    checkout: Path,
    search: MCPClient,
    graph: MCPClient,
) -> dict:
    excluded = set(oracle["expected_files"])
    candidates = [
        path
        for path in tracked_paths(checkout)
        if path.endswith(".py")
        and path not in excluded
        and (checkout / path).is_file()
        and (checkout / path).stat().st_size < 1_000_000
    ]
    if not candidates:
        raise MeasurementError(f"{case['case_id']}: no incremental probe file")
    relative = sorted(candidates)[0]
    path = checkout / relative
    marker = "code_intel_incremental_probe_20260811"
    original = path.read_bytes()
    path.write_bytes(original + f"\n# {marker}\n".encode("utf-8"))
    try:
        search_result, search_ns, search_rss = index_search(search, checkout, True)
        graph_result, graph_ns, graph_rss = index_graph(graph, checkout)
        identity = require_coherent_identities(search_result, graph_result, case["revision"])
        if identity["dirty_fingerprint"] == "clean":
            raise MeasurementError("incremental update did not capture dirty source")
        probe = search.call_tool(
            "search_code_evidence",
            {
                "query": marker,
                "k": 5,
                "search_mode": "keyword",
                "file_pattern": relative,
                "include_context": False,
                "auto_reindex": False,
            },
        )
        found = any(
            isinstance(item, dict) and item.get("file") == relative
            for item in probe.get("results", [])
        )
        if not found:
            raise MeasurementError("incremental code-search index did not expose probe")
        return {
            "case_id": case["case_id"],
            "modified_path": relative,
            "search_index_ns": search_ns,
            "search_peak_rss_bytes": search_rss,
            "search_files_added": search_result.get("files_added"),
            "search_files_modified": search_result.get("files_modified"),
            "graph_index_ns": graph_ns,
            "graph_peak_rss_bytes": graph_rss,
            "graph_action": graph_result.get("_metadata", {}).get("action_outcome"),
            "probe_found": True,
            "dirty_index_generation": identity["index_generation"],
        }
    finally:
        path.write_bytes(original)
        if git(checkout, "status", "--porcelain=v1", "--untracked-files=all"):
            raise MeasurementError("incremental probe checkout was not restored")


def instrument_check(
    workspace: Path,
    search_server: Path,
    graph_server: Path,
    local_model: Path,
) -> dict:
    fixture = workspace / "instrument-repository"
    shutil.copytree(ROOT / "bench" / "e2e" / "target-repo", fixture)
    environment = isolated_environment(workspace / "instrument-runtime", local_model)
    initialize_checkout(fixture, environment)
    deadline = time.monotonic() + 600
    search = MCPClient("code-search", str(search_server), deadline, environment, fixture)
    graph = MCPClient("code-graph", str(graph_server), deadline, environment, fixture)
    try:
        search_result, search_ns, _ = index_search(search, fixture)
        graph_result, graph_ns, _ = index_graph(graph, fixture)
        require_coherent_identities(
            search_result, graph_result, git(fixture, "rev-parse", "HEAD")
        )
        search_files, search_query_ns, _ = timed_search(
            search,
            "Where are bearer tokens validated?",
            {
                "search": {
                    "requested_chunks": 50,
                    "search_mode": "semantic",
                    "include_context": False,
                    "auto_reindex": False,
                },
                "top_k_files": 10,
            },
        )
        graph_files, graph_query_ns, _ = timed_graph(
            graph,
            graph_result["project"],
            ["bearer", "token", "validate"],
            {
                "graph": {
                    "requested_entities": 50,
                    "depth": 3,
                    "seed_strategy": "substring",
                },
                "top_k_files": 10,
            },
        )
        target = "src/auth/token.py"
        if not search_files or search_files[0] != target:
            raise MeasurementError("instrument code-search did not rank target first")
        if not graph_files or graph_files[0] != target:
            raise MeasurementError("instrument code-graph did not rank target first")
        return {
            "status": "passed",
            "expected_file": target,
            "search_rank": 1,
            "graph_rank": 1,
            "search_index_ns": search_ns,
            "graph_index_ns": graph_ns,
            "search_query_ns": search_query_ns,
            "graph_query_ns": graph_query_ns,
        }
    finally:
        search.close()
        graph.close()


def aggregate(case_results: list[dict]) -> dict:
    summary: dict[str, dict] = {}
    for arm in ARMS:
        scores = [case["arms"][arm]["score"] for case in case_results]
        latencies = [
            value
            for case in case_results
            for value in case["arms"][arm].get("latencies_ns", [])
        ]
        summary[arm] = {
            "cases": len(scores),
            "file_acc_at_1": statistics.fmean(
                float(score["file_acc_at_1"]) for score in scores
            ),
            "file_acc_at_3": statistics.fmean(
                float(score["file_acc_at_3"]) for score in scores
            ),
            "file_acc_at_10": statistics.fmean(
                float(score["file_acc_at_10"]) for score in scores
            ),
            "file_mrr_at_10": statistics.fmean(
                score["file_mrr_at_10"] for score in scores
            ),
            "latency_p50_ns": percentile(latencies, 0.50),
            "latency_p95_ns": percentile(latencies, 0.95),
        }
    return summary


def run_measurement(args: argparse.Namespace) -> dict:
    paths = {
        "contract": args.contract.resolve(),
        "cases": args.cases.resolve(),
        "oracle": args.oracle.resolve(),
        "selection_pin": args.selection_pin.resolve(),
        "dataset_parquet": args.dataset_parquet.resolve(),
        "search_server": args.search_server.resolve(),
        "graph_server": args.graph_server.resolve(),
        "local_model": args.local_model.resolve(),
    }
    contract = load_object(paths["contract"])
    case_set = load_object(paths["cases"])
    oracle_set = load_object(paths["oracle"])
    selection_pin = load_object(paths["selection_pin"])
    if (
        sha256_file(paths["selection_pin"])
        != contract.get("selection", {}).get("resolved_pin_sha256")
    ):
        raise MeasurementError("resolved n=200 selection pin digest mismatch")
    cases, oracles = validate_inputs(contract, case_set, oracle_set, selection_pin)
    dataset = case_set["dataset"]
    if sha256_file(paths["dataset_parquet"]) != dataset["parquet_sha256"]:
        raise MeasurementError("LocBench Parquet digest mismatch")
    model_weights = paths["local_model"] / "model.safetensors"
    if (
        not model_weights.is_file()
        or sha256_file(model_weights)
        != contract["local_embedding_model"]["model_safetensors_sha256"]
    ):
        raise MeasurementError("local embedding model digest mismatch")
    for component in ("search_server", "graph_server"):
        if not paths[component].is_file():
            raise MeasurementError(f"installed component is missing: {paths[component]}")
    if args.workspace.exists() or args.output.exists():
        raise MeasurementError("workspace and output must both be initially absent")
    args.workspace.mkdir(parents=True)

    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    github_evidence = {
        case["case_id"]: verify_github_oracle(case, oracles[case["case_id"]])
        for case in cases
    }
    instrument = instrument_check(
        args.workspace,
        paths["search_server"],
        paths["graph_server"],
        paths["local_model"],
    )
    case_results: list[dict] = []
    incremental: dict | None = None

    for case in cases:
        case_id = case["case_id"]
        print(f"[{len(case_results) + 1}/{len(cases)}] {case_id}", flush=True)
        checkout = args.workspace / "repositories" / case_id
        ensure_checkout(case, checkout)
        stats = repository_stats(checkout)
        anchors = query_anchors(case["query"])
        if not anchors:
            raise MeasurementError(f"{case_id}: query adapter produced no anchors")
        expected = set(oracles[case_id]["expected_files"])

        native_files, native_ns = native_lexical(
            checkout, anchors, contract["top_k_files"]
        )
        sourcegraph_error = None
        sourcegraph_metadata: dict = {}
        try:
            sourcegraph_files, sourcegraph_ns, sourcegraph_metadata = sourcegraph_search(
                contract, case, anchors
            )
        except MeasurementError as exc:
            sourcegraph_error = str(exc)
            sourcegraph_files, sourcegraph_ns = [], 0

        runtime_root = args.workspace / "runtimes" / case_id
        environment = isolated_environment(runtime_root, paths["local_model"])
        deadline = time.monotonic() + 1800
        search = MCPClient(
            "code-search", str(paths["search_server"]), deadline, environment, checkout
        )
        graph = MCPClient(
            "code-graph", str(paths["graph_server"]), deadline, environment, checkout
        )
        try:
            required_search = {
                "index_directory",
                "get_indexing_progress",
                "get_index_status",
                "search_code_evidence",
            }
            required_graph = {"index_repository", "index_status", "code_localize"}
            if not required_search <= set(search.list_tools()):
                raise MeasurementError("installed code-search tool contract is incomplete")
            if not required_graph <= set(graph.list_tools()):
                raise MeasurementError("installed code-graph tool contract is incomplete")
            search_index, search_index_ns, search_rss = index_search(search, checkout)
            graph_index, graph_index_ns, graph_rss = index_graph(graph, checkout)
            identity = require_coherent_identities(
                search_index, graph_index, case["revision"]
            )
            search_files, first_search_ns, search_metadata = timed_search(
                search, case["query"], contract
            )
            graph_error = None
            try:
                graph_files, first_graph_ns, graph_metadata = timed_graph(
                    graph, graph_index["project"], anchors, contract
                )
            except SmokeError as exc:
                graph_error = str(exc)
                graph_files, first_graph_ns, graph_metadata = [], 0, {}
            search_latencies = [first_search_ns]
            graph_latencies = [first_graph_ns] if first_graph_ns else []
            search_stable = True
            graph_stable = True
            for _ in range(contract["warm_query_repetitions"] - 1):
                repeated_search, elapsed, _ = timed_search(search, case["query"], contract)
                search_latencies.append(elapsed)
                search_stable = search_stable and repeated_search == search_files
                if graph_error is None:
                    repeated_graph, elapsed, _ = timed_graph(
                        graph, graph_index["project"], anchors, contract
                    )
                    graph_latencies.append(elapsed)
                    graph_stable = graph_stable and repeated_graph == graph_files

            composed_files = rrf(
                [search_files, graph_files],
                contract["composition"]["rrf_k"],
                contract["top_k_files"],
            )
            if case_id == contract["incremental_case_id"]:
                incremental = incremental_measurement(
                    case, oracles[case_id], checkout, search, graph
                )
            scale = {
                **stats,
                "search_cold_index_ns": search_index_ns,
                "search_cold_peak_rss_bytes": search_rss,
                "search_index_bytes": directory_bytes(
                    runtime_root / "code-search-storage"
                ),
                "search_chunks": search_index.get("index_stats", {}).get(
                    "chunks_indexed"
                ),
                "graph_cold_index_ns": graph_index_ns,
                "graph_cold_peak_rss_bytes": graph_rss,
                "graph_index_bytes": directory_bytes(
                    runtime_root / "home" / ".cache" / "codebase-memory-mcp"
                ),
                "graph_nodes": graph_index.get("nodes"),
                "graph_edges": graph_index.get("edges"),
                "search_warm_latency_p50_ns": percentile(search_latencies, 0.50),
                "search_warm_latency_p95_ns": percentile(search_latencies, 0.95),
                "graph_warm_latency_p50_ns": percentile(graph_latencies, 0.50),
                "graph_warm_latency_p95_ns": percentile(graph_latencies, 0.95),
            }
        finally:
            search.close()
            graph.close()

        arm_payloads = {
            "native": {
                "files": native_files,
                "latencies_ns": [native_ns],
                "adapter": {"anchors": anchors},
            },
            "sourcegraph": {
                "files": sourcegraph_files,
                "latencies_ns": [sourcegraph_ns] if sourcegraph_ns else [],
                "adapter": sourcegraph_metadata,
                "error": sourcegraph_error,
            },
            "code-search": {
                "files": search_files,
                "latencies_ns": search_latencies,
                "stable_across_repetitions": search_stable,
                "metadata": search_metadata,
            },
            "code-graph": {
                "files": graph_files,
                "latencies_ns": graph_latencies,
                "stable_across_repetitions": graph_stable,
                "metadata": graph_metadata,
                "error": graph_error,
            },
            "composed": {
                "files": composed_files,
                "latencies_ns": [
                    search_latencies[index] + graph_latencies[index]
                    for index in range(min(len(search_latencies), len(graph_latencies)))
                ],
                "method": contract["composition"],
            },
        }
        for payload in arm_payloads.values():
            payload["score"] = score_ranking(payload["files"], expected)
        case_results.append(
            {
                "case_id": case_id,
                "category": case["category"],
                "repository": case["repository"],
                "revision": case["revision"],
                "query_sha256": hashlib.sha256(case["query"].encode()).hexdigest(),
                "anchors": anchors,
                "expected_files": sorted(expected),
                "index_identity": identity,
                "arms": arm_payloads,
                "scale": scale,
            }
        )

    if incremental is None:
        raise MeasurementError("the predeclared incremental measurement did not run")
    finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "schema_version": 1,
        "status": "completed",
        "measurement_id": contract["measurement_id"],
        "started_at": started,
        "finished_at": finished,
        "provenance": {
            "plugin_revision": git(ROOT, "rev-parse", "HEAD"),
            "contract_sha256": sha256_file(paths["contract"]),
            "cases_sha256": sha256_file(paths["cases"]),
            "oracle_sha256": sha256_file(paths["oracle"]),
            "selection_pin_sha256": sha256_file(paths["selection_pin"]),
            "dataset_parquet_sha256": sha256_file(paths["dataset_parquet"]),
            "search_server_sha256": sha256_file(paths["search_server"]),
            "graph_server_sha256": sha256_file(paths["graph_server"]),
            "local_model": contract["local_embedding_model"],
            "language_model_calls": 0,
            "github_oracle_evidence": github_evidence,
        },
        "instrument": instrument,
        "cases": case_results,
        "aggregate": aggregate(case_results),
        "incremental": incremental,
        "interpretation": {
            "scope": "directional balanced n=4 public pilot",
            "statistical_superiority_claim_allowed": False,
            "unavailable_competitors": ["Cursor", "Augment", "Greptile"],
        },
    }
    result["result_sha256"] = hashlib.sha256(canonical_json(result)).hexdigest()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--selection-pin", type=Path, required=True)
    parser.add_argument("--dataset-parquet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--search-server", type=Path, required=True)
    parser.add_argument("--graph-server", type=Path, required=True)
    parser.add_argument("--local-model", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_measurement(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (MeasurementError, SmokeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "completed", "result_sha256": result["result_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
