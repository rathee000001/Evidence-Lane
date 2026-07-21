from __future__ import annotations
import re, hashlib

UNSAFE_PATTERNS = [r'(?i)subprocess', r'(?i)os\.system', r'(?i)powershell', r'(?i)curl\s+', r'(?i)wget\s+', r'(?i)secret', r'(?i)token\s*=', r'(?i)ignore.*instruction', r'(?i)drop\s+table']

def validate_schema_contract(text: str) -> tuple[bool, str]:
    for pat in UNSAFE_PATTERNS:
        if re.search(pat, text or ''):
            return False, f'UNSAFE_SCHEMA_CONTRACT_PATTERN:{pat}'
    return True, 'OK'

def parse_fields(text: str) -> list[str]:
    fields=[]
    for line in (text or '').splitlines():
        line=line.strip().strip('-*| `')
        if not line or set(line) <= {'-','|',':'}: continue
        token = re.split(r'[:|,\s]+', line)[0].strip()
        if token and re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', token): fields.append(token)
    return list(dict.fromkeys(fields))

def contract_hash(text: str) -> str:
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()

def is_allowed_source_for_lane(purpose_lane: str, source_type: str) -> bool:
    purpose_lane = purpose_lane.upper(); source_type = source_type.upper()
    if purpose_lane == 'CODE':
        return source_type in {'GITHUB_REPO','LOCAL_CODE_PROJECT'}
    return True
