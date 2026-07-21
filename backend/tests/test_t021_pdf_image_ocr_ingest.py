from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw

from sqlite_brain_builder.runtime import ocr_ingest
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_semantic_lane


def _ocr_result(text: str = "Scanned evidence", confidence: float = 91.5) -> dict:
    block = {
        "id": "ocr_block_fixture",
        "order": 1,
        "text": text,
        "confidence": confidence,
        "bbox": [10, 10, 180, 40],
    }
    return {
        "status": "OCR_COMPLETE",
        "error": "",
        "blocks": [block],
        "lines": [{**block, "id": "ocr_line_fixture"}],
        "regions": [],
        "confidence": confidence,
        "review_required": False,
    }


def _image_fixture(path: Path) -> None:
    image = Image.new("RGB", (320, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "Scanned evidence", fill="black")
    image.save(path)


def _pdf_fixture(path: Path, image_path: Path, kinds: list[str]) -> None:
    import fitz

    document = fitz.open()
    for kind in kinds:
        page = document.new_page(width=400, height=220)
        if kind in {"text", "mixed"}:
            page.insert_text((30, 45), "Native evidence text")
        if kind in {"scan", "mixed"}:
            page.insert_image(fitz.Rect(30, 70, 350, 190), filename=str(image_path))
    document.save(path)
    document.close()


def _counts(database: Path, tables: list[str]) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}


def test_tesseract_resolver_finds_standard_windows_install_without_path(monkeypatch, tmp_path: Path) -> None:
    program_files = tmp_path / "Program Files"
    executable = program_files / "Tesseract-OCR" / "tesseract.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(ocr_ingest.os, "name", "nt")
    monkeypatch.setenv("ProgramFiles", str(program_files))
    monkeypatch.delenv("EVIDENCEOS_TESSERACT_CMD", raising=False)
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(ocr_ingest.shutil, "which", lambda _name: None)

    assert ocr_ingest._resolve_tesseract_executable() == str(executable.resolve())


def test_pdf_text_scanned_and_mixed_fixtures_route_ocr(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ocr_ingest, "_ocr_pil_image", lambda _image: _ocr_result())
    image = tmp_path / "scan.png"
    _image_fixture(image)
    fixtures = {
        "text": ["text"],
        "scanned": ["scan"],
        "mixed": ["text", "scan"],
    }
    parsed = {}
    for name, kinds in fixtures.items():
        path = tmp_path / f"{name}.pdf"
        _pdf_fixture(path, image, kinds)
        parsed[name] = ocr_ingest.parse_pdf_ocr(path)

    assert parsed["text"]["pages"][0]["classification"] == "TEXT"
    assert parsed["text"]["pages"][0]["ocr"]["status"] == "OCR_NOT_REQUIRED_TEXT_LAYER"
    assert parsed["scanned"]["pages"][0]["classification"] == "SCANNED"
    assert parsed["scanned"]["pages"][0]["ocr"]["blocks"][0]["confidence"] == 91.5
    assert [page["classification"] for page in parsed["mixed"]["pages"]] == ["TEXT", "SCANNED"]
    assert parsed["mixed"]["metadata"] == {
        "page_count": 2,
        "text_page_count": 1,
        "scanned_page_count": 1,
        "ocr_page_count": 1,
    }


def test_pdf_and_image_ingestion_is_idempotent_with_stable_fts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ocr_ingest, "_ocr_pil_image", lambda _image: _ocr_result())
    image = tmp_path / "scan.png"
    pdf = tmp_path / "scan.pdf"
    _image_fixture(image)
    _pdf_fixture(pdf, image, ["scan"])

    cases = [
        ("pdf_ocr", pdf, ["pdf_page", "pdf_image_block", "pdf_ocr_run", "pdf_ocr_block", "pdf_ocr_line", "pdf_fts"]),
        ("images_ocr", image, ["image_metadata", "image_ocr_run", "image_ocr_block", "image_ocr_line", "image_ocr_fts"]),
    ]
    for lane, source, tables in cases:
        database = tmp_path / f"{lane}.sqlite"
        payload = {"source_id": f"source-{lane}", "lane_key": lane, "path": str(source), "display_name": source.name}
        build_semantic_lane(database, lane, payload, None, 0)
        first = _counts(database, tables)
        build_semantic_lane(database, lane, payload, None, 0)
        assert _counts(database, tables) == first
        assert all(value > 0 for value in first.values())


def test_low_confidence_image_routes_review_region(monkeypatch, tmp_path: Path) -> None:
    low_confidence = _ocr_result(confidence=41.0)
    low_confidence["review_required"] = True
    low_confidence["regions"] = [{
        "id": "review-region",
        "bbox": [10, 10, 180, 40],
        "confidence": 41.0,
        "review_required": True,
        "reason": "LOW_OCR_CONFIDENCE",
    }]
    monkeypatch.setattr(ocr_ingest, "_ocr_pil_image", lambda _image: low_confidence)
    image = tmp_path / "review.png"
    _image_fixture(image)

    database = tmp_path / "review.sqlite"
    build_semantic_lane(
        database,
        "images_ocr",
        {"source_id": "review-image", "lane_key": "images_ocr", "path": str(image)},
        None,
        0,
    )
    assert _counts(database, ["image_review_region"])["image_review_region"] == 1
