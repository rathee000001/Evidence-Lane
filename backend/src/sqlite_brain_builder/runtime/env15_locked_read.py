from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


LOCKED_READ_RESOURCE_DIRECTORY = "env15_locked_read"
CHATGPT_GEMINI_ARCHIVE_NAME = (
    "UEPC_ENV15_PUBLIC_LOCKED_SECTION_PACKAGE_CHATGPT_GEMINI.zip"
)
CHATGPT_GEMINI_ARCHIVE_SHA256 = (
    "670F3FE2EBC7C13DD187258833107880107606CCFA90AC53D1C59342AE80B9ED"
)
CHATGPT_GEMINI_ARCHIVE_SIZE = 2_602_101
CHATGPT_GEMINI_ENTRY_COUNT = 128
CHATGPT_GEMINI_FILE_COUNT = 128
CHATGPT_GEMINI_DIRECTORY_COUNT = 0
CODEX_ARCHIVE_NAME = "UEPC_ENV15_PUBLIC_LOCKED_SECTION_PACKAGE_CODEX.zip"
CODEX_ARCHIVE_SHA256 = (
    "016A3CA6B62D134B5352E2B3708A71BE468E06C642EAEC47C399EE740CDA0CAF"
)
CODEX_ARCHIVE_SIZE = 2_449_660
CODEX_ARCHIVE_ENTRY_COUNT = 161
CODEX_ARCHIVE_FILE_COUNT = 135
CODEX_ARCHIVE_DIRECTORY_COUNT = 26
CODEX_NON_SUPPORT_EXTRA_FILES = ("research/v15 research on temp.md",)

# Compatibility aliases intentionally identify the Env/UOP authority used by
# ChatGPT and Gemini. Codex support is sourced only through CODEX_ARCHIVE_*.
LOCKED_READ_ARCHIVE_NAME = CHATGPT_GEMINI_ARCHIVE_NAME
LOCKED_READ_PACKAGE_CLASS = "PUBLIC_LOCKED_FULL_ENV14_BASE_PLUS_ENV15_DELTA"
LOCKED_READ_ARCHIVE_SHA256 = CHATGPT_GEMINI_ARCHIVE_SHA256
LOCKED_READ_ARCHIVE_SIZE = CHATGPT_GEMINI_ARCHIVE_SIZE
LOCKED_READ_MEMBER_COUNT = CHATGPT_GEMINI_ENTRY_COUNT
LOCKED_READ_SQLITE_COUNT = 17
LOCKED_READ_PACKAGE_MEMBER = f"public_read/{LOCKED_READ_ARCHIVE_NAME}"
SUPPLIED_ENV_UOP_DATABASE_MEMBERS = (
    "env/env_sqlite.sqlite",
    "uop/uop_sqlite.sqlite",
)
SUPPLIED_CODEX_SUPPORT_MEMBERS = (
    "codex/CODEX_EXPECTED_FILE_MAP.json",
    "codex/CODEX_PUBLIC_SECTION_PATCH_SCOPE.md",
    "codex/UEPC_ENV15_CODEX_PUBLIC_SECTION_UPDATE_HANDOFF.md",
    "codex/UEPC_ENV15_CODEX_PUBLIC_SECTION_UPDATE_PROMPT.md",
    "codex/codex_update_flow.mmd",
    "codex/codex_update_flow.svg",
)
SUPPLIED_ENV_UOP_EXACT_MEMBERS = (
    "env/env_law.md",
    "env/env_mmd.mmd",
    "env/env_mmd.svg",
    "env/env_mmd.png",
    "env/env_sqlite.sqlite",
    "env/locked_mmd_hash.txt",
    "uop/uop_law.md",
    "uop/uop_mmd.mmd",
    "uop/uop_mmd.svg",
    "uop/uop_mmd.png",
    "uop/uop_sqlite.sqlite",
    "uop/locked_mmd_hash.txt",
)
SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT = (
    "receipts/ENV_UOP_SUPPLIED_AUTHORITY_PROVENANCE.json"
)
SUPPLIED_CODEX_SUPPORT_PROVENANCE = (
    "manifests/CODEX_SUPPLIED_SUPPORT_PROVENANCE.json"
)
DERIVED_ENV_UOP_AUTHORITY_MEMBERS = (
    ".uepc_env",
    ".uepc_profile",
    ".uepc_project",
    "env/env_law.md",
    "env/env_mmd.mmd",
    "env/env_mmd.dot",
    "env/evidence_lane_project_authority_law.md",
    "uop/uop_law.md",
    "uop/uop_mmd.mmd",
    "uop/uop_mmd.dot",
    "uop/evidence_lane_live_project_operator_law.md",
    "project/pointers/CANONICAL_LANE_REGISTRY.json",
    "project/lineage/LINEAGE_HEAD.json",
)

# These immutable read assets are mounted into the live package roots because
# the public-model package contract addresses them at their historical paths.
# The live Project router and populated sector databases are intentionally not
# part of this overlay; the exact original archive remains available under
# public_read for byte-for-byte authority and audit.
LOCKED_ROOT_OVERLAY_MEMBERS = (
    "env/env_law.md",
    "env/env_mmd.dot",
    "env/env_mmd.mmd",
    "env/env_mmd.png",
    "env/env_mmd.svg",
    "env/env_sqlite.sqlite",
    "env/locked_mmd_hash.txt",
    "uop/locked_mmd_hash.txt",
    "uop/uop_law.md",
    "uop/uop_mmd.dot",
    "uop/uop_mmd.mmd",
    "uop/uop_mmd.png",
    "uop/uop_mmd.svg",
    "uop/uop_sqlite.sqlite",
    "project/PROJECT_SECTORS_README.md",
    "project/PROJECT_SECTOR_REGISTRY.json",
    "project/locked_mmd_hash.txt",
    "project/project_allowed_rw_policy.json",
    "project/project_artifact_manifest.json",
    "project/project_public_law.md",
    "project/project_section_router.json",
    "project/project_supersede_ledger.json",
    "project/project_template.sqlite",
    "project/project_template_footprint.md",
    "project/project_topology_template.dot",
    "project/project_topology_template.mmd",
    "project/project_topology_template.png",
    "project/project_topology_template.svg",
)

_PAYLOAD_ENTRY = re.compile(r"^(.+?)\|(\d+)\|([0-9a-fA-F]{64})$")


class Env15LockedReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class LockedReadValidation:
    archive_path: str
    valid: bool
    errors: tuple[str, ...]
    archive_sha256: str
    archive_size: int
    member_count: int
    sqlite_count: int
    package_class: str
    payload_member_count: int
    payload_hashes_valid: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SplitAuthorityValidation:
    chatgpt_gemini_archive_path: str
    codex_archive_path: str
    valid: bool
    errors: tuple[str, ...]
    chatgpt_gemini_sha256: str
    codex_sha256: str
    chatgpt_gemini_entry_count: int
    codex_entry_count: int
    chatgpt_gemini_file_count: int
    codex_file_count: int
    codex_directory_count: int
    common_member_count: int
    common_member_hashes_valid: bool
    chatgpt_gemini_codex_member_count: int
    codex_support_member_count: int
    codex_non_support_extra_files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _safe_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    return bool(normalized) and not pure.is_absolute() and ".." not in pure.parts and not re.match(
        r"^[A-Za-z]:", normalized
    )


def _archive_candidates(archive_name: str = LOCKED_READ_ARCHIVE_NAME) -> tuple[Path, ...]:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(
            Path(meipass)
            / "sqlite_brain_builder"
            / "resources"
            / LOCKED_READ_RESOURCE_DIRECTORY
            / archive_name
        )
    if getattr(sys, "frozen", False):
        candidates.append(
            Path(sys.executable).resolve().parent
            / "resources"
            / LOCKED_READ_RESOURCE_DIRECTORY
            / archive_name
        )
    candidates.append(
        Path(__file__).resolve().parents[1]
        / "resources"
        / LOCKED_READ_RESOURCE_DIRECTORY
        / archive_name
    )
    return tuple(candidates)


def _find_authority_archive(archive_name: str) -> Path:
    checked: list[str] = []
    for candidate in _archive_candidates(archive_name):
        resolved = candidate.resolve()
        checked.append(str(resolved))
        if resolved.is_file():
            return resolved
    raise Env15LockedReadError(
        f"ENV15_LOCKED_READ_ARCHIVE_NOT_FOUND:{archive_name}:" + ";".join(checked)
    )


def find_locked_read_archive() -> Path:
    return _find_authority_archive(CHATGPT_GEMINI_ARCHIVE_NAME)


def find_codex_locked_read_archive() -> Path:
    return _find_authority_archive(CODEX_ARCHIVE_NAME)


def _parse_payload_manifest(data: bytes) -> dict[str, tuple[int, str]]:
    manifest: dict[str, tuple[int, str]] = {}
    for raw_line in data.decode("utf-8").splitlines():
        match = _PAYLOAD_ENTRY.match(raw_line.strip())
        if not match:
            continue
        name, size, digest = match.groups()
        if not _safe_member(name):
            raise Env15LockedReadError(f"ENV15_LOCKED_READ_UNSAFE_MANIFEST_MEMBER:{name}")
        if name in manifest:
            raise Env15LockedReadError(f"ENV15_LOCKED_READ_DUPLICATE_MANIFEST_MEMBER:{name}")
        manifest[name] = (int(size), digest.upper())
    return manifest


@functools.lru_cache(maxsize=4)
def _validate_locked_read_archive_cached(path_text: str, size: int, modified_ns: int) -> LockedReadValidation:
    del size, modified_ns
    archive_path = Path(path_text)
    errors: list[str] = []
    archive_hash = _sha256_file(archive_path)
    archive_size = archive_path.stat().st_size
    if archive_hash != LOCKED_READ_ARCHIVE_SHA256:
        errors.append(f"ARCHIVE_SHA256:{archive_hash}")
    if archive_size != LOCKED_READ_ARCHIVE_SIZE:
        errors.append(f"ARCHIVE_SIZE:{archive_size}")

    member_count = 0
    sqlite_count = 0
    package_class = ""
    payload_member_count = 0
    payload_hashes_valid = False
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            member_count = len(infos)
            sqlite_count = sum(name.casefold().endswith(".sqlite") for name in names)
            if archive.testzip() is not None:
                errors.append("ZIP_CRC_FAILED")
            if member_count != LOCKED_READ_MEMBER_COUNT:
                errors.append(f"MEMBER_COUNT:{member_count}")
            if sqlite_count != LOCKED_READ_SQLITE_COUNT:
                errors.append(f"SQLITE_COUNT:{sqlite_count}")
            if len(names) != len(set(names)):
                errors.append("DUPLICATE_MEMBER")
            unsafe = [name for name in names if not _safe_member(name)]
            if unsafe:
                errors.append("UNSAFE_MEMBER:" + ",".join(unsafe[:5]))
            required = {
                "manifests/PACKAGE_CLASS.txt",
                "manifests/PAYLOAD_TREE_HASH.txt",
                *LOCKED_ROOT_OVERLAY_MEMBERS,
            }
            missing = sorted(required - set(names))
            if missing:
                errors.append("MISSING_REQUIRED:" + ",".join(missing))
            class_text = archive.read("manifests/PACKAGE_CLASS.txt").decode("utf-8")
            for line in class_text.splitlines():
                if line.startswith("PACKAGE_CLASS="):
                    package_class = line.split("=", 1)[1].strip()
                    break
            if package_class != LOCKED_READ_PACKAGE_CLASS:
                errors.append(f"PACKAGE_CLASS:{package_class}")
            payload = _parse_payload_manifest(archive.read("manifests/PAYLOAD_TREE_HASH.txt"))
            payload_member_count = len(payload)
            payload_errors = []
            missing_payload_members: set[str] = set()
            for name, (expected_size, expected_hash) in payload.items():
                if name not in names:
                    missing_payload_members.add(name)
                    continue
                info = archive.getinfo(name)
                if info.file_size != expected_size:
                    payload_errors.append(f"SIZE:{name}")
                    continue
                if _sha256_bytes(archive.read(name)) != expected_hash:
                    payload_errors.append(f"HASH:{name}")
            # The supplied ChatGPT/Gemini split is the user's byte authority.
            # Its carried-forward payload manifest still lists the six Codex
            # support members that were deliberately removed from this variant.
            # Accept only that exact, bounded split discrepancy; all remaining
            # declared members must still match size and SHA-256.
            allowed_split_missing = set(SUPPLIED_CODEX_SUPPORT_MEMBERS)
            if missing_payload_members != allowed_split_missing:
                payload_errors.extend(
                    f"MISSING:{name}" for name in sorted(missing_payload_members)
                )
            payload_hashes_valid = (
                not payload_errors
                and payload_member_count
                == member_count - 1 + len(allowed_split_missing)
            )
            if not payload_hashes_valid:
                errors.append("PAYLOAD_HASHES:" + ",".join(payload_errors[:5]))
    except Exception as exc:
        errors.append(f"ARCHIVE_READ:{type(exc).__name__}:{exc}")

    return LockedReadValidation(
        archive_path=str(archive_path),
        valid=not errors,
        errors=tuple(errors),
        archive_sha256=archive_hash,
        archive_size=archive_size,
        member_count=member_count,
        sqlite_count=sqlite_count,
        package_class=package_class,
        payload_member_count=payload_member_count,
        payload_hashes_valid=payload_hashes_valid,
    )


def validate_locked_read_archive(path: str | Path | None = None) -> LockedReadValidation:
    archive = Path(path).resolve() if path is not None else find_locked_read_archive()
    if not archive.is_file():
        raise Env15LockedReadError(f"ENV15_LOCKED_READ_ARCHIVE_NOT_FILE:{archive}")
    stat_result = archive.stat()
    return _validate_locked_read_archive_cached(str(archive), stat_result.st_size, stat_result.st_mtime_ns)


def validate_split_authority_archives(
    chatgpt_gemini_path: str | Path | None = None,
    codex_path: str | Path | None = None,
) -> SplitAuthorityValidation:
    """Validate the two supplied Env15 authorities without normalizing them.

    The ChatGPT/Gemini archive is the common Env/UOP authority and must contain
    no Codex member. The Codex archive must contain the exact same common files
    byte-for-byte plus the six approved ``codex/`` support files. Its single
    research note outside that support tree is retained as source evidence but
    is never imported into a provider package.
    """

    chat_archive = (
        Path(chatgpt_gemini_path).resolve()
        if chatgpt_gemini_path is not None
        else find_locked_read_archive()
    )
    codex_archive = (
        Path(codex_path).resolve()
        if codex_path is not None
        else find_codex_locked_read_archive()
    )
    errors: list[str] = []
    for label, path in (("CHATGPT_GEMINI", chat_archive), ("CODEX", codex_archive)):
        if not path.is_file():
            errors.append(f"{label}_ARCHIVE_NOT_FILE:{path}")

    chat_hash = _sha256_file(chat_archive) if chat_archive.is_file() else ""
    codex_hash = _sha256_file(codex_archive) if codex_archive.is_file() else ""
    if chat_hash != CHATGPT_GEMINI_ARCHIVE_SHA256:
        errors.append(f"CHATGPT_GEMINI_SHA256:{chat_hash}")
    if codex_hash != CODEX_ARCHIVE_SHA256:
        errors.append(f"CODEX_SHA256:{codex_hash}")
    if chat_archive.is_file() and chat_archive.stat().st_size != CHATGPT_GEMINI_ARCHIVE_SIZE:
        errors.append(f"CHATGPT_GEMINI_SIZE:{chat_archive.stat().st_size}")
    if codex_archive.is_file() and codex_archive.stat().st_size != CODEX_ARCHIVE_SIZE:
        errors.append(f"CODEX_SIZE:{codex_archive.stat().st_size}")

    chat_entries = chat_files = codex_entries = codex_files = codex_dirs = 0
    common_count = 0
    common_hashes_valid = False
    chat_codex_count = codex_support_count = 0
    codex_extras: tuple[str, ...] = ()
    if chat_archive.is_file() and codex_archive.is_file():
        try:
            with zipfile.ZipFile(chat_archive) as chat, zipfile.ZipFile(codex_archive) as codex:
                if chat.testzip() is not None:
                    errors.append("CHATGPT_GEMINI_CRC_FAILED")
                if codex.testzip() is not None:
                    errors.append("CODEX_CRC_FAILED")
                chat_infos = chat.infolist()
                codex_infos = codex.infolist()
                chat_entries = len(chat_infos)
                codex_entries = len(codex_infos)
                chat_file_names = {
                    item.filename for item in chat_infos if not item.is_dir()
                }
                codex_file_names = {
                    item.filename for item in codex_infos if not item.is_dir()
                }
                codex_directory_names = {
                    item.filename for item in codex_infos if item.is_dir()
                }
                chat_files = len(chat_file_names)
                codex_files = len(codex_file_names)
                codex_dirs = len(codex_directory_names)
                if chat_entries != CHATGPT_GEMINI_ENTRY_COUNT:
                    errors.append(f"CHATGPT_GEMINI_ENTRY_COUNT:{chat_entries}")
                if chat_files != CHATGPT_GEMINI_FILE_COUNT:
                    errors.append(f"CHATGPT_GEMINI_FILE_COUNT:{chat_files}")
                if any(item.is_dir() for item in chat_infos):
                    errors.append("CHATGPT_GEMINI_DIRECTORY_ENTRY_PRESENT")
                if codex_entries != CODEX_ARCHIVE_ENTRY_COUNT:
                    errors.append(f"CODEX_ENTRY_COUNT:{codex_entries}")
                if codex_files != CODEX_ARCHIVE_FILE_COUNT:
                    errors.append(f"CODEX_FILE_COUNT:{codex_files}")
                if codex_dirs != CODEX_ARCHIVE_DIRECTORY_COUNT:
                    errors.append(f"CODEX_DIRECTORY_COUNT:{codex_dirs}")
                unsafe = sorted(
                    name
                    for name in (*chat_file_names, *codex_file_names, *codex_directory_names)
                    if not _safe_member(name)
                )
                if unsafe:
                    errors.append("UNSAFE_MEMBER:" + ",".join(unsafe[:5]))
                chat_codex_members = sorted(
                    name for name in chat_file_names if name.casefold().startswith("codex/")
                )
                chat_codex_count = len(chat_codex_members)
                if chat_codex_members:
                    errors.append("CHATGPT_GEMINI_CODEX_LEAK:" + ",".join(chat_codex_members))
                observed_support = set(SUPPLIED_CODEX_SUPPORT_MEMBERS).intersection(
                    codex_file_names
                )
                codex_support_count = len(observed_support)
                if observed_support != set(SUPPLIED_CODEX_SUPPORT_MEMBERS):
                    errors.append("CODEX_SUPPORT_SET_MISMATCH")
                common = chat_file_names.intersection(codex_file_names)
                common_count = len(common)
                if common != chat_file_names:
                    errors.append("CODEX_COMMON_AUTHORITY_SET_MISMATCH")
                common_mismatches = [
                    name
                    for name in sorted(common)
                    if _sha256_bytes(chat.read(name)) != _sha256_bytes(codex.read(name))
                ]
                common_hashes_valid = not common_mismatches and common == chat_file_names
                if common_mismatches:
                    errors.append(
                        "COMMON_MEMBER_HASH_MISMATCH:" + ",".join(common_mismatches[:5])
                    )
                extras = codex_file_names - chat_file_names - set(SUPPLIED_CODEX_SUPPORT_MEMBERS)
                codex_extras = tuple(sorted(extras))
                if codex_extras != tuple(sorted(CODEX_NON_SUPPORT_EXTRA_FILES)):
                    errors.append("CODEX_NON_SUPPORT_EXTRA_SET_MISMATCH")
                if sum(name.casefold().endswith(".sqlite") for name in chat_file_names) != LOCKED_READ_SQLITE_COUNT:
                    errors.append("CHATGPT_GEMINI_SQLITE_COUNT_MISMATCH")
                if sum(name.casefold().endswith(".sqlite") for name in codex_file_names) != LOCKED_READ_SQLITE_COUNT:
                    errors.append("CODEX_SQLITE_COUNT_MISMATCH")
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            errors.append(f"SPLIT_ARCHIVE_READ:{type(exc).__name__}:{exc}")

    return SplitAuthorityValidation(
        chatgpt_gemini_archive_path=str(chat_archive),
        codex_archive_path=str(codex_archive),
        valid=not errors,
        errors=tuple(errors),
        chatgpt_gemini_sha256=chat_hash,
        codex_sha256=codex_hash,
        chatgpt_gemini_entry_count=chat_entries,
        codex_entry_count=codex_entries,
        chatgpt_gemini_file_count=chat_files,
        codex_file_count=codex_files,
        codex_directory_count=codex_dirs,
        common_member_count=common_count,
        common_member_hashes_valid=common_hashes_valid,
        chatgpt_gemini_codex_member_count=chat_codex_count,
        codex_support_member_count=codex_support_count,
        codex_non_support_extra_files=codex_extras,
    )


def _make_replaceable(path: Path) -> None:
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)


def _replace_if_different(path: Path, data: bytes) -> bool:
    if path.is_file() and path.stat().st_size == len(data) and _sha256_file(path) == _sha256_bytes(data):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(data)
    _make_replaceable(path)
    temporary.replace(path)
    return True


def _archive_member_rows(
    members: tuple[str, ...],
    *,
    provider: str = "CHATGPT_GEMINI",
) -> list[dict[str, Any]]:
    split_validation = validate_split_authority_archives()
    if not split_validation.valid:
        raise Env15LockedReadError(
            "ENV15_SUPPLIED_SPLIT_ARCHIVE_VALIDATION_FAILED:"
            + ";".join(split_validation.errors)
        )
    archive_path = (
        find_codex_locked_read_archive()
        if provider == "CODEX"
        else find_locked_read_archive()
    )
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        missing = sorted(set(members) - names)
        if missing:
            raise Env15LockedReadError(
                "ENV15_SUPPLIED_MEMBER_MISSING:" + ";".join(missing)
            )
        for member in members:
            payload = archive.read(member)
            rows.append(
                {
                    "member": member,
                    "byte_size": len(payload),
                    "sha256": _sha256_bytes(payload),
                }
            )
    return rows


@functools.lru_cache(maxsize=1)
def supplied_env_uop_database_rows() -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (str(row["member"]), int(row["byte_size"]), str(row["sha256"]))
        for row in _archive_member_rows(SUPPLIED_ENV_UOP_DATABASE_MEMBERS)
    )


@functools.lru_cache(maxsize=1)
def supplied_codex_support_rows() -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (str(row["member"]), int(row["byte_size"]), str(row["sha256"]))
        for row in _archive_member_rows(
            SUPPLIED_CODEX_SUPPORT_MEMBERS,
            provider="CODEX",
        )
    )


@functools.lru_cache(maxsize=32)
def read_chatgpt_gemini_authority_member(member: str) -> bytes:
    if not _safe_member(member):
        raise Env15LockedReadError(f"ENV15_SUPPLIED_MEMBER_UNSAFE:{member}")
    split_validation = validate_split_authority_archives()
    if not split_validation.valid:
        raise Env15LockedReadError(
            "ENV15_SUPPLIED_SPLIT_ARCHIVE_VALIDATION_FAILED:"
            + ";".join(split_validation.errors)
        )
    with zipfile.ZipFile(find_locked_read_archive()) as archive:
        try:
            return archive.read(member)
        except KeyError as exc:
            raise Env15LockedReadError(
                f"ENV15_CHATGPT_GEMINI_MEMBER_MISSING:{member}"
            ) from exc


def _sqlite_read_validation(path: Path) -> dict[str, Any]:
    integrity: list[str] = []
    foreign_keys: list[list[Any]] = []
    error = ""
    try:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        error = f"{type(exc).__name__}:{exc}"
    return {
        "integrity_check": integrity,
        "foreign_key_violations": foreign_keys,
        "error": error,
        "valid": not error and integrity == ["ok"] and not foreign_keys,
    }


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def write_supplied_env_uop_provenance_receipt(
    brain_root: str | Path,
    *,
    require_complete_derivation: bool = False,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    source_authority_rows = _archive_member_rows(SUPPLIED_ENV_UOP_EXACT_MEMBERS)
    database_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for member, expected_size, expected_hash in supplied_env_uop_database_rows():
        path = root / Path(member)
        observed_hash = _sha256_file(path) if path.is_file() else "MISSING"
        observed_size = path.stat().st_size if path.is_file() else 0
        sqlite_validation = (
            _sqlite_read_validation(path)
            if path.is_file()
            else {
                "integrity_check": [],
                "foreign_key_violations": [],
                "error": "MISSING",
                "valid": False,
            }
        )
        if observed_size != expected_size:
            errors.append(f"DATABASE_SIZE_MISMATCH:{member}")
        if observed_hash != expected_hash:
            errors.append(f"DATABASE_HASH_MISMATCH:{member}")
        if not sqlite_validation["valid"]:
            errors.append(f"DATABASE_SQLITE_INVALID:{member}")
        database_rows.append(
            {
                "member": member,
                "source_byte_size": expected_size,
                "source_sha256": expected_hash,
                "installed_byte_size": observed_size,
                "installed_sha256": observed_hash,
                "exact_source_bytes": (
                    observed_size == expected_size and observed_hash == expected_hash
                ),
                "sqlite_validation": sqlite_validation,
            }
        )

    derived_rows: list[dict[str, Any]] = []
    missing_derived: list[str] = []
    for member in DERIVED_ENV_UOP_AUTHORITY_MEMBERS:
        path = root / Path(member)
        if not path.is_file():
            missing_derived.append(member)
            continue
        derived_rows.append(
            {
                "member": member,
                "byte_size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    if require_complete_derivation and missing_derived:
        errors.extend(f"DERIVED_MEMBER_MISSING:{member}" for member in missing_derived)

    derivation_input = {
        "source_archive_sha256": LOCKED_READ_ARCHIVE_SHA256,
        "source_authority_members": source_authority_rows,
        "database_authority": database_rows,
        "derived_members": derived_rows,
    }
    payload = {
        "contract": "EVIDENCE_LANE_SUPPLIED_ENV_UOP_AUTHORITY_PROVENANCE_V1",
        "status": (
            "PASS"
            if not errors and not missing_derived
            else "PASS_DATABASE_AUTHORITY_DERIVATION_PENDING"
            if not errors
            else "FAIL"
        ),
        "errors": errors,
        "source_archive_sha256": LOCKED_READ_ARCHIVE_SHA256,
        "source_archive_size": LOCKED_READ_ARCHIVE_SIZE,
        "source_archive_member_count": LOCKED_READ_MEMBER_COUNT,
        "source_archive_provider_scope": "CHATGPT_GEMINI_COMMON_ENV_UOP",
        "codex_support_source_archive_sha256": CODEX_ARCHIVE_SHA256,
        "codex_support_source_archive_size": CODEX_ARCHIVE_SIZE,
        "provider_authority_split_validated": True,
        "source_archive_mounted_or_nested": False,
        "locked_read_only_authorities": ["env", "uop"],
        "project_authority": "LIVE_GOVERNED_SECTOR_GRAPH",
        "project_template_authority_imported": False,
        "database_authority": database_rows,
        "source_authority_members": source_authority_rows,
        "source_mmd_base_sha256": {
            str(row["member"]): str(row["sha256"])
            for row in source_authority_rows
            if str(row["member"]).endswith("_mmd.mmd")
        },
        "derived_artifact_policy": (
            "DETERMINISTIC_FROM_EXACT_ENV_UOP_SQLITE_AND_LIVE_PROJECT_POINTER_STATE"
        ),
        "derived_members": derived_rows,
        "missing_derived_members": missing_derived,
        "codex_support_member_count": len(SUPPLIED_CODEX_SUPPORT_MEMBERS),
        "codex_support_imported_into_brain_or_provider_base": False,
        "derivation_input_sha256": _canonical_sha256(derivation_input),
    }
    receipt_path = root / Path(SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT)
    _replace_if_different(
        receipt_path,
        (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return {**payload, "receipt_path": str(receipt_path)}


def install_supplied_env_uop_database_authority(
    brain_root: str | Path,
) -> dict[str, Any]:
    """Install the exact common Env/UOP authority from the split source.

    The SQLite, law, MMD and rendered MMD bytes are copied from the supplied
    ChatGPT/Gemini authority. DOT and live pointers are derived later from this
    exact base plus the current governed Project mutable-sector router. The
    source ZIP is validation input, never a mounted or nested package member.
    Project-template and Codex-support members are deliberately not imported.
    """

    root = Path(brain_root).resolve()
    archive_path = find_locked_read_archive()
    split_validation = validate_split_authority_archives()
    if not split_validation.valid:
        raise Env15LockedReadError(
            "ENV15_SUPPLIED_SPLIT_ARCHIVE_VALIDATION_FAILED:"
            + ";".join(split_validation.errors)
        )
    changed: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        for member in SUPPLIED_ENV_UOP_EXACT_MEMBERS:
            path = root / Path(member)
            if _replace_if_different(path, archive.read(member)):
                changed.append(member)
            if member in SUPPLIED_ENV_UOP_DATABASE_MEMBERS:
                path.chmod(path.stat().st_mode & ~stat.S_IWRITE)
    receipt = write_supplied_env_uop_provenance_receipt(root)
    if receipt["status"] == "FAIL":
        raise Env15LockedReadError(
            "ENV15_SUPPLIED_DATABASE_AUTHORITY_FAILED:" + ";".join(receipt["errors"])
        )
    return {
        "contract": "EVIDENCE_LANE_SUPPLIED_ENV_UOP_DATABASE_AUTHORITY_V1",
        "status": "PASS",
        "source_archive_sha256": LOCKED_READ_ARCHIVE_SHA256,
        "source_archive_provider_scope": "CHATGPT_GEMINI_COMMON_ENV_UOP",
        "installed_members": list(SUPPLIED_ENV_UOP_EXACT_MEMBERS),
        "exact_mmd_base_members": [
            "env/env_mmd.mmd",
            "uop/uop_mmd.mmd",
        ],
        "changed_members": changed,
        "raw_archive_mounted": False,
        "project_template_imported": False,
        "codex_support_imported": False,
        "provenance": receipt,
    }


def verify_supplied_env_uop_database_authority(
    brain_root: str | Path,
    *,
    require_complete_derivation: bool = False,
) -> dict[str, Any]:
    root = Path(brain_root).resolve()
    receipt = write_supplied_env_uop_provenance_receipt(
        root,
        require_complete_derivation=require_complete_derivation,
    )
    errors = list(receipt["errors"])
    if (root / "public_read").exists():
        errors.append("RETIRED_PUBLIC_READ_CONTAINER_PRESENT")
    for relative in (
        "project/project_template.sqlite",
        "project/project_topology_template.mmd",
        "project/project_topology_template.svg",
        "project/project_topology_template.png",
    ):
        if (root / Path(relative)).exists():
            errors.append(f"RETIRED_PROJECT_TEMPLATE_PRESENT:{relative}")
    if require_complete_derivation and receipt["missing_derived_members"]:
        errors.extend(
            f"DERIVED_MEMBER_MISSING:{member}"
            for member in receipt["missing_derived_members"]
            if f"DERIVED_MEMBER_MISSING:{member}" not in errors
        )
    return {
        **receipt,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def install_supplied_codex_support(destination_root: str | Path) -> dict[str, Any]:
    root = Path(destination_root).resolve()
    archive_path = find_codex_locked_read_archive()
    split_validation = validate_split_authority_archives()
    if not split_validation.valid:
        raise Env15LockedReadError(
            "ENV15_SUPPLIED_SPLIT_ARCHIVE_VALIDATION_FAILED:"
            + ";".join(split_validation.errors)
        )
    rows: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive_path) as archive:
        for member, expected_size, expected_hash in supplied_codex_support_rows():
            payload = archive.read(member)
            target = root / Path(member)
            _replace_if_different(target, payload)
            rows.append(
                {
                    "member": member,
                    "byte_size": expected_size,
                    "sha256": expected_hash,
                }
            )
    provenance = {
        "contract": "EVIDENCE_LANE_SUPPLIED_CODEX_SUPPORT_PROVENANCE_V1",
        "status": "PASS",
        "source_archive_sha256": CODEX_ARCHIVE_SHA256,
        "source_archive_size": CODEX_ARCHIVE_SIZE,
        "source_archive_mounted_or_nested": False,
        "support_member_count": len(rows),
        "support_members": rows,
        "provider_scope": "CODEX_ONLY",
        "non_support_source_evidence_imported": False,
        "chatgpt_gemini_common_archive_sha256": CHATGPT_GEMINI_ARCHIVE_SHA256,
    }
    _replace_if_different(
        root / Path(SUPPLIED_CODEX_SUPPORT_PROVENANCE),
        (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return provenance


def verify_supplied_codex_support(destination_root: str | Path) -> dict[str, Any]:
    root = Path(destination_root).resolve()
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    for member, expected_size, expected_hash in supplied_codex_support_rows():
        path = root / Path(member)
        observed_size = path.stat().st_size if path.is_file() else 0
        observed_hash = _sha256_file(path) if path.is_file() else "MISSING"
        if observed_size != expected_size:
            errors.append(f"CODEX_SUPPORT_SIZE_MISMATCH:{member}")
        if observed_hash != expected_hash:
            errors.append(f"CODEX_SUPPORT_HASH_MISMATCH:{member}")
        rows.append(
            {
                "member": member,
                "expected_byte_size": expected_size,
                "expected_sha256": expected_hash,
                "observed_byte_size": observed_size,
                "observed_sha256": observed_hash,
            }
        )
    if (root / Path(LOCKED_READ_PACKAGE_MEMBER)).exists():
        errors.append("RAW_AUTHORITY_ZIP_NESTED_IN_CODEX")
    return {
        "contract": "EVIDENCE_LANE_SUPPLIED_CODEX_SUPPORT_PROVENANCE_V1",
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "support_member_count": len(rows),
        "support_members": rows,
    }


def install_locked_public_read_authority(brain_root: str | Path) -> dict[str, Any]:
    """Compatibility entrypoint for the retired public-read mount.

    The name is retained for callers, but the operation now installs only the
    exact Env/UOP database authority. It never creates ``public_read`` and
    never imports the retired Project template.
    """

    return install_supplied_env_uop_database_authority(brain_root)


def verify_locked_public_read_authority(brain_root: str | Path) -> dict[str, Any]:
    return verify_supplied_env_uop_database_authority(brain_root)


__all__ = [
    "CHATGPT_GEMINI_ARCHIVE_NAME",
    "CHATGPT_GEMINI_ARCHIVE_SHA256",
    "CHATGPT_GEMINI_ARCHIVE_SIZE",
    "CODEX_ARCHIVE_NAME",
    "CODEX_ARCHIVE_SHA256",
    "CODEX_ARCHIVE_SIZE",
    "Env15LockedReadError",
    "LOCKED_READ_ARCHIVE_NAME",
    "LOCKED_READ_ARCHIVE_SHA256",
    "LOCKED_READ_ARCHIVE_SIZE",
    "LOCKED_READ_MEMBER_COUNT",
    "LOCKED_READ_PACKAGE_MEMBER",
    "LOCKED_ROOT_OVERLAY_MEMBERS",
    "DERIVED_ENV_UOP_AUTHORITY_MEMBERS",
    "SUPPLIED_CODEX_SUPPORT_MEMBERS",
    "SUPPLIED_CODEX_SUPPORT_PROVENANCE",
    "SUPPLIED_ENV_UOP_EXACT_MEMBERS",
    "SUPPLIED_ENV_UOP_DATABASE_MEMBERS",
    "SUPPLIED_ENV_UOP_PROVENANCE_RECEIPT",
    "LockedReadValidation",
    "SplitAuthorityValidation",
    "find_codex_locked_read_archive",
    "find_locked_read_archive",
    "install_supplied_codex_support",
    "install_supplied_env_uop_database_authority",
    "install_locked_public_read_authority",
    "read_chatgpt_gemini_authority_member",
    "supplied_codex_support_rows",
    "supplied_env_uop_database_rows",
    "validate_locked_read_archive",
    "validate_split_authority_archives",
    "verify_supplied_codex_support",
    "verify_supplied_env_uop_database_authority",
    "verify_locked_public_read_authority",
    "write_supplied_env_uop_provenance_receipt",
]
