"""Canonical T021 lane identities and sector-write routing.

This module is intentionally independent of the legacy ``LANE_DEFS`` mapping.
It gives backend and frontend integration code one immutable, validated source
of truth for the eighteen T021 lanes.  Callers must resolve external names
before computing a sector target; duplicate aliases for the same lane are
rejected by :func:`plan_sector_writes` so one intake operation cannot write the
same source into two sectors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, Sequence


AliasScope = Literal["any", "frontend", "backend"]

MUTATION_AUTOMATIC_APPEND_ONLY = "automatic_append_only"
MUTATION_NAMED_GRANT_RELOCK = "explicit_named_one_turn_grant_receipt_snapshot_relock"
PRIMARY_CODE_LANES = frozenset({"github_code", "local_code"})

CANONICAL_LANE_IDS = (
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


class LaneRegistryError(ValueError):
    """Base class for canonical-lane validation and resolution failures."""


class InvalidLaneDefinitionError(LaneRegistryError):
    """A lane is missing a required field or contains an invalid value."""


class DuplicateCanonicalLaneError(LaneRegistryError):
    """Two definitions declare the same canonical lane ID."""


class DuplicateLaneAliasError(LaneRegistryError):
    """One explicit alias list contains the same normalized alias twice."""


class AmbiguousLaneAliasError(LaneRegistryError):
    """One normalized alias resolves to more than one canonical lane."""


class UnknownLaneAliasError(LaneRegistryError):
    """An alias is not registered for the requested layer."""


class DuplicateSectorTargetError(LaneRegistryError):
    """Two canonical definitions point at the same sector folder or file."""


class DuplicateSectorWriteError(LaneRegistryError):
    """One operation requested the same canonical sector more than once."""


class MutuallyExclusiveCodeLaneError(LaneRegistryError):
    """One build attempted to activate both primary code-intake modes."""


@dataclass(frozen=True, slots=True)
class LaneDefinition:
    """One immutable canonical lane contract."""

    canonical_lane_id: str
    display_label: str
    frontend_aliases: tuple[str, ...]
    backend_aliases: tuple[str, ...]
    sector_folder: str
    sqlite_filename: str
    schema_contract: tuple[str, ...]
    source_types: tuple[str, ...]
    extensions: tuple[str, ...]
    parser_id: str
    chunker_version: str
    fts_table: str
    mmd_node_id: str
    mutation_policy: str

    @property
    def sector_relative_path(self) -> Path:
        """Return the canonical path below ``project/sectors``."""

        return Path(self.sector_folder) / self.sqlite_filename

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe payload without exposing mutable registry state."""

        return {
            "canonical_lane_id": self.canonical_lane_id,
            "display_label": self.display_label,
            "frontend_aliases": list(self.frontend_aliases),
            "backend_aliases": list(self.backend_aliases),
            "sector_folder": self.sector_folder,
            "sqlite_filename": self.sqlite_filename,
            "schema_contract": list(self.schema_contract),
            "source_types": list(self.source_types),
            "extensions": list(self.extensions),
            "parser_id": self.parser_id,
            "chunker_version": self.chunker_version,
            "fts_table": self.fts_table,
            "mmd_node_id": self.mmd_node_id,
            "mutation_policy": self.mutation_policy,
        }


@dataclass(frozen=True, slots=True)
class SectorWriteTarget:
    """A canonical, unique sector destination for one intake operation."""

    canonical_lane_id: str
    sector_folder: str
    sqlite_filename: str

    @property
    def relative_path(self) -> Path:
        return Path(self.sector_folder) / self.sqlite_filename


CODE_SCHEMA_CONTRACT = (
    "sector_meta",
    "sector_head",
    "source_registry",
    "artifact_registry",
    "chunk_index",
    "relation_edge",
    "mutation_receipt",
    "code_source_registry",
    "code_file_snapshot",
    "code_chunk",
    "code_symbol",
    "code_import",
    "code_route_api_boundary",
    "code_config_build_test_chunk",
    "code_index_checkpoint",
    "code_source_active_head",
    "code_workflow_edge",
    "code_semantic_diff",
    "code_synthetic_snapshot_file",
    "code_snapshot_history",
    "code_good_snapshot",
    "snapshot_git_bridge",
    "git_commit_registry",
    "git_commit_parent",
    "git_file_change",
    "git_patch_hunk",
    "git_exact_line_change",
    "git_ref_registry",
    "git_push_event",
    "git_route_impact",
    "git_symbol_impact",
    "git_dependency_impact",
    "git_test_impact",
    "git_artifact_impact",
    "code_chunk_fts",
)

CODE_EXTENSIONS = (
    ".bat",
    ".c",
    ".cfg",
    ".cjs",
    ".cmd",
    ".css",
    ".htm",
    ".html",
    ".ini",
    ".js",
    ".jsx",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".scss",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
)


def _lane(
    canonical_lane_id: str,
    display_label: str,
    *,
    frontend_aliases: Sequence[str],
    backend_aliases: Sequence[str],
    schema_contract: Sequence[str],
    source_types: Sequence[str],
    extensions: Sequence[str],
    parser_id: str,
    chunker_version: str,
    fts_table: str,
    mutation_policy: str = MUTATION_NAMED_GRANT_RELOCK,
) -> LaneDefinition:
    """Construct a lane using the canonical sector naming law."""

    return LaneDefinition(
        canonical_lane_id=canonical_lane_id,
        display_label=display_label,
        frontend_aliases=tuple(frontend_aliases),
        backend_aliases=tuple(backend_aliases),
        sector_folder=canonical_lane_id,
        sqlite_filename=f"{canonical_lane_id}_sector_v001.sqlite",
        schema_contract=tuple(schema_contract),
        source_types=tuple(source_types),
        extensions=tuple(extensions),
        parser_id=parser_id,
        chunker_version=chunker_version,
        fts_table=fts_table,
        mmd_node_id=f"lane_{canonical_lane_id}",
        mutation_policy=mutation_policy,
    )


_LANE_DEFINITIONS = (
    _lane(
        "github_code",
        "GitHub",
        frontend_aliases=("GitHub", "GitHub Code"),
        backend_aliases=("github", "github_repo"),
        schema_contract=CODE_SCHEMA_CONTRACT,
        source_types=("github_repository", "git_remote"),
        extensions=CODE_EXTENSIONS,
        parser_id="github_code_reverse_history_v2",
        chunker_version="tiered_git_history_v2_index_once",
        fts_table="code_chunk_fts",
    ),
    _lane(
        "local_code",
        "Local Code",
        frontend_aliases=("Local Code", "Code Folder"),
        backend_aliases=("local", "local_code_folder"),
        schema_contract=CODE_SCHEMA_CONTRACT,
        source_types=("local_code_folder", "local_git_worktree"),
        extensions=CODE_EXTENSIONS,
        parser_id="local_code_snapshot_v2",
        chunker_version="tiered_git_history_v2_index_once",
        fts_table="code_chunk_fts",
    ),
    _lane(
        "chat_lineage",
        "Chat Lineage",
        frontend_aliases=("Chat Lineage", "Chat"),
        backend_aliases=("chat_history", "conversation_lineage"),
        schema_contract=(
            "writeback_policy",
            "turn_prepare",
            "prompt_raw_exact",
            "prompt_normalized_summary",
            "response_raw_visible_exact",
            "response_summary",
            "visible_reasoning_summary",
            "file_link_registry",
            "source_normalization_receipt",
            "mode_classification_run",
            "gate_evaluation_run",
            "operator_activation_run",
            "entry_exit_receipt",
            "state_hash_chain",
            "turn_commit",
            "lineage_head",
            "legacy_project_carry_forward",
            "turn_fts",
        ),
        source_types=("chat_export", "prompt_response_packet", "lineage_append_packet"),
        extensions=(".docx", ".json", ".jsonl", ".md", ".txt", ".zip"),
        parser_id="chat_lineage_state_travel_v57",
        chunker_version="prepare_response_commit_v1",
        fts_table="turn_fts",
        mutation_policy=MUTATION_AUTOMATIC_APPEND_ONLY,
    ),
    _lane(
        "discussion",
        "Discussion",
        frontend_aliases=("Discussion",),
        backend_aliases=("discussion_notes",),
        schema_contract=(
            "discussion_source",
            "discussion_turn",
            "discussion_item",
            "discussion_decision",
            "discussion_delta",
            "discussion_hard_gate",
            "discussion_artifact_reference",
            "discussion_next_action",
            "discussion_fts",
        ),
        source_types=("discussion_document", "meeting_notes", "conversation_export"),
        extensions=(".docx", ".md", ".pdf", ".txt"),
        parser_id="discussion_structured_v1",
        chunker_version="semantic_blocks_v1",
        fts_table="discussion_fts",
    ),
    _lane(
        "analysis",
        "Analysis",
        frontend_aliases=("Analysis",),
        backend_aliases=("analytical_notes",),
        schema_contract=(
            "analysis_source",
            "analysis_claim",
            "analysis_supporting_evidence",
            "analysis_risk",
            "analysis_alternative",
            "analysis_open_question",
            "analysis_accepted_decision",
            "analysis_blocked_item",
            "analysis_fts",
        ),
        source_types=("analysis_document", "audit_report", "decision_analysis"),
        extensions=(".docx", ".md", ".pdf", ".txt"),
        parser_id="analysis_structured_v1",
        chunker_version="semantic_blocks_v1",
        fts_table="analysis_fts",
    ),
    _lane(
        "plan",
        "Plan",
        frontend_aliases=("Plan", "Planning"),
        backend_aliases=("project_plan",),
        schema_contract=(
            "plan_source",
            "plan_phase",
            "plan_milestone",
            "plan_task",
            "plan_dependency",
            "plan_owner",
            "plan_status",
            "plan_acceptance_criteria",
            "plan_blocker",
            "plan_next_action",
            "plan_fts",
        ),
        source_types=("project_plan", "implementation_plan", "task_plan"),
        extensions=(".docx", ".json", ".md", ".pdf", ".txt"),
        parser_id="plan_structured_v1",
        chunker_version="semantic_blocks_v1",
        fts_table="plan_fts",
    ),
    _lane(
        "mode",
        "Mode",
        frontend_aliases=("Mode", "Operating Mode"),
        backend_aliases=("mode_contract",),
        schema_contract=(
            "mode_source",
            "mode_rule",
            "mode_scope",
            "mode_gate",
            "mode_allowed_action",
            "mode_blocked_action",
            "mode_trigger",
            "mode_response_template",
            "mode_priority",
            "mode_supersede_ledger",
            "mode_fts",
        ),
        source_types=("mode_contract", "operating_rules", "control_prompt"),
        extensions=(".docx", ".json", ".md", ".txt"),
        parser_id="mode_contract_v1",
        chunker_version="rule_blocks_v1",
        fts_table="mode_fts",
    ),
    _lane(
        "docs",
        "Docs",
        frontend_aliases=("Docs", "Documents"),
        backend_aliases=("document", "documentation"),
        schema_contract=(
            "doc_file",
            "doc_structure",
            "doc_heading",
            "doc_paragraph",
            "doc_chunk",
            "doc_table_extract",
            "doc_image_reference",
            "source_structure_signature",
            "doc_fts",
        ),
        source_types=("document", "markdown", "html_document", "xml_document"),
        extensions=(".doc", ".docx", ".html", ".md", ".odt", ".rst", ".rtf", ".txt", ".xml"),
        parser_id="document_structure_v1",
        chunker_version="document_hierarchy_v1",
        fts_table="doc_fts",
    ),
    _lane(
        "data_excel",
        "Data / Excel / CSV",
        frontend_aliases=("Data", "Data / Excel / CSV", "Excel / CSV"),
        backend_aliases=("data_excel_csv", "excel_csv", "spreadsheet_data"),
        schema_contract=(
            "data_source",
            "sheet_workbook",
            "sheet_tab",
            "sheet_range",
            "sheet_table",
            "sheet_formula",
            "sheet_formula_dependency_edge",
            "sheet_cell_sample",
            "sheet_chart_metadata",
            "csv_header",
            "csv_row_sample",
            "data_chunk",
            "data_structure_signature",
            "data_fts",
        ),
        source_types=("spreadsheet", "delimited_data", "structured_data"),
        extensions=(".csv", ".json", ".jsonl", ".parquet", ".tsv", ".xls", ".xlsm", ".xlsx"),
        parser_id="office_data_structural_v1",
        chunker_version="table_structure_v1",
        fts_table="data_fts",
    ),
    _lane(
        "ppt",
        "PPT / Presentation",
        frontend_aliases=("PPT", "PPT / Presentation", "Presentation"),
        backend_aliases=("ppt_presentation", "powerpoint"),
        schema_contract=(
            "ppt_file",
            "ppt_slide",
            "ppt_shape",
            "ppt_text_block",
            "ppt_notes",
            "ppt_table",
            "ppt_image_reference",
            "ppt_slide_relationship",
            "ppt_chunk",
            "ppt_structure_signature",
            "ppt_fts",
        ),
        source_types=("presentation", "slide_deck"),
        extensions=(".odp", ".ppt", ".pptx"),
        parser_id="presentation_structure_v1",
        chunker_version="slide_structure_v1",
        fts_table="ppt_fts",
    ),
    _lane(
        "pdf_ocr",
        "PDF / OCR",
        frontend_aliases=("PDF", "PDF / OCR"),
        backend_aliases=("pdf", "portable_document"),
        schema_contract=(
            "pdf_file",
            "pdf_page",
            "pdf_text_block",
            "pdf_image_block",
            "pdf_ocr_run",
            "pdf_ocr_block",
            "pdf_ocr_line",
            "pdf_review_region",
            "pdf_structure_signature",
            "pdf_fts",
        ),
        source_types=("pdf_document", "scanned_pdf"),
        extensions=(".pdf",),
        parser_id="pdf_ocr_structural_v2",
        chunker_version="page_block_v1",
        fts_table="pdf_fts",
    ),
    _lane(
        "images_ocr",
        "Images / OCR",
        frontend_aliases=("Images", "Images / OCR"),
        backend_aliases=("images", "image_ocr"),
        schema_contract=(
            "image_file",
            "image_metadata",
            "image_ocr_run",
            "image_ocr_block",
            "image_ocr_line",
            "image_review_region",
            "image_ocr_fts",
        ),
        source_types=("image", "scanned_image"),
        extensions=(".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"),
        parser_id="image_ocr_structural_v2",
        chunker_version="ocr_region_v1",
        fts_table="image_ocr_fts",
    ),
    _lane(
        "artifacts",
        "Artifacts",
        frontend_aliases=("Artifacts", "Project Artifacts"),
        backend_aliases=("artifact", "artifact_vault"),
        schema_contract=(
            "project_artifact",
            "artifact_metadata",
            "artifact_text_extract",
            "artifact_relation_edge",
            "artifact_review_required",
            "artifact_fts",
        ),
        source_types=("project_artifact", "structured_artifact", "notebook"),
        extensions=(".csv", ".html", ".ipynb", ".json", ".jsonl", ".md", ".mmd", ".parquet", ".svg", ".txt", ".xml"),
        parser_id="artifact_structure_v1",
        chunker_version="artifact_blocks_v1",
        fts_table="artifact_fts",
    ),
    _lane(
        "custom",
        "Custom",
        frontend_aliases=("Custom", "Custom Source"),
        backend_aliases=("generic", "other"),
        schema_contract=(
            "custom_source",
            "custom_item",
            "custom_evidence",
            "custom_decision",
            "custom_next_action",
            "custom_fts",
        ),
        source_types=("custom_file", "custom_folder"),
        extensions=(".csv", ".db", ".docx", ".html", ".json", ".jsonl", ".md", ".pdf", ".sqlite", ".sqlite3", ".tsv", ".txt", ".xml", ".yaml", ".yml", ".zip"),
        parser_id="custom_best_effort_v1",
        chunker_version="bounded_text_v1",
        fts_table="custom_fts",
    ),
    _lane(
        "brain_loader",
        "Brain Loader",
        frontend_aliases=("Brain Loader", "Load Brain"),
        backend_aliases=("brainloader", "brain_import"),
        schema_contract=(
            "brain_loader_source",
            "brain_loader_package",
            "brain_loader_member",
            "brain_loader_manifest",
            "brain_loader_pointer",
            "brain_loader_database",
            "brain_loader_schema_object",
            "brain_loader_relationship",
            "brain_loader_receipt",
            "brain_loader_fts",
        ),
        source_types=("sqlite_brain_package", "brain_folder", "brain_database"),
        extensions=(".db", ".sqlite", ".sqlite3", ".zip"),
        parser_id="brain_package_loader_v1",
        chunker_version="package_member_v1",
        fts_table="brain_loader_fts",
    ),
    _lane(
        "research",
        "Research",
        frontend_aliases=("Research", "Research Evidence"),
        backend_aliases=("research_sources",),
        schema_contract=(
            "research_source",
            "research_question",
            "research_hypothesis",
            "research_method",
            "research_evidence",
            "research_finding",
            "research_limitation",
            "research_citation",
            "research_open_question",
            "research_receipt",
            "research_fts",
        ),
        source_types=("research_document", "research_dataset", "research_note"),
        extensions=(".csv", ".docx", ".json", ".md", ".parquet", ".pdf", ".txt", ".xlsx"),
        parser_id="research_evidence_v1",
        chunker_version="claim_evidence_v1",
        fts_table="research_fts",
    ),
    _lane(
        "project_engulf",
        "Project Engulf",
        frontend_aliases=("Project Engulf", "Engulf Project"),
        backend_aliases=("project_brain_builder", "project_import"),
        schema_contract=(
            "project_engulf_source",
            "project_engulf_file",
            "project_engulf_chunk",
            "project_engulf_component",
            "project_engulf_relationship",
            "project_engulf_origin",
            "project_engulf_schema_mapping",
            "project_engulf_sector_target",
            "project_engulf_conflict",
            "project_engulf_object_decision",
            "project_engulf_run",
            "project_engulf_topology_update",
            "project_engulf_receipt",
            "project_engulf_fts",
        ),
        source_types=("project_folder", "project_archive"),
        extensions=(".zip",),
        parser_id="project_engulf_v1",
        chunker_version="project_structure_v1",
        fts_table="project_engulf_fts",
    ),
    _lane(
        "sqlite_brain",
        "SQLite Brain",
        frontend_aliases=("SQLite Brain", "SQLite"),
        backend_aliases=("sqlitebrain", "sqlite_brain_import"),
        schema_contract=(
            # SQLite reserves the sqlite_* object-name prefix.  Keep the lane
            # identity canonical while using legal, explicit loaded_* tables.
            "loaded_sqlite_brain_source",
            "loaded_sqlite_brain_database",
            "loaded_sqlite_brain_schema_object",
            "loaded_sqlite_brain_table_stat",
            "loaded_sqlite_brain_foreign_key",
            "loaded_sqlite_brain_fts_table",
            "loaded_sqlite_brain_relationship",
            "loaded_sqlite_brain_integrity_result",
            "loaded_sqlite_brain_package_pointer",
            "loaded_sqlite_brain_compatibility",
            "loaded_sqlite_brain_sector_mapping",
            "loaded_sqlite_brain_receipt",
            "loaded_sqlite_brain_fts",
        ),
        source_types=("sqlite_database", "sqlite_brain_package"),
        extensions=(".db", ".sqlite", ".sqlite3", ".zip"),
        parser_id="sqlite_brain_inspector_v1",
        chunker_version="sqlite_schema_row_v1",
        fts_table="loaded_sqlite_brain_fts",
    ),
)


_CANONICAL_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def normalize_lane_alias(value: str) -> str:
    """Normalize a user/layer alias without guessing an unregistered lane."""

    if not isinstance(value, str):
        raise UnknownLaneAliasError(f"Lane alias must be a string, got {type(value).__name__}")
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold()).strip("_")
    if not normalized:
        raise UnknownLaneAliasError("Lane alias cannot be empty")
    return normalized


def _validate_explicit_aliases(lane: LaneDefinition, field_name: str, aliases: Sequence[str]) -> None:
    seen: dict[str, str] = {}
    for alias in aliases:
        normalized = normalize_lane_alias(alias)
        previous = seen.get(normalized)
        if previous is not None:
            raise DuplicateLaneAliasError(
                f"{lane.canonical_lane_id}.{field_name} repeats normalized alias "
                f"{normalized!r}: {previous!r} and {alias!r}"
            )
        seen[normalized] = alias


def _validate_lane(lane: LaneDefinition) -> None:
    if not _CANONICAL_ID_RE.fullmatch(lane.canonical_lane_id):
        raise InvalidLaneDefinitionError(f"Invalid canonical lane ID: {lane.canonical_lane_id!r}")
    required_text = {
        "display_label": lane.display_label,
        "sector_folder": lane.sector_folder,
        "sqlite_filename": lane.sqlite_filename,
        "parser_id": lane.parser_id,
        "chunker_version": lane.chunker_version,
        "fts_table": lane.fts_table,
        "mmd_node_id": lane.mmd_node_id,
        "mutation_policy": lane.mutation_policy,
    }
    for field_name, value in required_text.items():
        if not isinstance(value, str) or not value.strip():
            raise InvalidLaneDefinitionError(f"{lane.canonical_lane_id}.{field_name} must be non-empty")
    if lane.sector_folder != lane.canonical_lane_id:
        raise InvalidLaneDefinitionError(
            f"{lane.canonical_lane_id} must use canonical sector folder {lane.canonical_lane_id!r}"
        )
    expected_filename = f"{lane.canonical_lane_id}_sector_v001.sqlite"
    if lane.sqlite_filename != expected_filename:
        raise InvalidLaneDefinitionError(
            f"{lane.canonical_lane_id} must use canonical SQLite filename {expected_filename!r}"
        )
    if not lane.schema_contract or lane.fts_table not in lane.schema_contract:
        raise InvalidLaneDefinitionError(
            f"{lane.canonical_lane_id}.schema_contract must include fts_table {lane.fts_table!r}"
        )
    if len(set(lane.schema_contract)) != len(lane.schema_contract):
        raise InvalidLaneDefinitionError(f"{lane.canonical_lane_id}.schema_contract contains duplicates")
    if not lane.source_types or not lane.extensions:
        raise InvalidLaneDefinitionError(
            f"{lane.canonical_lane_id} requires source_types and extensions"
        )
    if lane.mutation_policy not in {MUTATION_AUTOMATIC_APPEND_ONLY, MUTATION_NAMED_GRANT_RELOCK}:
        raise InvalidLaneDefinitionError(
            f"{lane.canonical_lane_id} has unsupported mutation policy {lane.mutation_policy!r}"
        )
    _validate_explicit_aliases(lane, "frontend_aliases", lane.frontend_aliases)
    _validate_explicit_aliases(lane, "backend_aliases", lane.backend_aliases)


def _aliases_for_scope(lane: LaneDefinition, scope: AliasScope) -> tuple[str, ...]:
    common = (lane.canonical_lane_id, lane.display_label)
    if scope == "frontend":
        return common + lane.frontend_aliases
    if scope == "backend":
        return common + lane.backend_aliases
    if scope == "any":
        return common + lane.frontend_aliases + lane.backend_aliases
    raise ValueError(f"Unsupported alias scope: {scope!r}")


def validate_registry(
    definitions: Iterable[LaneDefinition],
    *,
    expected_ids: Sequence[str] | None = None,
) -> Mapping[str, LaneDefinition]:
    """Validate lane IDs, aliases, schemas, and unique sector destinations.

    The returned mapping is immutable.  Alias duplication inside one explicit
    list is rejected.  Reusing an alias across frontend/backend scopes for the
    *same* lane is safe; reusing it for different lanes is ambiguous and fails.
    """

    lanes = tuple(definitions)
    registry: dict[str, LaneDefinition] = {}
    folders: dict[str, str] = {}
    targets: dict[tuple[str, str], str] = {}

    for lane in lanes:
        _validate_lane(lane)
        if lane.canonical_lane_id in registry:
            raise DuplicateCanonicalLaneError(f"Duplicate canonical lane ID: {lane.canonical_lane_id}")
        folder_key = lane.sector_folder.casefold()
        previous_folder = folders.get(folder_key)
        if previous_folder is not None:
            raise DuplicateSectorTargetError(
                f"Lanes {previous_folder!r} and {lane.canonical_lane_id!r} share sector folder {lane.sector_folder!r}"
            )
        target_key = (folder_key, lane.sqlite_filename.casefold())
        previous_target = targets.get(target_key)
        if previous_target is not None:
            raise DuplicateSectorTargetError(
                f"Lanes {previous_target!r} and {lane.canonical_lane_id!r} share sector target {target_key!r}"
            )
        registry[lane.canonical_lane_id] = lane
        folders[folder_key] = lane.canonical_lane_id
        targets[target_key] = lane.canonical_lane_id

    if expected_ids is not None and tuple(registry) != tuple(expected_ids):
        raise InvalidLaneDefinitionError(
            f"Canonical lane order/content mismatch: observed={tuple(registry)!r}, expected={tuple(expected_ids)!r}"
        )

    for scope in ("any", "frontend", "backend"):
        owners: dict[str, str] = {}
        for lane in lanes:
            for alias in _aliases_for_scope(lane, scope):
                normalized = normalize_lane_alias(alias)
                previous = owners.get(normalized)
                if previous is not None and previous != lane.canonical_lane_id:
                    raise AmbiguousLaneAliasError(
                        f"Alias {alias!r} ({normalized!r}) is ambiguous in {scope} scope: "
                        f"{previous!r} vs {lane.canonical_lane_id!r}"
                    )
                owners[normalized] = lane.canonical_lane_id

    return MappingProxyType(registry)


LANE_REGISTRY = validate_registry(_LANE_DEFINITIONS, expected_ids=CANONICAL_LANE_IDS)
CANONICAL_LANE_REGISTRY = LANE_REGISTRY


def _build_alias_index(scope: AliasScope) -> Mapping[str, str]:
    index: dict[str, str] = {}
    for lane in LANE_REGISTRY.values():
        for alias in _aliases_for_scope(lane, scope):
            normalized = normalize_lane_alias(alias)
            previous = index.get(normalized)
            if previous is not None and previous != lane.canonical_lane_id:
                # Import-time validation should make this unreachable, but the
                # guard keeps resolution safe if construction changes later.
                raise AmbiguousLaneAliasError(
                    f"Alias {alias!r} maps to both {previous!r} and {lane.canonical_lane_id!r}"
                )
            index[normalized] = lane.canonical_lane_id
    return MappingProxyType(index)


_ALIAS_INDEX = {
    "any": _build_alias_index("any"),
    "frontend": _build_alias_index("frontend"),
    "backend": _build_alias_index("backend"),
}


def resolve_lane_id(alias: str, *, scope: AliasScope = "any") -> str:
    """Resolve a registered alias to exactly one canonical lane ID.

    No fallback-to-custom behavior is allowed.  Unknown or wrong-layer aliases
    fail closed so they cannot silently write to an unintended sector.
    """

    if scope not in _ALIAS_INDEX:
        raise ValueError(f"Unsupported alias scope: {scope!r}")
    normalized = normalize_lane_alias(alias)
    canonical = _ALIAS_INDEX[scope].get(normalized)
    if canonical is None:
        raise UnknownLaneAliasError(
            f"Unknown {scope} lane alias {alias!r}; canonical IDs are {', '.join(CANONICAL_LANE_IDS)}"
        )
    return canonical


def get_lane(alias: str, *, scope: AliasScope = "any") -> LaneDefinition:
    """Return the immutable canonical definition for an ID or registered alias."""

    return LANE_REGISTRY[resolve_lane_id(alias, scope=scope)]


def canonical_sector_path(project_root: str | Path, alias: str, *, scope: AliasScope = "any") -> Path:
    """Return ``project/sectors/<canonical>/<canonical>_sector_v001.sqlite``."""

    lane = get_lane(alias, scope=scope)
    return Path(project_root) / "sectors" / lane.sector_relative_path


def plan_sector_writes(aliases: Iterable[str], *, scope: AliasScope = "any") -> tuple[SectorWriteTarget, ...]:
    """Resolve an intake batch and reject any duplicate canonical destination.

    For example, ``["data", "data_excel_csv"]`` raises because both aliases
    resolve to ``data_excel``.  Rejecting the batch before returning targets is
    the no-double-sector-write guard; callers should create/write sectors only
    after this function succeeds.
    """

    targets: list[SectorWriteTarget] = []
    requested_by_lane: dict[str, str] = {}
    requested_by_path: dict[tuple[str, str], str] = {}
    for alias in aliases:
        lane = get_lane(alias, scope=scope)
        previous_alias = requested_by_lane.get(lane.canonical_lane_id)
        if previous_alias is not None:
            raise DuplicateSectorWriteError(
                f"Aliases {previous_alias!r} and {alias!r} both resolve to canonical lane "
                f"{lane.canonical_lane_id!r}"
            )
        path_key = (lane.sector_folder.casefold(), lane.sqlite_filename.casefold())
        previous_path_alias = requested_by_path.get(path_key)
        if previous_path_alias is not None:
            raise DuplicateSectorWriteError(
                f"Aliases {previous_path_alias!r} and {alias!r} share sector target {path_key!r}"
            )
        requested_by_lane[lane.canonical_lane_id] = alias
        requested_by_path[path_key] = alias
        targets.append(
            SectorWriteTarget(
                canonical_lane_id=lane.canonical_lane_id,
                sector_folder=lane.sector_folder,
                sqlite_filename=lane.sqlite_filename,
            )
        )
    active_code_lanes = PRIMARY_CODE_LANES.intersection(requested_by_lane)
    if len(active_code_lanes) > 1:
        raise MutuallyExclusiveCodeLaneError(
            "github_code and local_code are mutually exclusive primary-code modes; "
            "a local checkout containing .git remains local_code and records its "
            "embedded current-checkout Git provenance in that same authority"
        )
    return tuple(targets)


def validate_primary_code_mode(
    aliases: Iterable[str], *, scope: AliasScope = "any"
) -> str | None:
    """Return the one selected primary code mode, rejecting dual activation.

    A local folder does not become ``github_code`` merely because it contains
    ``.git``.  Source selection owns the mode; embedded Git is provenance of
    the local snapshot, while ``github_code`` is the full reverse-history
    intake mode.
    """

    resolved = {resolve_lane_id(alias, scope=scope) for alias in aliases}
    active = sorted(PRIMARY_CODE_LANES.intersection(resolved))
    if len(active) > 1:
        raise MutuallyExclusiveCodeLaneError(
            "PRIMARY_CODE_MODE_CONFLICT:github_code,local_code"
        )
    return active[0] if active else None


def registry_payload() -> dict[str, dict[str, object]]:
    """Return a fresh JSON-safe registry payload for IPC/UI integration."""

    return {lane_id: lane.as_dict() for lane_id, lane in LANE_REGISTRY.items()}


__all__ = [
    "AliasScope",
    "AmbiguousLaneAliasError",
    "CANONICAL_LANE_IDS",
    "CANONICAL_LANE_REGISTRY",
    "DuplicateCanonicalLaneError",
    "DuplicateLaneAliasError",
    "DuplicateSectorTargetError",
    "DuplicateSectorWriteError",
    "InvalidLaneDefinitionError",
    "LANE_REGISTRY",
    "LaneDefinition",
    "LaneRegistryError",
    "MUTATION_AUTOMATIC_APPEND_ONLY",
    "MUTATION_NAMED_GRANT_RELOCK",
    "MutuallyExclusiveCodeLaneError",
    "PRIMARY_CODE_LANES",
    "SectorWriteTarget",
    "UnknownLaneAliasError",
    "canonical_sector_path",
    "get_lane",
    "normalize_lane_alias",
    "plan_sector_writes",
    "registry_payload",
    "resolve_lane_id",
    "validate_primary_code_mode",
    "validate_registry",
]
