from __future__ import annotations

import zipfile
from pathlib import Path

from sqlite_brain_builder.runtime.gemini_exact10 import _deterministic_zip
from sqlite_brain_builder.runtime.package_validation import T021_GEMINI_EXACT10_NAMES
from sqlite_brain_builder.runtime.provider_package_compatibility import (
    audit_provider_package,
)


def _stage_exact10(root: Path) -> Path:
    stage = root / "stage"
    stage.mkdir()
    for index, name in enumerate(T021_GEMINI_EXACT10_NAMES, 1):
        (stage / name).write_bytes((f"fixture-{index}-{name}\n" * 5).encode("utf-8"))
    return stage


def test_gemini_deterministic_zip_is_pkzip20_without_forced_zip64(
    tmp_path: Path,
) -> None:
    package = tmp_path / "gemini.zip"
    _deterministic_zip(_stage_exact10(tmp_path), package)

    audit = audit_provider_package(package, "GEMINI")

    assert audit["status"] == "PASS"
    assert audit["member_count"] == 10
    assert audit["maximum_extract_version"] <= 20
    assert all(
        0x0001 not in member["central_extra_field_ids"]
        and 0x0001 not in member["local_header"]["extra_field_ids"]
        for member in audit["members"]
    )


def test_consumer_audit_rejects_forced_zip64_even_when_the_payload_is_small(
    tmp_path: Path,
) -> None:
    package = tmp_path / "forced-zip64.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(T021_GEMINI_EXACT10_NAMES, 1):
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            with archive.open(info, "w", force_zip64=True) as stream:
                stream.write(f"small-{index}\n".encode("utf-8"))

    audit = audit_provider_package(package, "GEMINI")

    assert audit["status"] == "FAIL"
    assert audit["maximum_extract_version"] == 45
    assert any("EXTRACT_VERSION:45" in error for error in audit["errors"])


def test_chatgpt_rejects_nested_raw_env15_authority_zip(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "locked.zip"
    with zipfile.ZipFile(nested, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("LOCKED_README.txt", "immutable authority\n")
    package = tmp_path / "chatgpt.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("FLASH_ME_FIRST_SINGLE_PROMPT.txt", "open the package\n")
        archive.write(
            nested,
            "public_read/UEPC_ENV15_PUBLIC_LOCKED_SECTION_PACKAGE.zip",
        )

    audit = audit_provider_package(package, "CHATGPT_LOCALAI")

    assert audit["status"] == "FAIL"
    assert audit["nested_archives"][0]["status"] == "PASS"
    assert any("NESTED_ZIP_FORBIDDEN" in error for error in audit["errors"])
    assert audit["provider_ingestion_status"] == "HUMAN_FRESH_BRAIN_HIL_PENDING"
