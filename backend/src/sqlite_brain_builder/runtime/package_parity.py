from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


CODE_LANE_TABLES = (
    "code_file_snapshot",
    "code_chunk",
    "code_symbol",
    "code_import",
    "code_route_api_boundary",
    "code_config_build_test_chunk",
    "code_workflow_edge",
    "source_byte_coverage",
    "code_file_version",
    "code_line_snapshot",
    "code_exact_byte_chunk",
)
PROVIDER_PROJECTION_CONTRACT = "T023_PROVIDER_SIZE_BOUNDED_LOGICAL_PROJECTION_V2"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _cell(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, int):
        return ["integer", value]
    return ["text", str(value)]


def _row_digest(row: Iterable[Any]) -> str:
    encoded = json.dumps([_cell(value) for value in row], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()


def _object_contract(connection: sqlite3.Connection) -> set[tuple[str, str, str, str]]:
    return {
        (str(kind), str(name), str(table), str(sql or ""))
        for kind, name, table, sql in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    }


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _table_rows(connection: sqlite3.Connection, table: str) -> Counter[str]:
    return Counter(_row_digest(row) for row in connection.execute(f"SELECT * FROM {_quote(table)}"))


def _table_rows_except(
    connection: sqlite3.Connection,
    table: str,
    excluded_columns: set[str],
) -> Counter[str]:
    columns = [
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
        if str(row[1]) not in excluded_columns
    ]
    if not columns:
        return Counter()
    selected = ",".join(_quote(column) for column in columns)
    return Counter(
        _row_digest(row)
        for row in connection.execute(f"SELECT {selected} FROM {_quote(table)}")
    )


def _integrity(connection: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    return "ok" if rows == ["ok"] else ";".join(rows)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _exact_payload_stream(connection: sqlite3.Connection) -> tuple[int, int, str]:
    count = 0
    byte_size = 0
    digest = hashlib.sha256()
    for (payload,) in connection.execute(
        "SELECT compressed_payload FROM code_exact_byte_chunk ORDER BY rowid"
    ):
        data = bytes(payload or b"")
        digest.update(len(data).to_bytes(8, "little"))
        digest.update(data)
        count += 1
        byte_size += len(data)
    return count, byte_size, digest.hexdigest()


def _provider_projection_map(package: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    receipt_path = package / "receipts" / "PROVIDER_PACKAGE_SIZE_PROJECTION_RECEIPT.json"
    if not receipt_path.is_file():
        return {}, []
    errors: list[str] = []
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"PROVIDER_PROJECTION_RECEIPT_INVALID:{type(exc).__name__}"]
    if receipt.get("contract") != PROVIDER_PROJECTION_CONTRACT:
        errors.append("PROVIDER_PROJECTION_CONTRACT_INVALID")
    if receipt.get("live_brain_mutated") is not False:
        errors.append("PROVIDER_PROJECTION_LIVE_MUTATION_LAW_INVALID")
    rows = list(receipt.get("projections") or [])
    if int(receipt.get("projection_count") or 0) != len(rows):
        errors.append("PROVIDER_PROJECTION_COUNT_MISMATCH")
    projections: dict[str, dict[str, Any]] = {}
    for row in rows:
        member = str(row.get("package_member") or "").replace("\\", "/")
        if not member.startswith("project/") or member in projections:
            errors.append(f"PROVIDER_PROJECTION_MEMBER_INVALID:{member}")
            continue
        projections[member.removeprefix("project/")] = dict(row)
    return projections, errors


def _validate_projected_database(
    active: Path,
    packaged: Path,
    projection: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    package_sha256 = _sha256_file(packaged)
    if projection.get("contract") != PROVIDER_PROJECTION_CONTRACT:
        errors.append("PROJECTION_CONTRACT_INVALID")
    if projection.get("package_sha256") != package_sha256:
        errors.append("PROJECTION_PACKAGE_SHA256_MISMATCH")
    if int(projection.get("package_byte_size") or -1) != packaged.stat().st_size:
        errors.append("PROJECTION_PACKAGE_BYTE_SIZE_MISMATCH")
    if projection.get("integrity_check") != "ok":
        errors.append("PROJECTION_RECEIPT_INTEGRITY_INVALID")
    if int(projection.get("foreign_key_violation_count") or 0) != 0:
        errors.append("PROJECTION_RECEIPT_FOREIGN_KEYS_INVALID")

    try:
        active_connection = sqlite3.connect(f"file:{active.as_posix()}?mode=ro", uri=True)
        packaged_connection = sqlite3.connect(f"file:{packaged.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return errors + [f"PROJECTION_OPEN_FAILED:{exc}"]
    try:
        if _integrity(active_connection) != "ok":
            errors.append("PROJECTION_ACTIVE_QUICK_CHECK_FAILED")
        if _integrity(packaged_connection) != "ok":
            errors.append("PROJECTION_PACKAGE_QUICK_CHECK_FAILED")
        if list(active_connection.execute("PRAGMA foreign_key_check")):
            errors.append("PROJECTION_ACTIVE_FOREIGN_KEYS_FAILED")
        if list(packaged_connection.execute("PRAGMA foreign_key_check")):
            errors.append("PROJECTION_PACKAGE_FOREIGN_KEYS_FAILED")

        metadata = dict(
            packaged_connection.execute(
                "SELECT key,value FROM provider_package_projection"
            ).fetchall()
        )
        expected_metadata = {
            "contract": PROVIDER_PROJECTION_CONTRACT,
            "live_brain_mutated": "false",
            "exact_byte_authority": "code_exact_byte_chunk.compressed_payload",
            # SQLite's online backup API preserves logical state but may
            # normalize page layout. Bind these fields to the copied source
            # image recorded immediately before projection; logical parity to
            # the active database is checked table-by-table below.
            "source_database_sha256": str(projection.get("source_sha256") or ""),
            "source_database_byte_size": str(projection.get("source_byte_size") or ""),
        }
        for key, expected in expected_metadata.items():
            if metadata.get(key) != expected:
                errors.append(f"PROJECTION_METADATA_MISMATCH:{key}")

        active_exact = _exact_payload_stream(active_connection)
        package_exact = _exact_payload_stream(packaged_connection)
        receipt_exact = (
            int(projection.get("exact_payload_count") or 0),
            int(projection.get("exact_payload_bytes") or 0),
            str(projection.get("exact_payload_stream_sha256") or ""),
        )
        if active_exact != receipt_exact:
            errors.append("PROJECTION_ACTIVE_EXACT_PAYLOAD_MISMATCH")
        if package_exact != receipt_exact:
            errors.append("PROJECTION_PACKAGE_EXACT_PAYLOAD_MISMATCH")

        critical = dict(projection.get("critical_row_counts") or {})
        for table, expected_count in critical.items():
            active_count = int(
                active_connection.execute(f"SELECT COUNT(*) FROM {_quote(str(table))}").fetchone()[0]
            )
            package_count = int(
                packaged_connection.execute(f"SELECT COUNT(*) FROM {_quote(str(table))}").fetchone()[0]
            )
            if active_count != int(expected_count) or package_count != int(expected_count):
                errors.append(f"PROJECTION_CRITICAL_ROW_COUNT_MISMATCH:{table}")

        transformed_columns = {
            "code_chunk": {"chunk_text"},
            "chunk_index": {"content"},
            "code_chunk_fts": {"chunk_text"},
            "git_exact_line_change": set(),
        }
        package_objects = {
            str(row[0]): str(row[1])
            for row in packaged_connection.execute(
                "SELECT name,type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        for table in _table_names(active_connection):
            if table.startswith("code_chunk_fts_"):
                # FTS5 shadow rows are physical index implementation detail.
                # Logical FTS rows are compared through code_chunk_fts below.
                continue
            if table in transformed_columns:
                continue
            if table not in package_objects:
                errors.append(f"PROJECTION_UNCHANGED_TABLE_MISSING:{table}")
                continue
            if _table_rows(active_connection, table) != _table_rows(
                packaged_connection, table
            ):
                errors.append(f"PROJECTION_UNCHANGED_TABLE_ROWS_MISMATCH:{table}")

        for table, excluded_columns in transformed_columns.items():
            active_object = active_connection.execute(
                "SELECT type FROM sqlite_schema WHERE name=?", (table,)
            ).fetchone()
            package_object = packaged_connection.execute(
                "SELECT type FROM sqlite_schema WHERE name=?", (table,)
            ).fetchone()
            if not active_object and not package_object:
                continue
            if not active_object or not package_object:
                errors.append(f"PROJECTION_TRANSFORMED_OBJECT_MISSING:{table}")
                continue
            if _table_rows_except(
                active_connection, table, excluded_columns
            ) != _table_rows_except(packaged_connection, table, excluded_columns):
                errors.append(f"PROJECTION_TRANSFORMED_LOGICAL_ROWS_MISMATCH:{table}")
    except sqlite3.Error as exc:
        errors.append(f"PROJECTION_VALIDATION_FAILED:{exc}")
    finally:
        active_connection.close()
        packaged_connection.close()
    return errors


def _compare_database(active: Path, packaged: Path, *, allow_packaged_superset: bool) -> list[str]:
    errors: list[str] = []
    try:
        active_connection = sqlite3.connect(f"file:{active.as_posix()}?mode=ro", uri=True)
        packaged_connection = sqlite3.connect(f"file:{packaged.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [f"{active.name}:OPEN_FAILED:{exc}"]
    try:
        active_integrity = _integrity(active_connection)
        package_integrity = _integrity(packaged_connection)
        if active_integrity != "ok":
            errors.append(f"{active.name}:ACTIVE_QUICK_CHECK:{active_integrity}")
        if package_integrity != "ok":
            errors.append(f"{active.name}:PACKAGED_QUICK_CHECK:{package_integrity}")

        active_objects = _object_contract(active_connection)
        package_objects = _object_contract(packaged_connection)
        if allow_packaged_superset:
            missing_objects = active_objects - package_objects
            if missing_objects:
                errors.append(f"{active.name}:PACKAGED_SCHEMA_MISSING:{len(missing_objects)}")
        elif active_objects != package_objects:
            errors.append(
                f"{active.name}:SCHEMA_MISMATCH:active={len(active_objects)}:packaged={len(package_objects)}"
            )

        active_tables = _table_names(active_connection)
        package_tables = set(_table_names(packaged_connection))
        for table in active_tables:
            if table not in package_tables:
                errors.append(f"{active.name}:{table}:TABLE_MISSING")
                continue
            active_rows = _table_rows(active_connection, table)
            package_rows = _table_rows(packaged_connection, table)
            if allow_packaged_superset:
                missing_rows = active_rows - package_rows
                if missing_rows:
                    errors.append(
                        f"{active.name}:{table}:ACTIVE_ROWS_MISSING_FROM_PACKAGE:{sum(missing_rows.values())}"
                    )
            elif active_rows != package_rows:
                errors.append(
                    f"{active.name}:{table}:LOGICAL_ROWS_MISMATCH:"
                    f"active={sum(active_rows.values())}:packaged={sum(package_rows.values())}"
                )
    except sqlite3.Error as exc:
        errors.append(f"{active.name}:SQLITE_COMPARE_FAILED:{exc}")
    finally:
        active_connection.close()
        packaged_connection.close()
    return errors


def _code_lane_assertions(project_root: Path) -> dict[str, dict[str, int]]:
    assertions: dict[str, dict[str, int]] = {}
    sectors = project_root / "sectors"
    if not sectors.is_dir():
        return assertions
    for database in sorted(sectors.glob("*/*.sqlite")):
        lane = database.parent.name
        counts = {table: 0 for table in CODE_LANE_TABLES}
        try:
            with closing(
                sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
            ) as connection:
                tables = set(_table_names(connection))
                for table in CODE_LANE_TABLES:
                    if table in tables:
                        counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
        except sqlite3.Error:
            continue
        if any(table in tables for table in CODE_LANE_TABLES):
            assertions[lane] = counts
    return assertions


def validate_active_project_package_parity(
    brain_root: str | Path,
    package_folder: str | Path,
) -> dict[str, Any]:
    """Fail closed unless every active Project SQLite is preserved in the package."""

    root = Path(brain_root).resolve()
    package = Path(package_folder).resolve()
    active_project = root / "project"
    packaged_project = package if package.name.casefold() == "project" else package / "project"
    errors: list[str] = []
    if not active_project.is_dir():
        errors.append(f"ACTIVE_PROJECT_MISSING:{active_project}")
    if not packaged_project.is_dir():
        errors.append(f"PACKAGED_PROJECT_MISSING:{packaged_project}")
    if errors:
        return {
            "contract": "ACTIVE_PROJECT_PACKAGE_SQLITE_PARITY_V1",
            "status": "FAIL",
            "errors": errors,
            "sqlite_file_count": 0,
            "code_lane_assertions": {},
        }

    active_files = {
        path.relative_to(active_project).as_posix(): path
        for path in active_project.rglob("*.sqlite")
        if path.is_file()
    }
    packaged_files = {
        path.relative_to(packaged_project).as_posix(): path
        for path in packaged_project.rglob("*.sqlite")
        if path.is_file()
    }
    projections, projection_errors = _provider_projection_map(package)
    errors.extend(projection_errors)
    for relative in sorted(active_files.keys() - packaged_files.keys()):
        errors.append(f"PROJECT_SQLITE_MISSING_FROM_PACKAGE:{relative}")
    for relative in sorted(packaged_files.keys() - active_files.keys()):
        errors.append(f"UNEXPECTED_PROJECT_SQLITE_IN_PACKAGE:{relative}")

    file_results: list[dict[str, Any]] = []
    for relative in sorted(active_files.keys() & packaged_files.keys()):
        projection = projections.get(relative)
        allow_superset = "chat_lineage" in relative.casefold()
        database_errors = (
            _validate_projected_database(
                active_files[relative], packaged_files[relative], projection
            )
            if projection
            else _compare_database(
                active_files[relative],
                packaged_files[relative],
                allow_packaged_superset=allow_superset,
            )
        )
        errors.extend(f"{relative}:{error}" for error in database_errors)
        file_results.append(
            {
                "relative_path": relative,
                "status": "FAIL" if database_errors else "PASS",
                "package_superset_allowed": allow_superset,
                "provider_projection_validated": bool(projection),
            }
        )

    return {
        "contract": "ACTIVE_PROJECT_PACKAGE_SQLITE_PARITY_V1",
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "sqlite_file_count": len(active_files),
        "active_project": str(active_project),
        "packaged_project": str(packaged_project),
        "files": file_results,
        "provider_projection_contract": (
            PROVIDER_PROJECTION_CONTRACT if projections else None
        ),
        "projected_sqlite_count": len(projections),
        "code_lane_assertions": _code_lane_assertions(active_project),
    }


__all__ = ["CODE_LANE_TABLES", "validate_active_project_package_parity"]
