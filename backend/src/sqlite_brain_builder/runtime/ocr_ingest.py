from __future__ import annotations

import hashlib
import io
import os
import shutil
from pathlib import Path
from typing import Any


class OcrIngestError(RuntimeError):
    pass


def _resolve_tesseract_executable() -> str:
    """Resolve the installed OCR engine without depending on the shell PATH."""
    configured = [
        os.environ.get("EVIDENCEOS_TESSERACT_CMD", ""),
        os.environ.get("TESSERACT_CMD", ""),
        shutil.which("tesseract") or "",
    ]
    if os.name == "nt":
        configured.extend(
            str(Path(root) / "Tesseract-OCR" / "tesseract.exe")
            for root in (
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                os.environ.get("LOCALAPPDATA", ""),
            )
            if root
        )
    for candidate in configured:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return ""


def _configure_tesseract(pytesseract_module: Any) -> str:
    """Point pytesseract at a real installed binary and return its path."""
    current = str(getattr(pytesseract_module.pytesseract, "tesseract_cmd", "") or "")
    if current and (Path(current).is_file() or shutil.which(current)):
        return str(Path(current).resolve()) if Path(current).is_file() else str(shutil.which(current))
    resolved = _resolve_tesseract_executable()
    if resolved:
        pytesseract_module.pytesseract.tesseract_cmd = resolved
    return resolved


def _stable_id(kind: str, *parts: object) -> str:
    material = "\x1f".join([kind, *(str(part) for part in parts)])
    return f"{kind}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _ocr_pil_image(image: Any) -> dict[str, Any]:
    """Return deterministic OCR blocks and lines, or an honest unavailable state."""
    try:
        import pytesseract
        from pytesseract import Output

        _configure_tesseract(pytesseract)
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception as exc:
        return {
            "status": "OCR_ENGINE_UNAVAILABLE_REVIEW_REQUIRED",
            "error": str(exc)[:300],
            "blocks": [],
            "lines": [],
            "regions": [],
            "confidence": None,
            "review_required": True,
        }

    words: list[dict[str, Any]] = []
    count = len(data.get("text", []))
    for index in range(count):
        text = str(data["text"][index] or "").strip()
        try:
            confidence = float(data.get("conf", [-1] * count)[index])
        except (TypeError, ValueError):
            confidence = -1.0
        if not text or confidence < 0:
            continue
        words.append(
            {
                "text": text,
                "confidence": confidence,
                "left": int(data["left"][index]),
                "top": int(data["top"][index]),
                "width": int(data["width"][index]),
                "height": int(data["height"][index]),
                "block_num": int(data.get("block_num", [0] * count)[index]),
                "par_num": int(data.get("par_num", [0] * count)[index]),
                "line_num": int(data.get("line_num", [0] * count)[index]),
            }
        )

    def aggregate(group_key: str) -> list[dict[str, Any]]:
        grouped: dict[tuple[int, ...], list[dict[str, Any]]] = {}
        for word in words:
            key = (word["block_num"],) if group_key == "block" else (
                word["block_num"], word["par_num"], word["line_num"]
            )
            grouped.setdefault(key, []).append(word)
        records = []
        for order, (key, values) in enumerate(sorted(grouped.items()), start=1):
            left = min(item["left"] for item in values)
            top = min(item["top"] for item in values)
            right = max(item["left"] + item["width"] for item in values)
            bottom = max(item["top"] + item["height"] for item in values)
            text = " ".join(item["text"] for item in values)
            confidence = sum(item["confidence"] for item in values) / len(values)
            records.append(
                {
                    "id": _stable_id(f"ocr_{group_key}", key, text),
                    "order": order,
                    "text": text,
                    "confidence": round(confidence, 2),
                    "bbox": [left, top, right, bottom],
                }
            )
        return records

    blocks = aggregate("block")
    lines = aggregate("line")
    confidence = round(sum(item["confidence"] for item in words) / len(words), 2) if words else None
    review_required = confidence is None or confidence < 60.0
    regions = [
        {
            "id": _stable_id("ocr_region", item["id"]),
            "bbox": item["bbox"],
            "confidence": item["confidence"],
            "review_required": item["confidence"] < 60.0,
            "reason": "LOW_OCR_CONFIDENCE" if item["confidence"] < 60.0 else "OCR_VERIFIED_CANDIDATE",
        }
        for item in blocks
        if item["confidence"] < 60.0
    ]
    return {
        "status": "OCR_COMPLETE" if words else "OCR_EMPTY_REVIEW_REQUIRED",
        "error": "",
        "blocks": blocks,
        "lines": lines,
        "regions": regions,
        "confidence": confidence,
        "review_required": review_required,
    }


def parse_image_ocr(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    from PIL import Image

    with Image.open(source) as image:
        metadata = {
            "format": image.format or source.suffix.lstrip(".").upper(),
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "frame_count": int(getattr(image, "n_frames", 1)),
            "dpi": list(image.info.get("dpi", ())) if image.info.get("dpi") else [],
            "exif": {str(key): str(value) for key, value in sorted(image.getexif().items())},
        }
        ocr = _ocr_pil_image(image.convert("RGB"))
    return {"parser": "image_ocr_structural_v2", "metadata": metadata, **ocr}


def parse_pdf_ocr(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.suffix.casefold() != ".pdf":
        raise OcrIngestError(f"PDF_EXTENSION_REQUIRED:{source}")
    import fitz
    from PIL import Image

    document = fitz.open(source)
    pages = []
    total_text_pages = 0
    total_scanned_pages = 0
    total_ocr_pages = 0
    try:
        for page_index, page in enumerate(document, start=1):
            page_dict = page.get_text("dict")
            text_blocks = []
            image_regions = []
            for block_order, block in enumerate(page_dict.get("blocks", []), start=1):
                bbox = [round(float(value), 3) for value in block.get("bbox", (0, 0, 0, 0))]
                if block.get("type") == 0:
                    lines = []
                    for line in block.get("lines", []):
                        line_text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                        if line_text:
                            lines.append(line_text)
                    text = "\n".join(lines).strip()
                    if text:
                        text_blocks.append(
                            {
                                "id": _stable_id("pdf_text", page_index, block_order, text),
                                "order": block_order,
                                "text": text,
                                "bbox": bbox,
                            }
                        )
                elif block.get("type") == 1:
                    image_regions.append(
                        {
                            "id": _stable_id("pdf_image", page_index, block_order, block.get("xref", 0), bbox),
                            "order": block_order,
                            "bbox": bbox,
                            "width": block.get("width"),
                            "height": block.get("height"),
                            "extension": block.get("ext", ""),
                            "xref": block.get("xref", 0),
                        }
                    )
            text = "\n".join(item["text"] for item in text_blocks).strip()
            has_text = bool(text)
            scan_candidate = not has_text and bool(image_regions)
            ocr = {
                "status": "OCR_NOT_REQUIRED_TEXT_LAYER",
                "error": "",
                "blocks": [],
                "lines": [],
                "regions": [],
                "confidence": None,
                "review_required": False,
            }
            if scan_candidate:
                total_scanned_pages += 1
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                    ocr = _ocr_pil_image(image.convert("RGB"))
                if ocr["status"] == "OCR_COMPLETE":
                    total_ocr_pages += 1
            if has_text:
                total_text_pages += 1
            pages.append(
                {
                    "id": _stable_id("pdf_page", page_index),
                    "page": page_index,
                    "width": round(float(page.rect.width), 3),
                    "height": round(float(page.rect.height), 3),
                    "classification": "TEXT" if has_text and not image_regions else "MIXED" if has_text else "SCANNED" if image_regions else "EMPTY",
                    "text_blocks": text_blocks,
                    "image_regions": image_regions,
                    "ocr": ocr,
                }
            )
    finally:
        document.close()
    review_required = any(page["ocr"]["review_required"] for page in pages)
    return {
        "parser": "pdf_ocr_structural_v2",
        "metadata": {
            "page_count": len(pages),
            "text_page_count": total_text_pages,
            "scanned_page_count": total_scanned_pages,
            "ocr_page_count": total_ocr_pages,
        },
        "pages": pages,
        "review_required": review_required,
    }


__all__ = ["OcrIngestError", "parse_image_ocr", "parse_pdf_ocr"]
