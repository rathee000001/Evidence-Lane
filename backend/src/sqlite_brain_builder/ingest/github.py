from __future__ import annotations
from pathlib import Path
import subprocess, urllib.parse
from sqlite_brain_builder.security.token_sanitizer import sanitize_token


def sanitize_repo_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password:
        netloc = parsed.hostname or ''
        if parsed.port: netloc += f':{parsed.port}'
        parsed = parsed._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parsed)


def clone_repository(repo_url: str, staging_dir: str | Path, branch: str | None = None, token: str | None = None) -> dict:
    staging_dir = Path(staging_dir); staging_dir.mkdir(parents=True, exist_ok=True)
    safe_url = sanitize_repo_url(repo_url)
    clone_url = repo_url
    if token and repo_url.startswith('https://'):
        clone_url = repo_url.replace('https://', f'https://{token}@', 1)
    target = staging_dir / (Path(urllib.parse.urlparse(safe_url).path).stem or 'repo')
    cmd = ['git','clone']
    if branch: cmd += ['--branch', branch]
    cmd += [clone_url, str(target)]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = sanitize_token(p.stdout, token)
    return {'ok': p.returncode == 0, 'target': str(target), 'sanitized_url': safe_url, 'output': output, 'returncode': p.returncode}
