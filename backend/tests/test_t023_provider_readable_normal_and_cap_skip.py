from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from sqlite_brain_builder.runtime.env15_topology import generate_env15_topologies
from sqlite_brain_builder.runtime.package_validation import validate_gemini_exact10
from sqlite_brain_builder.runtime.provider_package_projection import (
    CHATGPT_PACKAGE_MAX_BYTES,
    GEMINI_PACKAGE_MAX_BYTES,
)
from sqlite_brain_builder.runtime.provider_readable_corpus import (
    GEMINI_READABLE_CONTRACT,
    GEMINI_READABLE_NAMES,
    export_gemini_provider_readable,
)
from sqlite_brain_builder.runtime import stable_runtime_v53
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
)


def _ready_brain(tmp_path: Path, brain_name: str = "Provider Readable Brain") -> tuple[Path, bytes]:
    source = tmp_path / "source"
    source.mkdir()
    payload = b"def provider_truth():\n    return 'ordinary readable exact bytes'\n"
    (source / "main.py").write_bytes(payload)
    built = build_brain(
        str(tmp_path),
        brain_name,
        [
            {
                "source_id": "provider_local_code",
                "lane_key": "local_code",
                "path": str(source),
                "active": True,
            }
        ],
        generate_mmd=False,
    )
    brain_root = Path(built["brain_root"])
    generated = generate_env15_topologies(brain_root)
    master = Path(generated["PROJECT"].mmd_path)
    master.with_suffix(".svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'></svg>\n",
        encoding="utf-8",
    )
    master.with_suffix(".png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    return brain_root, payload


def test_production_provider_packages_are_readable_hash_bound_and_leave_no_staging(
    tmp_path: Path,
) -> None:
    brain_name = "Provider Readable Brain"
    brain_root, payload = _ready_brain(tmp_path, brain_name)

    chatgpt = export_one_upload_package(str(tmp_path), brain_name)
    chatgpt_archive = Path(chatgpt["package_zip"])
    assert chatgpt["package_folder"] == ""
    assert chatgpt["staging_removed"] is True
    assert not Path(chatgpt["staging_path"]).exists()
    assert chatgpt["project_package_parity"]["status"] == "PASS"
    assert chatgpt["validation"]["provider_readability"]["status"] == "PASS"
    with zipfile.ZipFile(chatgpt_archive) as archive:
        names = archive.namelist()
        assert not any(name.casefold().endswith(".zip") for name in names)
        indexed_main = next(
            name
            for name in names
            if name.startswith("provider_readable/source/") and name.endswith("/main.py")
        )
        assert archive.read(indexed_main) == payload

    gemini = export_gemini_exact10(
        str(tmp_path),
        brain_name,
        chatgpt_package=chatgpt_archive,
    )
    gemini_archive = Path(gemini["gemini_package_zip"])
    validation = validate_gemini_exact10(gemini_archive)
    assert gemini["package_contract"] == GEMINI_READABLE_CONTRACT
    assert gemini["package_folder"] == ""
    assert gemini["staging_removed"] is True
    assert not Path(gemini["staging_path"]).exists()
    assert validation["status"] == "PASS"
    assert validation["package_contract"] == GEMINI_READABLE_CONTRACT
    assert validation["nested_zip_count"] == 0
    assert validation["all_files_directly_readable"] is True
    assert validation["dominant_opaque_binary_member"] is False
    assert validation["source_chatgpt_package_sha256"] == hashlib.sha256(
        chatgpt_archive.read_bytes()
    ).hexdigest()
    with zipfile.ZipFile(gemini_archive) as archive:
        assert set(archive.namelist()) == set(GEMINI_READABLE_NAMES)
        assert not any(name.casefold().endswith((".sqlite", ".db", ".zip")) for name in archive.namelist())
        for name in archive.namelist():
            content = archive.read(name)
            assert b"\x00" not in content
            content.decode("utf-8")
        assert payload.decode("utf-8").strip() in archive.read("PROJECT_CORPUS.txt").decode("utf-8")
    assert brain_root.is_dir()


def test_provider_caps_create_canonical_skips_and_retire_transient_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain_name = "Provider Cap Brain"
    brain_root, _payload = _ready_brain(tmp_path, brain_name)
    assert CHATGPT_PACKAGE_MAX_BYTES == 512_000_000
    assert GEMINI_PACKAGE_MAX_BYTES == 100_000_000

    monkeypatch.setattr(stable_runtime_v53, "CHATGPT_PACKAGE_MAX_BYTES", 1)
    chatgpt = export_one_upload_package(str(tmp_path), brain_name)
    assert chatgpt["status"] == "SKIPPED_PROJECT_CORPUS_TOO_LARGE"
    assert chatgpt["codex_package_required"] is True
    assert chatgpt["pipeline_failure"] is False
    assert chatgpt["package_zip"] == ""
    assert chatgpt["package_folder"] == ""
    assert chatgpt["staging_removed"] is True
    assert not Path(chatgpt["staging_path"]).exists()
    assert Path(chatgpt["skip_marker"]).is_file()
    assert Path(chatgpt["downstream_gemini_skip"]["skip_marker"]).is_file()
    assert not list((brain_root / "packages").glob("ChatGPT_*.zip"))

    gemini = export_gemini_provider_readable(
        brain_root,
        brain_name,
        "Read only provider test",
        package_use_mode="CANONICAL_FLASHABLE",
        chatgpt_package=None,
        maximum_bytes=1,
    )
    assert gemini["status"] == "SKIPPED_PROJECT_CORPUS_TOO_LARGE"
    assert gemini["codex_package_required"] is True
    assert gemini["pipeline_failure"] is False
    assert gemini["gemini_package_zip"] == ""
    assert gemini["package_folder"] == ""
    assert gemini["staging_removed"] is True
    assert not Path(gemini["staging_path"]).exists()
    assert Path(gemini["skip_marker"]).is_file()
    assert not list((brain_root / "packages").glob("Gemini_*.zip"))
