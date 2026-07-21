from __future__ import annotations
from pathlib import Path
import subprocess, json, re
from sqlite_brain_builder.core import uid, now, file_sha256, sha256_text
from sqlite_brain_builder.storage.sqlite_utils import connect, apply_schema

CODE_EXTS = {'.py':'Python','.ts':'TypeScript','.tsx':'TSX','.js':'JavaScript','.jsx':'JSX','.css':'CSS','.scss':'SCSS','.html':'HTML','.json':'JSON','.yaml':'YAML','.yml':'YAML','.sql':'SQL','.md':'Markdown','.toml':'TOML','.ini':'INI'}
ARTIFACT_EXTS = {'.csv':'CSV','.png':'PNG','.svg':'SVG','.mmd':'MMD','.ipynb':'NOTEBOOK','.parquet':'PARQUET','.pkl':'PICKLE_METADATA_ONLY'}


def _run(cmd, cwd):
    try:
        if cmd and cmd[0] == 'git':
            # Clean-source staging and isolated EXE validation may copy a
            # repository across Windows principals. Trust only the exact
            # selected repository for this invocation; never mutate global Git
            # configuration or broaden safe.directory.
            safe_directory = str(Path(cwd).resolve()).replace('\\', '/')
            cmd = [
                'git',
                '-c', 'core.longpaths=true',
                '-c', f'safe.directory={safe_directory}',
                *cmd[1:],
            ]
        return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    except Exception as e:
        class R:
            returncode=1; stdout=''; stderr=str(e)
        return R()


def git_commits(repo: Path):
    if not (repo / '.git').exists():
        return []
    fmt = '%H%x1f%h%x1f%an%x1f%ae%x1f%ad%x1f%s'
    p = _run(['git', 'log', '--all', '--date=iso', f'--pretty=format:{fmt}'], repo)
    commits = []
    if p.returncode == 0:
        for order, line in enumerate(reversed([l for l in p.stdout.splitlines() if l.strip()])):
            parts = line.split('\x1f')
            if len(parts) >= 6:
                commits.append({
                    'commit_sha': parts[0], 'short_sha': parts[1], 'author_name': parts[2],
                    'author_email_hash_or_redacted': sha256_text(parts[3])[:16], 'commit_time': parts[4],
                    'message_subject': parts[5], 'commit_order': order,
                })
    return commits


def language_for(path: Path):
    return CODE_EXTS.get(path.suffix.lower(), 'Text')


def chunk_code(text: str, language: str):
    lines = text.splitlines()
    chunks = []
    import_lines = []
    for i, l in enumerate(lines, start=1):
        if re.match(r'\s*(import|from |const .*require|require\()', l):
            import_lines.append((i, l))
    if import_lines:
        chunks.append(('IMPORT_BLOCK', import_lines[0][0], import_lines[-1][0], '\n'.join(l for _, l in import_lines), None))
    for i, l in enumerate(lines, start=1):
        m = re.match(r'\s*(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)', l)
        if not m:
            m = re.match(r'\s*(export\s+)?(function|class)\s+([A-Za-z_][A-Za-z0-9_]*)', l)
        if not m:
            m = re.match(r'\s*(const|let|var)\s+([A-Z][A-Za-z0-9_]*)\s*=\s*\(', l)
        if m:
            name = m.group(m.lastindex)
            end = min(len(lines), i + 30)
            ctype = 'REACT_COMPONENT' if language in {'TSX', 'JSX'} and name[:1].isupper() else ('CLASS' if 'class' in l else 'FUNCTION')
            chunks.append((ctype, i, end, '\n'.join(lines[i-1:end]), name))
    if not chunks and text.strip():
        chunks.append(('FILE_TEXT', 1, len(lines), text[:8000], None))
    return chunks


def build_code_sector(db_path: str | Path, repo_path: str | Path, source_id: str | None = None, sanitized_remote_url: str = '') -> dict:
    repo = Path(repo_path)
    source_id = source_id or uid('source')
    con = connect(db_path)
    apply_schema(con, 'sqlite_brain_builder.storage', 'code_sector_schema.sql')
    repo_id = uid('repo')
    branch = ''
    p = _run(['git', 'branch', '--show-current'], repo)
    if p.returncode == 0:
        branch = p.stdout.strip()
    con.execute('INSERT INTO code_repo VALUES(?,?,?,?,?,?,?)', (repo_id, source_id, str(repo), sanitized_remote_url, branch or 'main', branch, 'OK'))
    branch_id = uid('branch')
    commits = git_commits(repo)
    con.execute('INSERT INTO git_branch VALUES(?,?,?,?,?)', (branch_id, repo_id, branch or 'main', 1, len(commits)))
    for c in commits:
        con.execute('INSERT OR IGNORE INTO git_commit VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (c['commit_sha'], c['short_sha'], repo_id, branch_id, '', c['author_name'], c['author_email_hash_or_redacted'], c['commit_time'], c['message_subject'], '', 0, c['commit_order']))
        con.execute('INSERT INTO commit_fts VALUES(?,?,?,?,?,?,?)', ('commit', c['commit_sha'], '', '', c['commit_sha'], sha256_text(c['message_subject']), c['message_subject']))
    current_commit = commits[-1]['commit_sha'] if commits else None
    files = []
    for path in repo.rglob('*'):
        if path.is_dir() or '.git' in path.parts or '__pycache__' in path.parts:
            continue
        rel = str(path.relative_to(repo)).replace('\\', '/')
        ext = path.suffix.lower()
        lane = 'CODE' if ext in CODE_EXTS else ('ARTIFACT' if ext in ARTIFACT_EXTS else 'BINARY_METADATA_ONLY')
        size = path.stat().st_size
        fhash = file_sha256(path)
        file_id = uid('file')
        con.execute('INSERT INTO source_file VALUES(?,?,?,?,?,?,?,?,?,?,?)', (file_id, source_id, rel, str(path), ext, lane, size, fhash, 'PARSED' if lane == 'CODE' else 'METADATA_ONLY', 'SEMANTIC_CHUNKED' if lane == 'CODE' else 'METADATA_ONLY', ''))
        con.execute('INSERT INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)', (uid('coverage'), file_id, 'SEMANTIC_CHUNKED' if lane == 'CODE' else 'METADATA_ONLY', size, size, '', now()))
        files.append((path, rel, ext, lane, size, fhash, file_id))
        if lane == 'CODE':
            lang = language_for(path)
            text = path.read_text(encoding='utf-8', errors='replace')
            code_file_id = file_id
            con.execute('INSERT INTO code_file VALUES(?,?,?,?,?,?,?,?,?,?)', (code_file_id, source_id, rel, rel, lang, ext, current_commit, current_commit, fhash, 1))
            fver = uid('fver')
            norm = sha256_text('\n'.join(line.rstrip() for line in text.splitlines()))
            lines = text.splitlines()
            con.execute('INSERT INTO code_file_version VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (fver, code_file_id, current_commit, rel, fhash, norm, lang, ext, len(lines), size, 0, 0, now()))
            for idx, line in enumerate(lines, start=1):
                lh = sha256_text(line)
                nlh = sha256_text(line.strip())
                con.execute('INSERT INTO code_line_snapshot VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (uid('line'), fver, code_file_id, current_commit, idx, line, lh, nlh, len(line) - len(line.lstrip()), 1 if not line.strip() else 0, 1 if line.strip().startswith(('#', '//', '/*', '*')) else 0, line))
                con.execute('INSERT INTO line_fts VALUES(?,?,?,?,?,?,?)', ('line', uid('linefts'), code_file_id, fver, current_commit, lh, line))
            for ctype, start, end, ctext, symname in chunk_code(text, lang):
                ch = sha256_text(ctext)
                sym_id = None
                if symname:
                    sym_id = uid('sym')
                    sig = ctext.splitlines()[0] if ctext.splitlines() else symname
                    con.execute('INSERT INTO code_symbol VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (sym_id, symname, ctype, fver, code_file_id, current_commit, lang, start, end, sig, sha256_text(sig), '', 1))
                    con.execute('INSERT INTO symbol_fts VALUES(?,?,?,?,?,?,?)', ('symbol', sym_id, code_file_id, fver, current_commit, sha256_text(sig), sig))
                chunk_id = uid('chunk')
                con.execute('INSERT INTO code_chunk VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (chunk_id, ch, fver, code_file_id, current_commit, ctype, lang, start, end, ctext, sym_id, None, None, ctext))
                con.execute('INSERT INTO code_fts VALUES(?,?,?,?,?,?,?)', ('chunk', chunk_id, code_file_id, fver, current_commit, ch, ctext))
            if any(part in rel.lower() for part in ['pages/', 'app/', 'routes/', 'api/']) or rel.lower().endswith(('main.py', 'app.py')):
                route_id = uid('route')
                route_path = '/' + rel.replace('src/', '').rsplit('.', 1)[0].replace('/index', '').replace('pages/', '').replace('app/', '')
                rtype = 'API_ENDPOINT' if 'api' in rel.lower() else 'FRONTEND_PAGE'
                rh = sha256_text(route_path + rel)
                con.execute('INSERT INTO app_route VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (route_id, route_path, rtype, 'heuristic', code_file_id, fver, current_commit, None, 'GET', '', '', rh))
                con.execute('INSERT INTO route_fts VALUES(?,?,?,?,?,?,?)', ('route', route_id, code_file_id, fver, current_commit, rh, route_path + ' ' + rel))
        elif lane == 'ARTIFACT':
            art_id = uid('artifact')
            con.execute('INSERT INTO project_artifact VALUES(?,?,?,?,?,?,?,?,?,?)', (art_id, file_id, ARTIFACT_EXTS.get(ext, 'ARTIFACT'), fhash, rel, current_commit, None, None, json.dumps({'size': size}), 'METADATA_ONLY'))
            con.execute('INSERT INTO artifact_fts VALUES(?,?,?,?,?,?,?)', ('artifact', art_id, file_id, '', current_commit, fhash, rel))
    for path, rel, ext, lane, size, fhash, file_id in files:
        name = Path(rel).name.lower()
        if name in {'package.json', 'requirements.txt', 'pyproject.toml'} or name.startswith('dockerfile') or name.endswith('config.js') or name.endswith('config.ts'):
            man_id = uid('manifest')
            ecosystem = 'python' if name in {'requirements.txt', 'pyproject.toml'} else 'node' if name == 'package.json' else 'config'
            con.execute('INSERT INTO dependency_manifest VALUES(?,?,?,?,?,?,?)', (man_id, file_id, name, ecosystem, rel, current_commit, fhash))
            text = path.read_text(encoding='utf-8', errors='replace')
            deps = []
            if name == 'package.json':
                try:
                    data = json.loads(text)
                    for group in ['dependencies', 'devDependencies']:
                        for k, v in data.get(group, {}).items():
                            deps.append((k, v, group))
                except Exception:
                    pass
            elif name == 'requirements.txt':
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        pkg = re.split(r'[=<>~!]', line)[0].strip()
                        deps.append((pkg, line, 'runtime'))
            for pkg, ver, group in deps:
                dep_id = uid('dep')
                con.execute('INSERT INTO dependency_item VALUES(?,?,?,?,?,?,?,?)', (dep_id, man_id, pkg, ver, ecosystem, group, file_id, current_commit))
    con.commit()
    con.close()
    return {'files': len(files), 'commits': len(commits)}
