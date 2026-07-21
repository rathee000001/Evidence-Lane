from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PACKAGE_ROOT_AUTHORITY_CONTRACT = "EVIDENCE_LANE_PACKAGE_ROOT_AUTHORITY_V1"
DEFAULT_ROOT_AUTHORITY_RELATIVE = "manifests/PACKAGE_ROOT_AUTHORITY.json"
UNTRUSTED_CANDIDATE = "UNTRUSTED_CANDIDATE"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest().upper()


def _safe_relative(root: Path, relative: str) -> Path:
    normalized = str(relative or "").replace("\\", "/")
    candidate = (root / Path(normalized)).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"PACKAGE_ROOT_AUTHORITY_PATH_ESCAPE:{relative}")
    return candidate


def package_member_rows(
    package_root: str | Path,
    *,
    excluded_relatives: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    root = Path(package_root).resolve()
    excluded = {str(value).replace("\\", "/") for value in excluded_relatives}
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        rows.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    return rows


def write_package_root_authority(
    package_root: str | Path,
    *,
    package_profile: str,
    package_use_mode: str,
    manifest_relative: str = DEFAULT_ROOT_AUTHORITY_RELATIVE,
    parent_authority: Mapping[str, Any] | None = None,
    lineage_head_relative: str | None = "project/lineage/LINEAGE_HEAD.json",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the final, outermost package authority over every other file.

    The manifest deliberately excludes only itself to avoid a self-hash cycle.
    A successful structural validation never promotes the package: every newly
    written archive remains an untrusted candidate until explicit human HIL.
    """

    root = Path(package_root).resolve()
    manifest_relative = manifest_relative.replace("\\", "/")
    manifest_path = _safe_relative(root, manifest_relative)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.unlink(missing_ok=True)
    rows = package_member_rows(root, excluded_relatives={manifest_relative})
    lineage: dict[str, Any] | None = None
    if lineage_head_relative:
        lineage_path = _safe_relative(root, lineage_head_relative)
        if lineage_path.is_file():
            lineage = {
                "path": lineage_head_relative.replace("\\", "/"),
                "size_bytes": lineage_path.stat().st_size,
                "sha256": _sha256_file(lineage_path),
            }
    document: dict[str, Any] = {
        "contract": PACKAGE_ROOT_AUTHORITY_CONTRACT,
        "status": "VALIDATED_UNTRUSTED_CANDIDATE",
        "acceptance_state": UNTRUSTED_CANDIDATE,
        "promotion_authority": "EXPLICIT_HUMAN_HIL_ONLY",
        "receipts_override_underlying_state": False,
        "current_root_authority": True,
        "manifest_scope": "CURRENT_OUTERMOST_PACKAGE_EXCLUDING_SELF",
        "manifest_path": manifest_relative,
        "package_profile": str(package_profile),
        "package_use_mode": str(package_use_mode),
        "member_count_excluding_manifest": len(rows),
        "member_set_sha256": _canonical_sha256(rows),
        "files": rows,
        "lineage_head": lineage,
        "parent_authority": dict(parent_authority or {}),
        "created_at": _utc_now(),
    }
    if extra:
        document["package_facts"] = dict(extra)
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def validate_package_root_authority(
    package_root: str | Path,
    *,
    manifest_relative: str = DEFAULT_ROOT_AUTHORITY_RELATIVE,
) -> dict[str, Any]:
    root = Path(package_root).resolve()
    manifest_relative = manifest_relative.replace("\\", "/")
    manifest_path = _safe_relative(root, manifest_relative)
    errors: list[str] = []
    document: dict[str, Any] = {}
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "contract": PACKAGE_ROOT_AUTHORITY_CONTRACT,
            "status": "FAIL",
            "errors": [f"PACKAGE_ROOT_AUTHORITY_READ_FAILED:{type(error).__name__}"],
        }
    if document.get("contract") != PACKAGE_ROOT_AUTHORITY_CONTRACT:
        errors.append("PACKAGE_ROOT_AUTHORITY_CONTRACT_INVALID")
    if document.get("current_root_authority") is not True:
        errors.append("PACKAGE_ROOT_AUTHORITY_CURRENT_FLAG_INVALID")
    if document.get("manifest_scope") != "CURRENT_OUTERMOST_PACKAGE_EXCLUDING_SELF":
        errors.append("PACKAGE_ROOT_AUTHORITY_SCOPE_INVALID")
    if document.get("acceptance_state") != UNTRUSTED_CANDIDATE:
        errors.append("PACKAGE_ROOT_AUTHORITY_CANDIDATE_STATE_INVALID")
    if document.get("promotion_authority") != "EXPLICIT_HUMAN_HIL_ONLY":
        errors.append("PACKAGE_ROOT_AUTHORITY_HIL_BOUNDARY_INVALID")
    if document.get("receipts_override_underlying_state") is not False:
        errors.append("PACKAGE_ROOT_AUTHORITY_RECEIPT_PRECEDENCE_INVALID")

    declared_rows = document.get("files")
    if not isinstance(declared_rows, list):
        declared_rows = []
        errors.append("PACKAGE_ROOT_AUTHORITY_FILES_INVALID")
    actual_rows = package_member_rows(root, excluded_relatives={manifest_relative})
    declared_by_path: dict[str, dict[str, Any]] = {}
    for row in declared_rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            errors.append("PACKAGE_ROOT_AUTHORITY_ROW_INVALID")
            continue
        relative = str(row["path"]).replace("\\", "/")
        if relative in declared_by_path:
            errors.append(f"PACKAGE_ROOT_AUTHORITY_DUPLICATE_MEMBER:{relative}")
        declared_by_path[relative] = row
    actual_by_path = {row["path"]: row for row in actual_rows}
    for relative in sorted(set(actual_by_path) - set(declared_by_path)):
        errors.append(f"PACKAGE_ROOT_AUTHORITY_UNDECLARED_MEMBER:{relative}")
    for relative in sorted(set(declared_by_path) - set(actual_by_path)):
        errors.append(f"PACKAGE_ROOT_AUTHORITY_MISSING_MEMBER:{relative}")
    for relative in sorted(set(actual_by_path) & set(declared_by_path)):
        actual = actual_by_path[relative]
        declared = declared_by_path[relative]
        try:
            declared_size = int(declared.get("size_bytes"))
        except (TypeError, ValueError):
            declared_size = -1
        if declared_size != actual["size_bytes"]:
            errors.append(f"PACKAGE_ROOT_AUTHORITY_SIZE_MISMATCH:{relative}")
        if str(declared.get("sha256") or "").upper() != actual["sha256"]:
            errors.append(f"PACKAGE_ROOT_AUTHORITY_HASH_MISMATCH:{relative}")
    if int(document.get("member_count_excluding_manifest") or -1) != len(actual_rows):
        errors.append("PACKAGE_ROOT_AUTHORITY_MEMBER_COUNT_MISMATCH")
    if str(document.get("member_set_sha256") or "").upper() != _canonical_sha256(actual_rows):
        errors.append("PACKAGE_ROOT_AUTHORITY_MEMBER_SET_HASH_MISMATCH")
    return {
        "contract": PACKAGE_ROOT_AUTHORITY_CONTRACT,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "acceptance_state": document.get("acceptance_state"),
        "member_count_excluding_manifest": len(actual_rows),
        "member_set_sha256": _canonical_sha256(actual_rows),
        "manifest": str(manifest_path),
    }


def validate_zip_package_root_authority(
    archive_path: str | Path,
    *,
    manifest_relative: str = DEFAULT_ROOT_AUTHORITY_RELATIVE,
) -> dict[str, Any]:
    manifest_relative = manifest_relative.replace("\\", "/")
    errors: list[str] = []
    document: dict[str, Any] = {}
    actual_rows: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(Path(archive_path), "r") as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
            if len(names) != len(set(names)):
                errors.append("PACKAGE_ROOT_AUTHORITY_DUPLICATE_ZIP_MEMBER")
            document = json.loads(archive.read(manifest_relative).decode("utf-8"))
            for info in sorted(
                (row for row in archive.infolist() if not row.is_dir() and row.filename != manifest_relative),
                key=lambda row: row.filename,
            ):
                digest = hashlib.sha256()
                with archive.open(info, "r") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(block)
                actual_rows.append(
                    {
                        "path": info.filename,
                        "size_bytes": info.file_size,
                        "sha256": digest.hexdigest().upper(),
                    }
                )
    except (OSError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        return {
            "contract": PACKAGE_ROOT_AUTHORITY_CONTRACT,
            "status": "FAIL",
            "errors": [f"PACKAGE_ROOT_AUTHORITY_ZIP_READ_FAILED:{type(error).__name__}"],
        }
    if document.get("contract") != PACKAGE_ROOT_AUTHORITY_CONTRACT:
        errors.append("PACKAGE_ROOT_AUTHORITY_CONTRACT_INVALID")
    if document.get("current_root_authority") is not True:
        errors.append("PACKAGE_ROOT_AUTHORITY_CURRENT_FLAG_INVALID")
    if document.get("manifest_path") != manifest_relative:
        errors.append("PACKAGE_ROOT_AUTHORITY_MANIFEST_PATH_INVALID")
    if document.get("acceptance_state") != UNTRUSTED_CANDIDATE:
        errors.append("PACKAGE_ROOT_AUTHORITY_CANDIDATE_STATE_INVALID")
    if document.get("promotion_authority") != "EXPLICIT_HUMAN_HIL_ONLY":
        errors.append("PACKAGE_ROOT_AUTHORITY_HIL_BOUNDARY_INVALID")
    if document.get("receipts_override_underlying_state") is not False:
        errors.append("PACKAGE_ROOT_AUTHORITY_RECEIPT_PRECEDENCE_INVALID")
    declared_rows = document.get("files")
    if not isinstance(declared_rows, list):
        declared_rows = []
        errors.append("PACKAGE_ROOT_AUTHORITY_FILES_INVALID")
    declared_by_path = {
        str(row.get("path") or "").replace("\\", "/"): row
        for row in declared_rows
        if isinstance(row, dict)
    }
    actual_by_path = {row["path"]: row for row in actual_rows}
    for relative in sorted(set(actual_by_path) - set(declared_by_path)):
        errors.append(f"PACKAGE_ROOT_AUTHORITY_UNDECLARED_MEMBER:{relative}")
    for relative in sorted(set(declared_by_path) - set(actual_by_path)):
        errors.append(f"PACKAGE_ROOT_AUTHORITY_MISSING_MEMBER:{relative}")
    for relative in sorted(set(actual_by_path) & set(declared_by_path)):
        actual = actual_by_path[relative]
        declared = declared_by_path[relative]
        try:
            declared_size = int(declared.get("size_bytes"))
        except (TypeError, ValueError):
            declared_size = -1
        if declared_size != actual["size_bytes"]:
            errors.append(f"PACKAGE_ROOT_AUTHORITY_SIZE_MISMATCH:{relative}")
        if str(declared.get("sha256") or "").upper() != actual["sha256"]:
            errors.append(f"PACKAGE_ROOT_AUTHORITY_HASH_MISMATCH:{relative}")
    if int(document.get("member_count_excluding_manifest") or -1) != len(actual_rows):
        errors.append("PACKAGE_ROOT_AUTHORITY_MEMBER_COUNT_MISMATCH")
    actual_set_hash = _canonical_sha256(actual_rows)
    if str(document.get("member_set_sha256") or "").upper() != actual_set_hash:
        errors.append("PACKAGE_ROOT_AUTHORITY_MEMBER_SET_HASH_MISMATCH")
    return {
        "contract": PACKAGE_ROOT_AUTHORITY_CONTRACT,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "acceptance_state": document.get("acceptance_state"),
        "member_count_excluding_manifest": len(actual_rows),
        "member_set_sha256": actual_set_hash,
        "manifest": manifest_relative,
    }


__all__ = [
    "DEFAULT_ROOT_AUTHORITY_RELATIVE",
    "PACKAGE_ROOT_AUTHORITY_CONTRACT",
    "UNTRUSTED_CANDIDATE",
    "package_member_rows",
    "validate_package_root_authority",
    "validate_zip_package_root_authority",
    "write_package_root_authority",
]
