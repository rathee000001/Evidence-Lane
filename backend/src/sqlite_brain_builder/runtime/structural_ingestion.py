"""T021 structural ingestion for the four governed intake lanes.

The module is deliberately isolated from ``stable_runtime_v53`` and the IPC
worker.  It never mutates an input source, never extracts an archive into the
source tree, and never falls back to ``custom``.  All entity IDs are derived
from stable source facts and all writes use uniqueness constraints plus
``INSERT OR IGNORE`` so replaying identical input adds no base or FTS rows.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence

from sqlite_brain_builder.runtime.canonical_lanes import (
    LANE_REGISTRY,
    UnknownLaneAliasError,
    resolve_lane_id,
)


SUPPORTED_STRUCTURAL_LANES = (
    "brain_loader",
    "research",
    "project_engulf",
    "sqlite_brain",
)

SQLITE_MAGIC = b"SQLite format 3\x00"
READ_CHUNK_BYTES = 1024 * 1024
TEXT_EXTENSIONS = {
    ".adoc", ".cfg", ".conf", ".css", ".csv", ".env", ".html", ".ini",
    ".js", ".json", ".jsonl", ".jsx", ".md", ".mmd", ".py", ".rst",
    ".rs", ".sql", ".svg", ".toml", ".ts", ".tsx", ".txt", ".xml",
    ".yaml", ".yml",
}
CODE_EXTENSIONS = {
    ".bat", ".c", ".cfg", ".cjs", ".cmd", ".cpp", ".cs", ".css", ".go",
    ".h", ".hpp", ".html", ".ini", ".java", ".js", ".jsx", ".kt", ".mjs",
    ".php", ".ps1", ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".swift",
    ".toml", ".ts", ".tsx", ".vue", ".yaml", ".yml",
}


class StructuralIngestionError(RuntimeError):
    """Base exception for safe structural ingestion failures."""


class UnsupportedStructuralLaneError(StructuralIngestionError):
    """The dispatcher received a lane outside the four governed lanes."""


class UnsafeSourceError(StructuralIngestionError):
    """The source or destination violates the read-only source boundary."""


class UnsafeArchiveError(UnsafeSourceError):
    """A ZIP contains unsafe paths, links, encryption, or bomb-like sizes."""


class SourceChangedDuringIngestionError(StructuralIngestionError):
    """Input bytes changed while the transaction was running."""


class ReadOnlyQueryError(StructuralIngestionError):
    """A query is not permitted by the SQLite read-only interface."""


@dataclass(frozen=True, slots=True)
class InspectionLimits:
    max_files: int = 20_000
    max_archive_members: int = 10_000
    max_member_bytes: int = 512 * 1024 * 1024
    max_total_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: float = 1_000.0
    max_text_bytes: int = 4 * 1024 * 1024
    max_sqlite_bytes: int = 1024 * 1024 * 1024
    text_chunk_lines: int = 120
    max_query_rows: int = 1_000


@dataclass(frozen=True, slots=True)
class SourceItem:
    logical_path: str
    physical_path: Path | None
    archive_path: Path | None
    archive_member_name: str | None
    item_kind: str
    size_bytes: int
    sha256: str
    is_sqlite: bool
    is_archive: bool
    is_symlink: bool
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class SourceInspection:
    source_path: Path
    source_kind: str
    display_name: str
    sha256: str
    size_bytes: int
    items: tuple[SourceItem, ...]
    archive_crc_status: str | None


@dataclass(frozen=True, slots=True)
class SQLiteObject:
    object_type: str
    name: str
    table_name: str
    rootpage: int
    sql: str | None
    schema_sha256: str
    columns: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SQLiteTableStat:
    object_name: str
    object_type: str
    row_count: int | None
    count_error: str | None


@dataclass(frozen=True, slots=True)
class SQLiteForeignKey:
    table_name: str
    fk_id: int
    sequence: int
    target_table: str
    from_column: str | None
    to_column: str | None
    on_update: str | None
    on_delete: str | None
    match: str | None


@dataclass(frozen=True, slots=True)
class SQLiteInspection:
    logical_path: str
    sha256: str
    size_bytes: int
    sqlite_version: str
    integrity_check: tuple[str, ...]
    foreign_key_violations: tuple[Mapping[str, Any], ...]
    schema_hash: str
    objects: tuple[SQLiteObject, ...]
    table_stats: tuple[SQLiteTableStat, ...]
    foreign_keys: tuple[SQLiteForeignKey, ...]
    fts_tables: tuple[str, ...]
    error: str | None

    @property
    def is_healthy(self) -> bool:
        return self.error is None and self.integrity_check == ("ok",) and not self.foreign_key_violations

    def receipt_payload(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "sqlite_version": self.sqlite_version,
            "integrity_check": list(self.integrity_check),
            "foreign_key_violation_count": len(self.foreign_key_violations),
            "foreign_key_violation_sample": list(self.foreign_key_violations[:20]),
            "schema_hash": self.schema_hash,
            "schema_object_count": len(self.objects),
            "fts_tables": list(self.fts_tables),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class IngestionReceipt:
    lane_id: str
    run_id: str
    source_id: str
    source_sha256: str
    sector_database: Path
    source_preserved: bool
    before_counts: Mapping[str, int]
    after_counts: Mapping[str, int]
    growth: Mapping[str, int]
    source_sqlite_receipts: tuple[Mapping[str, Any], ...]
    output_integrity_check: tuple[str, ...]
    output_foreign_key_violations: tuple[Mapping[str, Any], ...]
    status: str


@dataclass(frozen=True, slots=True)
class ReadOnlyQueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_id(prefix: str, *parts: Any) -> str:
    material = "\x1f".join("" if part is None else str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def _hash_stream(stream: BinaryIO, *, limit: int | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while True:
        block = stream.read(READ_CHUNK_BYTES)
        if not block:
            break
        total += len(block)
        if limit is not None and total > limit:
            raise UnsafeSourceError(f"Source exceeds safe read limit of {limit} bytes")
        digest.update(block)
    return digest.hexdigest(), total


def _hash_file(path: Path, *, limit: int | None = None) -> tuple[str, int]:
    with path.open("rb") as stream:
        return _hash_stream(stream, limit=limit)


def _read_header(path: Path, size: int = 16) -> bytes:
    with path.open("rb") as stream:
        return stream.read(size)


def _normalize_zip_member(name: str) -> str:
    if "\x00" in name:
        raise UnsafeArchiveError("ZIP member contains a NUL byte")
    candidate = name.replace("\\", "/")
    if candidate.startswith(("/", "//")) or re.match(r"^[A-Za-z]:", candidate):
        raise UnsafeArchiveError(f"ZIP member uses an absolute path: {name!r}")
    parts = PurePosixPath(candidate).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafeArchiveError(f"ZIP member uses an unsafe path: {name!r}")
    normalized = "/".join(parts)
    if normalized.startswith("../") or "/../" in normalized:
        raise UnsafeArchiveError(f"ZIP member escapes its root: {name!r}")
    return normalized


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(unix_mode) == stat.S_IFLNK


def _zip_member_header(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with zf.open(info, "r") as stream:
        return stream.read(16)


def _inspect_zip(path: Path, limits: InspectionLimits) -> SourceInspection:
    archive_hash, archive_size = _hash_file(path)
    items: list[SourceItem] = []
    normalized_names: dict[str, str] = {}
    total_uncompressed = 0
    with zipfile.ZipFile(path, "r") as zf:
        file_infos = [info for info in zf.infolist() if not info.is_dir()]
        if len(file_infos) > limits.max_archive_members:
            raise UnsafeArchiveError(
                f"ZIP contains {len(file_infos)} files; limit is {limits.max_archive_members}"
            )
        for info in sorted(file_infos, key=lambda row: row.filename.casefold()):
            normalized = _normalize_zip_member(info.filename)
            collision_key = normalized.casefold()
            previous = normalized_names.get(collision_key)
            if previous is not None:
                raise UnsafeArchiveError(
                    f"ZIP has duplicate/case-colliding members: {previous!r}, {info.filename!r}"
                )
            normalized_names[collision_key] = info.filename
            if info.flag_bits & 0x1:
                raise UnsafeArchiveError(f"Encrypted ZIP member is not inspectable: {info.filename!r}")
            if _zip_member_is_symlink(info):
                raise UnsafeArchiveError(f"ZIP symlink member is not allowed: {info.filename!r}")
            if info.file_size > limits.max_member_bytes:
                raise UnsafeArchiveError(
                    f"ZIP member {info.filename!r} is {info.file_size} bytes; limit is {limits.max_member_bytes}"
                )
            total_uncompressed += info.file_size
            if total_uncompressed > limits.max_total_uncompressed_bytes:
                raise UnsafeArchiveError("ZIP declared uncompressed size exceeds the configured limit")
            ratio = info.file_size / max(1, info.compress_size)
            if info.file_size > 1024 * 1024 and ratio > limits.max_compression_ratio:
                raise UnsafeArchiveError(
                    f"ZIP member {info.filename!r} compression ratio {ratio:.1f} exceeds limit"
                )
            header = _zip_member_header(zf, info)
            with zf.open(info, "r") as stream:
                digest, actual_size = _hash_stream(stream, limit=limits.max_member_bytes)
            if actual_size != info.file_size:
                raise UnsafeArchiveError(
                    f"ZIP member size mismatch for {info.filename!r}: {actual_size} != {info.file_size}"
                )
            suffix = PurePosixPath(normalized).suffix.casefold()
            items.append(
                SourceItem(
                    logical_path=normalized,
                    physical_path=None,
                    archive_path=path,
                    archive_member_name=info.filename,
                    item_kind="sqlite" if header == SQLITE_MAGIC else "archive_member",
                    size_bytes=actual_size,
                    sha256=digest,
                    is_sqlite=header == SQLITE_MAGIC,
                    is_archive=suffix == ".zip" or header.startswith(b"PK\x03\x04"),
                    is_symlink=False,
                    metadata={
                        "crc32": f"{info.CRC:08x}",
                        "compressed_size": info.compress_size,
                        "compress_type": info.compress_type,
                    },
                )
            )
    return SourceInspection(
        source_path=path,
        source_kind="zip",
        display_name=path.name,
        sha256=archive_hash,
        size_bytes=archive_size,
        items=tuple(items),
        archive_crc_status="PASS",
    )


def _directory_items(root: Path, limits: InspectionLimits) -> tuple[SourceItem, ...]:
    items: list[SourceItem] = []

    def visit(directory: Path, relative: PurePosixPath) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise UnsafeSourceError(f"Cannot enumerate source directory {directory}: {exc}") from exc
        for entry in entries:
            logical = (relative / entry.name).as_posix()
            if entry.is_symlink():
                target = os.readlink(entry.path)
                payload = target.encode("utf-8", errors="surrogatepass")
                items.append(
                    SourceItem(
                        logical_path=logical,
                        physical_path=Path(entry.path),
                        archive_path=None,
                        archive_member_name=None,
                        item_kind="symlink",
                        size_bytes=len(payload),
                        sha256=_sha256_bytes(payload),
                        is_sqlite=False,
                        is_archive=False,
                        is_symlink=True,
                        metadata={"link_target": target, "followed": False},
                    )
                )
            elif entry.is_dir(follow_symlinks=False):
                visit(Path(entry.path), relative / entry.name)
            elif entry.is_file(follow_symlinks=False):
                path = Path(entry.path)
                digest, size = _hash_file(path)
                header = _read_header(path)
                suffix = path.suffix.casefold()
                items.append(
                    SourceItem(
                        logical_path=logical,
                        physical_path=path,
                        archive_path=None,
                        archive_member_name=None,
                        item_kind="sqlite" if header == SQLITE_MAGIC else "file",
                        size_bytes=size,
                        sha256=digest,
                        is_sqlite=header == SQLITE_MAGIC,
                        is_archive=suffix == ".zip" or header.startswith(b"PK\x03\x04"),
                        is_symlink=False,
                        metadata={},
                    )
                )
            else:
                raise UnsafeSourceError(f"Unsupported filesystem entry: {entry.path}")
            if len(items) > limits.max_files:
                raise UnsafeSourceError(f"Source exceeds the {limits.max_files}-file inspection limit")

    visit(root, PurePosixPath())
    return tuple(items)


def inspect_source_structure(
    source_path: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> SourceInspection:
    """Safely inventory a ZIP, directory, SQLite database, or ordinary file."""

    limits = limits or InspectionLimits()
    requested_path = Path(source_path).expanduser()
    if requested_path.is_symlink():
        raise UnsafeSourceError(f"Top-level source symlinks are not allowed: {requested_path}")
    path = requested_path.resolve(strict=True)
    # Office Open XML files are ZIP containers internally, but their package
    # members are not independent user sources. Preserve the file boundary so
    # the DOCX/XLSX/XLSM/PPTX structural parser receives the actual document.
    office_open_xml = {".docx", ".xlsx", ".xlsm", ".pptx"}
    if path.is_file() and path.suffix.casefold() not in office_open_xml and zipfile.is_zipfile(path):
        return _inspect_zip(path, limits)
    if path.is_dir():
        items = _directory_items(path, limits)
        tree_material = "".join(
            f"{item.logical_path}\0{item.item_kind}\0{item.sha256}\0{item.size_bytes}\n"
            for item in items
        ).encode("utf-8")
        return SourceInspection(
            source_path=path,
            source_kind="directory",
            display_name=path.name,
            sha256=_sha256_bytes(tree_material),
            size_bytes=sum(item.size_bytes for item in items),
            items=items,
            archive_crc_status=None,
        )
    if path.is_file():
        digest, size = _hash_file(path)
        header = _read_header(path)
        item = SourceItem(
            logical_path=path.name,
            physical_path=path,
            archive_path=None,
            archive_member_name=None,
            item_kind="sqlite" if header == SQLITE_MAGIC else "file",
            size_bytes=size,
            sha256=digest,
            is_sqlite=header == SQLITE_MAGIC,
            is_archive=path.suffix.casefold() == ".zip" or header.startswith(b"PK\x03\x04"),
            is_symlink=False,
            metadata={},
        )
        return SourceInspection(path, "sqlite" if item.is_sqlite else "file", path.name, digest, size, (item,), None)
    raise UnsafeSourceError(f"Source must be a regular file or directory: {path}")


@contextlib.contextmanager
def _open_item(item: SourceItem) -> Iterator[BinaryIO]:
    if item.is_symlink:
        raise UnsafeSourceError(f"Symlink content is not read: {item.logical_path}")
    if item.archive_path is not None and item.archive_member_name is not None:
        with zipfile.ZipFile(item.archive_path, "r") as zf:
            with zf.open(item.archive_member_name, "r") as stream:
                yield stream
        return
    if item.physical_path is None:
        raise UnsafeSourceError(f"No readable location for {item.logical_path}")
    with item.physical_path.open("rb") as stream:
        yield stream


def _read_item_bytes(item: SourceItem, *, limit: int) -> bytes:
    if item.size_bytes > limit:
        raise UnsafeSourceError(
            f"{item.logical_path} is {item.size_bytes} bytes; read limit is {limit}"
        )
    with _open_item(item) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise UnsafeSourceError(f"{item.logical_path} exceeded the read limit")
    if _sha256_bytes(data) != item.sha256:
        raise SourceChangedDuringIngestionError(f"Source item changed: {item.logical_path}")
    return data


def _read_item_text(item: SourceItem, limits: InspectionLimits) -> str | None:
    if item.is_symlink or item.is_sqlite or item.size_bytes > limits.max_text_bytes:
        return None
    suffix = PurePosixPath(item.logical_path).suffix.casefold()
    if suffix not in TEXT_EXTENSIONS and suffix not in {""}:
        return None
    data = _read_item_bytes(item, limit=limits.max_text_bytes)
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            text = data.decode(encoding)
            if "\x00" not in text[:4096]:
                return text
        except UnicodeDecodeError:
            continue
    return None


def read_source_item_text(
    item: SourceItem,
    *,
    limits: InspectionLimits | None = None,
) -> str | None:
    """Public bounded text reader for an already verified source item."""

    return _read_item_text(item, limits or InspectionLimits())


@contextlib.contextmanager
def _materialize_sqlite_item(item: SourceItem, limits: InspectionLimits) -> Iterator[Path]:
    if not item.is_sqlite:
        raise StructuralIngestionError(f"Not a SQLite item: {item.logical_path}")
    if item.size_bytes > limits.max_sqlite_bytes:
        raise UnsafeSourceError(
            f"SQLite item {item.logical_path} exceeds {limits.max_sqlite_bytes} bytes"
        )
    if item.physical_path is not None and item.archive_path is None:
        yield item.physical_path
        return
    with tempfile.TemporaryDirectory(prefix="evidenceos_sqlite_inspect_") as temp_dir:
        temp_path = Path(temp_dir) / "inspection.sqlite"
        with _open_item(item) as source, temp_path.open("wb") as destination:
            copied = 0
            while True:
                block = source.read(READ_CHUNK_BYTES)
                if not block:
                    break
                copied += len(block)
                if copied > limits.max_sqlite_bytes:
                    raise UnsafeSourceError(f"SQLite item grew past safe limit: {item.logical_path}")
                destination.write(block)
        digest, size = _hash_file(temp_path)
        if digest != item.sha256 or size != item.size_bytes:
            raise SourceChangedDuringIngestionError(f"SQLite archive member changed: {item.logical_path}")
        yield temp_path


def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _row_mapping(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _inspect_sqlite_path(path: Path, logical_path: str, digest: str, size_bytes: int) -> SQLiteInspection:
    objects: list[SQLiteObject] = []
    stats: list[SQLiteTableStat] = []
    foreign_keys: list[SQLiteForeignKey] = []
    fts_tables: list[str] = []
    integrity: tuple[str, ...] = ()
    violations: tuple[Mapping[str, Any], ...] = ()
    schema_hash = _sha256_bytes(b"")
    error: str | None = None
    version = sqlite3.sqlite_version
    connection: sqlite3.Connection | None = None
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall())
        raw_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        violations = tuple(
            {
                "table": row[0],
                "rowid": row[1],
                "parent": row[2],
                "fkid": row[3],
            }
            for row in raw_violations
        )
        schema_rows = connection.execute(
            "SELECT type,name,tbl_name,rootpage,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()
        schema_material: list[str] = []
        for row in schema_rows:
            sql = row["sql"]
            schema_material.append(
                f"{row['type']}\0{row['name']}\0{row['tbl_name']}\0{sql or ''}\n"
            )
            columns: tuple[Mapping[str, Any], ...] = ()
            if row["type"] in {"table", "view"}:
                try:
                    columns = tuple(
                        _row_mapping(column)
                        for column in connection.execute(f"PRAGMA table_xinfo({_qident(row['name'])})").fetchall()
                    )
                except sqlite3.DatabaseError:
                    columns = ()
                try:
                    count = int(connection.execute(f"SELECT count(*) FROM {_qident(row['name'])}").fetchone()[0])
                    stats.append(SQLiteTableStat(row["name"], row["type"], count, None))
                except sqlite3.DatabaseError as exc:
                    stats.append(SQLiteTableStat(row["name"], row["type"], None, str(exc)))
                if row["type"] == "table":
                    try:
                        for fk in connection.execute(f"PRAGMA foreign_key_list({_qident(row['name'])})").fetchall():
                            foreign_keys.append(
                                SQLiteForeignKey(
                                    table_name=row["name"],
                                    fk_id=int(fk["id"]),
                                    sequence=int(fk["seq"]),
                                    target_table=fk["table"],
                                    from_column=fk["from"],
                                    to_column=fk["to"],
                                    on_update=fk["on_update"],
                                    on_delete=fk["on_delete"],
                                    match=fk["match"],
                                )
                            )
                    except sqlite3.DatabaseError:
                        pass
            if sql and re.search(r"\busing\s+fts[345]\b", sql, flags=re.IGNORECASE):
                fts_tables.append(row["name"])
            objects.append(
                SQLiteObject(
                    object_type=row["type"],
                    name=row["name"],
                    table_name=row["tbl_name"],
                    rootpage=int(row["rootpage"] or 0),
                    sql=sql,
                    schema_sha256=_sha256_bytes((sql or "").encode("utf-8")),
                    columns=columns,
                )
            )
        schema_hash = _sha256_bytes("".join(schema_material).encode("utf-8"))
    except sqlite3.DatabaseError as exc:
        error = str(exc)
        if not integrity:
            integrity = (f"error:{exc}",)
    finally:
        if connection is not None:
            connection.close()
    return SQLiteInspection(
        logical_path=logical_path,
        sha256=digest,
        size_bytes=size_bytes,
        sqlite_version=version,
        integrity_check=integrity,
        foreign_key_violations=violations,
        schema_hash=schema_hash,
        objects=tuple(objects),
        table_stats=tuple(stats),
        foreign_keys=tuple(foreign_keys),
        fts_tables=tuple(sorted(set(fts_tables))),
        error=error,
    )


def _inspect_sqlite_item(item: SourceItem, limits: InspectionLimits) -> SQLiteInspection:
    with _materialize_sqlite_item(item, limits) as path:
        return _inspect_sqlite_path(path, item.logical_path, item.sha256, item.size_bytes)


def inspect_sqlite_database(database_path: str | Path) -> SQLiteInspection:
    """Inspect one SQLite file read-only and return integrity/FK/schema facts."""

    inspection = inspect_source_structure(database_path)
    if len(inspection.items) != 1 or not inspection.items[0].is_sqlite:
        raise StructuralIngestionError(f"Not a standalone SQLite database: {database_path}")
    return _inspect_sqlite_item(inspection.items[0], InspectionLimits())


def _source_fingerprint(path: Path, limits: InspectionLimits) -> str:
    return inspect_source_structure(path, limits=limits).sha256


def _assert_safe_destination(source: Path, destination: Path) -> None:
    source_resolved = source.resolve(strict=True)
    destination_resolved = destination.expanduser().resolve(strict=False)
    if source_resolved == destination_resolved:
        raise UnsafeSourceError("Sector database cannot replace the input source")
    if source_resolved.is_dir():
        try:
            destination_resolved.relative_to(source_resolved)
        except ValueError:
            pass
        else:
            raise UnsafeSourceError("Sector database cannot be written inside the input project")


COMMON_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS structural_meta(
  meta_key TEXT PRIMARY KEY,
  meta_value TEXT NOT NULL
);
"""


def _fts_schema(prefix: str) -> str:
    return f"""
CREATE TABLE IF NOT EXISTS {prefix}_fts_document(
  document_id TEXT PRIMARY KEY,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  locator TEXT NOT NULL,
  UNIQUE(entity_kind,entity_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS {prefix}_fts USING fts5(
  entity_kind UNINDEXED,
  entity_id UNINDEXED,
  title,
  body,
  locator,
  content='{prefix}_fts_document',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS {prefix}_fts_document_ai AFTER INSERT ON {prefix}_fts_document BEGIN
  INSERT INTO {prefix}_fts(rowid,entity_kind,entity_id,title,body,locator)
  VALUES(new.rowid,new.entity_kind,new.entity_id,new.title,new.body,new.locator);
END;
CREATE TRIGGER IF NOT EXISTS {prefix}_fts_document_ad AFTER DELETE ON {prefix}_fts_document BEGIN
  INSERT INTO {prefix}_fts({prefix}_fts,rowid,entity_kind,entity_id,title,body,locator)
  VALUES('delete',old.rowid,old.entity_kind,old.entity_id,old.title,old.body,old.locator);
END;
CREATE TRIGGER IF NOT EXISTS {prefix}_fts_document_au AFTER UPDATE ON {prefix}_fts_document BEGIN
  INSERT INTO {prefix}_fts({prefix}_fts,rowid,entity_kind,entity_id,title,body,locator)
  VALUES('delete',old.rowid,old.entity_kind,old.entity_id,old.title,old.body,old.locator);
  INSERT INTO {prefix}_fts(rowid,entity_kind,entity_id,title,body,locator)
  VALUES(new.rowid,new.entity_kind,new.entity_id,new.title,new.body,new.locator);
END;
"""


RECEIPT_COLUMNS = """
  receipt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  source_id TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  source_preserved INTEGER NOT NULL CHECK(source_preserved IN (0,1)),
  source_sqlite_receipts_json TEXT NOT NULL,
  entity_growth_json TEXT NOT NULL,
  output_integrity_json TEXT NOT NULL,
  output_fk_violation_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_utc TEXT NOT NULL
"""


BRAIN_LOADER_SCHEMA = COMMON_SCHEMA + """
CREATE TABLE IF NOT EXISTS brain_loader_source(
  source_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  display_name TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  item_count INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(display_name,sha256)
);
CREATE TABLE IF NOT EXISTS brain_loader_package(
  package_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES brain_loader_source(source_id),
  package_class TEXT NOT NULL,
  member_count INTEGER NOT NULL,
  manifest_count INTEGER NOT NULL,
  pointer_count INTEGER NOT NULL,
  sqlite_count INTEGER NOT NULL,
  source_brain_identity TEXT,
  schema_version TEXT,
  compatibility_status TEXT NOT NULL,
  import_decision TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(source_id,package_class)
);
CREATE TABLE IF NOT EXISTS brain_loader_member(
  member_id TEXT PRIMARY KEY,
  package_id TEXT NOT NULL REFERENCES brain_loader_package(package_id),
  logical_path TEXT NOT NULL,
  item_kind TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  is_manifest INTEGER NOT NULL,
  is_pointer INTEGER NOT NULL,
  is_sqlite INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(package_id,logical_path)
);
CREATE TABLE IF NOT EXISTS brain_loader_database(
  database_id TEXT PRIMARY KEY,
  member_id TEXT NOT NULL REFERENCES brain_loader_member(member_id),
  schema_hash TEXT NOT NULL,
  integrity_json TEXT NOT NULL,
  fk_violation_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  fts_tables_json TEXT NOT NULL,
  compatibility_status TEXT NOT NULL,
  UNIQUE(member_id)
);
CREATE TABLE IF NOT EXISTS brain_loader_schema_object(
  object_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES brain_loader_database(database_id),
  object_type TEXT NOT NULL,
  object_name TEXT NOT NULL,
  table_name TEXT NOT NULL,
  schema_sql TEXT,
  schema_sha256 TEXT NOT NULL,
  UNIQUE(database_id,object_type,object_name)
);
CREATE TABLE IF NOT EXISTS brain_loader_relationship(
  relationship_id TEXT PRIMARY KEY,
  from_kind TEXT NOT NULL,
  from_id TEXT NOT NULL,
  to_kind TEXT NOT NULL,
  to_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  locator TEXT NOT NULL,
  UNIQUE(from_kind,from_id,to_kind,to_id,relation)
);
CREATE TABLE IF NOT EXISTS brain_loader_receipt(
""" + RECEIPT_COLUMNS + """
);
""" + _fts_schema("brain_loader")


RESEARCH_SCHEMA = COMMON_SCHEMA + """
CREATE TABLE IF NOT EXISTS research_source(
  source_id TEXT PRIMARY KEY,
  root_source_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  logical_path TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(root_source_id,logical_path,sha256)
);
""" + "\n".join(
    f"""
CREATE TABLE IF NOT EXISTS research_{kind}(
  {kind}_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES research_source(source_id),
  ordinal INTEGER NOT NULL,
  content TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  line_number INTEGER,
  locator TEXT NOT NULL,
  UNIQUE(source_id,ordinal,content_sha256)
);"""
    for kind in (
        "question", "hypothesis", "method", "evidence", "finding", "limitation",
        "citation", "open_question",
    )
) + """
CREATE TABLE IF NOT EXISTS research_receipt(
""" + RECEIPT_COLUMNS + """
);
""" + _fts_schema("research")


PROJECT_ENGULF_SCHEMA = COMMON_SCHEMA + """
CREATE TABLE IF NOT EXISTS project_engulf_source(
  source_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  display_name TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  item_count INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(display_name,sha256)
);
CREATE TABLE IF NOT EXISTS project_engulf_origin(
  origin_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES project_engulf_source(source_id),
  origin_kind TEXT NOT NULL,
  origin_locator TEXT NOT NULL,
  origin_sha256 TEXT NOT NULL,
  UNIQUE(source_id)
);
CREATE TABLE IF NOT EXISTS project_engulf_package_identity(
  identity_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES project_engulf_source(source_id),
  package_name TEXT NOT NULL,
  package_version TEXT,
  identity_source TEXT NOT NULL,
  identity_sha256 TEXT NOT NULL,
  UNIQUE(source_id,package_name,identity_source)
);
CREATE TABLE IF NOT EXISTS project_engulf_file(
  file_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES project_engulf_source(source_id),
  logical_path TEXT NOT NULL,
  file_kind TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  is_symlink INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(source_id,logical_path)
);
CREATE TABLE IF NOT EXISTS project_engulf_chunk(
  chunk_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES project_engulf_file(file_id),
  ordinal INTEGER NOT NULL,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL,
  chunk_text TEXT NOT NULL,
  chunk_sha256 TEXT NOT NULL,
  locator TEXT NOT NULL,
  UNIQUE(file_id,ordinal,chunk_sha256)
);
CREATE TABLE IF NOT EXISTS project_engulf_component(
  component_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES project_engulf_file(file_id),
  component_type TEXT NOT NULL,
  component_name TEXT NOT NULL,
  locator TEXT NOT NULL,
  UNIQUE(file_id,component_type)
);
CREATE TABLE IF NOT EXISTS project_engulf_relationship(
  relationship_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES project_engulf_source(source_id),
  from_path TEXT NOT NULL,
  to_path TEXT NOT NULL,
  relation TEXT NOT NULL,
  UNIQUE(source_id,from_path,to_path,relation)
);
CREATE TABLE IF NOT EXISTS project_engulf_schema_mapping(
  mapping_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES project_engulf_file(file_id),
  source_object TEXT NOT NULL,
  source_object_type TEXT NOT NULL,
  proposed_lane_id TEXT NOT NULL,
  mapping_status TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  UNIQUE(file_id,source_object_type,source_object)
);
CREATE TABLE IF NOT EXISTS project_engulf_sector_target(
  target_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES project_engulf_file(file_id),
  canonical_lane_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  UNIQUE(file_id)
);
CREATE TABLE IF NOT EXISTS project_engulf_conflict(
  conflict_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES project_engulf_file(file_id),
  conflict_code TEXT NOT NULL,
  severity TEXT NOT NULL,
  disposition TEXT NOT NULL,
  details TEXT NOT NULL,
  UNIQUE(file_id,conflict_code)
);
CREATE TABLE IF NOT EXISTS project_engulf_object_decision(
  decision_id TEXT PRIMARY KEY,
  file_id TEXT NOT NULL REFERENCES project_engulf_file(file_id),
  object_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  UNIQUE(file_id)
);
CREATE TABLE IF NOT EXISTS project_engulf_run(
  run_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES project_engulf_source(source_id),
  accepted_count INTEGER NOT NULL,
  skipped_count INTEGER NOT NULL,
  blocked_count INTEGER NOT NULL,
  status TEXT NOT NULL,
  UNIQUE(source_id)
);
CREATE TABLE IF NOT EXISTS project_engulf_topology_update(
  update_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES project_engulf_run(run_id),
  update_status TEXT NOT NULL,
  node_count INTEGER NOT NULL,
  details_json TEXT NOT NULL,
  UNIQUE(run_id)
);
CREATE TABLE IF NOT EXISTS project_engulf_receipt(
""" + RECEIPT_COLUMNS + """
);
""" + _fts_schema("project_engulf")


SQLITE_BRAIN_SCHEMA = COMMON_SCHEMA + """
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_source(
  source_id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  display_name TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  item_count INTEGER NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(display_name,sha256)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_database(
  database_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_source(source_id),
  logical_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sqlite_version TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  object_count INTEGER NOT NULL,
  table_count INTEGER NOT NULL,
  view_count INTEGER NOT NULL,
  index_count INTEGER NOT NULL,
  trigger_count INTEGER NOT NULL,
  UNIQUE(source_id,logical_path,sha256)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_schema_object(
  object_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  object_type TEXT NOT NULL,
  object_name TEXT NOT NULL,
  table_name TEXT NOT NULL,
  rootpage INTEGER NOT NULL,
  schema_sql TEXT,
  schema_sha256 TEXT NOT NULL,
  columns_json TEXT NOT NULL,
  UNIQUE(database_id,object_type,object_name)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_table_stat(
  stat_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  object_name TEXT NOT NULL,
  object_type TEXT NOT NULL,
  row_count INTEGER,
  count_error TEXT,
  UNIQUE(database_id,object_type,object_name)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_foreign_key(
  foreign_key_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  table_name TEXT NOT NULL,
  fk_id INTEGER NOT NULL,
  sequence INTEGER NOT NULL,
  target_table TEXT NOT NULL,
  from_column TEXT,
  to_column TEXT,
  on_update TEXT,
  on_delete TEXT,
  match_rule TEXT,
  UNIQUE(database_id,table_name,fk_id,sequence)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_fts_table(
  fts_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  table_name TEXT NOT NULL,
  UNIQUE(database_id,table_name)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_integrity_result(
  integrity_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  integrity_json TEXT NOT NULL,
  fk_violation_count INTEGER NOT NULL,
  fk_violation_sample_json TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  UNIQUE(database_id)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_package_pointer(
  pointer_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_source(source_id),
  pointer_path TEXT NOT NULL,
  target_database_path TEXT NOT NULL,
  relation TEXT NOT NULL,
  UNIQUE(source_id,pointer_path,target_database_path)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_relationship(
  relationship_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  from_object TEXT NOT NULL,
  to_object TEXT NOT NULL,
  relation TEXT NOT NULL,
  UNIQUE(database_id,from_object,to_object,relation)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_compatibility(
  compatibility_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  compatibility_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  UNIQUE(database_id)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_sector_mapping(
  mapping_id TEXT PRIMARY KEY,
  database_id TEXT NOT NULL REFERENCES loaded_sqlite_brain_database(database_id),
  source_object TEXT NOT NULL,
  proposed_lane_id TEXT NOT NULL,
  mapping_status TEXT NOT NULL,
  UNIQUE(database_id,source_object)
);
CREATE TABLE IF NOT EXISTS loaded_sqlite_brain_receipt(
""" + RECEIPT_COLUMNS + """
);
""" + _fts_schema("loaded_sqlite_brain")


LANE_SCHEMAS = {
    "brain_loader": BRAIN_LOADER_SCHEMA,
    "research": RESEARCH_SCHEMA,
    "project_engulf": PROJECT_ENGULF_SCHEMA,
    "sqlite_brain": SQLITE_BRAIN_SCHEMA,
}

LANE_RECEIPT_TABLES = {
    "brain_loader": "brain_loader_receipt",
    "research": "research_receipt",
    "project_engulf": "project_engulf_receipt",
    "sqlite_brain": "loaded_sqlite_brain_receipt",
}

LANE_STORAGE_PREFIXES = {
    "sqlite_brain": "loaded_sqlite_brain",
}


def _table_counts(connection: sqlite3.Connection, lane_id: str) -> dict[str, int]:
    storage_prefix = LANE_STORAGE_PREFIXES.get(lane_id, lane_id)
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        if not any(
            row[0].startswith(f"{storage_prefix}_fts_{suffix}")
            for suffix in ("data", "idx", "content", "docsize", "config")
        )
    ]
    return {name: int(connection.execute(f"SELECT count(*) FROM {_qident(name)}").fetchone()[0]) for name in names}


def _migrate_generic_placeholders(connection: sqlite3.Connection, lane_id: str) -> None:
    """Archive pre-T021 generic placeholders before installing real schemas.

    Older brains initialized every contract table as the same seven-column
    placeholder (or a two-column FTS table).  Those shapes cannot satisfy the
    governed structural schemas.  Preserve any rows in ordinary legacy tables,
    then remove the placeholders so the authoritative schema can be created.
    """

    if connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='structural_meta'"
    ).fetchone():
        return
    for table in reversed(LANE_REGISTRY[lane_id].schema_contract):
        row = connection.execute(
            "SELECT type,sql FROM sqlite_schema WHERE name=? AND type IN ('table','view')",
            (table,),
        ).fetchone()
        if not row:
            continue
        columns = {item[1] for item in connection.execute(f"PRAGMA table_info({_qident(table)})")}
        is_generic = columns <= {
            "id", "source_id", "name", "path", "value", "metadata_json", "created_at",
            "entity_id", "text",
        }
        if not is_generic:
            continue
        count = int(connection.execute(f"SELECT count(*) FROM {_qident(table)}").fetchone()[0])
        if count:
            backup = f"legacy_pre_t021_{table}"
            if not connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name=?", (backup,)
            ).fetchone():
                connection.execute(
                    f"CREATE TABLE {_qident(backup)} AS SELECT * FROM {_qident(table)}"
                )
        connection.execute(f"DROP TABLE {_qident(table)}")


def _insert_fts(
    connection: sqlite3.Connection,
    prefix: str,
    entity_kind: str,
    entity_id: str,
    title: str,
    body: str,
    locator: str,
) -> None:
    document_id = _stable_id("fts", prefix, entity_kind, entity_id)
    connection.execute(
        f"INSERT OR IGNORE INTO {prefix}_fts_document(document_id,entity_kind,entity_id,title,body,locator) VALUES(?,?,?,?,?,?)",
        (document_id, entity_kind, entity_id, title, body, locator),
    )


def _insert_root_source(
    connection: sqlite3.Connection,
    table: str,
    lane_id: str,
    inspection: SourceInspection,
) -> str:
    source_id = _stable_id("source", lane_id, inspection.display_name, inspection.sha256)
    connection.execute(
        f"""INSERT OR IGNORE INTO {table}(
          source_id,source_path,display_name,source_kind,sha256,size_bytes,item_count,metadata_json
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            source_id,
            str(inspection.source_path),
            inspection.display_name,
            inspection.source_kind,
            inspection.sha256,
            inspection.size_bytes,
            len(inspection.items),
            _json({"archive_crc_status": inspection.archive_crc_status}),
        ),
    )
    return source_id


def _manifest_and_pointer_flags(logical_path: str) -> tuple[bool, bool]:
    name = PurePosixPath(logical_path).name.casefold()
    is_manifest = ("manifest" in name and name.endswith(".json")) or name in {
        "package_contents.json", "sector_index.json",
    }
    is_pointer = name.startswith(".uepc_") or name.endswith("_pointer.json") or name in {
        "project_pointer.json", "project_pointer.txt",
    }
    return is_manifest, is_pointer


def _recursive_manifest_value(value: Any, keys: Sequence[str]) -> str | None:
    if isinstance(value, Mapping):
        normalized = {str(key).casefold(): item for key, item in value.items()}
        for key in keys:
            candidate = normalized.get(key.casefold())
            if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                return str(candidate).strip()
        for child in value.values():
            found = _recursive_manifest_value(child, keys)
            if found:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found = _recursive_manifest_value(child, keys)
            if found:
                return found
    return None


def _package_facts(inspection: SourceInspection, limits: InspectionLimits) -> dict[str, Any]:
    manifests: list[tuple[str, Any]] = []
    pointers: list[str] = []
    for item in inspection.items:
        is_manifest, is_pointer = _manifest_and_pointer_flags(item.logical_path)
        if is_pointer:
            pointers.append(item.logical_path)
        if is_manifest:
            text = _read_item_text(item, limits)
            if text:
                try:
                    manifests.append((item.logical_path, json.loads(text)))
                except json.JSONDecodeError:
                    manifests.append((item.logical_path, {"parse_status": "INVALID_JSON"}))
    manifest_values = [value for _, value in manifests]
    identity = next(
        (
            value
            for value in (
                _recursive_manifest_value(data, ("source_brain_identity", "brain_id", "brain_name", "project_id"))
                for data in manifest_values
            )
            if value
        ),
        None,
    )
    schema_version = next(
        (
            value
            for value in (
                _recursive_manifest_value(data, ("schema_version", "contract_version", "version"))
                for data in manifest_values
            )
            if value
        ),
        None,
    )
    paths = {item.logical_path.casefold() for item in inspection.items}
    sqlite_count = sum(item.is_sqlite for item in inspection.items)
    if {"env", "uop", "project"}.issubset(
        {PurePosixPath(path).parts[0] for path in paths if PurePosixPath(path).parts}
    ):
        package_class = "SQLITE_BRAIN_PACKAGE"
    elif sqlite_count:
        package_class = "SQLITE_COLLECTION"
    elif inspection.source_kind == "directory":
        package_class = "PROJECT_DIRECTORY"
    else:
        package_class = "STRUCTURED_PACKAGE"
    if schema_version and re.fullmatch(r"(?:v)?0*1|v001|1(?:\.0+)?", schema_version, flags=re.IGNORECASE):
        compatibility = "SUPPORTED_CANONICAL"
        decision = "IMPORT_READ_ONLY"
    elif sqlite_count and all(item.size_bytes > 0 for item in inspection.items if item.is_sqlite):
        compatibility = "REVIEW_REQUIRED"
        decision = "CARRY_FORWARD_REVIEW_REQUIRED"
    else:
        compatibility = "STRUCTURE_ONLY"
        decision = "INSPECT_ONLY"
    return {
        "manifests": manifests,
        "pointers": pointers,
        "identity": identity,
        "schema_version": schema_version,
        "package_class": package_class,
        "sqlite_count": sqlite_count,
        "compatibility": compatibility,
        "decision": decision,
    }


def _ingest_brain_loader_rows(
    connection: sqlite3.Connection,
    inspection: SourceInspection,
    limits: InspectionLimits,
) -> tuple[str, list[SQLiteInspection], dict[str, Any]]:
    source_id = _insert_root_source(connection, "brain_loader_source", "brain_loader", inspection)
    facts = _package_facts(inspection, limits)
    package_id = _stable_id("package", source_id, facts["package_class"])
    connection.execute(
        """INSERT OR IGNORE INTO brain_loader_package VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            package_id,
            source_id,
            facts["package_class"],
            len(inspection.items),
            len(facts["manifests"]),
            len(facts["pointers"]),
            facts["sqlite_count"],
            facts["identity"],
            facts["schema_version"],
            facts["compatibility"],
            facts["decision"],
            _json({"archive_crc_status": inspection.archive_crc_status}),
        ),
    )
    _insert_fts(connection, "brain_loader", "package", package_id, inspection.display_name, _json(facts), str(inspection.source_path))
    sqlite_receipts: list[SQLiteInspection] = []
    member_ids: dict[str, str] = {}
    for item in inspection.items:
        is_manifest, is_pointer = _manifest_and_pointer_flags(item.logical_path)
        member_id = _stable_id("member", package_id, item.logical_path, item.sha256)
        member_ids[item.logical_path] = member_id
        connection.execute(
            """INSERT OR IGNORE INTO brain_loader_member VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                member_id,
                package_id,
                item.logical_path,
                item.item_kind,
                item.size_bytes,
                item.sha256,
                int(is_manifest),
                int(is_pointer),
                int(item.is_sqlite),
                _json(dict(item.metadata)),
            ),
        )
        relation_id = _stable_id("rel", package_id, "contains", member_id)
        connection.execute(
            "INSERT OR IGNORE INTO brain_loader_relationship VALUES(?,?,?,?,?,?,?)",
            (relation_id, "package", package_id, "member", member_id, "contains", item.logical_path),
        )
        _insert_fts(connection, "brain_loader", "member", member_id, item.logical_path, item.item_kind, item.logical_path)
        if item.is_sqlite:
            sqlite_inspection = _inspect_sqlite_item(item, limits)
            sqlite_receipts.append(sqlite_inspection)
            database_id = _stable_id("database", member_id, item.sha256)
            connection.execute(
                "INSERT OR IGNORE INTO brain_loader_database VALUES(?,?,?,?,?,?,?,?)",
                (
                    database_id,
                    member_id,
                    sqlite_inspection.schema_hash,
                    _json(list(sqlite_inspection.integrity_check)),
                    len(sqlite_inspection.foreign_key_violations),
                    len(sqlite_inspection.objects),
                    _json(list(sqlite_inspection.fts_tables)),
                    "COMPATIBLE" if sqlite_inspection.is_healthy else "REVIEW_REQUIRED",
                ),
            )
            for obj in sqlite_inspection.objects:
                object_id = _stable_id("object", database_id, obj.object_type, obj.name)
                connection.execute(
                    "INSERT OR IGNORE INTO brain_loader_schema_object VALUES(?,?,?,?,?,?,?)",
                    (object_id, database_id, obj.object_type, obj.name, obj.table_name, obj.sql, obj.schema_sha256),
                )
                _insert_fts(connection, "brain_loader", "schema_object", object_id, obj.name, obj.sql or "", f"{item.logical_path}::{obj.name}")
    # Pointer-to-database facts are relations only.  No pointer or project file is changed.
    for pointer_path in facts["pointers"]:
        pointer_item = next(item for item in inspection.items if item.logical_path == pointer_path)
        pointer_text = _read_item_text(pointer_item, limits) or ""
        for db_receipt in sqlite_receipts:
            if db_receipt.logical_path in pointer_text or PurePosixPath(db_receipt.logical_path).name in pointer_text:
                relationship_id = _stable_id("rel", member_ids[pointer_path], "references", db_receipt.logical_path)
                connection.execute(
                    "INSERT OR IGNORE INTO brain_loader_relationship VALUES(?,?,?,?,?,?,?)",
                    (
                        relationship_id,
                        "pointer",
                        member_ids[pointer_path],
                        "database_path",
                        db_receipt.logical_path,
                        "references",
                        pointer_path,
                    ),
                )
    return source_id, sqlite_receipts, facts


RESEARCH_KIND_TO_TABLE = {
    "question": "research_question",
    "hypothesis": "research_hypothesis",
    "method": "research_method",
    "evidence": "research_evidence",
    "finding": "research_finding",
    "limitation": "research_limitation",
    "citation": "research_citation",
    "open_question": "research_open_question",
}

RESEARCH_MARKER_RE = re.compile(
    r"^(?:#{1,6}\s*)?(research\s+question|open\s+question|question|hypothesis|method(?:ology)?|"
    r"evidence|finding|result|limitation|citation|source)\s*[:\-]\s*(.+)$",
    flags=re.IGNORECASE,
)


def _research_kind(label: str) -> str:
    normalized = re.sub(r"\s+", "_", label.strip().casefold())
    return {
        "research_question": "question",
        "methodology": "method",
        "result": "finding",
        "source": "citation",
    }.get(normalized, normalized)


def _research_records(text: str) -> list[tuple[str, int, str]]:
    records: list[tuple[str, int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        match = RESEARCH_MARKER_RE.match(line)
        if match:
            records.append((_research_kind(match.group(1)), line_number, match.group(2).strip()))
        elif re.search(r"https?://|\bdoi:\s*10\.", line, flags=re.IGNORECASE):
            records.append(("citation", line_number, line))
        elif line.endswith("?") and len(line) <= 500:
            records.append(("question", line_number, line))
    if not records and text.strip():
        excerpt = text.strip()[:12_000]
        records.append(("evidence", 1, excerpt))
    return records


def _ingest_research_rows(
    connection: sqlite3.Connection,
    inspection: SourceInspection,
    limits: InspectionLimits,
) -> tuple[str, list[SQLiteInspection], dict[str, Any]]:
    root_source_id = _stable_id("source", "research", inspection.display_name, inspection.sha256)
    inserted_records = 0
    text_sources = 0
    for item in inspection.items:
        text = _read_item_text(item, limits)
        if text is None:
            continue
        text_sources += 1
        source_id = _stable_id("research_source", root_source_id, item.logical_path, item.sha256)
        connection.execute(
            "INSERT OR IGNORE INTO research_source VALUES(?,?,?,?,?,?,?,?)",
            (
                source_id,
                root_source_id,
                str(inspection.source_path),
                item.logical_path,
                item.item_kind,
                item.sha256,
                item.size_bytes,
                _json(dict(item.metadata)),
            ),
        )
        ordinal_by_kind: dict[str, int] = {kind: 0 for kind in RESEARCH_KIND_TO_TABLE}
        for kind, line_number, content in _research_records(text):
            if kind not in RESEARCH_KIND_TO_TABLE:
                kind = "evidence"
            ordinal_by_kind[kind] += 1
            ordinal = ordinal_by_kind[kind]
            table = RESEARCH_KIND_TO_TABLE[kind]
            entity_id = _stable_id(kind, source_id, ordinal, _sha256_bytes(content.encode("utf-8")))
            locator = f"{item.logical_path}:{line_number}"
            connection.execute(
                f"INSERT OR IGNORE INTO {table} VALUES(?,?,?,?,?,?,?)",
                (entity_id, source_id, ordinal, content, _sha256_bytes(content.encode("utf-8")), line_number, locator),
            )
            _insert_fts(connection, "research", kind, entity_id, f"{kind}: {item.logical_path}", content, locator)
            inserted_records += 1
    return root_source_id, [], {"text_source_count": text_sources, "classified_record_count": inserted_records}


def _project_component_type(item: SourceItem) -> str:
    suffix = PurePosixPath(item.logical_path).suffix.casefold()
    name = PurePosixPath(item.logical_path).name.casefold()
    if item.is_symlink:
        return "symlink"
    if item.is_sqlite:
        return "sqlite_database"
    if item.is_archive:
        return "archive"
    if suffix in CODE_EXTENSIONS:
        return "source_code"
    if name.startswith("test_") or "/tests/" in f"/{item.logical_path.casefold()}/":
        return "test"
    if name in {"package.json", "pyproject.toml", "requirements.txt", "cargo.toml"}:
        return "dependency_manifest"
    if suffix in {".md", ".rst", ".txt", ".docx", ".pdf"}:
        return "documentation"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        return "configuration"
    return "project_artifact"


def _project_target_lane(item: SourceItem) -> tuple[str, str]:
    suffix = PurePosixPath(item.logical_path).suffix.casefold()
    if item.is_sqlite:
        return "sqlite_brain", "SQLite structure requires governed SQLite Brain mapping"
    if item.is_archive:
        return "brain_loader", "Archive requires governed package inspection"
    if suffix in CODE_EXTENSIONS:
        return "local_code", "Code snapshot"
    if suffix in {".docx", ".html", ".md", ".rst", ".txt", ".xml"}:
        return "docs", "Document structure"
    if suffix in {".csv", ".parquet", ".tsv", ".xls", ".xlsx"}:
        return "data_excel", "Structured data"
    if suffix in {".ppt", ".pptx", ".odp"}:
        return "ppt", "Presentation structure"
    if suffix == ".pdf":
        return "pdf_ocr", "PDF structure"
    if suffix in {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}:
        return "images_ocr", "Image/OCR structure"
    return "artifacts", "Unclassified project artifact"


def _is_env_uop_promotion_path(logical_path: str) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(logical_path).parts)
    name = PurePosixPath(logical_path).name.casefold()
    return bool(parts and parts[0] in {"env", "uop"}) or name.startswith(".uepc_env") or name.startswith(".uepc_uop")


def _project_package_identity(inspection: SourceInspection, limits: InspectionLimits) -> tuple[str, str | None, str]:
    for item in inspection.items:
        name = PurePosixPath(item.logical_path).name.casefold()
        text = _read_item_text(item, limits)
        if text is None:
            continue
        if name == "package.json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            package_name = str(payload.get("name") or "").strip()
            if package_name:
                return package_name, str(payload.get("version") or "").strip() or None, item.logical_path
        if name == "pyproject.toml":
            name_match = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, flags=re.MULTILINE)
            version_match = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', text, flags=re.MULTILINE)
            if name_match:
                return name_match.group(1), version_match.group(1) if version_match else None, item.logical_path
    return inspection.display_name, None, "source_root"


def _ingest_project_engulf_rows(
    connection: sqlite3.Connection,
    inspection: SourceInspection,
    limits: InspectionLimits,
) -> tuple[str, list[SQLiteInspection], dict[str, Any]]:
    source_id = _insert_root_source(connection, "project_engulf_source", "project_engulf", inspection)
    origin_id = _stable_id("origin", source_id)
    connection.execute(
        "INSERT OR IGNORE INTO project_engulf_origin VALUES(?,?,?,?,?)",
        (origin_id, source_id, inspection.source_kind, str(inspection.source_path), inspection.sha256),
    )
    package_name, package_version, identity_source = _project_package_identity(inspection, limits)
    identity_sha256 = _sha256_bytes(
        f"{package_name}\x1f{package_version or ''}\x1f{identity_source}".encode("utf-8")
    )
    connection.execute(
        "INSERT OR IGNORE INTO project_engulf_package_identity VALUES(?,?,?,?,?,?)",
        (_stable_id("package_identity", source_id, identity_sha256), source_id, package_name, package_version, identity_source, identity_sha256),
    )
    sqlite_receipts: list[SQLiteInspection] = []
    accepted = 0
    skipped = 0
    blocked = 0
    code_nodes = 0
    for item in inspection.items:
        file_id = _stable_id("file", source_id, item.logical_path, item.sha256)
        component_type = _project_component_type(item)
        connection.execute(
            "INSERT OR IGNORE INTO project_engulf_file VALUES(?,?,?,?,?,?,?,?)",
            (
                file_id,
                source_id,
                item.logical_path,
                item.item_kind,
                item.size_bytes,
                item.sha256,
                int(item.is_symlink),
                _json(dict(item.metadata)),
            ),
        )
        component_id = _stable_id("component", file_id, component_type)
        connection.execute(
            "INSERT OR IGNORE INTO project_engulf_component VALUES(?,?,?,?,?)",
            (component_id, file_id, component_type, PurePosixPath(item.logical_path).name, item.logical_path),
        )
        parent = PurePosixPath(item.logical_path).parent.as_posix()
        relationship_id = _stable_id("rel", source_id, parent, item.logical_path, "contains")
        connection.execute(
            "INSERT OR IGNORE INTO project_engulf_relationship VALUES(?,?,?,?,?)",
            (relationship_id, source_id, parent, item.logical_path, "contains"),
        )
        target_lane, reason = _project_target_lane(item)
        is_blocked = _is_env_uop_promotion_path(item.logical_path)
        decision = "BLOCKED" if is_blocked else ("SKIPPED_SYMLINK" if item.is_symlink else "PROPOSED")
        target_id = _stable_id("target", file_id)
        connection.execute(
            "INSERT OR IGNORE INTO project_engulf_sector_target VALUES(?,?,?,?,?)",
            (target_id, file_id, target_lane, decision, reason),
        )
        if is_blocked:
            blocked += 1
            conflict_id = _stable_id("conflict", file_id, "ENV_UOP_PROMOTION_PROHIBITED")
            connection.execute(
                "INSERT OR IGNORE INTO project_engulf_conflict VALUES(?,?,?,?,?,?)",
                (
                    conflict_id,
                    file_id,
                    "ENV_UOP_PROMOTION_PROHIBITED",
                    "CRITICAL",
                    "BLOCK_AND_QUARANTINE_MAPPING",
                    "Project sources may never be promoted into Env/UOP law.",
                ),
            )
            object_status = "BLOCKED"
        elif item.is_symlink:
            skipped += 1
            object_status = "SKIPPED"
        else:
            accepted += 1
            if target_lane == "local_code":
                code_nodes += 1
            object_status = "ACCEPTED_FOR_GOVERNED_MAPPING"
        decision_id = _stable_id("decision", file_id)
        connection.execute(
            "INSERT OR IGNORE INTO project_engulf_object_decision VALUES(?,?,?,?)",
            (decision_id, file_id, object_status, reason),
        )
        text = _read_item_text(item, limits)
        if text is not None and not is_blocked:
            lines = text.splitlines(keepends=True) or [""]
            for offset in range(0, len(lines), limits.text_chunk_lines):
                ordinal = offset // limits.text_chunk_lines + 1
                block = "".join(lines[offset : offset + limits.text_chunk_lines])
                block_hash = _sha256_bytes(block.encode("utf-8"))
                chunk_id = _stable_id("chunk", file_id, ordinal, block_hash)
                start_line = offset + 1
                end_line = min(len(lines), offset + limits.text_chunk_lines)
                locator = f"{item.logical_path}:{start_line}-{end_line}"
                connection.execute(
                    "INSERT OR IGNORE INTO project_engulf_chunk VALUES(?,?,?,?,?,?,?,?)",
                    (chunk_id, file_id, ordinal, start_line, end_line, block, block_hash, locator),
                )
                _insert_fts(connection, "project_engulf", "chunk", chunk_id, item.logical_path, block, locator)
        if item.is_sqlite:
            sqlite_inspection = _inspect_sqlite_item(item, limits)
            sqlite_receipts.append(sqlite_inspection)
            for obj in sqlite_inspection.objects:
                mapping_id = _stable_id("mapping", file_id, obj.object_type, obj.name)
                connection.execute(
                    "INSERT OR IGNORE INTO project_engulf_schema_mapping VALUES(?,?,?,?,?,?,?)",
                    (
                        mapping_id,
                        file_id,
                        obj.name,
                        obj.object_type,
                        "sqlite_brain",
                        "PROPOSED_READ_ONLY",
                        obj.schema_sha256,
                    ),
                )
    run_id = _stable_id("engulf_run", source_id, inspection.sha256)
    status = "REVIEW_REQUIRED" if blocked or any(not receipt.is_healthy for receipt in sqlite_receipts) else "INSPECTED"
    connection.execute(
        "INSERT OR IGNORE INTO project_engulf_run VALUES(?,?,?,?,?,?)",
        (run_id, source_id, accepted, skipped, blocked, status),
    )
    update_id = _stable_id("topology_update", run_id)
    topology_status = "PENDING_GOVERNED_APPLY" if code_nodes else "SKIPPED_NO_CODE_LANES"
    topology_proposal = {
        "source_id": source_id,
        "accepted_project_nodes": accepted,
        "blocked_env_uop_nodes": blocked,
        "code_nodes": code_nodes,
        "apply_policy": topology_status,
    }
    topology_proposal_sha256 = _sha256_bytes(_json(topology_proposal).encode("utf-8"))
    connection.execute(
        "INSERT OR IGNORE INTO project_engulf_topology_update VALUES(?,?,?,?,?)",
        (
            update_id,
            run_id,
            topology_status,
            code_nodes,
            _json({
                "source_only": True,
                "env_uop_promotion": "PROHIBITED",
                "proposal": topology_proposal,
                "proposal_sha256": topology_proposal_sha256,
            }),
        ),
    )
    return source_id, sqlite_receipts, {
        "engulf_run_id": run_id,
        "accepted": accepted,
        "skipped": skipped,
        "blocked": blocked,
        "topology_update": topology_status,
        "topology_proposal_sha256": topology_proposal_sha256,
        "package_identity": package_name,
    }


def _proposed_lane_for_object(object_name: str) -> str:
    name = object_name.casefold()
    if any(token in name for token in ("research", "citation", "hypothesis")):
        return "research"
    if any(token in name for token in ("lineage", "prompt", "response", "turn")):
        return "chat_lineage"
    if any(token in name for token in ("code", "symbol", "route", "git_")):
        return "local_code"
    return "sqlite_brain"


def _ingest_sqlite_brain_rows(
    connection: sqlite3.Connection,
    inspection: SourceInspection,
    limits: InspectionLimits,
) -> tuple[str, list[SQLiteInspection], dict[str, Any]]:
    source_id = _insert_root_source(connection, "loaded_sqlite_brain_source", "sqlite_brain", inspection)
    sqlite_receipts: list[SQLiteInspection] = []
    database_ids: dict[str, str] = {}
    for item in inspection.items:
        if not item.is_sqlite:
            continue
        sqlite_inspection = _inspect_sqlite_item(item, limits)
        sqlite_receipts.append(sqlite_inspection)
        database_id = _stable_id("database", source_id, item.logical_path, item.sha256)
        database_ids[item.logical_path] = database_id
        type_counts = {
            object_type: sum(obj.object_type == object_type for obj in sqlite_inspection.objects)
            for object_type in ("table", "view", "index", "trigger")
        }
        connection.execute(
            "INSERT OR IGNORE INTO loaded_sqlite_brain_database VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                database_id,
                source_id,
                item.logical_path,
                item.sha256,
                item.size_bytes,
                sqlite_inspection.sqlite_version,
                sqlite_inspection.schema_hash,
                len(sqlite_inspection.objects),
                type_counts["table"],
                type_counts["view"],
                type_counts["index"],
                type_counts["trigger"],
            ),
        )
        for obj in sqlite_inspection.objects:
            object_id = _stable_id("object", database_id, obj.object_type, obj.name)
            connection.execute(
                "INSERT OR IGNORE INTO loaded_sqlite_brain_schema_object VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    object_id,
                    database_id,
                    obj.object_type,
                    obj.name,
                    obj.table_name,
                    obj.rootpage,
                    obj.sql,
                    obj.schema_sha256,
                    _json(list(obj.columns)),
                ),
            )
            mapping_id = _stable_id("mapping", database_id, obj.name)
            connection.execute(
                "INSERT OR IGNORE INTO loaded_sqlite_brain_sector_mapping VALUES(?,?,?,?,?)",
                (mapping_id, database_id, obj.name, _proposed_lane_for_object(obj.name), "PROPOSED_READ_ONLY"),
            )
            _insert_fts(connection, "loaded_sqlite_brain", "schema_object", object_id, obj.name, obj.sql or "", f"{item.logical_path}::{obj.name}")
            if obj.table_name and obj.table_name != obj.name:
                relationship_id = _stable_id("rel", database_id, obj.name, obj.table_name, "belongs_to")
                connection.execute(
                    "INSERT OR IGNORE INTO loaded_sqlite_brain_relationship VALUES(?,?,?,?,?)",
                    (relationship_id, database_id, obj.name, obj.table_name, "belongs_to"),
                )
        for table_stat in sqlite_inspection.table_stats:
            stat_id = _stable_id("stat", database_id, table_stat.object_type, table_stat.object_name)
            connection.execute(
                "INSERT OR IGNORE INTO loaded_sqlite_brain_table_stat VALUES(?,?,?,?,?,?)",
                (
                    stat_id,
                    database_id,
                    table_stat.object_name,
                    table_stat.object_type,
                    table_stat.row_count,
                    table_stat.count_error,
                ),
            )
        for foreign_key in sqlite_inspection.foreign_keys:
            foreign_key_id = _stable_id(
                "fk", database_id, foreign_key.table_name, foreign_key.fk_id, foreign_key.sequence
            )
            connection.execute(
                "INSERT OR IGNORE INTO loaded_sqlite_brain_foreign_key VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    foreign_key_id,
                    database_id,
                    foreign_key.table_name,
                    foreign_key.fk_id,
                    foreign_key.sequence,
                    foreign_key.target_table,
                    foreign_key.from_column,
                    foreign_key.to_column,
                    foreign_key.on_update,
                    foreign_key.on_delete,
                    foreign_key.match,
                ),
            )
            relationship_id = _stable_id(
                "rel", database_id, foreign_key.table_name, foreign_key.target_table, "foreign_key"
            )
            connection.execute(
                "INSERT OR IGNORE INTO loaded_sqlite_brain_relationship VALUES(?,?,?,?,?)",
                (relationship_id, database_id, foreign_key.table_name, foreign_key.target_table, "foreign_key"),
            )
        for table_name in sqlite_inspection.fts_tables:
            fts_id = _stable_id("source_fts", database_id, table_name)
            connection.execute(
                "INSERT OR IGNORE INTO loaded_sqlite_brain_fts_table VALUES(?,?,?)",
                (fts_id, database_id, table_name),
            )
        integrity_id = _stable_id("integrity", database_id)
        compatibility = "STRUCTURALLY_COMPATIBLE" if sqlite_inspection.is_healthy else "REVIEW_REQUIRED"
        connection.execute(
            "INSERT OR IGNORE INTO loaded_sqlite_brain_integrity_result VALUES(?,?,?,?,?,?,?)",
            (
                integrity_id,
                database_id,
                _json(list(sqlite_inspection.integrity_check)),
                len(sqlite_inspection.foreign_key_violations),
                _json(list(sqlite_inspection.foreign_key_violations[:20])),
                "PASS" if sqlite_inspection.is_healthy else "REVIEW_REQUIRED",
                sqlite_inspection.error,
            ),
        )
        compatibility_id = _stable_id("compatibility", database_id)
        connection.execute(
            "INSERT OR IGNORE INTO loaded_sqlite_brain_compatibility VALUES(?,?,?,?)",
            (
                compatibility_id,
                database_id,
                compatibility,
                "integrity_check=ok and foreign_key_check=0" if sqlite_inspection.is_healthy else "Inspect integrity/FK receipt",
            ),
        )
    pointer_items = [item for item in inspection.items if _manifest_and_pointer_flags(item.logical_path)[1]]
    for pointer_item in pointer_items:
        pointer_text = _read_item_text(pointer_item, limits) or ""
        for db_path, database_id in database_ids.items():
            if db_path in pointer_text or PurePosixPath(db_path).name in pointer_text:
                pointer_id = _stable_id("pointer", source_id, pointer_item.logical_path, db_path)
                connection.execute(
                    "INSERT OR IGNORE INTO loaded_sqlite_brain_package_pointer VALUES(?,?,?,?,?)",
                    (pointer_id, source_id, pointer_item.logical_path, db_path, "references"),
                )
                relationship_id = _stable_id("rel", database_id, pointer_item.logical_path, db_path, "package_pointer")
                connection.execute(
                    "INSERT OR IGNORE INTO loaded_sqlite_brain_relationship VALUES(?,?,?,?,?)",
                    (relationship_id, database_id, pointer_item.logical_path, db_path, "package_pointer"),
                )
    return source_id, sqlite_receipts, {
        "database_count": len(sqlite_receipts),
        "healthy_database_count": sum(receipt.is_healthy for receipt in sqlite_receipts),
        "read_only_query_interface": "query_sqlite_read_only",
    }


ROW_INGESTERS: Mapping[
    str,
    Callable[[sqlite3.Connection, SourceInspection, InspectionLimits], tuple[str, list[SQLiteInspection], dict[str, Any]]],
] = {
    "brain_loader": _ingest_brain_loader_rows,
    "research": _ingest_research_rows,
    "project_engulf": _ingest_project_engulf_rows,
    "sqlite_brain": _ingest_sqlite_brain_rows,
}


def _output_validation(connection: sqlite3.Connection) -> tuple[tuple[str, ...], tuple[Mapping[str, Any], ...]]:
    integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall())
    violations = tuple(
        {"table": row[0], "rowid": row[1], "parent": row[2], "fkid": row[3]}
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    )
    return integrity, violations


def _ingest(
    lane_id: str,
    source_path: str | Path,
    sector_database: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> IngestionReceipt:
    limits = limits or InspectionLimits()
    source = Path(source_path).expanduser().resolve(strict=True)
    destination = Path(sector_database).expanduser().resolve(strict=False)
    _assert_safe_destination(source, destination)
    inspection = inspect_source_structure(source, limits=limits)
    before_source_hash = inspection.sha256
    destination.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(destination)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    _migrate_generic_placeholders(connection, lane_id)
    connection.executescript(LANE_SCHEMAS[lane_id])
    connection.execute("INSERT OR IGNORE INTO structural_meta VALUES('lane_id',?)", (lane_id,))
    connection.execute("INSERT OR IGNORE INTO structural_meta VALUES('id_policy','sha256 deterministic IDs; INSERT OR IGNORE index-once')")
    connection.commit()
    before_counts = _table_counts(connection, lane_id)
    run_id = _stable_id("run", lane_id, inspection.display_name, inspection.sha256)
    try:
        connection.execute("BEGIN IMMEDIATE")
        source_id, sqlite_inspections, details = ROW_INGESTERS[lane_id](connection, inspection, limits)
        provisional_counts = _table_counts(connection, lane_id)
        provisional_growth = {
            name: provisional_counts.get(name, 0) - before_counts.get(name, 0)
            for name in sorted(set(before_counts) | set(provisional_counts))
            if name != LANE_RECEIPT_TABLES[lane_id]
        }
        after_source_hash = _source_fingerprint(source, limits)
        if after_source_hash != before_source_hash:
            raise SourceChangedDuringIngestionError(
                f"Source changed during {lane_id} ingestion: {before_source_hash} != {after_source_hash}"
            )
        integrity, output_fk = _output_validation(connection)
        source_sqlite_receipts = tuple(receipt.receipt_payload() for receipt in sqlite_inspections)
        source_sqlite_healthy = all(receipt.is_healthy for receipt in sqlite_inspections)
        status = "PASS" if integrity == ("ok",) and not output_fk and source_sqlite_healthy else "REVIEW_REQUIRED"
        receipt_id = _stable_id("receipt", run_id)
        connection.execute(
            f"""INSERT OR IGNORE INTO {LANE_RECEIPT_TABLES[lane_id]} VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                receipt_id,
                run_id,
                source_id,
                inspection.sha256,
                1,
                _json(source_sqlite_receipts),
                _json(provisional_growth),
                _json(list(integrity)),
                len(output_fk),
                status,
                _json(details),
                _utc_now(),
            ),
        )
        connection.commit()
        final_integrity, final_fk = _output_validation(connection)
        after_counts = _table_counts(connection, lane_id)
        growth = {
            name: after_counts.get(name, 0) - before_counts.get(name, 0)
            for name in sorted(set(before_counts) | set(after_counts))
        }
        final_status = "PASS" if final_integrity == ("ok",) and not final_fk and source_sqlite_healthy else "REVIEW_REQUIRED"
        return IngestionReceipt(
            lane_id=lane_id,
            run_id=run_id,
            source_id=source_id,
            source_sha256=inspection.sha256,
            sector_database=destination,
            source_preserved=True,
            before_counts=before_counts,
            after_counts=after_counts,
            growth=growth,
            source_sqlite_receipts=source_sqlite_receipts,
            output_integrity_check=final_integrity,
            output_foreign_key_violations=final_fk,
            status=final_status,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def ingest_brain_loader(
    source_path: str | Path,
    sector_database: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> IngestionReceipt:
    """Index package identity, manifests, pointers, SQLite inventory, and decision."""

    return _ingest("brain_loader", source_path, sector_database, limits=limits)


def ingest_research(
    source_path: str | Path,
    sector_database: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> IngestionReceipt:
    """Index research questions, hypotheses, methods, evidence, findings, and citations."""

    return _ingest("research", source_path, sector_database, limits=limits)


def ingest_project_engulf(
    source_path: str | Path,
    sector_database: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> IngestionReceipt:
    """Inventory a project and propose governed sector mappings without applying them."""

    return _ingest("project_engulf", source_path, sector_database, limits=limits)


def ingest_sqlite_brain(
    source_path: str | Path,
    sector_database: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> IngestionReceipt:
    """Index SQLite schemas, counts, FKs, FTS tables, pointers, and compatibility."""

    return _ingest("sqlite_brain", source_path, sector_database, limits=limits)


def ingest_structural_lane(
    lane: str,
    source_path: str | Path,
    sector_database: str | Path,
    *,
    limits: InspectionLimits | None = None,
) -> IngestionReceipt:
    """Strict dispatcher for the four structural lanes; never falls back to custom."""

    try:
        canonical = resolve_lane_id(lane, scope="backend")
    except UnknownLaneAliasError as exc:
        raise UnsupportedStructuralLaneError(str(exc)) from exc
    if canonical not in SUPPORTED_STRUCTURAL_LANES:
        raise UnsupportedStructuralLaneError(
            f"Lane {canonical!r} is not handled by structural ingestion; expected one of "
            f"{', '.join(SUPPORTED_STRUCTURAL_LANES)}"
        )
    return _ingest(canonical, source_path, sector_database, limits=limits)


_ALLOWED_PRAGMAS = {
    "compile_options",
    "database_list",
    "foreign_key_list",
    "index_info",
    "index_list",
    "integrity_check",
    "page_count",
    "page_size",
    "quick_check",
    "schema_version",
    "table_info",
    "table_xinfo",
    "user_version",
}


def _validate_read_only_sql(sql: str) -> str:
    statement = sql.strip()
    if not statement:
        raise ReadOnlyQueryError("Query cannot be empty")
    without_trailing = statement[:-1].rstrip() if statement.endswith(";") else statement
    if ";" in without_trailing:
        raise ReadOnlyQueryError("Only one SQL statement is allowed")
    first = re.match(r"^[A-Za-z]+", without_trailing)
    keyword = first.group(0).casefold() if first else ""
    if keyword in {"select", "with"}:
        return without_trailing
    if keyword == "pragma":
        if "=" in without_trailing:
            raise ReadOnlyQueryError("PRAGMA assignment is not permitted")
        match = re.match(r"^pragma\s+(?:[A-Za-z0-9_]+\.)?([A-Za-z0-9_]+)", without_trailing, flags=re.IGNORECASE)
        if not match or match.group(1).casefold() not in _ALLOWED_PRAGMAS:
            raise ReadOnlyQueryError("PRAGMA is not on the read-only allowlist")
        return without_trailing
    raise ReadOnlyQueryError("Only SELECT, WITH, and allowlisted read-only PRAGMA queries are permitted")


def query_sqlite_read_only(
    database_path: str | Path,
    sql: str,
    parameters: Sequence[Any] = (),
    *,
    row_limit: int = 500,
) -> ReadOnlyQueryResult:
    """Execute one bounded read-only query against a standalone SQLite file."""

    if row_limit < 1 or row_limit > InspectionLimits().max_query_rows:
        raise ReadOnlyQueryError(
            f"row_limit must be between 1 and {InspectionLimits().max_query_rows}"
        )
    statement = _validate_read_only_sql(sql)
    path = Path(database_path).expanduser().resolve(strict=True)
    if _read_header(path) != SQLITE_MAGIC:
        raise ReadOnlyQueryError(f"Not a SQLite database: {path}")
    uri = f"file:{path.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        cursor = connection.execute(statement, tuple(parameters))
        columns = tuple(description[0] for description in (cursor.description or ()))
        fetched = cursor.fetchmany(row_limit + 1)
        return ReadOnlyQueryResult(
            columns=columns,
            rows=tuple(tuple(row) for row in fetched[:row_limit]),
            truncated=len(fetched) > row_limit,
        )
    except sqlite3.DatabaseError as exc:
        raise ReadOnlyQueryError(str(exc)) from exc
    finally:
        connection.close()


__all__ = [
    "IngestionReceipt",
    "InspectionLimits",
    "ReadOnlyQueryError",
    "ReadOnlyQueryResult",
    "SQLiteInspection",
    "SourceChangedDuringIngestionError",
    "SourceInspection",
    "StructuralIngestionError",
    "SUPPORTED_STRUCTURAL_LANES",
    "UnsafeArchiveError",
    "UnsafeSourceError",
    "UnsupportedStructuralLaneError",
    "ingest_brain_loader",
    "ingest_project_engulf",
    "ingest_research",
    "ingest_sqlite_brain",
    "ingest_structural_lane",
    "inspect_source_structure",
    "inspect_sqlite_database",
    "query_sqlite_read_only",
]
