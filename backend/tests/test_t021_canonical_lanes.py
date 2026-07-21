from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.canonical_lanes import (
    CANONICAL_LANE_IDS,
    LANE_REGISTRY,
    MUTATION_AUTOMATIC_APPEND_ONLY,
    MUTATION_NAMED_GRANT_RELOCK,
    AmbiguousLaneAliasError,
    DuplicateCanonicalLaneError,
    DuplicateLaneAliasError,
    DuplicateSectorWriteError,
    MutuallyExclusiveCodeLaneError,
    UnknownLaneAliasError,
    canonical_sector_path,
    get_lane,
    plan_sector_writes,
    registry_payload,
    resolve_lane_id,
    validate_registry,
)
from sqlite_brain_builder.runtime.env15_project_schema import ENV15_LANE_TO_SECTOR
from sqlite_brain_builder.runtime.universal_lane_registry import LANE_REGISTRY as COMPATIBILITY_REGISTRY
from sqlite_brain_builder.runtime.stable_runtime_v53 import LANE_DEFS


EXPECTED_LANE_IDS = (
    "github_code",
    "local_code",
    "chat_lineage",
    "discussion",
    "analysis",
    "plan",
    "mode",
    "docs",
    "data_excel",
    "ppt",
    "pdf_ocr",
    "images_ocr",
    "artifacts",
    "custom",
    "brain_loader",
    "research",
    "project_engulf",
    "sqlite_brain",
)


def test_registry_contains_exactly_the_18_t021_canonical_ids_in_order() -> None:
    assert CANONICAL_LANE_IDS == EXPECTED_LANE_IDS
    assert tuple(LANE_REGISTRY) == EXPECTED_LANE_IDS
    assert len(LANE_REGISTRY) == 18
    assert tuple(COMPATIBILITY_REGISTRY) == EXPECTED_LANE_IDS


def test_every_lane_has_complete_contract_and_canonical_sector_target() -> None:
    target_paths: set[tuple[str, str]] = set()
    mmd_nodes: set[str] = set()

    for lane_id, lane in LANE_REGISTRY.items():
        assert lane.canonical_lane_id == lane_id
        assert lane.display_label
        assert lane.frontend_aliases
        assert lane.backend_aliases
        assert lane.sector_folder == lane_id
        assert lane.sqlite_filename == f"{lane_id}_sector_v001.sqlite"
        assert lane.schema_contract
        assert lane.fts_table in lane.schema_contract
        assert lane.source_types
        assert lane.extensions
        assert lane.parser_id
        assert lane.chunker_version
        assert lane.mmd_node_id
        assert lane.mutation_policy

        target = (lane.sector_folder, lane.sqlite_filename)
        assert target not in target_paths
        target_paths.add(target)
        assert lane.mmd_node_id not in mmd_nodes
        mmd_nodes.add(lane.mmd_node_id)


def test_runtime_payload_exposes_the_complete_contract_for_all_18_lanes() -> None:
    required = {
        "canonical_lane_id", "label", "frontend_aliases", "backend_aliases",
        "sector_folder", "sqlite_filename", "schema", "source_types",
        "extensions", "parser_id", "chunker_version", "fts_table",
        "mmd_node_id", "mutation_policy",
    }
    assert tuple(LANE_DEFS) == EXPECTED_LANE_IDS
    for lane_id, payload in LANE_DEFS.items():
        assert required <= payload.keys()
        assert payload["canonical_lane_id"] == lane_id
        assert payload["sector_folder"] == lane_id
        assert payload["sqlite_filename"] == f"{lane_id}_sector_v001.sqlite"
        assert payload["fts_table"] in payload["schema"]


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("github", "github_code"),
        ("GitHub Code", "github_code"),
        ("local_code", "local_code"),
        ("Chat Lineage", "chat_lineage"),
        ("data", "data_excel"),
        ("data_excel_csv", "data_excel"),
        ("PPT / Presentation", "ppt"),
        ("ppt_presentation", "ppt"),
        ("images", "images_ocr"),
        ("brain_loader", "brain_loader"),
        ("Project Engulf", "project_engulf"),
        ("SQLite Brain", "sqlite_brain"),
    ],
)
def test_legacy_and_display_aliases_resolve_to_one_canonical_id(alias: str, expected: str) -> None:
    assert resolve_lane_id(alias) == expected
    assert get_lane(alias).canonical_lane_id == expected


def test_alias_scope_is_strict_for_layer_specific_aliases() -> None:
    assert resolve_lane_id("Code Folder", scope="frontend") == "local_code"
    with pytest.raises(UnknownLaneAliasError):
        resolve_lane_id("Code Folder", scope="backend")

    assert resolve_lane_id("github_repo", scope="backend") == "github_code"
    with pytest.raises(UnknownLaneAliasError):
        resolve_lane_id("github_repo", scope="frontend")


@pytest.mark.parametrize("alias", ["", "not-a-real-lane", "data warehouse", "customish"])
def test_unknown_aliases_fail_closed_without_custom_fallback(alias: str) -> None:
    with pytest.raises(UnknownLaneAliasError):
        resolve_lane_id(alias)


def test_registry_validation_rejects_duplicate_canonical_ids() -> None:
    definitions = list(LANE_REGISTRY.values())
    with pytest.raises(DuplicateCanonicalLaneError):
        validate_registry(definitions + [definitions[0]])


def test_registry_validation_rejects_duplicate_aliases_inside_one_alias_list() -> None:
    lane = replace(
        LANE_REGISTRY["research"],
        frontend_aliases=("Research Source", "research-source"),
    )
    with pytest.raises(DuplicateLaneAliasError):
        validate_registry([lane])


def test_registry_validation_rejects_aliases_owned_by_two_lanes() -> None:
    definitions = list(LANE_REGISTRY.values())
    definitions[1] = replace(
        definitions[1],
        frontend_aliases=definitions[1].frontend_aliases + ("GitHub",),
    )
    with pytest.raises(AmbiguousLaneAliasError):
        validate_registry(definitions)


def test_no_double_sector_write_when_two_aliases_name_the_same_lane() -> None:
    with pytest.raises(DuplicateSectorWriteError):
        plan_sector_writes(["data", "data_excel_csv"])

    with pytest.raises(DuplicateSectorWriteError):
        plan_sector_writes(["Brain Loader", "brain_loader"])


def test_all_18_canonical_lanes_plan_18_unique_sector_writes() -> None:
    with pytest.raises(MutuallyExclusiveCodeLaneError):
        plan_sector_writes(CANONICAL_LANE_IDS)

    for excluded_mode in ("github_code", "local_code"):
        permitted = tuple(lane for lane in CANONICAL_LANE_IDS if lane != excluded_mode)
        targets = plan_sector_writes(permitted)
        assert tuple(target.canonical_lane_id for target in targets) == permitted
        assert len({target.relative_path for target in targets}) == 17


def test_canonical_sector_path_never_uses_the_incoming_alias() -> None:
    project_root = Path("brain") / "project"
    assert canonical_sector_path(project_root, "data_excel_csv") == (
        project_root / "sectors" / "data_excel" / "data_excel_sector_v001.sqlite"
    )
    assert canonical_sector_path(project_root, "github") == (
        project_root / "sectors" / "github_code" / "github_code_sector_v001.sqlite"
    )


def test_all_logical_lanes_resolve_to_an_explicit_env15_physical_sector() -> None:
    assert set(ENV15_LANE_TO_SECTOR) == set(EXPECTED_LANE_IDS)
    assert ENV15_LANE_TO_SECTOR["discussion"] == "custom"
    assert ENV15_LANE_TO_SECTOR["data_excel"] == "data_excel"


def test_brain_loader_is_a_real_lane_and_never_aliases_to_custom() -> None:
    assert resolve_lane_id("Brain Loader") == "brain_loader"
    assert get_lane("Brain Loader") is LANE_REGISTRY["brain_loader"]
    assert get_lane("Brain Loader") is not LANE_REGISTRY["custom"]


def test_env15_mutation_policy_is_append_only_only_for_chat_lineage() -> None:
    assert LANE_REGISTRY["chat_lineage"].mutation_policy == MUTATION_AUTOMATIC_APPEND_ONLY
    assert all(
        lane.mutation_policy == MUTATION_NAMED_GRANT_RELOCK
        for lane_id, lane in LANE_REGISTRY.items()
        if lane_id != "chat_lineage"
    )


def test_registry_payload_is_json_safe_and_cannot_mutate_the_registry() -> None:
    payload = registry_payload()
    assert tuple(payload) == CANONICAL_LANE_IDS
    assert payload["docs"]["canonical_lane_id"] == "docs"
    assert isinstance(payload["docs"]["schema_contract"], list)

    payload["docs"]["frontend_aliases"].append("mutated")
    assert "mutated" not in LANE_REGISTRY["docs"].frontend_aliases
