"""Compatibility view of the one authoritative T021 canonical lane registry.

Legacy GUI/runtime modules historically imported a mutable dictionary from
this module.  Keep their presentation helpers, but derive every identity,
schema and sector target from :mod:`canonical_lanes`; no second registry lives
here.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from sqlite_brain_builder.runtime.canonical_lanes import (
    CANONICAL_LANE_IDS,
    LANE_REGISTRY as CANONICAL_LANE_REGISTRY,
    canonical_sector_path,
    plan_sector_writes,
    registry_payload,
    resolve_lane_id,
)


_TABS = {
    "github_code": "GitHub",
    "local_code": "Local Code",
    "chat_lineage": "Chat Lineage",
    "discussion": "Discussion",
    "analysis": "Analysis",
    "plan": "Plan",
    "mode": "Mode",
    "docs": "Docs",
    "data_excel": "Data",
    "ppt": "PPT",
    "pdf_ocr": "PDF/OCR",
    "images_ocr": "Images/OCR",
    "artifacts": "Artifacts",
    "custom": "Custom",
    "brain_loader": "Brain Loader",
    "research": "Research",
    "project_engulf": "Project Engulf",
    "sqlite_brain": "SQLite Brain",
}


def _filetypes(label: str, extensions: tuple[str, ...]) -> list[tuple[str, str]]:
    return [(f"{label} files", " ".join(f"*{extension}" for extension in extensions))]


def _legacy_view(lane_id: str) -> dict[str, Any]:
    lane = CANONICAL_LANE_REGISTRY[lane_id]
    return {
        **lane.as_dict(),
        "key": lane_id,
        "label": lane.display_label,
        "tab": _TABS[lane_id],
        "schema": list(lane.schema_contract),
        "schema_tables": list(lane.schema_contract),
        "filetypes": _filetypes(lane.display_label, lane.extensions),
        "mmd_required": lane_id in {"github_code", "local_code"},
    }


LANE_REGISTRY = MappingProxyType({lane_id: _legacy_view(lane_id) for lane_id in CANONICAL_LANE_IDS})
TAB_ORDER = [*(_TABS[lane_id] for lane_id in CANONICAL_LANE_IDS), "Packages", "Receipts"]
SIDEBAR_LANES = [(_TABS[lane_id], lane_id) for lane_id in CANONICAL_LANE_IDS]


def get_lane(alias: str) -> dict[str, Any]:
    return dict(LANE_REGISTRY[resolve_lane_id(alias, scope="any")])


def lane_key_from_label(label: str) -> str:
    return resolve_lane_id(label, scope="frontend")


def key_from_tab(tab: str) -> str:
    return resolve_lane_id(tab, scope="frontend")


def lane_labels() -> list[str]:
    return [_TABS[lane_id] for lane_id in CANONICAL_LANE_IDS]


__all__ = [
    "CANONICAL_LANE_IDS",
    "CANONICAL_LANE_REGISTRY",
    "LANE_REGISTRY",
    "SIDEBAR_LANES",
    "TAB_ORDER",
    "canonical_sector_path",
    "get_lane",
    "key_from_tab",
    "lane_key_from_label",
    "lane_labels",
    "plan_sector_writes",
    "registry_payload",
    "resolve_lane_id",
]
