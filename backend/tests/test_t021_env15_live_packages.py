from __future__ import annotations

import json
import hashlib
import zipfile
from pathlib import Path

from sqlite_brain_builder.runtime.package_validation import (
    validate_chatgpt_package,
    validate_gemini_exact10,
)
from sqlite_brain_builder.runtime.provider_readable_corpus import GEMINI_READABLE_NAMES
from sqlite_brain_builder.runtime.stable_runtime_v53 import (
    build_brain,
    export_gemini_exact10,
    export_one_upload_package,
    render_topology,
)
from sqlite_brain_builder.codex_env15_package import create_codex_env15_package
from sqlite_brain_builder.runtime.env15_locked_read import (
    SUPPLIED_CODEX_SUPPORT_MEMBERS,
    supplied_codex_support_rows,
)


def test_live_packages_contain_corrected_topologies_and_relocked_project_state(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "evidence.md"
    source.write_text("# Evidence\nGoverned live package fixture.\n", encoding="utf-8")
    brain_name = "Live Package Brain"
    result = build_brain(
        str(tmp_path),
        brain_name,
        [{"source_id": "source_code", "lane_key": "local_code", "path": str(repository), "active": True}],
        generate_mmd=False,
    )
    rendered = render_topology(str(tmp_path), brain_name)
    chatgpt = export_one_upload_package(str(tmp_path), brain_name)
    gemini = export_gemini_exact10(
        str(tmp_path), brain_name, chatgpt_package=chatgpt["package_zip"]
    )
    codex = create_codex_env15_package(
        result["brain_root"],
        tmp_path / "codex-package",
        Path(result["brain_root"]) / "packages",
        brain_name=brain_name,
        goal_pointer={
            "parent_goal_id": "T023",
            "current_task_pointer": "ENV_UOP_AUTHORITY",
        },
        delta_ledger={"delta_id": "ENV_UOP_AUTHORITY_DELTA"},
        chatgpt_package=chatgpt["package_zip"],
        gemini_package=gemini["gemini_package_zip"],
    )

    assert len(rendered["rendered"]) == 3
    assert {
        item["status"] for item in rendered["rendered"]
        if Path(item["mmd"]).name in {"env_mmd.mmd", "uop_mmd.mmd"}
    } == {
        "SQLITE_DERIVED_ENV_MMD_TO_SVG_TO_PNG_PASS",
        "SQLITE_DERIVED_UOP_MMD_TO_SVG_TO_PNG_PASS",
    }
    assert [item["status"] for item in rendered["rendered"]].count(
        "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS"
    ) == 1
    assert validate_chatgpt_package(chatgpt["package_zip"])["status"] == "PASS"
    assert validate_gemini_exact10(gemini["gemini_package_zip"])["status"] == "PASS"

    with zipfile.ZipFile(chatgpt["package_zip"]) as archive:
        assert not any(name.casefold().endswith(".zip") for name in archive.namelist())
        assert archive.read("env/env_mmd.mmd").startswith(b"flowchart")
        assert archive.read("uop/uop_mmd.mmd").startswith(b"flowchart")
        assert "PROJECT_ROUTER" in archive.read(
            "project/topology/project_master_topology.mmd"
        ).decode("utf-8")
        assert "receipts/ENV15_LIVE_PROJECT_RUNTIME_STATE.json" in archive.namelist()
        assert not any("codex_handoff" in name.casefold() or "CODEX_NEXT_SESSION" in name for name in archive.namelist())
        png_members = [item for item in archive.infolist() if item.filename.casefold().endswith(".png")]
        assert png_members
        assert all(item.compress_type == zipfile.ZIP_STORED for item in png_members)
        assert all(archive.read(item.filename)[:8] == b"\x89PNG\r\n\x1a\n" for item in png_members)
        assert any(item.filename.endswith("_HD.png") for item in png_members)
        assert not any(name.casefold().startswith("codex/") for name in archive.namelist())

    with zipfile.ZipFile(gemini["gemini_package_zip"]) as archive:
        assert not any("codex_handoff" in name.casefold() or "CODEX_NEXT_SESSION" in name for name in archive.namelist())
        assert set(archive.namelist()) == set(GEMINI_READABLE_NAMES)
        assert "Governed live package fixture." in archive.read("PROJECT_CORPUS.txt").decode("utf-8")
        assert all(b"\x00" not in archive.read(name) for name in archive.namelist())
        assert not any(name in SUPPLIED_CODEX_SUPPORT_MEMBERS for name in archive.namelist())

    with zipfile.ZipFile(codex["archive"]) as archive:
        names = archive.namelist()
        assert set(SUPPLIED_CODEX_SUPPORT_MEMBERS) <= set(names)
        assert not any(name.casefold().endswith(".zip") for name in names)
        assert not any("project_topology_template" in name.casefold() for name in names)
        for member, expected_size, expected_hash in supplied_codex_support_rows():
            payload = archive.read(member)
            assert len(payload) == expected_size
            assert hashlib.sha256(payload).hexdigest().upper() == expected_hash

    assert gemini["package_folder"] == ""
    assert gemini["staging_removed"] is True
    assert not Path(gemini["staging_path"]).exists()
    assert chatgpt["package_folder"] == ""
    assert chatgpt["staging_removed"] is True
    assert not Path(chatgpt["staging_path"]).exists()
    assert Path(result["brain_root"]).is_dir()
