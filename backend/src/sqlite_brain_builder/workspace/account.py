from __future__ import annotations
import os, hashlib, hmac


def hash_password(password: str, salt: bytes | None = None) -> tuple[str,str]:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 200_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, digest = hash_password(password, salt)
    return hmac.compare_digest(digest, digest_hex)
