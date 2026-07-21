from __future__ import annotations
import re

def sanitize_token(text: str, token: str | None = None) -> str:
    if not text: return text
    result = text
    if token:
        result = result.replace(token, '<TOKEN_REDACTED>')
    result = re.sub(r'(ghp_|github_pat_|gho_|ghu_)[A-Za-z0-9_]+', '<TOKEN_REDACTED>', result)
    return result
