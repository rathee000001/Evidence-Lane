from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Callable

from sqlite_brain_builder.runtime.path_policy import brain_output_dir, slugify_name
from sqlite_brain_builder.runtime.universal_lane_registry import get_lane

ProgressCallback = Callable[[dict], None]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", ".next", "__pycache__", ".pytest_cache", ".mypy_cache"}
CODE_TEXT_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".scss", ".html", ".json", ".yaml", ".yml", ".sql", ".md", ".toml", ".ini", ".txt", ".csv", ".tsv", ".env"}
ASSET_PAYLOAD_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".ico", ".svg", ".glb", ".gltf", ".fbx", ".obj", ".mp4", ".mov", ".avi", ".mkv", ".webm", ".mp3", ".wav", ".zip", ".rar", ".7z", ".exe", ".dll", ".bin", ".pkl", ".pickle", ".pdf"}

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def uid(prefix: str) -> str:
    return prefix + "_" + hashlib.sha1(f"{prefix}:{time.time_ns()}".encode()).hexdigest()[:16]

def mermaid_safe(value, limit=90) -> str:
    text = str(value or "").replace("\\", "/").replace('"', "'")
    text = re.sub(r"[\[\]{}<>|`]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text or "none"

def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

def emit(callback, tracker, stage, task, file="", inc=1):
    tracker["done"] = min(tracker["total"], tracker["done"] + inc)
    pct = int(tracker["done"] * 100 / max(1, tracker["total"]))
    elapsed = int(time.time() - tracker["start"])
    eta = int(max(0, tracker["total"] - tracker["done"]) * max(1, elapsed) / max(1, tracker["done"]))
    finish_epoch = int(time.time() + eta)
    if callback:
        callback({
            "stage": stage,
            "task": task,
            "file": str(file),
            "done": tracker["done"],
            "total": tracker["total"],
            "percent": pct,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "finish_epoch": finish_epoch,
        })

def iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and not any(part in SKIP_DIRS for part in p.parts):
            yield p

def read_text_limited(path: Path, limit=500_000):
    try:
        return path.read_bytes()[:limit].decode("utf-8", errors="replace")
    except Exception:
        return ""

def classify_code_role(rel: str, ext: str, text: str) -> str:
    r = rel.replace("\\", "/").lower()
    t = (text or "")[:4000].lower()
    name = Path(r).name

    if ext in ASSET_PAYLOAD_EXTS:
        return "ARTIFACT_PAYLOAD"
    if "test" in r or name.endswith((".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx", ".test.js", ".test.py")):
        return "TEST_QA"
    if r.startswith("app/") or r.startswith("src/app/") or "/pages/" in r:
        if "/api/" in r or "route.ts" in r or "route.js" in r:
            return "API_BACKEND_ROUTE"
        return "UI_PAGE_ROUTE"
    if "/components/" in r or "/ui/" in r or "jsx" in ext or "tsx" in ext:
        if any(x in t for x in ["use client", "onclick", "classname", "<button", "<div", "return <"]):
            return "UI_UX_COMPONENT"
    if "/api/" in r or "/server/" in r or "fastapi" in t or "express" in t or "uvicorn" in t:
        return "BACKEND_PROCESS"
    if "/lib/" in r or "/utils/" in r or "/services/" in r or "/core/" in r:
        return "SERVICE_OR_UTILITY"
    if "/store/" in r or "/state/" in r or "zustand" in t or "redux" in t or "contextprovider" in t:
        return "STATE_MANAGEMENT"
    if "/data/" in r or "/db/" in r or ext == ".sql" or "sqlite" in t or "select " in t:
        return "DATA_DB_LAYER"
    if name in {"package.json", "requirements.txt", "pyproject.toml"} or ext in {".toml", ".yaml", ".yml", ".ini"}:
        return "CONFIG_BUILD_TOOLING"
    if ext in {".md", ".txt"}:
        return "DOCS_OR_NOTES"
    if ext in {".css", ".scss"}:
        return "UI_STYLE_THEME"
    return "GENERAL_CODE"

def language_for(ext: str) -> str:
    return {
        ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
        ".css": "css", ".scss": "scss", ".html": "html", ".json": "json", ".md": "markdown",
        ".sql": "sql", ".csv": "csv"
    }.get(ext, ext.strip(".") or "unknown")

def init_router(db: Path, brain_name: str):
    if db.exists():
        db.unlink()
    con = connect(db)
    con.executescript("""
    CREATE TABLE brain_manifest(brain_id TEXT PRIMARY KEY, brain_name TEXT, brain_slug TEXT, created_at TEXT, status TEXT);
    CREATE TABLE sector_registry(sector_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, sector_db_path TEXT, active_bool INTEGER, version TEXT, sector_hash TEXT, created_at TEXT);
    CREATE TABLE source_registry(source_id TEXT PRIMARY KEY, lane_key TEXT, lane_label TEXT, source_type TEXT, display_name TEXT, path TEXT, source_hash TEXT, active_bool INTEGER, created_at TEXT);
    CREATE TABLE package_manifest(package_id TEXT PRIMARY KEY, package_path TEXT, created_at TEXT, package_hash TEXT);
    """)
    slug = slugify_name(brain_name)
    con.execute("INSERT OR REPLACE INTO brain_manifest VALUES(?,?,?,?,?)", ("brain_" + slug, brain_name, slug, now(), "ACTIVE"))
    con.commit()
    con.close()

def init_code_db(db: Path):
    if db.exists():
        db.unlink()
    con = connect(db)
    con.executescript("""
    CREATE TABLE source_file(file_id TEXT PRIMARY KEY, logical_path TEXT, extension TEXT, size_bytes INTEGER, sha256 TEXT, lane_status TEXT, created_at TEXT);
    CREATE TABLE source_byte_coverage(coverage_id TEXT PRIMARY KEY, file_id TEXT, coverage_status TEXT, bytes_total INTEGER, bytes_accounted INTEGER, reason TEXT, created_at TEXT);
    CREATE TABLE git_commit(commit_sha TEXT PRIMARY KEY, short_sha TEXT, author_name TEXT, author_email_hash TEXT, commit_time TEXT, message TEXT, commit_order INTEGER);
    CREATE TABLE code_file(file_id TEXT PRIMARY KEY, canonical_path TEXT, language TEXT, extension TEXT, current_sha256 TEXT, is_active INTEGER);
    CREATE TABLE code_file_role(role_id TEXT PRIMARY KEY, file_id TEXT, role_name TEXT, role_reason TEXT, created_at TEXT);
    CREATE TABLE code_file_version(file_version_id TEXT PRIMARY KEY, file_id TEXT, commit_sha TEXT, path_at_commit TEXT, raw_file_sha256 TEXT, normalized_text_sha256 TEXT, language TEXT, extension TEXT, line_count INTEGER, byte_count INTEGER, created_at TEXT);
    CREATE TABLE code_chunk(chunk_id TEXT PRIMARY KEY, file_version_id TEXT, file_id TEXT, chunk_type TEXT, language TEXT, role_name TEXT, start_line INTEGER, end_line INTEGER, chunk_text TEXT, chunk_sha256 TEXT);
    CREATE TABLE code_symbol(symbol_id TEXT PRIMARY KEY, symbol_name TEXT, symbol_type TEXT, file_version_id TEXT, file_id TEXT, language TEXT, start_line INTEGER, signature TEXT, symbol_sha256 TEXT);
    CREATE TABLE app_route(route_id TEXT PRIMARY KEY, route_path TEXT, route_type TEXT, file_id TEXT, file_version_id TEXT, route_sha256 TEXT);
    CREATE TABLE dependency_manifest(manifest_id TEXT PRIMARY KEY, file_id TEXT, manifest_type TEXT, ecosystem TEXT, path TEXT, sha256 TEXT);
    CREATE TABLE dependency_item(dependency_id TEXT PRIMARY KEY, manifest_id TEXT, package_name TEXT, version_spec TEXT, ecosystem TEXT);
    CREATE TABLE code_import_edge(edge_id TEXT PRIMARY KEY, from_file_id TEXT, from_path TEXT, import_target TEXT, import_type TEXT, line_number INTEGER);
    CREATE TABLE workflow_node(node_id TEXT PRIMARY KEY, node_type TEXT, node_label TEXT, file_id TEXT, role_name TEXT);
    CREATE TABLE workflow_edge(edge_id TEXT PRIMARY KEY, from_node_id TEXT, to_node_id TEXT, relation_type TEXT, evidence TEXT);
    CREATE TABLE project_artifact(artifact_id TEXT PRIMARY KEY, file_id TEXT, artifact_type TEXT, source_path TEXT, artifact_sha256 TEXT, semantic_status TEXT, artifact_storage_path TEXT, size_bytes INTEGER);
    CREATE TABLE artifact_relation_edge(edge_id TEXT PRIMARY KEY, artifact_id TEXT, related_entity_type TEXT, related_entity_id TEXT, relation_type TEXT, confidence TEXT);
    CREATE VIRTUAL TABLE code_fts USING fts5(entity_id, text);
    """)
    con.commit()
    con.close()

def init_generic_db(db: Path):
    if db.exists():
        db.unlink()
    con = connect(db)
    con.executescript("""
    CREATE TABLE generic_source(source_id TEXT PRIMARY KEY, display_name TEXT, path TEXT, lane_key TEXT, source_type TEXT, sha256 TEXT, created_at TEXT);
    CREATE TABLE generic_chunk(chunk_id TEXT PRIMARY KEY, source_id TEXT, chunk_order INTEGER, chunk_text TEXT, chunk_sha256 TEXT, created_at TEXT);
    CREATE TABLE source_structure_signature(signature_id TEXT PRIMARY KEY, source_id TEXT, signature_json TEXT, structure_hash TEXT, created_at TEXT);
    CREATE VIRTUAL TABLE generic_fts USING fts5(chunk_id, text);
    """)
    con.commit()
    con.close()

def preserve_artifact_payload(source_path: Path, vault: Path, digest: str):
    vault.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source_path.name)
    target = vault / f"{digest[:16]}__{safe_name}"
    if not target.exists():
        shutil.copy2(source_path, target)
    return target

def parse_import_edges(rel: str, text: str):
    edges = []
    for idx, line in enumerate((text or "").splitlines(), start=1):
        s = line.strip()
        m = re.search(r"from\s+['\"]([^'\"]+)['\"]", s)
        if m:
            edges.append((m.group(1), "js_ts_import", idx))
        m = re.search(r"import\s+.*?\s+from\s+['\"]([^'\"]+)['\"]", s)
        if m:
            edges.append((m.group(1), "js_ts_import", idx))
        m = re.search(r"require\(['\"]([^'\"]+)['\"]\)", s)
        if m:
            edges.append((m.group(1), "commonjs_require", idx))
        m = re.match(r"from\s+([A-Za-z0-9_\.]+)\s+import\s+", s)
        if m:
            edges.append((m.group(1), "python_from_import", idx))
        m = re.match(r"import\s+([A-Za-z0-9_\.]+)", s)
        if m:
            edges.append((m.group(1), "python_import", idx))
    return edges

def add_code_file(con, root: Path, path: Path, vault: Path, artifact_manifest: list):
    rel = str(path.relative_to(root)).replace("\\", "/")
    ext = path.suffix.lower()
    file_id = "file_" + hashlib.sha1(rel.encode()).hexdigest()[:16]
    size = path.stat().st_size
    digest = sha256_file(path)
    con.execute("INSERT OR REPLACE INTO source_file VALUES(?,?,?,?,?,?,?)", (file_id, rel, ext, size, digest, "REGISTERED", now()))

    is_payload_asset = ext in ASSET_PAYLOAD_EXTS or (ext not in CODE_TEXT_EXTS and path.name.lower() not in {"dockerfile", "requirements.txt", "package.json", "pyproject.toml"})

    if is_payload_asset:
        stored = preserve_artifact_payload(path, vault, digest)
        artifact_id = uid("artifact")
        con.execute("INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)", (uid("coverage"), file_id, "ARTIFACT_PAYLOAD_PRESERVED_METADATA_ONLY_NO_CHUNK", size, size, "code sector asset preserved in vault; no text/code chunking", now()))
        con.execute("INSERT OR REPLACE INTO project_artifact VALUES(?,?,?,?,?,?,?,?)", (artifact_id, file_id, ext or "artifact", rel, digest, "CODE_ARTIFACT_PAYLOAD_PRESERVED_NO_CHUNK", str(stored), size))
        con.execute("INSERT OR REPLACE INTO artifact_relation_edge VALUES(?,?,?,?,?,?)", (uid("edge"), artifact_id, "code_file", file_id, "BELONGS_TO_CODE_PROJECT", "deterministic"))
        con.execute("INSERT OR REPLACE INTO code_file_role VALUES(?,?,?,?,?)", (uid("role"), file_id, "ARTIFACT_PAYLOAD", "binary/image/video/3d asset payload preserved; not chunked", now()))
        artifact_manifest.append({"artifact_id": artifact_id, "source_path": rel, "stored_path": str(stored), "sha256": digest, "size_bytes": size, "status": "PAYLOAD_PRESERVED_NO_CHUNK"})
        return

    text = read_text_limited(path)
    language = language_for(ext)
    role = classify_code_role(rel, ext, text)
    file_version_id = "fv_" + hashlib.sha1((rel + digest).encode()).hexdigest()[:16]
    lines = text.splitlines()

    con.execute("INSERT OR REPLACE INTO code_file VALUES(?,?,?,?,?,?)", (file_id, rel, language, ext, digest, 1))
    con.execute("INSERT OR REPLACE INTO code_file_role VALUES(?,?,?,?,?)", (uid("role"), file_id, role, "path/content deterministic role classifier", now()))
    con.execute("INSERT OR REPLACE INTO code_file_version VALUES(?,?,?,?,?,?,?,?,?,?,?)", (file_version_id, file_id, "", rel, digest, sha256_bytes(text.encode()), language, ext, len(lines), size, now()))
    con.execute("INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)", (uid("coverage"), file_id, "FAST_TEXT_CHUNKED", size, min(size, len(text.encode())), "bounded code chunks plus searchable role/symbol/import graph", now()))

    for idx, chunk in enumerate([text[i:i+8000] for i in range(0, min(len(text), 32000), 8000)]):
        if chunk.strip():
            chunk_id = uid("chunk")
            con.execute("INSERT OR REPLACE INTO code_chunk VALUES(?,?,?,?,?,?,?,?,?,?)", (chunk_id, file_version_id, file_id, "CODE_CHUNK", language, role, idx * 120 + 1, idx * 120 + 120, chunk, sha256_bytes(chunk.encode())))
            try:
                con.execute("INSERT INTO code_fts(entity_id,text) VALUES(?,?)", (file_id, chunk))
            except Exception:
                pass

    symbol_patterns = [
        (r"(def|function)\s+([A-Za-z_][A-Za-z0-9_]*)", "FUNCTION", 2),
        (r"class\s+([A-Za-z_][A-Za-z0-9_]*)", "CLASS", 1),
        (r"(export\s+)?(const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)", "CONST_OR_COMPONENT", 3),
        (r"export\s+default\s+function\s+([A-Za-z_][A-Za-z0-9_]*)", "DEFAULT_COMPONENT", 1),
    ]
    for line_no, line in enumerate(lines[:1500], start=1):
        stripped = line.strip()
        for pattern, stype, group in symbol_patterns:
            match = re.match(pattern, stripped)
            if match:
                name = match.group(group)
                con.execute("INSERT OR REPLACE INTO code_symbol VALUES(?,?,?,?,?,?,?,?,?)", (uid("sym"), name, stype, file_version_id, file_id, language, line_no, stripped, sha256_bytes(stripped.encode())))
                break

    for target, import_type, line_no in parse_import_edges(rel, text):
        con.execute("INSERT OR REPLACE INTO code_import_edge VALUES(?,?,?,?,?,?)", (uid("import"), file_id, rel, target, import_type, line_no))

    if role in {"UI_PAGE_ROUTE", "API_BACKEND_ROUTE"}:
        route = "/" + re.sub(r"(index|page|route)\.(tsx|ts|jsx|js|py)$", "", rel).strip("/")
        route_type = "API_ROUTE" if role == "API_BACKEND_ROUTE" else "PAGE_ROUTE"
        route_id = uid("route")
        con.execute("INSERT OR REPLACE INTO app_route VALUES(?,?,?,?,?,?)", (route_id, route, route_type, file_id, file_version_id, sha256_bytes(route.encode())))
        con.execute("INSERT OR REPLACE INTO workflow_node VALUES(?,?,?,?,?)", (route_id, route_type, route, file_id, role))
        con.execute("INSERT OR REPLACE INTO workflow_edge VALUES(?,?,?,?,?)", (uid("wf"), "ROOT_CODE_PROJECT", route_id, "EXPOSES_ROUTE", rel))

    if path.name.lower() in {"package.json", "requirements.txt", "pyproject.toml"}:
        manifest_id = uid("manifest")
        ecosystem = "node" if path.name.lower() == "package.json" else "python"
        con.execute("INSERT OR REPLACE INTO dependency_manifest VALUES(?,?,?,?,?,?)", (manifest_id, file_id, path.name, ecosystem, rel, digest))
        if path.name.lower() == "package.json":
            try:
                data = json.loads(text)
                for section in ["dependencies", "devDependencies"]:
                    for package_name, version_spec in data.get(section, {}).items():
                        con.execute("INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?)", (uid("dep"), manifest_id, package_name, str(version_spec), ecosystem))
            except Exception:
                pass
        if path.name.lower() == "requirements.txt":
            for raw_line in lines[:500]:
                x = raw_line.strip()
                if x and not x.startswith("#"):
                    package_name = re.split(r"[=<>!~ ]+", x)[0]
                    con.execute("INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?)", (uid("dep"), manifest_id, package_name, x, ecosystem))

def git_commits(root: Path):
    try:
        out = subprocess.run(["git", "-C", str(root), "log", "-n500", "--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"], text=True, capture_output=True, timeout=20)
        if out.returncode != 0:
            return []
        rows = []
        for i, line in enumerate(out.stdout.splitlines()):
            parts = line.split("\x1f")
            if len(parts) >= 6:
                rows.append((parts[0], parts[1], parts[2], sha256_bytes(parts[3].encode()), parts[4], parts[5], i))
        return rows
    except Exception:
        return []

def sql_count(con, table):
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0

def write_code_project_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)
    role_rows = con.execute("SELECT role_name, COUNT(*) FROM code_file_role GROUP BY role_name ORDER BY COUNT(*) DESC").fetchall()
    route_rows = con.execute("SELECT route_path, route_type FROM app_route ORDER BY route_path LIMIT 60").fetchall()
    dep_rows = con.execute("SELECT package_name, version_spec FROM dependency_item ORDER BY package_name LIMIT 60").fetchall()
    artifact_rows = con.execute("SELECT artifact_type, source_path, artifact_storage_path, size_bytes FROM project_artifact ORDER BY source_path LIMIT 120").fetchall()
    import_rows = con.execute("SELECT from_path, import_target, import_type FROM code_import_edge LIMIT 100").fetchall()
    symbol_rows = con.execute("SELECT symbol_name, symbol_type, language FROM code_symbol LIMIT 80").fetchall()

    lines = [
        "---",
        "title: Coded Project Brain Topology — SQLite Derived",
        "---",
        "flowchart TB",
        "classDef container fill:#f8f8f8,stroke:#111,stroke-width:2px,color:#111;",
        "classDef db fill:#e8f1ff,stroke:#245,stroke-width:1px,color:#111;",
        "classDef code fill:#eaffea,stroke:#275,stroke-width:1px,color:#111;",
        "classDef route fill:#fff4d6,stroke:#8a5a00,stroke-width:1px,color:#111;",
        "classDef artifact fill:#f3e8ff,stroke:#635,stroke-width:1px,color:#111;",
        "classDef warn fill:#ffe8e8,stroke:#922,stroke-width:1px,color:#111;",
        '  N_PROJECT["Coded Project Source"]:::container',
        '  N_DB["local_code_sector_v001.sqlite"]:::db',
        '  N_PROJECT --> N_DB',
        "  subgraph DB_CORE[SQLite code-sector table families]",
        f'    T_FILES["source_file / code_file\\nregistered={sql_count(con,"source_file")} | code={sql_count(con,"code_file")}"]:::db',
        f'    T_VERSION["code_file_version\\n{sql_count(con,"code_file_version")} versions"]:::db',
        f'    T_CHUNK["code_chunk + code_fts\\n{sql_count(con,"code_chunk")} chunks"]:::db',
        f'    T_SYMBOL["code_symbol\\n{sql_count(con,"code_symbol")} symbols"]:::db',
        f'    T_IMPORT["code_import_edge\\n{sql_count(con,"code_import_edge")} import edges"]:::db',
        f'    T_ROUTE["app_route + workflow_node\\n{sql_count(con,"app_route")} routes"]:::db',
        f'    T_DEP["dependency_manifest/item\\n{sql_count(con,"dependency_item")} deps"]:::db',
        f'    T_ART["project_artifact + artifact_relation_edge\\n{sql_count(con,"project_artifact")} payloads"]:::db',
        f'    T_COVER["source_byte_coverage\\n{sql_count(con,"source_byte_coverage")} rows"]:::db',
        "  end",
        "  N_DB --> T_FILES",
        "  T_FILES --> T_VERSION",
        "  T_VERSION --> T_CHUNK",
        "  T_CHUNK --> T_SYMBOL",
        "  T_CHUNK --> T_IMPORT",
        "  T_CHUNK --> T_ROUTE",
        "  T_FILES --> T_DEP",
        "  T_FILES --> T_ART",
        "  T_FILES --> T_COVER",
        "  subgraph ROLE_MAP[Deterministic code role map]",
    ]
    for idx, (role, count_value) in enumerate(role_rows):
        lines.append(f'    ROLE_{idx}["{mermaid_safe(role)}\\n{count_value} files"]:::code')
        lines.append(f"    T_FILES --> ROLE_{idx}")

    lines += ["  end", "  subgraph ROUTE_WORKFLOW[Routes and workflow surface]"]
    for idx, (route_path, route_type) in enumerate(route_rows):
        lines.append(f'    ROUTE_{idx}["{mermaid_safe(route_type)}\\n{mermaid_safe(route_path)}"]:::route')
        lines.append(f"    T_ROUTE --> ROUTE_{idx}")

    lines += ["  end", "  subgraph SYMBOL_IMPORT_GRAPH[Symbols and imports]"]
    for idx, (symbol_name, symbol_type, language) in enumerate(symbol_rows[:40]):
        lines.append(f'    SYM_{idx}["{mermaid_safe(symbol_type)} {mermaid_safe(symbol_name)}\\n{mermaid_safe(language)}"]:::code')
        lines.append(f"    T_SYMBOL --> SYM_{idx}")
    for idx, (from_path, target, import_type) in enumerate(import_rows[:40]):
        lines.append(f'    IMP_{idx}["{mermaid_safe(import_type)}\\n{mermaid_safe(from_path,40)} -> {mermaid_safe(target,40)}"]:::code')
        lines.append(f"    T_IMPORT --> IMP_{idx}")

    lines += ["  end", "  subgraph DEPENDENCY_GRAPH[Dependency graph]"]
    for idx, (package_name, version_spec) in enumerate(dep_rows):
        lines.append(f'    DEP_{idx}["{mermaid_safe(package_name)}\\n{mermaid_safe(version_spec)}"]:::route')
        lines.append(f"    T_DEP --> DEP_{idx}")

    lines += [
        "  end",
        "  subgraph ARTIFACT_VAULT[Artifact payload vault: preserved but not code-chunked]",
        '    VAULT_RULE["Images / GLB / video / binary assets are stored as payloads + hashes, not code chunks"]:::warn',
        "    T_ART --> VAULT_RULE",
    ]
    for idx, (artifact_type, source_path, storage_path, size_bytes) in enumerate(artifact_rows):
        lines.append(f'    ART_{idx}["{mermaid_safe(artifact_type)} | {size_bytes} bytes\\n{mermaid_safe(source_path)}"]:::artifact')
        lines.append(f"    T_ART --> ART_{idx}")

    lines += [
        "  end",
        "  subgraph PACKAGE_HANDOFF[Build output handoff]",
        '    OUT_ROUTER["project/project_router.sqlite"]:::db',
        '    OUT_SECTOR["project/sectors/local_code/local_code_sector_v001.sqlite"]:::db',
        '    OUT_TOPO["project/topology/*.mmd"]:::db',
        '    OUT_VAULT["project/artifacts/code_asset_vault"]:::artifact',
        "  end",
        "  N_DB --> OUT_SECTOR",
        "  T_ART --> OUT_VAULT",
        "  N_PROJECT --> OUT_ROUTER",
        "  N_PROJECT --> OUT_TOPO",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    con.close()

def write_master_mmd(router_db: Path, out: Path):
    con = sqlite3.connect(router_db)
    rows = con.execute("SELECT lane_key, lane_label, sector_db_path FROM sector_registry WHERE active_bool=1 ORDER BY lane_label").fetchall()
    lines = [
        "---",
        "title: Project Brain Master Topology — SQLite Router Derived",
        "---",
        "flowchart TB",
        "classDef container fill:#f8f8f8,stroke:#111,stroke-width:2px,color:#111;",
        "classDef db fill:#e8f1ff,stroke:#245,stroke-width:1px,color:#111;",
        "classDef artifact fill:#f3e8ff,stroke:#635,stroke-width:1px,color:#111;",
        '  BRAIN["Brain Workspace"]:::container',
        '  ROUTER["project/project_router.sqlite"]:::db',
        '  BRAIN --> ROUTER',
        '  BRAIN --> TOPO["project/topology"]:::db',
        '  BRAIN --> RECEIPTS["receipts"]:::db',
        '  BRAIN --> ARTIFACTS["project/artifacts"]:::artifact',
        "  subgraph ACTIVE_SECTORS[Active sector pointers from router]",
    ]
    for idx, (lane_key, label, path) in enumerate(rows):
        lines.append(f'    SECTOR_{idx}["{mermaid_safe(label)}\\n{mermaid_safe(path,70)}"]:::db')
        lines.append(f"    ROUTER --> SECTOR_{idx}")
        if lane_key in {"local_code", "github"}:
            lines.append(f'    CODE_GRAPH_{idx}["code graph: files -> versions -> chunks -> roles -> symbols -> routes -> deps -> artifacts"]:::db')
            lines.append(f"    SECTOR_{idx} --> CODE_GRAPH_{idx}")
            lines.append(f"    CODE_GRAPH_{idx} --> ARTIFACTS")
    lines += ["  end"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    con.close()

def text_for_source(src):
    if src.get("text") is not None:
        return src.get("text", "")
    p = Path(src.get("path", ""))
    if not p.exists():
        return ""
    if p.suffix.lower() in CODE_TEXT_EXTS:
        return read_text_limited(p)
    return f"METADATA_ONLY name={p.name} ext={p.suffix} size={p.stat().st_size}"

def build_fast_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress: ProgressCallback | None = None):
    active = [s for s in sources if s.get("active", True)]
    if not active:
        raise RuntimeError("NO_ACTIVE_SOURCES_LOADED")

    brain_root = brain_output_dir(workspace_dir, brain_name)
    project = brain_root / "project"
    sectors = project / "sectors"
    topology = project / "topology"
    artifacts = project / "artifacts"
    vault = artifacts / "code_asset_vault"
    receipts = brain_root / "receipts"
    for d in [brain_root, project, sectors, topology, artifacts, vault, receipts]:
        d.mkdir(parents=True, exist_ok=True)

    router = project / "project_router.sqlite"
    init_router(router, brain_name)

    total = 20
    for src in active:
        if src.get("lane_key") in {"local_code", "github"} and src.get("path") and Path(src["path"]).exists():
            total += sum(1 for _ in iter_files(Path(src["path"])))
        else:
            total += 5
    tracker = {"done": 0, "total": max(total, 20), "start": time.time()}
    emit(progress, tracker, "start", "starting V3.9 brain build", str(brain_root), 1)

    groups = {}
    for src in active:
        groups.setdefault(src.get("lane_key", "custom"), []).append(src)

    router_con = connect(router)
    artifact_manifest = []

    for lane_key, group in groups.items():
        lane = get_lane(lane_key)
        db = sectors / lane_key / f"{lane_key}_sector_v001.sqlite"
        emit(progress, tracker, "sector", f"building {lane['label']} sector", str(db), 1)

        if lane_key in {"local_code", "github"}:
            init_code_db(db)
            con = connect(db)
            con.execute("INSERT OR REPLACE INTO workflow_node VALUES(?,?,?,?,?)", ("ROOT_CODE_PROJECT", "ROOT", "Coded Project", "", "ROOT"))
            for src in group:
                root = Path(src.get("path", ""))
                if not root.exists():
                    raise RuntimeError(f"CODE_SOURCE_NOT_FOUND: {root}")
                for commit in git_commits(root):
                    con.execute("INSERT OR REPLACE INTO git_commit VALUES(?,?,?,?,?,?,?)", commit)
                files = list(iter_files(root))
                for idx, path in enumerate(files, start=1):
                    add_code_file(con, root, path, vault, artifact_manifest)
                    if idx % 25 == 0:
                        con.commit()
                    emit(progress, tracker, "code", f"indexing file {idx}/{len(files)}", str(path), 1)
            con.commit()
            con.close()
            write_code_project_mmd(db, topology / f"{lane_key}_lane.mmd")
        else:
            init_generic_db(db)
            con = connect(db)
            for src in group:
                source_id = src.get("source_id") or uid("source")
                display = src.get("display_name") or src.get("path") or lane_key
                text = text_for_source(src)
                con.execute("INSERT OR REPLACE INTO generic_source VALUES(?,?,?,?,?,?,?)", (source_id, display, src.get("path", ""), lane_key, src.get("source_type", ""), sha256_bytes(text.encode()), now()))
                for idx, chunk in enumerate([text[i:i+3500] for i in range(0, len(text), 3500)]):
                    if chunk.strip():
                        chunk_id = uid("chunk")
                        con.execute("INSERT OR REPLACE INTO generic_chunk VALUES(?,?,?,?,?,?)", (chunk_id, source_id, idx, chunk, sha256_bytes(chunk.encode()), now()))
                        try:
                            con.execute("INSERT INTO generic_fts(chunk_id,text) VALUES(?,?)", (chunk_id, chunk))
                        except Exception:
                            pass
                sig = json.dumps({"chars": len(text), "source_type": src.get("source_type", ""), "lane": lane_key}, sort_keys=True)
                con.execute("INSERT OR REPLACE INTO source_structure_signature VALUES(?,?,?,?,?)", (uid("sig"), source_id, sig, sha256_bytes(sig.encode()), now()))
                emit(progress, tracker, lane_key, f"indexed {lane['label']} source", display, 1)
            con.commit()
            con.close()
            (topology / f"{lane_key}_lane.mmd").write_text(f"flowchart TD\n  A[{lane['label']} Sector]\n", encoding="utf-8")

        router_con.execute("INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)", (uid("sector"), lane_key, lane["label"], str(db), 1, "v001", sha256_file(db), now()))
        for src in group:
            p = src.get("path") or src.get("display_name") or ""
            router_con.execute("INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)", (src.get("source_id") or uid("source"), lane_key, lane["label"], src.get("source_type", ""), src.get("display_name", ""), p, sha256_bytes(str(p).encode()), 1, now()))
        router_con.commit()

    (artifacts / "code_artifact_manifest.json").write_text(json.dumps(artifact_manifest, indent=2), encoding="utf-8")
    write_master_mmd(router, topology / "project_master_topology.mmd")
    router_con.close()

    (receipts / "build_receipt.md").write_text(
        f"# Build Receipt\n\nbrain={brain_name}\ncreated={now()}\nprofile=V3.9_E2E_MMD_ARTIFACT_PRESERVED\nartifact_payloads_preserved={len(artifact_manifest)}\n",
        encoding="utf-8"
    )

    emit(progress, tracker, "done", "brain build complete", str(brain_root), tracker["total"] - tracker["done"])
    if progress:
        progress({"stage": "done", "task": "complete", "file": str(brain_root), "done": tracker["total"], "total": tracker["total"], "percent": 100, "elapsed_seconds": int(time.time() - tracker["start"]), "eta_seconds": 0, "finish_epoch": int(time.time())})
    return {"brain_root": str(brain_root), "router_db": str(router), "package_zip": "", "package_hash": ""}

def find_env14_resource_root():
    if hasattr(sys, "_MEIPASS"):
        root = Path(sys._MEIPASS) / "sqlite_brain_builder" / "resources" / "public_model_env15"
    else:
        root = Path(__file__).resolve().parents[1] / "resources" / "public_model_env15"
    hits = list(root.rglob(".uepc_env")) if root.exists() else []
    return hits[0].parent if hits else (root if root.exists() else None)

def export_one_upload_package(workspace_dir: str, brain_name: str, progress: ProgressCallback | None = None):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    if not brain_root.exists():
        raise RuntimeError(f"BRAIN_ROOT_NOT_FOUND: {brain_root}")
    package_zip = brain_root / "packages" / f"{slugify_name(brain_name)}_one_upload_package_v001.zip"
    package_zip.parent.mkdir(parents=True, exist_ok=True)
    env14 = find_env14_resource_root()
    tracker = {"done": 0, "total": 100, "start": time.time()}
    emit(progress, tracker, "package", "creating one-upload package", str(package_zip), 1)
    if package_zip.exists():
        package_zip.unlink()
    with zipfile.ZipFile(package_zip, "w", zipfile.ZIP_DEFLATED) as z:
        if env14 and env14.exists():
            env_files = [p for p in env14.rglob("*") if p.is_file()]
            for idx, p in enumerate(env_files, start=1):
                z.write(p, p.relative_to(env14).as_posix())
                if idx % 25 == 0:
                    emit(progress, tracker, "package", f"injecting Env14 {idx}/{len(env_files)}", p.name, 1)
        for p in brain_root.rglob("*"):
            if p.is_file() and p.resolve() != package_zip.resolve():
                rel = p.relative_to(brain_root).as_posix()
                if not (rel.startswith("packages/") and rel.endswith(".zip")):
                    z.write(p, rel)
    digest = sha256_file(package_zip)
    emit(progress, tracker, "done", "package export complete", str(package_zip), 99)
    return {"brain_root": str(brain_root), "package_zip": str(package_zip), "package_hash": digest}

# ============================================================
# V4.3 topology writer override
# These names override earlier shallow MMD functions at runtime.
# ============================================================
from sqlite_brain_builder.runtime.topology_writer_v43 import (
    write_code_project_mmd,
    write_master_mmd,
    regenerate_topology_from_existing_brain,
)

# ============================================================
# V4.6 universal workflow topology override
# MMD is workflow topology, not database row dump.
# ============================================================
from sqlite_brain_builder.runtime.topology_writer_v46 import (
    write_code_project_mmd,
    write_master_mmd,
    regenerate_topology_from_existing_brain,
)

# ============================================================
# V4.7 route-first code topology override
# Code MMD = routes/pages -> code -> deps/artifacts/tooling.
# Project master MMD = package/sector/pointer overview only.
# ============================================================
from sqlite_brain_builder.runtime.topology_writer_v47 import (
    write_code_project_mmd,
    write_master_mmd,
    regenerate_topology_from_existing_brain,
)

# ============================================================
# V4.8 route-to-code topology override
# ============================================================
from sqlite_brain_builder.runtime.topology_writer_v48 import (
    write_code_project_mmd,
    write_master_mmd,
    regenerate_topology_from_existing_brain,
)
from sqlite_brain_builder.runtime.topology_writer_v49 import (
    write_code_project_mmd,
    write_master_mmd,
    regenerate_topology_from_existing_brain,
)
