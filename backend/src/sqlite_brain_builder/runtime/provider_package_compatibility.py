from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from sqlite_brain_builder.runtime.provider_package_projection import (
    CHATGPT_PACKAGE_MAX_BYTES,
    GEMINI_PACKAGE_MAX_BYTES,
)


CONTRACT = "T023_PROVIDER_PACKAGE_CONSUMER_COMPATIBILITY_V1"
CODEX_PACKAGE_MAX_BYTES = 500_000_000
PKZIP_20_MAX_EXTRACT_VERSION = 20
ALLOWED_COMPRESSION_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


class ProviderPackageCompatibilityError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _extra_field_ids(extra: bytes) -> list[int]:
    ids: list[int] = []
    offset = 0
    while offset + 4 <= len(extra):
        field_id, field_size = struct.unpack_from("<HH", extra, offset)
        offset += 4
        ids.append(int(field_id))
        offset += int(field_size)
    if offset != len(extra):
        ids.append(-1)
    return ids


def _local_header(archive_path: Path, info: zipfile.ZipInfo) -> dict[str, Any]:
    with archive_path.open("rb") as stream:
        stream.seek(info.header_offset)
        fixed = stream.read(30)
        if len(fixed) != 30 or fixed[:4] != b"PK\x03\x04":
            return {"status": "FAIL", "error": "LOCAL_HEADER_INVALID"}
        (
            _signature,
            version_needed,
            flags,
            compression,
            _time,
            _date,
            _crc,
            _compressed_size,
            _uncompressed_size,
            name_length,
            extra_length,
        ) = struct.unpack("<IHHHHHIIIHH", fixed)
        encoded_name = stream.read(name_length)
        extra = stream.read(extra_length)
    return {
        "status": "PASS",
        "version_needed": int(version_needed),
        "flags": int(flags),
        "compression": int(compression),
        "encoded_name_hex": encoded_name.hex(),
        "extra_field_ids": _extra_field_ids(extra),
    }


def _safe_member(name: str) -> bool:
    if not name or "\\" in name or ":" in name:
        return False
    member = PurePosixPath(name)
    return not member.is_absolute() and all(part not in {"", ".", ".."} for part in member.parts)


def _nested_zip_validation(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    data = archive.read(name)
    try:
        with zipfile.ZipFile(io.BytesIO(data), "r") as nested:
            bad_crc = nested.testzip()
            return {
                "member": name,
                "status": "PASS" if bad_crc is None else "FAIL",
                "member_count": len(nested.infolist()),
                "zip_crc": "PASS" if bad_crc is None else "FAIL",
                "sha256": hashlib.sha256(data).hexdigest().upper(),
            }
    except (OSError, zipfile.BadZipFile):
        return {
            "member": name,
            "status": "FAIL",
            "member_count": 0,
            "zip_crc": "FAIL",
            "sha256": hashlib.sha256(data).hexdigest().upper(),
        }


def audit_provider_package(
    package: str | Path,
    provider: str,
    *,
    read_all_members: bool = True,
) -> dict[str, Any]:
    """Audit the consumer-facing ZIP profile independently of package semantics.

    The audit intentionally rejects forced ZIP64, encryption, data descriptors,
    unsupported compression methods, unsafe/colliding names, and unreadable
    members. Provider upload itself remains a human HIL action.
    """

    path = Path(package).resolve()
    normalized_provider = str(provider or "").strip().upper().replace("/", "_")
    policies = {
        "GEMINI": {
            "maximum_bytes": GEMINI_PACKAGE_MAX_BYTES,
            "exact_member_count": 10,
            "nested_policy": "FORBID",
        },
        "CHATGPT_LOCALAI": {
            "maximum_bytes": CHATGPT_PACKAGE_MAX_BYTES,
            "exact_member_count": None,
            "nested_policy": "FORBID",
        },
        "CODEX": {
            "maximum_bytes": CODEX_PACKAGE_MAX_BYTES,
            "exact_member_count": None,
            "nested_policy": "FORBID",
        },
    }
    if normalized_provider not in policies:
        raise ProviderPackageCompatibilityError(
            f"PROVIDER_PACKAGE_COMPATIBILITY_PROVIDER_UNKNOWN:{normalized_provider}"
        )
    policy = policies[normalized_provider]
    errors: list[str] = []
    if not path.is_file():
        return {
            "contract": CONTRACT,
            "provider": normalized_provider,
            "package": str(path),
            "status": "FAIL",
            "errors": ["PACKAGE_MISSING"],
            "provider_upload_exercised": False,
        }

    package_byte_size = path.stat().st_size
    if package_byte_size > int(policy["maximum_bytes"]):
        errors.append(
            f"PACKAGE_SIZE_LIMIT_EXCEEDED:{package_byte_size}>{policy['maximum_bytes']}"
        )

    members: list[dict[str, Any]] = []
    nested_results: list[dict[str, Any]] = []
    names: list[str] = []
    bad_crc: str | None = None
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            bad_crc = archive.testzip()
            if bad_crc:
                errors.append(f"ZIP_CRC_FAILED:{bad_crc}")
            expected_count = policy["exact_member_count"]
            if expected_count is not None and len(infos) != int(expected_count):
                errors.append(f"MEMBER_COUNT:{len(infos)}!={expected_count}")
            if len(names) != len(set(names)):
                errors.append("DUPLICATE_MEMBER_NAME")
            casefolded = [name.casefold() for name in names]
            if len(casefolded) != len(set(casefolded)):
                errors.append("CASE_COLLIDING_MEMBER_NAME")

            for info in infos:
                member_errors: list[str] = []
                if info.is_dir() or not _safe_member(info.filename):
                    member_errors.append("UNSAFE_OR_DIRECTORY_MEMBER")
                if info.compress_type not in ALLOWED_COMPRESSION_METHODS:
                    member_errors.append(f"COMPRESSION_METHOD:{info.compress_type}")
                if info.flag_bits & 0x1:
                    member_errors.append("ENCRYPTED")
                if info.flag_bits & 0x8:
                    member_errors.append("DATA_DESCRIPTOR")
                central_extra_ids = _extra_field_ids(info.extra)
                if 0x0001 in central_extra_ids:
                    member_errors.append("CENTRAL_ZIP64_EXTRA")
                if info.extract_version > PKZIP_20_MAX_EXTRACT_VERSION:
                    member_errors.append(f"EXTRACT_VERSION:{info.extract_version}")
                local = _local_header(path, info)
                if local.get("status") != "PASS":
                    member_errors.append("LOCAL_HEADER_INVALID")
                else:
                    if int(local["version_needed"]) > PKZIP_20_MAX_EXTRACT_VERSION:
                        member_errors.append(
                            f"LOCAL_EXTRACT_VERSION:{local['version_needed']}"
                        )
                    if 0x0001 in set(local["extra_field_ids"]):
                        member_errors.append("LOCAL_ZIP64_EXTRA")
                    if int(local["compression"]) != info.compress_type:
                        member_errors.append("LOCAL_CENTRAL_COMPRESSION_MISMATCH")
                digest = hashlib.sha256()
                bytes_read = 0
                prefix = b""
                if read_all_members:
                    with archive.open(info, "r") as stream:
                        while True:
                            block = stream.read(8 * 1024 * 1024)
                            if not block:
                                break
                            if not prefix:
                                prefix = block[:4]
                            digest.update(block)
                            bytes_read += len(block)
                    if bytes_read != info.file_size:
                        member_errors.append(
                            f"UNCOMPRESSED_SIZE_MISMATCH:{bytes_read}!={info.file_size}"
                        )
                else:
                    with archive.open(info, "r") as stream:
                        prefix = stream.read(4)
                is_nested = info.filename.casefold().endswith(".zip") or prefix == b"PK\x03\x04"
                if is_nested:
                    if policy["nested_policy"] == "FORBID":
                        member_errors.append("NESTED_ZIP_FORBIDDEN")
                    nested_results.append(_nested_zip_validation(archive, info.filename))
                if member_errors:
                    errors.extend(
                        f"MEMBER:{info.filename}:{error}" for error in member_errors
                    )
                members.append(
                    {
                        "name": info.filename,
                        "uncompressed_bytes": info.file_size,
                        "compressed_bytes": info.compress_size,
                        "compression_method": info.compress_type,
                        "flags": info.flag_bits,
                        "extract_version": info.extract_version,
                        "central_extra_field_ids": central_extra_ids,
                        "local_header": local,
                        "sha256": digest.hexdigest().upper() if read_all_members else None,
                        "errors": member_errors,
                    }
                )
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        errors.append(f"PACKAGE_OPEN_FAILED:{type(exc).__name__}")

    if any(result["status"] != "PASS" for result in nested_results):
        errors.append("NESTED_ZIP_VALIDATION_FAILED")
    return {
        "contract": CONTRACT,
        "provider": normalized_provider,
        "package": str(path),
        "package_sha256": _sha256_file(path),
        "package_byte_size": package_byte_size,
        "provider_size_limit_bytes": int(policy["maximum_bytes"]),
        "zip_profile": "PKZIP_2_0_STORE_OR_DEFLATE_NO_ZIP64_NO_ENCRYPTION",
        "member_count": len(names),
        "total_uncompressed_bytes": sum(item["uncompressed_bytes"] for item in members),
        "maximum_member_uncompressed_bytes": max(
            (item["uncompressed_bytes"] for item in members), default=0
        ),
        "maximum_extract_version": max(
            (item["extract_version"] for item in members), default=0
        ),
        "zip_crc": "PASS" if bad_crc is None else "FAIL",
        "nested_archives": nested_results,
        "members": members,
        "read_all_members": read_all_members,
        "provider_upload_exercised": False,
        "provider_ingestion_status": "HUMAN_FRESH_BRAIN_HIL_PENDING",
        "errors": errors,
        "status": "PASS" if not errors else "FAIL",
    }


__all__ = [
    "ALLOWED_COMPRESSION_METHODS",
    "CONTRACT",
    "PKZIP_20_MAX_EXTRACT_VERSION",
    "ProviderPackageCompatibilityError",
    "audit_provider_package",
]
