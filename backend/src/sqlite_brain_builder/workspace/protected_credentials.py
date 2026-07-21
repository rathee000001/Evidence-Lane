from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


class ProtectedCredentialError(RuntimeError):
    pass


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _required_label(value: str, field: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise ProtectedCredentialError(f"CREDENTIAL_{field}_REQUIRED")
    if len(clean) > 1024 or any(ord(character) < 32 for character in clean):
        raise ProtectedCredentialError(f"CREDENTIAL_{field}_INVALID")
    return clean


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    if os.name != "nt":
        raise ProtectedCredentialError("WINDOWS_CREDENTIAL_MANAGER_UNAVAILABLE")
    advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    advapi.CredWriteW.restype = wintypes.BOOL
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredDeleteW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [wintypes.LPVOID]
    return advapi


def set_secret(target_name: str, account_name: str, secret: str) -> None:
    target = _required_label(target_name, "TARGET")
    account = _required_label(account_name, "ACCOUNT")
    if not isinstance(secret, str) or not secret:
        raise ProtectedCredentialError("CREDENTIAL_SECRET_REQUIRED")
    api = _api()
    raw = bytearray(secret.encode("utf-16-le"))
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer(raw)
    credential = _CREDENTIALW()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.UserName = account
    credential.CredentialBlobSize = len(raw)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    try:
        if not api.CredWriteW(ctypes.byref(credential), 0):
            raise ProtectedCredentialError(f"WINDOWS_CREDENTIAL_WRITE_FAILED:{ctypes.get_last_error()}")
    finally:
        for index in range(len(raw)):
            raw[index] = 0


def get_secret(target_name: str) -> str:
    target = _required_label(target_name, "TARGET")
    api = _api()
    pointer = ctypes.POINTER(_CREDENTIALW)()
    if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        raise ProtectedCredentialError(f"WINDOWS_CREDENTIAL_READ_FAILED:{ctypes.get_last_error()}")
    try:
        record = pointer.contents
        raw = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def delete_secret(target_name: str) -> None:
    target = _required_label(target_name, "TARGET")
    api = _api()
    if not api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        error = ctypes.get_last_error()
        if error != 1168:  # ERROR_NOT_FOUND keeps deletion idempotent.
            raise ProtectedCredentialError(f"WINDOWS_CREDENTIAL_DELETE_FAILED:{error}")


__all__ = ["ProtectedCredentialError", "set_secret", "get_secret", "delete_secret"]
