"""Canonical bindings for component installation descriptors."""

from __future__ import annotations

import hashlib
import json
import re


class DescriptorError(ValueError):
    """A component installation descriptor cannot be canonicalized."""


# A BOM in this promotion state points at component releases that have not
# been published yet. Every artifact digest is the literal PENDING_DIGEST,
# installers refuse to run, and validators report "pending" instead of "ready".
# A promotion run replaces the digests with real SHA-256 values, adds the
# attestation bundle, regenerates the tool snapshots and readiness evidence,
# and removes promotion_state.
PENDING_FIRST_RELEASE = "pending-first-release"
PENDING_DIGEST = "pending"
LOWER_HEX_SHA256 = re.compile(r"[0-9a-f]{64}")


def promotion_state(bom: object) -> str | None:
    """Return the BOM's promotion_state, or None for a released BOM."""
    if not isinstance(bom, dict):
        return None
    state = bom.get("promotion_state")
    return state if isinstance(state, str) and state else None


def is_pending_first_release(bom: object) -> bool:
    return promotion_state(bom) == PENDING_FIRST_RELEASE


def digest_is_pinned(value: object, *, allow_pending: bool) -> bool:
    """True for a lowercase SHA-256, or the pending placeholder when allowed."""
    if not isinstance(value, str):
        return False
    if allow_pending and value == PENDING_DIGEST:
        return True
    return LOWER_HEX_SHA256.fullmatch(value) is not None


GRAPH_ASSET_KEYS = frozenset(
    {
        "darwin-amd64",
        "darwin-arm64",
        "linux-amd64",
        "linux-arm64",
        "windows-amd64",
    }
)


def _require_exact_keys(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict:
    if not isinstance(value, dict):
        raise DescriptorError(f"{label} must be a JSON object")
    actual = set(value)
    if actual != expected:
        raise DescriptorError(
            f"{label} keys must exactly match "
            + ", ".join(sorted(expected))
        )
    return value


def validate_install_descriptor_shape(component: str, install: object) -> None:
    """Reject install descriptors whose full object shape is not understood."""
    if not isinstance(install, dict):
        raise DescriptorError(f"{component} install descriptor must be a JSON object")
    kind = install.get("kind")
    if component == "code-search" and kind == "git":
        _require_exact_keys(
            install,
            frozenset({"kind", "repository", "revision"}),
            "code-search git install descriptor",
        )
        return
    if component == "code-search" and kind == "github-release":
        _require_exact_keys(
            install,
            frozenset(
                {
                    "kind",
                    "repository",
                    "tag",
                    "source_revision",
                    "asset",
                    "attestation",
                    "checksums",
                }
            ),
            "code-search release install descriptor",
        )
        _require_exact_keys(
            install["asset"],
            frozenset({"name", "sha256"}),
            "code-search release asset",
        )
        attestation = _require_exact_keys(
            install["attestation"],
            frozenset(
                {
                    "bundle",
                    "signer_workflow",
                    "source_ref",
                    "deny_self_hosted_runners",
                }
            ),
            "code-search attestation",
        )
        _require_exact_keys(
            attestation["bundle"],
            frozenset({"name", "sha256"}),
            "code-search attestation bundle",
        )
        _require_exact_keys(
            install["checksums"],
            frozenset({"name", "sha256"}),
            "code-search checksums",
        )
        return
    if component == "code-graph" and kind == "github-release":
        _require_exact_keys(
            install,
            frozenset(
                {
                    "kind",
                    "repository",
                    "tag",
                    "source_revision",
                    "assets",
                    "attestation",
                    "checksums",
                }
            ),
            "code-graph release install descriptor",
        )
        assets = _require_exact_keys(
            install["assets"],
            GRAPH_ASSET_KEYS,
            "code-graph release assets",
        )
        for platform, asset in assets.items():
            _require_exact_keys(
                asset,
                frozenset({"name", "sha256"}),
                f"code-graph {platform} release asset",
            )
        _require_exact_keys(
            install["attestation"],
            frozenset(
                {
                    "bundle",
                    "signer_workflow",
                    "source_ref",
                    "deny_self_hosted_runners",
                }
            ),
            "code-graph attestation",
        )
        _require_exact_keys(
            install["attestation"]["bundle"],
            frozenset({"path", "sha256"}),
            "code-graph attestation bundle",
        )
        _require_exact_keys(
            install["checksums"],
            frozenset({"name", "sha256"}),
            "code-graph checksums",
        )
        return
    raise DescriptorError(
        f"unsupported install descriptor component/kind: {component}/{kind}"
    )


def canonical_install_descriptor(install: object) -> bytes:
    """Return the exact canonical JSON bytes bound by compatibility evidence."""
    if not isinstance(install, dict):
        raise DescriptorError("install descriptor must be a JSON object")
    try:
        rendered = json.dumps(
            install,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DescriptorError(f"install descriptor is not valid JSON: {exc}") from exc
    return rendered.encode("utf-8")


def install_descriptor_sha256(install: object) -> str:
    """Hash every install field, including artifact and provenance policy."""
    return hashlib.sha256(canonical_install_descriptor(install)).hexdigest()
