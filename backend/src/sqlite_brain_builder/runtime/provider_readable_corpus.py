from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import zipfile
from contextlib import closing
from datetime import datetime, timezone
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


READABILITY_CONTRACT = "T023_PROVIDER_READABLE_INDEXED_CORPUS_V1"
CORPUS_BEGIN = "===== BEGIN PROJECT READABLE CORPUS ====="
CORPUS_END = "===== END PROJECT READABLE CORPUS ====="
GEMINI_READABLE_CONTRACT = "T023_GEMINI_PROVIDER_READABLE_NORMAL_ZIP_V2"
GEMINI_READABLE_NAMES = (
    "OPEN_ME_FIRST.txt",
    "PROJECT_CORPUS.txt",
    "PROJECT_FILE_INDEX.json",
    "PROJECT_TOPOLOGY.mmd",
    "PROJECT_BUILD_SUMMARY.json",
    "PROJECT_DELTA_SUMMARY.txt",
    "ENV_GOVERNANCE.txt",
    "UOP_GOVERNANCE.txt",
    "PACKAGE_MANIFEST.json",
    "SHA256SUMS.txt",
)
_UNSAFE_PROVIDER_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ProviderReadableCorpusError(RuntimeError):
    pass


def _safe_component(value: object, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return candidate or fallback


def _safe_relative_path(value: object) -> Path:
    raw = str(value or "").replace("\\", "/").lstrip("/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ProviderReadableCorpusError(f"PROVIDER_CORPUS_PATH_UNSAFE:{raw}")
    return Path(*pure.parts)


def _sector_databases(brain_root: Path) -> list[Path]:
    sector_root = brain_root / "project" / "sectors"
    if not sector_root.is_dir():
        return []
    return sorted(
        (
            path
            for path in sector_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}
        ),
        key=lambda path: path.relative_to(sector_root).as_posix().casefold(),
    )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _decode_provider_text(payload: bytes) -> tuple[str, str]:
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16"), "utf-16"
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig"), "utf-8-sig"
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return payload.decode("cp1252", errors="replace"), "cp1252-replacement"


def _provider_visible_text(text: str, encoding: str) -> tuple[str, str]:
    """Render control bytes visibly while preserving exact hashes separately."""

    rendered, replacements = _UNSAFE_PROVIDER_CONTROL_RE.subn(
        lambda match: f"\\x{ord(match.group(0)):02x}",
        text,
    )
    return (
        rendered,
        encoding if replacements == 0 else encoding + "+visible-control-escapes",
    )


def _indexed_files(brain_root: Path) -> Iterator[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    for database in _sector_databases(brain_root):
        with closing(
            sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            tables = _table_names(connection)
            if not {"code_file_snapshot", "code_exact_byte_chunk"}.issubset(tables):
                continue
            file_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(code_file_snapshot)")
            }
            content_expression = "content_kind" if "content_kind" in file_columns else "'TEXT_INDEXED'"
            source_facts: dict[str, tuple[str, str]] = {}
            if "source_registry" in tables:
                source_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(source_registry)")
                }
                if {"source_id", "lane_id"}.issubset(source_columns):
                    name_expression = "source_name" if "source_name" in source_columns else "source_id"
                    source_facts = {
                        str(source_id): (str(lane_id or "code"), str(source_name or source_id))
                        for source_id, lane_id, source_name in connection.execute(
                            f"SELECT source_id,lane_id,{name_expression} FROM source_registry"
                        )
                    }
            files = connection.execute(
                "SELECT file_id,source_id,relative_path,size_bytes,sha256,"
                f"{content_expression} FROM code_file_snapshot "
                "ORDER BY source_id,relative_path,file_id"
            ).fetchall()
            for file_id, source_id, relative_path, size_bytes, expected_sha256, content_kind in files:
                if str(content_kind or "TEXT_INDEXED") != "TEXT_INDEXED":
                    continue
                identity = (str(source_id), str(file_id), str(relative_path))
                if identity in seen:
                    continue
                chunks = connection.execute(
                    "SELECT chunk_ordinal,compression,compressed_payload,raw_chunk_sha256 "
                    "FROM code_exact_byte_chunk WHERE source_id=? AND file_id=? "
                    "ORDER BY chunk_ordinal",
                    (source_id, file_id),
                ).fetchall()
                if not chunks and int(size_bytes or 0) != 0:
                    raise ProviderReadableCorpusError(
                        f"PROVIDER_CORPUS_EXACT_BYTES_MISSING:{source_id}:{relative_path}"
                    )
                payload_parts: list[bytes] = []
                for ordinal, compression, compressed_payload, raw_chunk_sha256 in chunks:
                    packed = bytes(compressed_payload or b"")
                    if str(compression).casefold() == "zlib":
                        raw = zlib.decompress(packed)
                    elif str(compression).casefold() in {"none", "stored", "identity"}:
                        raw = packed
                    else:
                        raise ProviderReadableCorpusError(
                            f"PROVIDER_CORPUS_COMPRESSION_UNSUPPORTED:{compression}:{relative_path}"
                        )
                    if hashlib.sha256(raw).hexdigest() != str(raw_chunk_sha256):
                        raise ProviderReadableCorpusError(
                            f"PROVIDER_CORPUS_CHUNK_HASH_MISMATCH:{ordinal}:{relative_path}"
                        )
                    payload_parts.append(raw)
                payload = b"".join(payload_parts)
                observed_sha256 = hashlib.sha256(payload).hexdigest()
                if len(payload) != int(size_bytes or 0) or observed_sha256 != str(expected_sha256):
                    raise ProviderReadableCorpusError(
                        f"PROVIDER_CORPUS_FILE_HASH_MISMATCH:{relative_path}"
                    )
                lane_id, source_name = source_facts.get(
                    str(source_id),
                    (database.parent.name or "code", str(source_id)),
                )
                seen.add(identity)
                yield {
                    "lane_id": lane_id,
                    "source_id": str(source_id),
                    "source_name": source_name,
                    "relative_path": str(relative_path).replace("\\", "/"),
                    "byte_size": len(payload),
                    "sha256": observed_sha256,
                    "payload": payload,
                    "sector_database": str(database),
                }


def materialize_provider_readable_tree(
    brain_root: str | Path,
    destination: str | Path,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    target = Path(destination).resolve()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    source_root = target / "source"
    source_root.mkdir()
    manifest_files: list[dict[str, Any]] = []
    for item in _indexed_files(root):
        relative = _safe_relative_path(item["relative_path"])
        output = (
            source_root
            / _safe_component(item["lane_id"], "code")
            / _safe_component(item["source_id"], "source")
            / relative
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(item["payload"])
        manifest_files.append(
            {
                key: item[key]
                for key in (
                    "lane_id",
                    "source_id",
                    "source_name",
                    "relative_path",
                    "byte_size",
                    "sha256",
                )
            }
            | {"package_path": output.relative_to(target).as_posix()}
        )
    manifest = {
        "contract": READABILITY_CONTRACT,
        "authority": "code_exact_byte_chunk reconstructed and SHA-256 verified",
        "file_count": len(manifest_files),
        "total_exact_text_bytes": sum(int(item["byte_size"]) for item in manifest_files),
        "files": manifest_files,
    }
    (target / "PROJECT_FILE_INDEX.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (target / "OPEN_ME_FIRST_PROVIDER_READABLE.md").write_text(
        "# Evidence OS provider-readable project corpus\n\n"
        f"Contract: `{READABILITY_CONTRACT}`\n\n"
        "Read `PROJECT_FILE_INDEX.json`, then read the files under `source/`. "
        "Those files were reconstructed from the indexed exact-byte authority and "
        "verified against the captured per-file SHA-256 values. Binary/media assets "
        "are represented by the governed brain metadata and are not injected as raw "
        "noise into this provider-readable corpus.\n",
        encoding="utf-8",
    )
    return manifest


def append_provider_readable_corpus(
    brain_root: str | Path,
    prompt_path: str | Path,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    prompt = Path(prompt_path)
    manifest_files: list[dict[str, Any]] = []
    with prompt.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\nPROVIDER_READABILITY_CONTRACT=" + READABILITY_CONTRACT + "\n")
        stream.write(CORPUS_BEGIN + "\n")
        for item in _indexed_files(root):
            text, encoding = _decode_provider_text(item["payload"])
            text, encoding = _provider_visible_text(text, encoding)
            relative = str(item["relative_path"])
            stream.write(
                "\n----- FILE "
                + f"lane={item['lane_id']} source={item['source_id']} path={relative} "
                + f"sha256={item['sha256']} encoding={encoding} -----\n"
            )
            stream.write(text)
            if not text.endswith("\n"):
                stream.write("\n")
            stream.write("----- END FILE -----\n")
            manifest_files.append(
                {
                    key: item[key]
                    for key in (
                        "lane_id",
                        "source_id",
                        "relative_path",
                        "byte_size",
                        "sha256",
                    )
                }
                | {"encoding": encoding}
            )
        stream.write(CORPUS_END + "\n")
    return {
        "contract": READABILITY_CONTRACT,
        "file_count": len(manifest_files),
        "total_exact_text_bytes": sum(int(item["byte_size"]) for item in manifest_files),
        "files": manifest_files,
        "prompt_path": str(prompt),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_text_if_present(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _deterministic_flat_zip(stage: Path, destination: Path) -> None:
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=False,
    ) as archive:
        for name in sorted(GEMINI_READABLE_NAMES):
            path = stage / name
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.extract_version = 20
            with path.open("rb") as source, archive.open(info, "w") as target:
                shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
    temporary.replace(destination)


def export_gemini_provider_readable(
    brain_root: str | Path,
    brain_name: str,
    flash_prompt: str,
    *,
    package_use_mode: str,
    chatgpt_package: str | Path | None,
    maximum_bytes: int,
) -> dict[str, Any]:
    # Local import avoids coupling the indexed-corpus reader to package policy.
    from sqlite_brain_builder.runtime.provider_package_projection import (
        clear_provider_skip_marker,
        remove_provider_stage,
        retire_provider_outputs,
        write_provider_skip_marker,
    )
    from sqlite_brain_builder.runtime.package_validation import validate_gemini_exact10

    root = Path(brain_root).resolve()
    packages = root / "packages"
    packages.mkdir(parents=True, exist_ok=True)
    retired = retire_provider_outputs(packages, "GEMINI")
    clear_provider_skip_marker(packages, "GEMINI")
    normalized_mode = str(package_use_mode or "CANONICAL_FLASHABLE").strip().upper()
    if normalized_mode not in {"CANONICAL_FLASHABLE", "READ_ONLY_STRESS_RESULT"}:
        raise ProviderReadableCorpusError(f"PACKAGE_USE_MODE_INVALID:{normalized_mode}")
    stage_variant = "read_only_stress_result" if normalized_mode == "READ_ONLY_STRESS_RESULT" else "current"
    stage = packages / (
        f"{_safe_component(brain_name, 'brain').lower()}_gemini_readable_{stage_variant}_v001"
    )
    stage.mkdir(parents=True)

    source_chatgpt_sha256 = ""
    if chatgpt_package:
        chatgpt_path = Path(chatgpt_package).resolve()
        if not chatgpt_path.is_file():
            raise ProviderReadableCorpusError(f"CHATGPT_SOURCE_PACKAGE_MISSING:{chatgpt_path}")
        source_chatgpt_sha256 = _sha256_file(chatgpt_path)

    corpus_path = stage / "PROJECT_CORPUS.txt"
    corpus_path.write_text(
        "EVIDENCE OS GEMINI PROVIDER-READABLE PROJECT CORPUS\n"
        f"PACKAGE_CONTRACT={GEMINI_READABLE_CONTRACT}\n"
        f"PACKAGE_USE_MODE={normalized_mode}\n",
        encoding="utf-8",
    )
    readability = append_provider_readable_corpus(root, corpus_path)
    (stage / "PROJECT_FILE_INDEX.json").write_text(
        json.dumps(readability, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    topology_path = root / "project" / "topology" / "project_master_topology.mmd"
    topology = _read_text_if_present(topology_path) or (
        "flowchart TD\n  ROOT[\"No rendered topology available; read PROJECT_FILE_INDEX.json\"]\n"
    )
    (stage / "PROJECT_TOPOLOGY.mmd").write_text(topology, encoding="utf-8")

    build_receipt = _read_text_if_present(root / "receipts" / "build_receipt.md")
    build_summary = {
        "contract": GEMINI_READABLE_CONTRACT,
        "brain_name": brain_name,
        "package_use_mode": normalized_mode,
        "source_chatgpt_package_sha256": source_chatgpt_sha256,
        "readable_file_count": readability["file_count"],
        "readable_exact_text_bytes": readability["total_exact_text_bytes"],
        "build_receipt_excerpt": build_receipt[:16_000],
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "provider_upload_performed": False,
    }
    (stage / "PROJECT_BUILD_SUMMARY.json").write_text(
        json.dumps(build_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (stage / "PROJECT_DELTA_SUMMARY.txt").write_text(
        "Evidence OS Delta authority remains in the immutable brain snapshot.\n"
        "This provider package is a readable snapshot projection; it does not authorize "
        "a Delta overlay, mutation, promotion, or provider upload.\n",
        encoding="utf-8",
    )
    env_governance = "\n\n".join(
        text
        for text in (
            _read_text_if_present(root / ".uepc_env"),
            _read_text_if_present(root / ".uepc_project"),
        )
        if text
    ) or "ENV15 immutable governance is carried by the canonical Evidence OS brain.\n"
    uop_governance = _read_text_if_present(root / ".uepc_profile") or (
        "UOP governance remains canonical in the Evidence OS brain; this projection is read-only.\n"
    )
    (stage / "ENV_GOVERNANCE.txt").write_text(env_governance, encoding="utf-8")
    (stage / "UOP_GOVERNANCE.txt").write_text(uop_governance, encoding="utf-8")
    (stage / "OPEN_ME_FIRST.txt").write_text(
        "EVIDENCE OS GEMINI PROVIDER-READABLE NORMAL ZIP\n"
        f"PACKAGE_CONTRACT={GEMINI_READABLE_CONTRACT}\n"
        f"PACKAGE_USE_MODE={normalized_mode}\n\n"
        + flash_prompt.rstrip()
        + "\n\nREAD ORDER\n"
        "1. PROJECT_FILE_INDEX.json\n"
        "2. PROJECT_CORPUS.txt\n"
        "3. PROJECT_TOPOLOGY.mmd\n"
        "4. PROJECT_BUILD_SUMMARY.json\n\n"
        "The project corpus is ordinary UTF-8 text reconstructed from the indexed exact-byte "
        "authority. No SQLite database or other opaque binary is included in this Gemini ZIP.\n",
        encoding="utf-8",
    )

    content_names = GEMINI_READABLE_NAMES[:8]
    manifest = {
        "contract": GEMINI_READABLE_CONTRACT,
        "package_use_mode": normalized_mode,
        "source_chatgpt_package_sha256": source_chatgpt_sha256,
        "provider_upload_performed": False,
        "files": [
            {
                "name": name,
                "byte_size": (stage / name).stat().st_size,
                "sha256": _sha256_file(stage / name),
                "media_type": "application/json" if name.endswith(".json") else "text/plain; charset=utf-8",
            }
            for name in content_names
        ],
    }
    (stage / "PACKAGE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    sums_names = GEMINI_READABLE_NAMES[:9]
    (stage / "SHA256SUMS.txt").write_text(
        "".join(f"{_sha256_file(stage / name)}  {name}\n" for name in sums_names),
        encoding="utf-8",
    )
    observed = tuple(sorted(path.name for path in stage.iterdir() if path.is_file()))
    if observed != tuple(sorted(GEMINI_READABLE_NAMES)):
        raise ProviderReadableCorpusError(f"GEMINI_READABLE_STAGE_CONTENT_MISMATCH:{observed!r}")

    suffix = "_Read_Only_Stress_Result" if normalized_mode == "READ_ONLY_STRESS_RESULT" else ""
    archive_path = packages / (
        f"Gemini_{_safe_component(brain_name, 'brain')}{suffix}_Readable_Project.zip"
    )
    _deterministic_flat_zip(stage, archive_path)
    observed_bytes = archive_path.stat().st_size
    if observed_bytes > int(maximum_bytes):
        retire_provider_outputs(packages, "GEMINI")
        skipped = write_provider_skip_marker(
            packages,
            "GEMINI",
            observed_bytes=observed_bytes,
            maximum_bytes=int(maximum_bytes),
            reason="GEMINI_READABLE_ARCHIVE_EXCEEDS_EXACT_100000000_BYTE_LIMIT",
        )
        return {
            **skipped,
            "gemini_package_zip": "",
            "package_folder": "",
            "sha256": "",
            "package_byte_size": observed_bytes,
            "package_use_mode": normalized_mode,
            "package_contract": GEMINI_READABLE_CONTRACT,
            "source_chatgpt_package_sha256": source_chatgpt_sha256,
            "provider_readability": readability,
            "retired_provider_outputs": retired,
            "staging_removed": True,
            "staging_path": str(stage),
        }
    validation = validate_gemini_exact10(archive_path)
    if validation.get("status") != "PASS":
        raise ProviderReadableCorpusError(
            "GEMINI_PROVIDER_READABLE_VALIDATION_FAILED:"
            + ";".join(validation.get("errors") or [])
        )
    remove_provider_stage(stage)
    return {
        "status": "PASS",
        "gemini_package_zip": str(archive_path),
        "package_folder": "",
        "sha256": _sha256_file(archive_path),
        "package_byte_size": observed_bytes,
        "provider_size_limit_bytes": int(maximum_bytes),
        "package_use_mode": normalized_mode,
        "package_contract": GEMINI_READABLE_CONTRACT,
        "source_chatgpt_package_sha256": source_chatgpt_sha256,
        "provider_readability": readability,
        "retired_provider_outputs": retired,
        "validation": validation,
        "staging_removed": True,
        "staging_path": str(stage),
    }


__all__ = [
    "CORPUS_BEGIN",
    "CORPUS_END",
    "GEMINI_READABLE_CONTRACT",
    "GEMINI_READABLE_NAMES",
    "READABILITY_CONTRACT",
    "ProviderReadableCorpusError",
    "append_provider_readable_corpus",
    "export_gemini_provider_readable",
    "materialize_provider_readable_tree",
]
