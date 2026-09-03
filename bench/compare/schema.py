"""Frozen control, arm, identity, and observation contracts."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .token_accounting import tokenizer_descriptor

SHA256 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
USD_DECIMAL = re.compile(r"(?:0|[1-9]\d*)\.\d{6}")
COMPARE_ROOT = Path(__file__).resolve().parent

SEARCH_TOOLS = (
    "mcp__code-search__code_localize",
    "mcp__code-search__find_similar_code",
    "mcp__code-search__get_file_context",
    "mcp__code-search__get_index_status",
    "mcp__code-search__list_projects",
    "mcp__code-search__search_code",
    "mcp__code-search__verify_index_integrity",
)
GRAPH_TOOLS = (
    "mcp__code-graph__detect_changes",
    "mcp__code-graph__get_architecture",
    "mcp__code-graph__get_code_snippet",
    "mcp__code-graph__query_graph",
    "mcp__code-graph__query_security_surfaces",
    "mcp__code-graph__query_stig_evidence",
    "mcp__code-graph__search_code",
    "mcp__code-graph__search_graph",
    "mcp__code-graph__trace_call_path",
    "mcp__code-graph__trace_data_flow",
)
FORBIDDEN_TOOLS = frozenset(
    {
        "Bash",
        "Edit",
        "NotebookEdit",
        "Web",
        "WebFetch",
        "WebSearch",
        "Write",
        "mcp__code-graph__delete_project",
        "mcp__code-graph__generate_report",
        "mcp__code-graph__index_repository",
        "mcp__code-graph__ingest_traces",
        "mcp__code-graph__manage_adr",
        "mcp__code-search__cancel_indexing",
        "mcp__code-search__clear_index",
        "mcp__code-search__delete_project",
        "mcp__code-search__index_directory",
        "mcp__code-search__index_test_project",
        "mcp__code-search__switch_project",
    }
)
MEASURED_OUTCOME_ERROR_CLASSES = frozenset(
    {
        "invalid_json",
        "provider_exhausted",
        "timeout",
        "tool_budget",
    }
)
NONFINALIZABLE_ERROR_CLASSES = frozenset(
    {
        "authentication",
        "cost_cap",
        "index_identity_mismatch",
        "schema_mismatch",
    }
)
LIVE_RETRYABLE_ERROR_CLASSES = (
    "provider_overloaded",
    "rate_limited",
    "transport_interrupted",
)


def scoring_policy_descriptor() -> dict:
    return {
        "schema_version": 1,
        "primary_endpoint": "file_acc_at_10",
        "primary_contrasts": [
            "composed-corpus",
            "composed-native",
        ],
        "bootstrap_resamples": 10_000,
        "bootstrap_seed": 42,
        "bootstrap_unit": "case",
        "bootstrap_stream": "sha256_counter_modulo_v1",
        "bootstrap_stream_scope": "reinitialized_per_contrast",
        "contrast_estimand": (
            "mean_case_delta_after_all_replicates_success_v1"
        ),
        "repeat_aggregation_scope": "primary_contrasts_only",
        "descriptive_metrics_unit": "replicate",
        "mcnemar_unit": "case",
        "repeat_aggregation": "all_replicates_success_v1",
        "multiplicity_correction": "holm",
    }


class ContractError(ValueError):
    """A benchmark control, result, or pinned identity is inconsistent."""


def canonical_usd_decimal(
    value: object,
    field_name: str,
    *,
    positive: bool,
    serialized: bool = False,
) -> Decimal:
    """Validate and normalize one exact six-place USD boundary value."""
    if serialized:
        if not isinstance(value, str) or USD_DECIMAL.fullmatch(value) is None:
            raise ContractError(f"{field_name} is malformed")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ContractError(f"{field_name} is malformed") from exc
    else:
        parsed = value
    if (
        not isinstance(parsed, Decimal)
        or not parsed.is_finite()
        or parsed.as_tuple().exponent != -6
    ):
        qualifier = "positive" if positive else "non-negative"
        raise ContractError(
            f"{field_name} must be an exact six-place {qualifier} decimal"
        )
    if parsed <= 0 if positive else parsed < 0:
        if serialized:
            raise ContractError(f"{field_name} is outside its bound")
        qualifier = "positive" if positive else "non-negative"
        raise ContractError(
            f"{field_name} must be an exact six-place {qualifier} decimal"
        )
    return parsed.copy_abs() if parsed.is_zero() else parsed


def usd_micros(
    value: object,
    field_name: str,
    *,
    positive: bool,
    serialized: bool = False,
) -> int:
    """Convert one canonical six-place USD value to exact integer micros."""
    parsed = canonical_usd_decimal(
        value,
        field_name,
        positive=positive,
        serialized=serialized,
    )
    digits = 0
    for digit in parsed.as_tuple().digits:
        digits = digits * 10 + digit
    return -digits if parsed.is_signed() and digits else digits


def usd_decimal_from_micros(
    micros: object,
    field_name: str,
    *,
    positive: bool,
) -> Decimal:
    """Convert exact integer micros to a canonical six-place USD Decimal."""
    if isinstance(micros, bool) or not isinstance(micros, int):
        raise ContractError(f"{field_name} microdollars must be an integer")
    if micros <= 0 if positive else micros < 0:
        qualifier = "positive" if positive else "non-negative"
        raise ContractError(f"{field_name} must be {qualifier}")
    whole, fractional = divmod(micros, 1_000_000)
    return Decimal(f"{whole}.{fractional:06d}")


def format_usd_decimal(
    value: Decimal,
    field_name: str,
    *,
    positive: bool,
) -> str:
    return format(
        canonical_usd_decimal(
            value,
            field_name,
            positive=positive,
        ),
        "f",
    )


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fixture_canary_challenge(
    *,
    arm: str,
    query: str,
    repository_revision: str,
    canary_sha256: str,
) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "arm": arm,
                "query": query,
                "repository_revision": repository_revision,
                "canary_sha256": canary_sha256,
            }
        )
    )


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ContractError(f"cannot hash {path}: {exc}") from exc


HARNESS_SOURCE_FILES = (
    "bench/compare/build_corpus_pack.py",
    "bench/compare/build_fixture.py",
    "bench/compare/build_pin.py",
    "bench/compare/fixture_executor.py",
    "bench/compare/live_runtime.py",
    "bench/compare/provenance.py",
    "bench/compare/run.py",
    "bench/compare/schema.py",
    "bench/compare/score.py",
    "bench/compare/token_accounting.py",
)
PLUGIN_REPOSITORY = (
    "https://github.com/brandyn-s/codebase-search-plugin.git"
)


def _canonical_plugin_repository(value: str) -> str:
    """Normalize GitHub's equivalent checkout URLs to one bound identity."""
    if value in (PLUGIN_REPOSITORY, PLUGIN_REPOSITORY.removesuffix(".git")):
        return PLUGIN_REPOSITORY
    raise ContractError("benchmark Git repository identity is unexpected")


def _git_output(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ContractError(f"cannot inspect benchmark Git identity: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ContractError(f"cannot inspect benchmark Git identity: {detail}")
    return completed.stdout.strip()


def _require_complete_harness_source_inventory(root: Path) -> None:
    bound = set(HARNESS_SOURCE_FILES)
    tracked = set(
        filter(
            None,
            _git_output(
                root,
                "ls-files",
                "--",
                "bench/compare/*.py",
            ).splitlines(),
        )
    )
    compare_root = root / "bench" / "compare"
    on_disk = {
        path.relative_to(root).as_posix()
        for path in compare_root.glob("*.py")
    }
    initializer = compare_root / "__init__.py"
    if initializer.exists() or initializer.is_symlink():
        raise ContractError(
            "benchmark package initializer is forbidden before source verification"
        )
    if tracked != bound:
        raise ContractError(
            "tracked benchmark Python sources differ from the bound source inventory"
        )
    if on_disk != bound:
        raise ContractError(
            "on-disk benchmark Python sources differ from the bound source inventory"
        )
    for relative in sorted(bound):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ContractError(f"benchmark source is missing or unsafe: {relative}")
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError(f"cannot read benchmark source {relative}: {exc}") from exc
        if not source.startswith("#!/usr/bin/env python3"):
            continue
        guard = source.find("\n_reject_package_initializer()\n")
        package_import = source.find("\nfrom bench.compare")
        if guard < 0 or package_import <= guard:
            raise ContractError(
                f"benchmark entrypoint lacks a pre-import initializer guard: {relative}"
            )


def harness_source_identity(root: Path) -> dict:
    root = Path(root).resolve()
    _require_complete_harness_source_inventory(root)
    repository = _canonical_plugin_repository(
        _git_output(root, "remote", "get-url", "origin")
    )
    revision = _git_output(root, "rev-parse", "HEAD")
    tree = _git_output(root, "rev-parse", "HEAD^{tree}")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None or re.fullmatch(
        r"[0-9a-f]{40}", tree
    ) is None:
        raise ContractError("benchmark Git revision identity is malformed")
    files = {
        relative: sha256_file(root / relative) for relative in HARNESS_SOURCE_FILES
    }
    revision_files_match = True
    for relative, digest in files.items():
        try:
            committed = subprocess.run(
                ["git", "show", f"{revision}:{relative}"],
                cwd=root,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ContractError(
                f"cannot inspect committed benchmark source: {exc}"
            ) from exc
        if (
            committed.returncode != 0
            or sha256_bytes(committed.stdout) != digest
        ):
            revision_files_match = False
    return {
        "schema_version": 1,
        "plugin_repository": repository,
        "plugin_revision": revision,
        "plugin_tree": tree,
        "files": files,
        "files_sha256": sha256_bytes(canonical_json(files)),
        "revision_files_match": revision_files_match,
    }


def require_reproducible_harness_source(identity: dict) -> None:
    if identity.get("revision_files_match") is not True:
        raise ContractError(
            "benchmark harness sources differ from the pinned plugin revision"
        )


@dataclass(frozen=True)
class ArmContract:
    name: str
    allowed_tools: tuple[str, ...]
    read_only: bool = True
    network: bool = False

    def descriptor(self) -> dict:
        return {
            "name": self.name,
            "allowed_tools": list(self.allowed_tools),
            "read_only": self.read_only,
            "network": self.network,
        }


ARM_CONTRACTS: Mapping[str, ArmContract] = {
    "corpus": ArmContract("corpus", ()),
    "native": ArmContract("native", ("Glob", "Grep", "Read")),
    "code-search": ArmContract("code-search", ("Read", *SEARCH_TOOLS)),
    "code-graph": ArmContract("code-graph", ("Read", *GRAPH_TOOLS)),
    "composed": ArmContract(
        "composed",
        ("Read", *SEARCH_TOOLS, *GRAPH_TOOLS),
    ),
}


@dataclass(frozen=True)
class FrozenControls:
    provider: str
    model_id: str
    cli_version: str
    temperature: Decimal
    top_k: int = 10
    max_discovery_tool_calls: int = 20
    evidence_token_budget: int = 64_000
    context_token_budget: int = 128_000
    wall_timeout_seconds: int = 600
    permission_mode: str = "plan"
    fresh_session: bool = True
    memory: bool = False
    cost_policy: str = "fixture_zero_cost"
    max_total_usd: Decimal = Decimal(0)
    max_unit_usd: Decimal = Decimal(0)
    calibration_sha256: str | None = None
    retry_policy: str = "fixture_no_retry"
    max_attempts: int = 1
    retryable_error_classes: tuple[str, ...] = ()

    @classmethod
    def fixture(cls, **changes: object) -> FrozenControls:
        controls = cls(
            provider="fixture",
            model_id="claude-fixture-no-live-call",
            cli_version="fixture-0",
            temperature=Decimal(0),
        )
        return replace(controls, **changes)

    @classmethod
    def live(
        cls,
        *,
        provider: str,
        model_id: str,
        cli_version: str,
        max_total_usd: Decimal,
        max_unit_usd: Decimal,
        calibration_sha256: str,
    ) -> FrozenControls:
        max_total_usd = canonical_usd_decimal(
            max_total_usd,
            "max_total_usd",
            positive=True,
        )
        max_unit_usd = canonical_usd_decimal(
            max_unit_usd,
            "max_unit_usd",
            positive=True,
        )
        return cls(
            provider=provider,
            model_id=model_id,
            cli_version=cli_version,
            temperature=Decimal(0),
            cost_policy="authoritative_operation_bound",
            max_total_usd=max_total_usd,
            max_unit_usd=max_unit_usd,
            calibration_sha256=calibration_sha256,
            retry_policy="reconcile_before_transient_retry_v1",
            max_attempts=2,
            retryable_error_classes=LIVE_RETRYABLE_ERROR_CLASSES,
        )

    def descriptor(self, root: Path) -> dict:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.provider, self.model_id, self.cli_version)
        ):
            raise ContractError("provider, model ID, and CLI version are required")
        if self.temperature < 0:
            raise ContractError("temperature must be non-negative")
        expected_integers = {
            "top_k": (self.top_k, 10),
            "max_discovery_tool_calls": (self.max_discovery_tool_calls, 20),
            "evidence_token_budget": (self.evidence_token_budget, 64_000),
            "context_token_budget": (self.context_token_budget, 128_000),
            "wall_timeout_seconds": (self.wall_timeout_seconds, 600),
        }
        for name, (actual, expected) in expected_integers.items():
            if isinstance(actual, bool) or actual != expected:
                raise ContractError(f"{name} must remain frozen at {expected}")
        if (
            self.permission_mode != "plan"
            or not self.fresh_session
            or self.memory
        ):
            raise ContractError("benchmark must use plan mode, fresh sessions, no memory")
        if self.cost_policy == "fixture_zero_cost":
            valid_cost = (
                self.max_total_usd == 0
                and self.max_unit_usd == 0
                and self.calibration_sha256 is None
                and self.retry_policy == "fixture_no_retry"
                and self.max_attempts == 1
                and self.retryable_error_classes == ()
            )
        elif self.cost_policy == "authoritative_operation_bound":
            max_total_usd = format_usd_decimal(
                self.max_total_usd,
                "max_total_usd",
                positive=True,
            )
            max_unit_usd = format_usd_decimal(
                self.max_unit_usd,
                "max_unit_usd",
                positive=True,
            )
            valid_cost = (
                self.max_unit_usd <= self.max_total_usd
                and isinstance(self.calibration_sha256, str)
                and SHA256.fullmatch(self.calibration_sha256) is not None
                and self.retry_policy
                == "reconcile_before_transient_retry_v1"
                and self.max_attempts == 2
                and self.retryable_error_classes
                == LIVE_RETRYABLE_ERROR_CLASSES
            )
        else:
            valid_cost = False
        if not valid_cost:
            raise ContractError("cost and retry controls are not frozen")
        if self.cost_policy == "fixture_zero_cost":
            max_total_usd = format(self.max_total_usd, "f")
            max_unit_usd = format(self.max_unit_usd, "f")
        compare = root / "bench" / "compare"
        hashes = {
            "prompt": sha256_file(compare / "prompt.md"),
            "response_schema": sha256_file(compare / "response-schema.json"),
            "system": sha256_file(compare / "system.md"),
        }
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "cli_version": self.cli_version,
            "temperature": format(self.temperature, "f"),
            "top_k": self.top_k,
            "max_discovery_tool_calls": self.max_discovery_tool_calls,
            "repository_evidence": {
                "unit": "novel_tokens",
                "tokenizer": tokenizer_descriptor(),
                "budget": self.evidence_token_budget,
            },
            "context_token_budget": self.context_token_budget,
            "wall_timeout_seconds": self.wall_timeout_seconds,
            "permission_mode": self.permission_mode,
            "fresh_session": self.fresh_session,
            "memory": self.memory,
            "network_tool": False,
            "cost": {
                "policy": self.cost_policy,
                "max_total_usd": max_total_usd,
                "max_unit_usd": max_unit_usd,
                "calibration_sha256": self.calibration_sha256,
            },
            "retry": {
                "policy": self.retry_policy,
                "max_attempts": self.max_attempts,
                "retryable_error_classes": list(self.retryable_error_classes),
            },
            "hashes": hashes,
        }


def _load_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot load {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{path}: expected an object")
    return value


def _install_descriptor_sha256(value: object) -> str:
    if not isinstance(value, dict):
        raise ContractError("component install descriptor must be an object")
    return sha256_bytes(canonical_json(value))


def component_identity(root: Path) -> dict:
    root = Path(root).resolve()
    bom_path = root / "component-bom.json"
    bom = _load_object(bom_path)
    components = bom.get("components")
    if not isinstance(components, dict):
        raise ContractError("component BOM has no components object")
    identity: dict = {
        "schema_version": 1,
        "bom_sha256": sha256_file(bom_path),
    }
    for component, required_tools in (
        ("code-search", SEARCH_TOOLS),
        ("code-graph", GRAPH_TOOLS),
    ):
        descriptor = components.get(component)
        if not isinstance(descriptor, dict):
            raise ContractError(f"component BOM has no {component}")
        install = descriptor.get("install")
        install_sha256 = _install_descriptor_sha256(install)
        snapshot_path = root / "compatibility" / f"{component}-tools.json"
        snapshot = _load_object(snapshot_path)
        source = snapshot.get("source")
        tools = snapshot.get("tools")
        if (
            not isinstance(source, dict)
            or source.get("install_descriptor_sha256") != install_sha256
            or not isinstance(tools, dict)
        ):
            raise ContractError(f"{component} tool snapshot is not bound to the BOM")
        missing = sorted(
            tool.removeprefix(f"mcp__{component}__")
            for tool in required_tools
            if tool.removeprefix(f"mcp__{component}__") not in tools
        )
        if missing:
            raise ContractError(
                f"{component} snapshot is missing benchmark tools: {', '.join(missing)}"
            )
        assert isinstance(install, dict)
        identity[component] = {
            "version": install.get("tag") or install.get("revision"),
            "source_revision": install.get("source_revision") or install.get("revision"),
            "install_descriptor_sha256": install_sha256,
            "tool_snapshot_sha256": sha256_file(snapshot_path),
        }
    routing_files = (
        root / "skills" / "code-explore" / "SKILL.md",
        root / "skills" / "code-explore" / "references" / "graph-queries.md",
    )
    routing = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in routing_files
    }
    identity["routing_policy_files"] = routing
    identity["routing_policy_sha256"] = sha256_bytes(canonical_json(routing))
    return identity


def component_identity_sha256(identity: dict) -> str:
    return sha256_bytes(canonical_json(identity))


def validate_component_identity(expected: dict, actual: dict) -> None:
    if canonical_json(expected) != canonical_json(actual):
        raise ContractError("component identity mismatch; refusing fallback")


def build_unit_contract(
    *,
    case_id: str,
    query: str,
    repository_revision: str,
    arm: str,
    replicate: int,
    controls: FrozenControls,
    root: Path,
) -> dict:
    if not isinstance(case_id, str) or not case_id or "|" in case_id:
        raise ContractError("case ID must be a nonempty stable-key segment")
    if not isinstance(query, str) or not query.strip():
        raise ContractError("query must be nonempty")
    if REVISION.fullmatch(repository_revision) is None:
        raise ContractError("repository revision must be a full object ID")
    if arm not in ARM_CONTRACTS:
        raise ContractError(f"unknown arm {arm!r}")
    if isinstance(replicate, bool) or replicate < 1:
        raise ContractError("replicate must be a positive integer")
    descriptor = controls.descriptor(Path(root))
    frozen = {
        "case_id": case_id,
        "query_sha256": sha256_bytes(query.encode("utf-8")),
        "repository_revision": repository_revision,
        "controls": descriptor,
    }
    control_sha256 = sha256_bytes(canonical_json(frozen))
    identity = component_identity(Path(root))
    identity_sha256 = component_identity_sha256(identity)
    contract = ARM_CONTRACTS[arm]
    return {
        "schema_version": 1,
        "unit_key": f"{case_id}|r{replicate}|{arm}",
        "case_id": case_id,
        "arm": arm,
        "replicate": replicate,
        "control_sha256": control_sha256,
        "controls": descriptor,
        "arm_contract": contract.descriptor(),
        "arm_contract_sha256": sha256_bytes(canonical_json(contract.descriptor())),
        "component_identity_sha256": identity_sha256,
    }


def latin_square_units(
    case_ids: list[str],
    *,
    replicates: int,
) -> list[dict]:
    if len(set(case_ids)) != len(case_ids) or any(not case_id for case_id in case_ids):
        raise ContractError("case IDs must be unique and nonempty")
    if isinstance(replicates, bool) or replicates < 1:
        raise ContractError("replicates must be a positive integer")
    arms = tuple(ARM_CONTRACTS)
    units: list[dict] = []
    for case_index, case_id in enumerate(sorted(case_ids)):
        for replicate in range(1, replicates + 1):
            row = (case_index + replicate - 1) % len(arms)
            order = arms[row:] + arms[:row]
            units.extend(
                {
                    "case_id": case_id,
                    "replicate": replicate,
                    "position": position,
                    "arm": arm,
                    "latin_square_row": row,
                }
                for position, arm in enumerate(order, 1)
            )
    return units


def _nonnegative_integer(record: dict, field: str) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{field} must be a non-negative integer")
    return value


def validate_unit_binding(
    record: dict,
    *,
    unit_key: str,
    case_id: str,
    arm: str,
    replicate: int,
    position: int,
    arm_contract_sha256: str,
) -> None:
    expected = {
        "stable_key": unit_key,
        "unit_key": unit_key,
        "case_id": case_id,
        "arm": arm,
        "replicate": replicate,
        "position": position,
        "arm_contract_sha256": arm_contract_sha256,
    }
    if any(record.get(field) != value for field, value in expected.items()):
        raise ContractError(f"{unit_key}: outcome binding mismatch")


def validate_observation(
    observation: dict,
    *,
    expected_control_sha256: str,
    expected_component_identity_sha256: str,
) -> None:
    if observation.get("status") != "ok":
        raise ContractError("successful observation status must be ok")
    if observation.get("control_sha256") != expected_control_sha256:
        raise ContractError("observation control identity mismatch")
    if (
        observation.get("component_identity_sha256")
        != expected_component_identity_sha256
    ):
        raise ContractError("observation component identity mismatch")
    requested_k = _nonnegative_integer(observation, "requested_k")
    candidate_count = _nonnegative_integer(observation, "candidate_count")
    effective_k = _nonnegative_integer(observation, "effective_k")
    if requested_k != 10 or effective_k > requested_k or candidate_count < effective_k:
        raise ContractError("invalid requested K, effective K, or candidate count")
    ranked = observation.get("ranked_entities")
    if not isinstance(ranked, list) or len(ranked) != effective_k:
        raise ContractError("ranked entities must exactly match effective K")
    for rank, entity in enumerate(ranked, 1):
        if (
            not isinstance(entity, dict)
            or entity.get("rank") != rank
            or not isinstance(entity.get("file"), str)
            or not entity["file"]
            or not (
                entity.get("symbol") is None
                or isinstance(entity.get("symbol"), str)
            )
        ):
            raise ContractError("ranked entity schema or ordering is invalid")
    truncated = observation.get("truncated")
    if not isinstance(truncated, bool) or truncated != (candidate_count > effective_k):
        raise ContractError("truncation metadata does not match candidate depth")
    if _nonnegative_integer(observation, "tool_calls") > 20:
        raise ContractError("tool call limit exceeded")
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_tokens",
        "tool_result_tokens",
        "evidence_tokens",
        "evidence_bytes",
        "context_tokens",
        "egress_bytes",
        "latency_ms",
    ):
        _nonnegative_integer(observation, field)
    if observation["evidence_tokens"] > 64_000:
        raise ContractError("repository evidence exceeds the 64000-token budget")
    if observation["context_tokens"] > 128_000:
        raise ContractError("context exceeds the 128000-token ceiling")
    cost_value = observation.get("cost_usd")
    if not isinstance(cost_value, str):
        raise ContractError("cost_usd must be an exact decimal string")
    try:
        cost = Decimal(cost_value)
    except (InvalidOperation, TypeError) as exc:
        raise ContractError("cost_usd must be an exact decimal string") from exc
    if cost < 0 or not cost.is_finite():
        raise ContractError("cost_usd must be finite and non-negative")
