from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.env15_resource import (
    ENV15_EXPECTED_MEMBER_COUNT,
    ENV15_EXPECTED_SECTOR_COUNT,
    ENV15_EXPECTED_SQLITE_COUNT,
    ENV15_MEMBER_CRC_LEDGER_SHA256,
    ENV15_MEMBER_LEDGER_SHA256,
    ENV15_SOURCE_PACKAGE_SHA256,
    EXPECTED_SECTORS,
    EXPECTED_SQLITE_PATHS,
    MMD_SEMANTIC_VALIDATION_STATUS,
    find_env15_resource_root,
    install_env15_resource,
    validate_env15_archive,
    validate_env15_resource,
)


SUPPLIED_ENV15_ARCHIVE = Path(os.environ.get("EVIDENCE_LANE_ENV15_SOURCE_ARCHIVE", ""))


def test_embedded_env15_resource_passes_exact_structural_contract() -> None:
    root = find_env15_resource_root()
    report = validate_env15_resource(root)

    assert root.name == "public_model_env15"
    assert (root / ".uepc_env").is_file()
    assert report.structural_validation_passed is True
    assert report.errors == ()
    assert report.actual_member_count == ENV15_EXPECTED_MEMBER_COUNT == 100
    assert report.actual_member_ledger_sha256 == ENV15_MEMBER_LEDGER_SHA256
    assert report.actual_crc_ledger_sha256 == ENV15_MEMBER_CRC_LEDGER_SHA256
    assert report.member_hashes_valid is True
    assert report.member_crc_valid is True
    assert report.pointers_valid is True
    assert report.sector_registry_valid is True
    assert report.actual_sector_count == ENV15_EXPECTED_SECTOR_COUNT == 14
    assert set(report.sectors) == EXPECTED_SECTORS
    assert report.actual_sqlite_count == ENV15_EXPECTED_SQLITE_COUNT == 18
    assert {item.relative_path for item in report.sqlite_results} == EXPECTED_SQLITE_PATHS
    assert all(item.integrity_check == ("ok",) for item in report.sqlite_results)
    assert all(item.foreign_key_violation_count == 0 for item in report.sqlite_results)
    assert all(item.valid for item in report.sqlite_results)
    assert all(present for _, present in report.mmd_assets_present)
    assert report.prior_version_label_hits == ()

    # Structural presence is not a semantic acceptance claim for the current MMDs.
    assert report.mmd_semantic_validation_required is True
    assert report.mmd_semantic_validation_status == MMD_SEMANTIC_VALIDATION_STATUS
    assert report.mmd_semantic_acceptance_claimed is False
    assert report.validation_scope == "STRUCTURAL_RESOURCE_VALIDATION_ONLY"
    assert not (root.parent / "public_model_env14").exists()
    assert [path.name for path in root.parent.iterdir() if path.is_dir() and path.name.startswith("public_model_env")] == ["public_model_env15"]


@pytest.mark.skipif(not SUPPLIED_ENV15_ARCHIVE.is_file(), reason="supplied Env15 source archive unavailable")
def test_supplied_env15_archive_hash_crc_and_members_match_embedded_contract() -> None:
    report = validate_env15_archive(SUPPLIED_ENV15_ARCHIVE)

    assert report.valid is True
    assert report.errors == ()
    assert report.archive_sha256 == ENV15_SOURCE_PACKAGE_SHA256
    assert report.member_count == ENV15_EXPECTED_MEMBER_COUNT
    assert report.crc_status == "PASS"
    assert report.first_bad_crc_member is None
    assert report.missing_members == ()
    assert report.unexpected_members == ()
    assert report.duplicate_members == ()
    assert report.unsafe_members == ()
    assert report.size_mismatches == ()
    assert report.hash_mismatches == ()
    assert report.actual_member_ledger_sha256 == ENV15_MEMBER_LEDGER_SHA256
    assert report.actual_crc_ledger_sha256 == ENV15_MEMBER_CRC_LEDGER_SHA256


def test_atomic_install_copies_exact_root_and_refuses_existing_brain(tmp_path: Path) -> None:
    destination = tmp_path / "new_brain"

    result = install_env15_resource(destination)

    assert result.atomic_commit is True
    assert Path(result.destination_root) == destination.resolve()
    assert result.installed_member_count == ENV15_EXPECTED_MEMBER_COUNT
    assert result.installed_member_ledger_sha256 == ENV15_MEMBER_LEDGER_SHA256
    assert result.validation.structural_validation_passed is True
    assert (destination / ".uepc_env").is_file()
    assert not (destination / "public_model_env15").exists()
    assert not list(tmp_path.glob(".new_brain.env15-stage-*"))

    marker = destination / "caller-owned-marker.txt"
    marker.write_text("preserve me", encoding="utf-8")
    with pytest.raises(FileExistsError, match="ENV15_DESTINATION_ALREADY_EXISTS"):
        install_env15_resource(destination)
    assert marker.read_text(encoding="utf-8") == "preserve me"


def test_tampered_pointer_is_rejected_without_claiming_mmd_acceptance(tmp_path: Path) -> None:
    tampered = tmp_path / "tampered_env15"
    shutil.copytree(find_env15_resource_root(), tampered)
    pointer = tampered / ".uepc_env"
    pointer.write_text(
        pointer.read_text(encoding="utf-8").replace("UEPC_ENV_VERSION=V15", "UEPC_ENV_VERSION=V14"),
        encoding="utf-8",
    )

    report = validate_env15_resource(tampered)

    assert report.structural_validation_passed is False
    assert ".uepc_env" in report.hash_mismatches
    assert report.member_hashes_valid is False
    assert report.member_crc_valid is False
    assert any(item.startswith(".uepc_env:") for item in report.prior_version_label_hits)
    assert any("POINTER_VALUE_MISMATCH:.uepc_env:UEPC_ENV_VERSION" in item for item in report.errors)
    assert report.mmd_semantic_validation_required is True
    assert report.mmd_semantic_acceptance_claimed is False
