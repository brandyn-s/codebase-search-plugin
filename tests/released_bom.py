"""BOM fixtures for the contract tests.

``released_bom``/``write_released_checkout`` produce a released-state BOM with
deterministic fake digests so strict artifact tests do not depend on the live
pins; ``pending_bom`` produces the pre-promotion ``pending-first-release`` shape
that the installers, capture, and validators must refuse.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from component_descriptor import (  # noqa: E402
    PENDING_DIGEST,
    PENDING_FIRST_RELEASE,
    install_descriptor_sha256,
)

BLOCKED_REASON = (
    "Fixture: release digests are recorded; runtime behaviour has not been "
    "re-observed, so integrated readiness stays blocked."
)


def fake_digest(*labels: str) -> str:
    """Deterministic 64-hex digest derived from a label path."""
    return hashlib.sha256("/".join(labels).encode("utf-8")).hexdigest()


def _fill_pending(value, path: tuple[str, ...]):
    if isinstance(value, dict):
        return {
            key: (
                fake_digest(*path, key)
                if key == "sha256" and item == PENDING_DIGEST
                else _fill_pending(item, path + (key,))
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_fill_pending(item, path + (str(index),)) for index, item in enumerate(value)]
    return value


def released_bom(bom: dict, *, readiness_status: str = "blocked") -> dict:
    """Return a copy of ``bom`` as it would look after a promotion run.

    ``promotion_state``/``promotion_reason`` are removed, every ``pending``
    digest becomes a deterministic fake SHA-256, and integrated readiness is
    ``blocked`` (no evidence) unless a caller asks for another status.
    """
    released = deepcopy(bom)
    released.pop("promotion_state", None)
    released.pop("promotion_reason", None)
    released["components"] = _fill_pending(released["components"], ("components",))
    readiness = released["integrated_readiness"]
    released["integrated_readiness"] = {
        "reason": BLOCKED_REASON,
        "requires": readiness["requires"],
        "status": readiness_status,
    }
    return released


def rebind_snapshots(checkout: Path, bom: dict) -> None:
    """Point both tool snapshots at the BOM's current install descriptors."""
    for component in ("code-search", "code-graph"):
        snapshot_path = checkout / "compatibility" / f"{component}-tools.json"
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        install = bom["components"][component]["install"]
        snapshot["source"]["install_descriptor_sha256"] = install_descriptor_sha256(install)
        snapshot["source"]["kind"] = install["kind"]
        snapshot["source"]["version"] = (
            install["revision"] if install["kind"] == "git" else install["tag"]
        )
        snapshot_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def write_released_checkout(checkout: Path, *, readiness_status: str = "blocked") -> dict:
    """Rewrite ``checkout`` (a copy of the repo) into a released-state fixture.

    Writes the released BOM, a fake vendored code-graph attestation bundle
    whose digest matches the BOM, and rebinds both tool snapshots. Returns the
    BOM that was written.
    """
    bom_path = checkout / "component-bom.json"
    bom = released_bom(
        json.loads(bom_path.read_text(encoding="utf-8")),
        readiness_status=readiness_status,
    )
    graph_install = bom["components"]["code-graph"]["install"]
    bundle = graph_install["attestation"]["bundle"]
    bundle_content = f'{{"fixture_bundle_for":"{graph_install["tag"]}"}}\n'.encode("utf-8")
    bundle["sha256"] = hashlib.sha256(bundle_content).hexdigest()
    bundle_path = checkout / bundle["path"]
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(bundle_content)
    bom_path.write_text(json.dumps(bom, indent=2) + "\n", encoding="utf-8")
    rebind_snapshots(checkout, bom)
    return bom


PENDING_REASON = (
    "Fixture: the pinned component releases are not published yet; digests are "
    "placeholders until a promotion run records the real artifacts."
)


def pending_bom(bom: dict) -> dict:
    """Return a copy of ``bom`` as it looked before the first promotion run.

    ``promotion_state``/``promotion_reason`` are set, every digest becomes the
    ``pending`` placeholder, readiness is ``pending`` and carries no evidence.
    """
    def _blank(value):
        if isinstance(value, dict):
            return {
                key: (PENDING_DIGEST if key == "sha256" else _blank(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [_blank(item) for item in value]
        return value

    pending = deepcopy(bom)
    pending["promotion_state"] = PENDING_FIRST_RELEASE
    pending["promotion_reason"] = PENDING_REASON
    pending["components"] = _blank(pending["components"])
    readiness = pending["integrated_readiness"]
    pending["integrated_readiness"] = {
        "reason": PENDING_REASON,
        "requires": readiness["requires"],
        "status": "pending",
    }
    return pending

