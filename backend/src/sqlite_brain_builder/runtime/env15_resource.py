from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
import zipfile
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ENV15_RESOURCE_DIRECTORY = "public_model_env15"
ENV15_PACKAGE_CLASS = "UEPC_ENV15_PUBLIC_FULL_LOCKED_RUNTIME"
ENV15_SOURCE_PACKAGE_SHA256 = "DFA0B8AEF82CFF6C9AC70B6FE8789F1C542B7CD11D2FAE445CB260080C1041AB"
ENV15_SOURCE_PACKAGE_SIZE = 11_465_552
ENV15_EXPECTED_MEMBER_COUNT = 100
ENV15_EXPECTED_SQLITE_COUNT = 18
ENV15_EXPECTED_SECTOR_COUNT = 14
ENV15_PAYLOAD_TREE_SHA256 = "75D856B70E6EAAE695553446ADC1A0DE5C8BB13C1FFE4A4BF4B6809A88301F42"
ENV15_MEMBER_LEDGER_SHA256 = "04E7B3C076D9A38E9CC9403DF2E1AD86E6DAA5A92626BB7B51CF550A9C7FD3CF"
ENV15_MEMBER_CRC_LEDGER_SHA256 = "DD31753BA34E5A19A20411F4C0164A5D61FC9570DC2DF9EE9B6F773478E0E6DC"
MMD_SEMANTIC_VALIDATION_STATUS = "REQUIRES_LATER_PROMPT_ROOTED_FULL_FLOW_CONTRACT_VALIDATION"

EXPECTED_SECTORS = frozenset(
    {
        "artifacts",
        "brain_loader",
        "chat_lineage",
        "custom",
        "data_excel",
        "docs",
        "github_code",
        "images_ocr",
        "local_code",
        "pdf_ocr",
        "ppt",
        "project_engulf",
        "research",
        "sqlite_brain",
    }
)

EXPECTED_SQLITE_PATHS = frozenset(
    {
        "env/env_sqlite.sqlite",
        "project/project_router.sqlite",
        "project/project_template.sqlite",
        "project/sectors/artifacts/artifacts_sector_v001.sqlite",
        "project/sectors/brain_loader/brain_loader_sector_v001.sqlite",
        "project/sectors/chat_lineage/chat_lineage_sector_v001.sqlite",
        "project/sectors/custom/custom_sector_v001.sqlite",
        "project/sectors/data_excel/data_excel_sector_v001.sqlite",
        "project/sectors/docs/docs_sector_v001.sqlite",
        "project/sectors/github_code/github_code_sector_v001.sqlite",
        "project/sectors/images_ocr/images_ocr_sector_v001.sqlite",
        "project/sectors/local_code/local_code_sector_v001.sqlite",
        "project/sectors/pdf_ocr/pdf_ocr_sector_v001.sqlite",
        "project/sectors/ppt/ppt_sector_v001.sqlite",
        "project/sectors/project_engulf/project_engulf_sector_v001.sqlite",
        "project/sectors/research/research_sector_v001.sqlite",
        "project/sectors/sqlite_brain/sqlite_brain_sector_v001.sqlite",
        "uop/uop_sqlite.sqlite",
    }
)

REQUIRED_MMD_ASSETS = (
    "env/env_mmd.mmd",
    "env/env_mmd.svg",
    "env/env_mmd.png",
    "uop/uop_mmd.mmd",
    "uop/uop_mmd.svg",
    "uop/uop_mmd.png",
    "project/project_topology_template.mmd",
    "project/project_topology_template.svg",
    "project/project_topology_template.png",
)

ACTIVE_RUNTIME_METADATA = (
    ".uepc_env",
    ".uepc_profile",
    ".uepc_project",
    "manifests/PACKAGE_CLASS.txt",
    "manifests/POINTER_SUMMARY.txt",
    "manifests/PROJECT_SECTOR_REGISTRY.json",
    "manifests/PUBLIC_SECTION_LOCK_REGISTRY.json",
    "project/PROJECT_SECTOR_REGISTRY.json",
)

POINTER_PATH_KEYS = {
    ".uepc_env": (
        "ENV_SQLITE",
        "ENV_LAW",
        "ENV_MMD",
        "ENV_MMD_SVG",
        "ENV_MMD_PNG",
        "PROFILE_POINTER",
        "PROJECT_POINTER",
        "PROJECT_ROUTER",
        "CHAT_LINEAGE_POINTER",
        "LAST_EXIT_RECEIPT",
    ),
    ".uepc_profile": ("UOP_SQLITE", "UOP_LAW", "UOP_MMD", "UOP_MMD_SVG", "UOP_MMD_PNG"),
    ".uepc_project": (
        "PROJECT_TEMPLATE_SQLITE",
        "PROJECT_ROUTER_SQLITE",
        "PROJECT_TOPOLOGY_MMD",
        "PROJECT_TOPOLOGY_SVG",
        "PROJECT_TOPOLOGY_PNG",
        "CHAT_LINEAGE_SQLITE",
    ),
}

PRIOR_ENV_LABEL = re.compile(
    r"(?i)(?:\bUEPC[_ -]?ENV(?:0?[1-9]|1[0-4])\b|"
    r"\bENV(?:0?[1-9]|1[0-4])\b|"
    r"\b(?:UEPC_)?ENV_VERSION\s*[=:]\s*V(?:0?[1-9]|1[0-4])\b|"
    r"\bV(?:0?[1-9]|1[0-4])\b)"
)
PAYLOAD_ENTRY = re.compile(r"^(.+?)\|(\d+)\|([0-9a-fA-F]{64})$")


class Env15ResourceError(RuntimeError):
    """Base error for the isolated Env15 resource manager."""


class Env15ResourceNotFoundError(Env15ResourceError):
    """Raised when no embedded Env15 resource root can be located."""


class Env15ResourceValidationError(Env15ResourceError):
    def __init__(self, report: "Env15ValidationReport") -> None:
        self.report = report
        joined = "; ".join(report.errors) or "unknown validation failure"
        super().__init__(f"ENV15_RESOURCE_VALIDATION_FAILED: {joined}")


@dataclass(frozen=True)
class SQLiteValidationResult:
    relative_path: str
    sha256: str
    integrity_check: tuple[str, ...]
    foreign_key_violation_count: int
    foreign_key_violation_sample: tuple[tuple[Any, ...], ...]
    valid: bool


@dataclass(frozen=True)
class Env15ValidationReport:
    resource_root: str
    structural_validation_passed: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    source_package_sha256_expected: str
    source_package_size_expected: int
    source_package_provenance: str
    expected_member_count: int
    actual_member_count: int
    missing_members: tuple[str, ...]
    unexpected_members: tuple[str, ...]
    size_mismatches: tuple[str, ...]
    hash_mismatches: tuple[str, ...]
    expected_member_ledger_sha256: str
    actual_member_ledger_sha256: str
    expected_crc_ledger_sha256: str
    actual_crc_ledger_sha256: str
    member_hashes_valid: bool
    member_crc_valid: bool
    pointers_valid: bool
    pointer_values: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    expected_sector_count: int
    actual_sector_count: int
    sectors: tuple[str, ...]
    sector_registry_valid: bool
    expected_sqlite_count: int
    actual_sqlite_count: int
    sqlite_results: tuple[SQLiteValidationResult, ...]
    mmd_assets_present: tuple[tuple[str, bool], ...]
    prior_version_label_hits: tuple[str, ...]
    mmd_semantic_validation_required: bool
    mmd_semantic_validation_status: str
    mmd_semantic_acceptance_claimed: bool
    validation_scope: str

    @property
    def valid(self) -> bool:
        return self.structural_validation_passed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Env15ArchiveValidationReport:
    archive_path: str
    valid: bool
    errors: tuple[str, ...]
    archive_sha256: str
    expected_archive_sha256: str
    archive_size: int
    expected_archive_size: int
    member_count: int
    expected_member_count: int
    crc_status: str
    first_bad_crc_member: str | None
    missing_members: tuple[str, ...]
    unexpected_members: tuple[str, ...]
    duplicate_members: tuple[str, ...]
    unsafe_members: tuple[str, ...]
    size_mismatches: tuple[str, ...]
    hash_mismatches: tuple[str, ...]
    actual_member_ledger_sha256: str
    actual_crc_ledger_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Env15InstallResult:
    source_root: str
    destination_root: str
    atomic_commit: bool
    installed_member_count: int
    installed_member_ledger_sha256: str
    validation: Env15ValidationReport

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _crc32(path: Path) -> str:
    value = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value = zlib.crc32(block, value)
    return f"{value & 0xFFFFFFFF:08X}"


def _safe_member_name(name: str) -> bool:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    return bool(normalized) and not pure.is_absolute() and ".." not in pure.parts and not re.match(r"^[A-Za-z]:", normalized)


def _member_ledger(manifest: dict[str, tuple[int, str]]) -> str:
    body = "".join(
        f"{name}\0{size}\0{digest.upper()}\n"
        for name, (size, digest) in sorted(manifest.items())
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest().upper()


def _crc_ledger(entries: Iterable[tuple[str, int, str]]) -> str:
    body = "".join(
        f"{name}\0{size}\0{crc.upper()}\n"
        for name, size, crc in sorted(entries, key=lambda item: item[0])
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest().upper()


def _runtime_resource_candidates() -> list[Path]:
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "sqlite_brain_builder" / "resources" / ENV15_RESOURCE_DIRECTORY)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "resources" / ENV15_RESOURCE_DIRECTORY)
    candidates.append(Path(__file__).resolve().parents[1] / "resources" / ENV15_RESOURCE_DIRECTORY)
    return candidates


def find_env15_resource_root() -> Path:
    checked: list[str] = []
    for candidate in _runtime_resource_candidates():
        resolved = candidate.resolve()
        checked.append(str(resolved))
        if (resolved / ".uepc_env").is_file() and (resolved / ".uepc_profile").is_file() and (resolved / ".uepc_project").is_file():
            return resolved
    raise Env15ResourceNotFoundError("EMBEDDED_ENV15_RESOURCE_NOT_FOUND. Checked: " + "; ".join(checked))


@functools.lru_cache(maxsize=1)
def _expected_member_manifest() -> dict[str, tuple[int, str]]:
    canonical_root = find_env15_resource_root()
    payload_path = canonical_root / "manifests" / "PAYLOAD_TREE_HASH.txt"
    if _sha256(payload_path) != ENV15_PAYLOAD_TREE_SHA256:
        raise Env15ResourceError("ENV15_PAYLOAD_TREE_MANIFEST_SHA256_MISMATCH")

    manifest: dict[str, tuple[int, str]] = {}
    for line in payload_path.read_text(encoding="utf-8").splitlines():
        match = PAYLOAD_ENTRY.match(line)
        if not match:
            continue
        name, size, digest = match.groups()
        if not _safe_member_name(name):
            raise Env15ResourceError(f"ENV15_PAYLOAD_TREE_UNSAFE_MEMBER: {name}")
        if name in manifest:
            raise Env15ResourceError(f"ENV15_PAYLOAD_TREE_DUPLICATE_MEMBER: {name}")
        manifest[name] = (int(size), digest.upper())

    manifest["manifests/PAYLOAD_TREE_HASH.txt"] = (
        payload_path.stat().st_size,
        ENV15_PAYLOAD_TREE_SHA256,
    )
    if len(manifest) != ENV15_EXPECTED_MEMBER_COUNT:
        raise Env15ResourceError(
            f"ENV15_EXPECTED_MEMBER_COUNT_MISMATCH: {len(manifest)} != {ENV15_EXPECTED_MEMBER_COUNT}"
        )
    ledger = _member_ledger(manifest)
    if ledger != ENV15_MEMBER_LEDGER_SHA256:
        raise Env15ResourceError(
            f"ENV15_EXPECTED_MEMBER_LEDGER_MISMATCH: {ledger} != {ENV15_MEMBER_LEDGER_SHA256}"
        )
    return manifest


def _parse_pointer(path: Path) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    errors: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"POINTER_INVALID_LINE:{path.name}:{line_number}")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key in values:
            errors.append(f"POINTER_DUPLICATE_KEY:{path.name}:{key}")
        values[key] = value
    return values, errors


def _is_within(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _validate_mmd_asset(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    suffix = path.suffix.lower()
    if suffix == ".png":
        return path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    if suffix == ".svg":
        return "<svg" in path.read_text(encoding="utf-8", errors="replace").lower()
    return bool(path.read_text(encoding="utf-8", errors="replace").strip())


def _sqlite_validation(path: Path, relative_path: str) -> SQLiteValidationResult:
    uri = "file:" + urllib.parse.quote(str(path.resolve()).replace("\\", "/"), safe="/:") + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only=ON")
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall())
        foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall())
        connection.close()
        valid = integrity == ("ok",) and not foreign_keys
        return SQLiteValidationResult(
            relative_path=relative_path,
            sha256=_sha256(path),
            integrity_check=integrity,
            foreign_key_violation_count=len(foreign_keys),
            foreign_key_violation_sample=foreign_keys[:10],
            valid=valid,
        )
    except sqlite3.DatabaseError as exc:
        return SQLiteValidationResult(
            relative_path=relative_path,
            sha256=_sha256(path),
            integrity_check=(f"ERROR:{exc}",),
            foreign_key_violation_count=-1,
            foreign_key_violation_sample=(),
            valid=False,
        )


def validate_env15_resource(resource_root: str | Path | None = None) -> Env15ValidationReport:
    root = Path(resource_root).resolve() if resource_root is not None else find_env15_resource_root()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        expected = _expected_member_manifest()
    except Env15ResourceError as exc:
        expected = {}
        errors.append(str(exc))

    if not root.is_dir():
        errors.append(f"RESOURCE_ROOT_NOT_DIRECTORY:{root}")

    actual_files: dict[str, Path] = {}
    if root.is_dir():
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                errors.append(f"RESOURCE_SYMLINK_FORBIDDEN:{candidate.relative_to(root).as_posix()}")
                continue
            if candidate.is_file():
                relative = candidate.relative_to(root).as_posix()
                if not _safe_member_name(relative):
                    errors.append(f"RESOURCE_UNSAFE_MEMBER:{relative}")
                actual_files[relative] = candidate

    expected_names = set(expected)
    actual_names = set(actual_files)
    missing = tuple(sorted(expected_names - actual_names))
    unexpected = tuple(sorted(actual_names - expected_names))
    if missing:
        errors.append(f"MISSING_MEMBERS:{len(missing)}")
    if unexpected:
        errors.append(f"UNEXPECTED_MEMBERS:{len(unexpected)}")

    actual_manifest: dict[str, tuple[int, str]] = {}
    size_mismatches: list[str] = []
    hash_mismatches: list[str] = []
    crc_entries: list[tuple[str, int, str]] = []
    for name, path in sorted(actual_files.items()):
        size = path.stat().st_size
        digest = _sha256(path)
        actual_manifest[name] = (size, digest)
        crc_entries.append((name, size, _crc32(path)))
        expected_entry = expected.get(name)
        if expected_entry is None:
            continue
        if size != expected_entry[0]:
            size_mismatches.append(name)
        if digest != expected_entry[1]:
            hash_mismatches.append(name)
    if size_mismatches:
        errors.append(f"MEMBER_SIZE_MISMATCHES:{len(size_mismatches)}")
    if hash_mismatches:
        errors.append(f"MEMBER_SHA256_MISMATCHES:{len(hash_mismatches)}")

    actual_member_ledger = _member_ledger(actual_manifest)
    actual_crc_ledger = _crc_ledger(crc_entries)
    member_hashes_valid = (
        not missing
        and not unexpected
        and not size_mismatches
        and not hash_mismatches
        and actual_member_ledger == ENV15_MEMBER_LEDGER_SHA256
    )
    member_crc_valid = (
        not missing
        and not unexpected
        and actual_crc_ledger == ENV15_MEMBER_CRC_LEDGER_SHA256
    )
    if not member_hashes_valid:
        errors.append("MEMBER_HASH_LEDGER_MISMATCH")
    if not member_crc_valid:
        errors.append("MEMBER_CRC_LEDGER_MISMATCH")

    pointer_values: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    pointers_valid = True
    parsed_pointers: dict[str, dict[str, str]] = {}
    for pointer_name in (".uepc_env", ".uepc_profile", ".uepc_project"):
        pointer_path = root / pointer_name
        if not pointer_path.is_file():
            pointers_valid = False
            errors.append(f"REQUIRED_POINTER_MISSING:{pointer_name}")
            continue
        values, pointer_errors = _parse_pointer(pointer_path)
        parsed_pointers[pointer_name] = values
        errors.extend(pointer_errors)
        if pointer_errors:
            pointers_valid = False
        pointer_values.append((pointer_name, tuple(sorted(values.items()))))
        for key in POINTER_PATH_KEYS[pointer_name]:
            value = values.get(key)
            if not value:
                pointers_valid = False
                errors.append(f"POINTER_REQUIRED_KEY_MISSING:{pointer_name}:{key}")
                continue
            referenced = root / Path(value.replace("/", os.sep))
            if not _is_within(root, referenced) or not referenced.is_file():
                pointers_valid = False
                errors.append(f"POINTER_TARGET_INVALID:{pointer_name}:{key}:{value}")

    required_pointer_values = (
        (".uepc_env", "UEPC_ENV_VERSION", "V15"),
        (".uepc_env", "PACKAGE_CLASS", ENV15_PACKAGE_CLASS),
        (".uepc_profile", "PROFILE_ID", "UOP_PUBLIC_GOVERNANCE_V15"),
        (".uepc_project", "PROJECT_ID", "PUBLIC_PROJECT_FRAMEWORK_V15"),
        (".uepc_project", "PROJECT_SECTOR_COUNT", str(ENV15_EXPECTED_SECTOR_COUNT)),
    )
    for pointer_name, key, expected_value in required_pointer_values:
        actual_value = parsed_pointers.get(pointer_name, {}).get(key)
        if actual_value != expected_value:
            pointers_valid = False
            errors.append(
                f"POINTER_VALUE_MISMATCH:{pointer_name}:{key}:{actual_value!r}!={expected_value!r}"
            )

    package_class_path = root / "manifests" / "PACKAGE_CLASS.txt"
    if not package_class_path.is_file() or ENV15_PACKAGE_CLASS not in package_class_path.read_text(
        encoding="utf-8", errors="replace"
    ):
        errors.append("ENV15_PACKAGE_CLASS_MISSING")

    sectors_root = root / "project" / "sectors"
    actual_sectors = tuple(
        sorted(path.name for path in sectors_root.iterdir() if path.is_dir())
    ) if sectors_root.is_dir() else ()
    if set(actual_sectors) != EXPECTED_SECTORS:
        errors.append("PROJECT_SECTOR_SET_MISMATCH")
    for sector in EXPECTED_SECTORS:
        sector_root = sectors_root / sector
        for suffix in ("_law.md", "_pointer.json", "_sector_v001.sqlite"):
            candidate = sector_root / f"{sector}{suffix}"
            if not candidate.is_file():
                errors.append(f"SECTOR_REQUIRED_FILE_MISSING:{candidate.relative_to(root).as_posix()}")

    sector_registry_valid = True
    registry_path = root / "project" / "PROJECT_SECTOR_REGISTRY.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry_sectors = registry.get("sectors", [])
        registry_ids = {str(item.get("sector_id")) for item in registry_sectors}
        if registry.get("version") != "ENV15" or registry.get("sector_count") != ENV15_EXPECTED_SECTOR_COUNT:
            sector_registry_valid = False
        if registry_ids != EXPECTED_SECTORS or len(registry_sectors) != ENV15_EXPECTED_SECTOR_COUNT:
            sector_registry_valid = False
        for item in registry_sectors:
            sector_id = str(item.get("sector_id"))
            sqlite_path = str(item.get("sqlite_path", ""))
            expected_access = (
                "APPEND_ONLY_READ_WRITE"
                if sector_id == "chat_lineage"
                else "READ_ONLY_UNLESS_EXPLICIT_NAMED_ONE_TURN_GRANT"
            )
            if item.get("access") != expected_access:
                sector_registry_valid = False
            expected_member = expected.get(sqlite_path)
            if expected_member is None or str(item.get("sha256", "")).upper() != expected_member[1]:
                sector_registry_valid = False
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        sector_registry_valid = False
        errors.append(f"PROJECT_SECTOR_REGISTRY_INVALID:{exc}")
    if not sector_registry_valid:
        errors.append("PROJECT_SECTOR_REGISTRY_CONTRACT_MISMATCH")

    actual_sqlite_paths = tuple(
        sorted(name for name in actual_names if name.lower().endswith((".sqlite", ".sqlite3", ".db")))
    )
    if set(actual_sqlite_paths) != EXPECTED_SQLITE_PATHS:
        errors.append("SQLITE_MEMBER_SET_MISMATCH")
    sqlite_results = tuple(
        _sqlite_validation(actual_files[name], name)
        for name in actual_sqlite_paths
        if name in actual_files
    )
    invalid_sqlite = [result.relative_path for result in sqlite_results if not result.valid]
    if invalid_sqlite:
        errors.append(f"SQLITE_VALIDATION_FAILURES:{len(invalid_sqlite)}")

    mmd_assets_present = tuple(
        (name, _validate_mmd_asset(root / Path(name.replace("/", os.sep))))
        for name in REQUIRED_MMD_ASSETS
    )
    if not all(present for _, present in mmd_assets_present):
        errors.append("MMD_ASSET_SET_INVALID")

    prior_hits: list[str] = []
    for name in ACTIVE_RUNTIME_METADATA:
        path = root / Path(name.replace("/", os.sep))
        if not path.is_file():
            continue
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if PRIOR_ENV_LABEL.search(line):
                prior_hits.append(f"{name}:{line_number}:{line.strip()}")
    if prior_hits:
        errors.append(f"PRIOR_ENV_VERSION_LABELS_IN_ACTIVE_METADATA:{len(prior_hits)}")

    if not (root / "manifests" / "PAYLOAD_TREE_HASH.txt").is_file():
        warnings.append("PAYLOAD_TREE_MANIFEST_MISSING")

    structural_valid = not errors
    return Env15ValidationReport(
        resource_root=str(root),
        structural_validation_passed=structural_valid,
        errors=tuple(errors),
        warnings=tuple(warnings),
        source_package_sha256_expected=ENV15_SOURCE_PACKAGE_SHA256,
        source_package_size_expected=ENV15_SOURCE_PACKAGE_SIZE,
        source_package_provenance="PINNED_BY_SOURCE_ARCHIVE_SHA256_AND_EXACT_MEMBER_LEDGER",
        expected_member_count=ENV15_EXPECTED_MEMBER_COUNT,
        actual_member_count=len(actual_files),
        missing_members=missing,
        unexpected_members=unexpected,
        size_mismatches=tuple(size_mismatches),
        hash_mismatches=tuple(hash_mismatches),
        expected_member_ledger_sha256=ENV15_MEMBER_LEDGER_SHA256,
        actual_member_ledger_sha256=actual_member_ledger,
        expected_crc_ledger_sha256=ENV15_MEMBER_CRC_LEDGER_SHA256,
        actual_crc_ledger_sha256=actual_crc_ledger,
        member_hashes_valid=member_hashes_valid,
        member_crc_valid=member_crc_valid,
        pointers_valid=pointers_valid,
        pointer_values=tuple(pointer_values),
        expected_sector_count=ENV15_EXPECTED_SECTOR_COUNT,
        actual_sector_count=len(actual_sectors),
        sectors=actual_sectors,
        sector_registry_valid=sector_registry_valid,
        expected_sqlite_count=ENV15_EXPECTED_SQLITE_COUNT,
        actual_sqlite_count=len(actual_sqlite_paths),
        sqlite_results=sqlite_results,
        mmd_assets_present=mmd_assets_present,
        prior_version_label_hits=tuple(prior_hits),
        mmd_semantic_validation_required=True,
        mmd_semantic_validation_status=MMD_SEMANTIC_VALIDATION_STATUS,
        mmd_semantic_acceptance_claimed=False,
        validation_scope="STRUCTURAL_RESOURCE_VALIDATION_ONLY",
    )


def validate_env15_archive(archive_path: str | Path) -> Env15ArchiveValidationReport:
    path = Path(archive_path).resolve()
    errors: list[str] = []
    expected = _expected_member_manifest()
    archive_sha = _sha256(path) if path.is_file() else ""
    archive_size = path.stat().st_size if path.is_file() else -1
    if archive_sha != ENV15_SOURCE_PACKAGE_SHA256:
        errors.append("SOURCE_ARCHIVE_SHA256_MISMATCH")
    if archive_size != ENV15_SOURCE_PACKAGE_SIZE:
        errors.append("SOURCE_ARCHIVE_SIZE_MISMATCH")

    first_bad_crc_member: str | None = None
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    duplicate_members: tuple[str, ...] = ()
    unsafe_members: tuple[str, ...] = ()
    size_mismatches: list[str] = []
    hash_mismatches: list[str] = []
    actual_member_manifest: dict[str, tuple[int, str]] = {}
    crc_entries: list[tuple[str, int, str]] = []
    member_count = 0
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            member_count = len(infos)
            duplicate_members = tuple(sorted({name for name in names if names.count(name) > 1}))
            unsafe_members = tuple(sorted(name for name in names if not _safe_member_name(name)))
            if any(info.is_dir() for info in infos):
                errors.append("SOURCE_ARCHIVE_EXPLICIT_DIRECTORY_ENTRIES")
            if duplicate_members:
                errors.append("SOURCE_ARCHIVE_DUPLICATE_MEMBERS")
            if unsafe_members:
                errors.append("SOURCE_ARCHIVE_UNSAFE_MEMBERS")
            first_bad_crc_member = archive.testzip()
            if first_bad_crc_member:
                errors.append(f"SOURCE_ARCHIVE_CRC_FAILURE:{first_bad_crc_member}")
            actual_names = set(names)
            missing = tuple(sorted(set(expected) - actual_names))
            unexpected = tuple(sorted(actual_names - set(expected)))
            if missing:
                errors.append(f"SOURCE_ARCHIVE_MISSING_MEMBERS:{len(missing)}")
            if unexpected:
                errors.append(f"SOURCE_ARCHIVE_UNEXPECTED_MEMBERS:{len(unexpected)}")
            for info in infos:
                digest = hashlib.sha256(archive.read(info.filename)).hexdigest().upper()
                actual_member_manifest[info.filename] = (info.file_size, digest)
                crc_entries.append((info.filename, info.file_size, f"{info.CRC:08X}"))
                expected_entry = expected.get(info.filename)
                if expected_entry is None:
                    continue
                if info.file_size != expected_entry[0]:
                    size_mismatches.append(info.filename)
                if digest != expected_entry[1]:
                    hash_mismatches.append(info.filename)
            if size_mismatches:
                errors.append(f"SOURCE_ARCHIVE_MEMBER_SIZE_MISMATCHES:{len(size_mismatches)}")
            if hash_mismatches:
                errors.append(f"SOURCE_ARCHIVE_MEMBER_HASH_MISMATCHES:{len(hash_mismatches)}")
    except (OSError, zipfile.BadZipFile) as exc:
        errors.append(f"SOURCE_ARCHIVE_OPEN_FAILED:{exc}")

    actual_member_ledger = _member_ledger(actual_member_manifest)
    actual_crc_ledger = _crc_ledger(crc_entries)
    if actual_member_ledger != ENV15_MEMBER_LEDGER_SHA256:
        errors.append("SOURCE_ARCHIVE_MEMBER_LEDGER_MISMATCH")
    if actual_crc_ledger != ENV15_MEMBER_CRC_LEDGER_SHA256:
        errors.append("SOURCE_ARCHIVE_CRC_LEDGER_MISMATCH")

    return Env15ArchiveValidationReport(
        archive_path=str(path),
        valid=not errors,
        errors=tuple(errors),
        archive_sha256=archive_sha,
        expected_archive_sha256=ENV15_SOURCE_PACKAGE_SHA256,
        archive_size=archive_size,
        expected_archive_size=ENV15_SOURCE_PACKAGE_SIZE,
        member_count=member_count,
        expected_member_count=ENV15_EXPECTED_MEMBER_COUNT,
        crc_status="PASS" if first_bad_crc_member is None and not errors else "FAIL",
        first_bad_crc_member=first_bad_crc_member,
        missing_members=missing,
        unexpected_members=unexpected,
        duplicate_members=duplicate_members,
        unsafe_members=unsafe_members,
        size_mismatches=tuple(size_mismatches),
        hash_mismatches=tuple(hash_mismatches),
        actual_member_ledger_sha256=actual_member_ledger,
        actual_crc_ledger_sha256=actual_crc_ledger,
    )


def install_env15_resource(
    destination_root: str | Path,
    *,
    resource_root: str | Path | None = None,
) -> Env15InstallResult:
    """Validate and atomically copy Env15 contents into a new brain root.

    ``destination_root`` must not already exist. The function stages a complete
    copy beside the destination, validates the staged bytes and SQLite files,
    then commits with one same-volume rename. It never replaces an existing
    brain and never mutates the embedded resource.
    """

    source = Path(resource_root).resolve() if resource_root is not None else find_env15_resource_root()
    destination = Path(destination_root).resolve()
    if destination.exists():
        raise FileExistsError(f"ENV15_DESTINATION_ALREADY_EXISTS:{destination}")
    if _is_within(source, destination) or _is_within(destination, source):
        raise Env15ResourceError("ENV15_SOURCE_DESTINATION_OVERLAP_FORBIDDEN")

    source_report = validate_env15_resource(source)
    if not source_report.structural_validation_passed:
        raise Env15ResourceValidationError(source_report)

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.env15-stage-", dir=destination.parent)
    )
    staged_root = staging_parent / "payload"
    committed = False
    try:
        shutil.copytree(source, staged_root, copy_function=shutil.copy2, symlinks=False)
        staged_report = validate_env15_resource(staged_root)
        if not staged_report.structural_validation_passed:
            raise Env15ResourceValidationError(staged_report)
        if destination.exists():
            raise FileExistsError(f"ENV15_DESTINATION_ALREADY_EXISTS:{destination}")
        os.replace(staged_root, destination)
        committed = True
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)

    installed_report = validate_env15_resource(destination)
    if not installed_report.structural_validation_passed:
        raise Env15ResourceValidationError(installed_report)
    return Env15InstallResult(
        source_root=str(source),
        destination_root=str(destination),
        atomic_commit=committed,
        installed_member_count=installed_report.actual_member_count,
        installed_member_ledger_sha256=installed_report.actual_member_ledger_sha256,
        validation=installed_report,
    )


__all__ = [
    "ENV15_EXPECTED_MEMBER_COUNT",
    "ENV15_EXPECTED_SECTOR_COUNT",
    "ENV15_EXPECTED_SQLITE_COUNT",
    "ENV15_MEMBER_CRC_LEDGER_SHA256",
    "ENV15_MEMBER_LEDGER_SHA256",
    "ENV15_PACKAGE_CLASS",
    "ENV15_SOURCE_PACKAGE_SHA256",
    "EXPECTED_SECTORS",
    "EXPECTED_SQLITE_PATHS",
    "MMD_SEMANTIC_VALIDATION_STATUS",
    "Env15ArchiveValidationReport",
    "Env15InstallResult",
    "Env15ResourceError",
    "Env15ResourceNotFoundError",
    "Env15ResourceValidationError",
    "Env15ValidationReport",
    "SQLiteValidationResult",
    "find_env15_resource_root",
    "install_env15_resource",
    "validate_env15_archive",
    "validate_env15_resource",
]
