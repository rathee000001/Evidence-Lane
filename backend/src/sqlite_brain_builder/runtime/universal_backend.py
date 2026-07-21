from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from sqlite_brain_builder.runtime.path_policy import normalize_workspace_dir, brain_output_dir, workspace_db_path
from typing import Callable, Iterable

from sqlite_brain_builder.runtime.universal_lane_registry import get_lane

ProgressCallback = Callable[[dict], None]

CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".css", ".scss", ".html", ".json", ".yaml", ".yml",
    ".sql", ".md", ".toml", ".ini", ".env", ".example", ".dockerfile"
}

TEXT_EXTS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html", ".css", ".js", ".ts",
    ".tsx", ".jsx", ".py", ".sql", ".toml", ".ini"
}

# In CODE sector these are never chunked. They are artifact metadata only.
CODE_ASSET_METADATA_ONLY_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".ico",
    ".svg", ".glb", ".gltf", ".fbx", ".obj", ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".wav", ".zip", ".rar", ".7z", ".exe", ".dll", ".bin", ".pkl", ".pickle"
}

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", ".next", "__pycache__"}

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def slugify(s: str) -> str:
    x = re.sub(r"[^A-Za-z0-9._-]+", "_", (s or "brain").strip()).strip("_").lower()
    return x or "brain"

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()

def uid(prefix: str, value: str = "") -> str:
    return prefix + "_" + hashlib.sha1(f"{prefix}:{value}:{time.time_ns()}".encode()).hexdigest()[:16]

def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def emit(cb: ProgressCallback | None, **payload):
    if cb:
        cb(payload)

def app_resource_root() -> Path:
    # Works in source and PyInstaller onefile.
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "sqlite_brain_builder" / "resources"
    return Path(__file__).resolve().parents[1] / "resources"

def find_env14_resource_root() -> Path | None:
    root = app_resource_root() / "public_model_env15"
    if not root.exists():
        return None

    # If extraction created one nested root, find the folder containing .uepc_env.
    candidates = [p.parent for p in root.rglob(".uepc_env")]
    if candidates:
        return candidates[0]
    return root if (root / ".uepc_env").exists() else None

def safe_copytree_contents(src: Path, dst: Path):
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        try:
            return path.read_text(encoding="latin-1", errors="replace")
        except Exception:
            return ""

def extract_office_xml_text(path: Path) -> str:
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                low = name.lower()
                if not low.endswith(".xml"):
                    continue
                if not any(x in low for x in ["document", "slide", "sheet", "sharedstrings", "workbook", "notes"]):
                    continue
                raw = z.read(name).decode("utf-8", errors="ignore")
                raw = re.sub(r"<[^>]+>", " ", raw)
                raw = re.sub(r"\s+", " ", raw)
                if raw.strip():
                    out.append(f"[{name}]\n{raw.strip()}")
    except Exception as e:
        out.append(f"[OFFICE_XML_FALLBACK_FAILED] {e}")
    return "\n\n".join(out)

def extract_pdf_text(path: Path) -> str:
    try:
        import fitz
        doc = fitz.open(str(path))
        pages = []
        for i, page in enumerate(doc):
            pages.append(f"[PDF_PAGE {i+1}]\n{page.get_text()}")
        return "\n\n".join(pages)
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        pages = []
        for i, page in enumerate(reader.pages):
            pages.append(f"[PDF_PAGE {i+1}]\n{page.extract_text() or ''}")
        return "\n\n".join(pages)
    except Exception as e:
        return f"[PDF_TEXT_EXTRACTION_REVIEW_REQUIRED] {e}"

def extract_image_text_or_metadata(path: Path) -> str:
    meta = []
    try:
        from PIL import Image
        im = Image.open(path)
        meta.append(f"IMAGE_METADATA: format={im.format} width={im.width} height={im.height} mode={im.mode}")
        try:
            import pytesseract
            text = pytesseract.image_to_string(im)
            if text.strip():
                meta.append("[OCR_TEXT]\n" + text)
            else:
                meta.append("[OCR_EMPTY_REVIEW_REQUIRED]")
        except Exception as e:
            meta.append(f"[OCR_TOOL_UNAVAILABLE_REVIEW_REQUIRED] {e}")
    except Exception as e:
        meta.append(f"[IMAGE_METADATA_REVIEW_REQUIRED] {e}")
    return "\n".join(meta)

def extract_source_text(path: Path, lane_key: str) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return safe_read_text(path), "TEXT_EXTRACTED"
    if ext in {".docx", ".pptx", ".xlsx"}:
        return extract_office_xml_text(path), "STRUCTURE_XML_EXTRACTED"
    if ext == ".pdf":
        return extract_pdf_text(path), "PDF_TEXT_OR_REVIEW"
    if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        # Images are OCR-capable outside code sector.
        return extract_image_text_or_metadata(path), "IMAGE_OCR_OR_METADATA"
    return f"METADATA_ONLY: name={path.name} ext={ext} size={path.stat().st_size if path.exists() else 0}", "METADATA_ONLY"

def chunk_text(text: str, max_chars: int = 3500) -> list[str]:
    if not (text or "").strip():
        return []
    return [text[i:i+max_chars] for i in range(0, len(text), max_chars)]

def init_router(router_db: Path, brain_name: str):
    con = connect(router_db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS brain_manifest(
        brain_id TEXT PRIMARY KEY,
        brain_name TEXT,
        brain_slug TEXT,
        created_at TEXT,
        status TEXT
    );
    CREATE TABLE IF NOT EXISTS sector_registry(
        sector_id TEXT PRIMARY KEY,
        lane_key TEXT,
        lane_label TEXT,
        sector_db_path TEXT,
        active_bool INTEGER,
        version TEXT,
        sector_hash TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS source_registry(
        source_id TEXT PRIMARY KEY,
        lane_key TEXT,
        lane_label TEXT,
        source_type TEXT,
        display_name TEXT,
        path TEXT,
        source_hash TEXT,
        active_bool INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS source_active_state(
        state_id TEXT PRIMARY KEY,
        source_id TEXT,
        lane_key TEXT,
        active_bool INTEGER,
        state TEXT,
        reason TEXT,
        changed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS package_manifest(
        package_id TEXT PRIMARY KEY,
        package_path TEXT,
        created_at TEXT,
        package_hash TEXT
    );
    CREATE TABLE IF NOT EXISTS env14_resource_manifest(
        id TEXT PRIMARY KEY,
        integrated_bool INTEGER,
        resource_path TEXT,
        copied_at TEXT,
        status TEXT
    );
    """)
    slug = slugify(brain_name)
    con.execute("INSERT OR REPLACE INTO brain_manifest VALUES(?,?,?,?,?)", ("brain_" + slug, brain_name, slug, now(), "ACTIVE"))
    con.commit()
    con.close()

def init_generic_sector_schema(db: Path):
    con = connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS sector_manifest(
        sector_id TEXT PRIMARY KEY,
        lane_key TEXT,
        lane_label TEXT,
        schema_contract TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS source_file(
        file_id TEXT PRIMARY KEY,
        source_id TEXT,
        display_name TEXT,
        path TEXT,
        extension TEXT,
        size_bytes INTEGER,
        sha256 TEXT,
        parser_status TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS source_byte_coverage(
        coverage_id TEXT PRIMARY KEY,
        file_id TEXT,
        coverage_status TEXT,
        bytes_total INTEGER,
        bytes_accounted INTEGER,
        reason TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS source_structure_signature(
        signature_id TEXT PRIMARY KEY,
        file_id TEXT,
        signature_type TEXT,
        key_metrics_json TEXT,
        structure_hash TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS extraction_signal(
        signal_id TEXT PRIMARY KEY,
        file_id TEXT,
        signal_name TEXT,
        signal_value TEXT,
        tool_source TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS chunk(
        chunk_id TEXT PRIMARY KEY,
        file_id TEXT,
        chunk_type TEXT,
        chunk_text TEXT,
        chunk_sha256 TEXT,
        chunk_order INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS review_required_item(
        review_id TEXT PRIMARY KEY,
        file_id TEXT,
        reason TEXT,
        created_at TEXT
    );
    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(chunk_id, chunk_text);
    """)
    con.commit()
    con.close()

def init_code_sector_schema(db: Path):
    init_generic_sector_schema(db)
    con = connect(db)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS git_commit(
        commit_sha TEXT PRIMARY KEY,
        short_sha TEXT,
        author_name TEXT,
        author_email_hash TEXT,
        commit_time TEXT,
        message TEXT,
        commit_order INTEGER
    );
    CREATE TABLE IF NOT EXISTS code_file(
        file_id TEXT PRIMARY KEY,
        canonical_path TEXT,
        language TEXT,
        extension TEXT,
        current_sha256 TEXT,
        is_active INTEGER
    );
    CREATE TABLE IF NOT EXISTS code_file_version(
        file_version_id TEXT PRIMARY KEY,
        file_id TEXT,
        commit_sha TEXT,
        path_at_commit TEXT,
        raw_file_sha256 TEXT,
        normalized_text_sha256 TEXT,
        language TEXT,
        extension TEXT,
        line_count INTEGER,
        byte_count INTEGER,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS code_line_snapshot(
        line_id TEXT PRIMARY KEY,
        file_version_id TEXT,
        file_id TEXT,
        commit_sha TEXT,
        line_number INTEGER,
        line_text TEXT,
        line_sha256 TEXT,
        normalized_line_sha256 TEXT,
        is_blank INTEGER,
        is_comment INTEGER
    );
    CREATE TABLE IF NOT EXISTS code_chunk(
        chunk_id TEXT PRIMARY KEY,
        file_version_id TEXT,
        file_id TEXT,
        commit_sha TEXT,
        chunk_type TEXT,
        language TEXT,
        start_line INTEGER,
        end_line INTEGER,
        chunk_text TEXT,
        chunk_sha256 TEXT
    );
    CREATE TABLE IF NOT EXISTS code_symbol(
        symbol_id TEXT PRIMARY KEY,
        symbol_name TEXT,
        symbol_type TEXT,
        file_version_id TEXT,
        file_id TEXT,
        language TEXT,
        start_line INTEGER,
        signature TEXT,
        symbol_sha256 TEXT
    );
    CREATE TABLE IF NOT EXISTS app_route(
        route_id TEXT PRIMARY KEY,
        route_path TEXT,
        route_type TEXT,
        file_id TEXT,
        file_version_id TEXT,
        route_sha256 TEXT
    );
    CREATE TABLE IF NOT EXISTS dependency_manifest(
        manifest_id TEXT PRIMARY KEY,
        file_id TEXT,
        manifest_type TEXT,
        ecosystem TEXT,
        path TEXT,
        sha256 TEXT
    );
    CREATE TABLE IF NOT EXISTS dependency_item(
        dependency_id TEXT PRIMARY KEY,
        manifest_id TEXT,
        package_name TEXT,
        version_spec TEXT,
        ecosystem TEXT
    );
    CREATE TABLE IF NOT EXISTS project_artifact(
        artifact_id TEXT PRIMARY KEY,
        file_id TEXT,
        artifact_type TEXT,
        path TEXT,
        artifact_sha256 TEXT,
        semantic_status TEXT
    );
    CREATE TABLE IF NOT EXISTS artifact_relation_edge(
        edge_id TEXT PRIMARY KEY,
        artifact_id TEXT,
        related_entity_type TEXT,
        related_entity_id TEXT,
        relation_type TEXT,
        confidence TEXT
    );
    """)
    con.commit()
    con.close()

def iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if set(p.parts) & SKIP_DIRS:
            continue
        yield p

def git_commits(root: Path) -> list[tuple]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "log", "--pretty=format:%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s"],
            text=True,
            capture_output=True,
            timeout=20
        )
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

def detect_language(path: Path) -> str:
    return {
        ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
        ".css": "css", ".scss": "scss", ".html": "html", ".json": "json", ".sql": "sql", ".md": "markdown"
    }.get(path.suffix.lower(), path.suffix.lower().strip(".") or "unknown")

def insert_code_file(con: sqlite3.Connection, root: Path, path: Path):
    rel = str(path.relative_to(root)).replace("\\", "/")
    ext = path.suffix.lower()
    fid = "file_" + hashlib.sha1(rel.encode()).hexdigest()[:16]
    raw = sha256_file(path)
    size = path.stat().st_size

    # Critical correction: in CODE sector, image/glb/video/binary assets are metadata/artifact only, never code-chunked.
    if ext in CODE_ASSET_METADATA_ONLY_EXTS or ext not in CODE_EXTS and path.name.lower() not in {"dockerfile", "requirements.txt", "package.json", "pyproject.toml"}:
        con.execute("INSERT OR REPLACE INTO source_file VALUES(?,?,?,?,?,?,?,?,?)", (fid, "", path.name, rel, ext, size, raw, "CODE_ASSET_METADATA_ONLY_NO_CHUNK", now()))
        con.execute("INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)", (uid("coverage", fid), fid, "METADATA_ONLY", size, size, "Asset in coded project sector: metadata/hash only, no chunking.", now()))
        con.execute("INSERT OR REPLACE INTO project_artifact VALUES(?,?,?,?,?,?)", (uid("artifact", rel), fid, ext or "artifact", rel, raw, "CODE_ASSET_METADATA_ONLY"))
        return

    text = safe_read_text(path)
    norm = sha256_bytes(text.replace("\r\n", "\n").encode("utf-8", errors="ignore"))
    lang = detect_language(path)
    fvid = "fv_" + hashlib.sha1((rel + raw).encode()).hexdigest()[:16]
    lines = text.splitlines()

    con.execute("INSERT OR REPLACE INTO code_file VALUES(?,?,?,?,?,?)", (fid, rel, lang, ext, raw, 1))
    con.execute("INSERT OR REPLACE INTO code_file_version VALUES(?,?,?,?,?,?,?,?,?,?,?)", (fvid, fid, "", rel, raw, norm, lang, ext, len(lines), size, now()))
    con.execute("INSERT OR REPLACE INTO source_file VALUES(?,?,?,?,?,?,?,?,?)", (fid, "", path.name, rel, ext, size, raw, "CODE_TEXT_EXTRACTED", now()))
    con.execute("INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)", (uid("coverage", fid), fid, "SEMANTIC_CHUNKED", size, size, "Code text parsed/chunked.", now()))

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        line_hash = sha256_bytes(line.encode("utf-8", errors="ignore"))
        norm_hash = sha256_bytes(stripped.encode("utf-8", errors="ignore"))
        is_comment = 1 if stripped.startswith(("#", "//", "/*", "*")) else 0
        con.execute("INSERT OR REPLACE INTO code_line_snapshot VALUES(?,?,?,?,?,?,?,?,?,?)", (uid("line", rel + str(idx)), fvid, fid, "", idx, line, line_hash, norm_hash, 1 if not stripped else 0, is_comment))

        sym = None
        stype = None
        m = re.match(r"\s*(def|function)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if m:
            stype = "FUNCTION"
            sym = m.group(2)
        m2 = re.match(r"\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if m2:
            stype = "CLASS"
            sym = m2.group(1)
        m3 = re.match(r"\s*(export\s+)?(const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)", line)
        if m3:
            stype = "CONSTANT_OR_COMPONENT"
            sym = m3.group(3)

        if sym:
            con.execute("INSERT OR REPLACE INTO code_symbol VALUES(?,?,?,?,?,?,?,?,?)", (uid("symbol", rel + sym + str(idx)), sym, stype, fvid, fid, lang, idx, stripped, sha256_bytes(stripped.encode())))

    for start in range(0, len(lines), 40):
        chunk_lines = lines[start:start + 40]
        if not chunk_lines:
            continue
        chunk_text = "\n".join(chunk_lines)
        con.execute("INSERT OR REPLACE INTO code_chunk VALUES(?,?,?,?,?,?,?,?,?,?)", (uid("chunk", rel + str(start)), fvid, fid, "", "CODE_BLOCK", lang, start + 1, start + len(chunk_lines), chunk_text, sha256_bytes(chunk_text.encode())))

    if "pages" in rel or "routes" in rel or "/api/" in rel or rel.startswith("app/") or rel.startswith("src/app/"):
        route_path = "/" + re.sub(r"(index|page)\.(tsx|ts|jsx|js|py)$", "", rel).replace("\\", "/")
        con.execute("INSERT OR REPLACE INTO app_route VALUES(?,?,?,?,?,?)", (uid("route", route_path), route_path, "ROUTE_CANDIDATE", fid, fvid, sha256_bytes(route_path.encode())))

    if path.name.lower() in {"package.json", "requirements.txt", "pyproject.toml"}:
        manifest_id = uid("manifest", rel)
        ecosystem = "node" if path.name == "package.json" else "python"
        con.execute("INSERT OR REPLACE INTO dependency_manifest VALUES(?,?,?,?,?,?)", (manifest_id, fid, path.name, ecosystem, rel, raw))
        if path.name == "requirements.txt":
            for line in lines:
                x = line.strip()
                if x and not x.startswith("#"):
                    con.execute("INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?)", (uid("dep", x), manifest_id, re.split(r"[=<>!~ ]+", x)[0], x, ecosystem))
        if path.name == "package.json":
            try:
                data = json.loads(text)
                for sect in ["dependencies", "devDependencies"]:
                    for name, ver in data.get(sect, {}).items():
                        con.execute("INSERT OR REPLACE INTO dependency_item VALUES(?,?,?,?,?)", (uid("dep", name), manifest_id, name, str(ver), ecosystem))
            except Exception:
                pass

def build_code_sector(db: Path, root: Path, lane_key: str):
    init_code_sector_schema(db)
    con = connect(db)
    lane = get_lane(lane_key)
    con.execute("INSERT OR REPLACE INTO sector_manifest VALUES(?,?,?,?,?)", ("sector_" + lane_key, lane_key, lane["label"], lane["default_schema"], now()))

    for commit in git_commits(root):
        con.execute("INSERT OR REPLACE INTO git_commit VALUES(?,?,?,?,?,?,?)", commit)

    for path in iter_files(root):
        insert_code_file(con, root, path)

    con.commit()
    con.close()

def structure_signature(path: Path, text: str, lane_key: str, parser_status: str) -> dict:
    ext = path.suffix.lower()
    return {
        "extension": ext,
        "lane": lane_key,
        "parser_status": parser_status,
        "bytes": path.stat().st_size if path.exists() else len(text.encode()),
        "chars": len(text),
        "lines": len(text.splitlines()),
        "chunks_est": max(1, len(text) // 3500) if text else 0,
        "has_tables_signal": "|" in text or "," in text[:1000],
        "has_code_signal": any(x in text for x in ["function ", "def ", "class ", "import "]),
    }

def build_generic_sector(db: Path, sources: list[dict], lane_key: str):
    init_generic_sector_schema(db)
    lane = get_lane(lane_key)
    con = connect(db)
    con.execute("INSERT OR REPLACE INTO sector_manifest VALUES(?,?,?,?,?)", ("sector_" + lane_key, lane_key, lane["label"], lane.get("default_schema", ""), now()))

    for src in sources:
        if src.get("text") is not None:
            display = src.get("display_name") or f"{lane['label']}: pasted_text"
            tmp_dir = db.parent / "_text_sources"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            path = tmp_dir / (slugify(display) + ".txt")
            path.write_text(src.get("text") or "", encoding="utf-8")
            text = src.get("text") or ""
            parser_status = "PASTE_TEXT_EXTRACTED"
        else:
            path = Path(src.get("path") or "")
            text, parser_status = extract_source_text(path, lane_key)
            display = src.get("display_name") or path.name

        fid = uid("file", str(path))
        h = sha256_file(path) if path.exists() and path.is_file() else sha256_bytes(text.encode())
        size = path.stat().st_size if path.exists() and path.is_file() else len(text.encode())

        con.execute("INSERT OR REPLACE INTO source_file VALUES(?,?,?,?,?,?,?,?,?)", (fid, src.get("source_id", uid("source")), display, str(path), path.suffix.lower(), size, h, parser_status, now()))
        coverage = "TEXT_CHUNKED" if text.strip() else "METADATA_ONLY"
        if lane_key in {"images_ocr", "pdf_ocr"} and "OCR" in parser_status:
            coverage = "OCR_CHUNKED"
        con.execute("INSERT OR REPLACE INTO source_byte_coverage VALUES(?,?,?,?,?,?,?)", (uid("coverage", fid), fid, coverage, size, size, parser_status, now()))

        sig = structure_signature(path, text, lane_key, parser_status)
        sig_json = json.dumps(sig, sort_keys=True)
        con.execute("INSERT OR REPLACE INTO source_structure_signature VALUES(?,?,?,?,?,?)", (uid("sig", fid), fid, lane_key.upper() + "_SIGNATURE", sig_json, sha256_bytes(sig_json.encode()), now()))

        for name, val in sig.items():
            con.execute("INSERT OR REPLACE INTO extraction_signal VALUES(?,?,?,?,?,?)", (uid("signal", fid + name), fid, str(name), str(val), "deterministic_tooling", now()))

        chunks = chunk_text(text)
        if not chunks:
            con.execute("INSERT OR REPLACE INTO review_required_item VALUES(?,?,?,?)", (uid("review", fid), fid, "No semantic text extracted; metadata/review-required lane.", now()))
        for i, chunk in enumerate(chunks):
            cid = uid("chunk", fid + str(i))
            ch = sha256_bytes(chunk.encode())
            con.execute("INSERT OR REPLACE INTO chunk VALUES(?,?,?,?,?,?,?)", (cid, fid, lane_key.upper() + "_CHUNK", chunk, ch, i, now()))
            try:
                con.execute("INSERT INTO chunk_fts(chunk_id, chunk_text) VALUES(?,?)", (cid, chunk))
            except Exception:
                pass

    con.commit()
    con.close()

def count_table(con, table: str) -> int:
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0

def write_mmd_for_sector(sector_db: Path, lane_key: str, out: Path):
    lane = get_lane(lane_key)
    lines = [
        "flowchart TD",
        f"  ROOT[{lane['label']} Sector]",
        f"  DB[(Sector SQLite)]",
        "  ROOT --> DB",
    ]
    con = sqlite3.connect(sector_db)

    # Rich code topology if code sector.
    if lane_key in {"github", "local_code"}:
        tables = {
            "Files": "code_file",
            "FileVersions": "code_file_version",
            "Lines": "code_line_snapshot",
            "Chunks": "code_chunk",
            "Symbols": "code_symbol",
            "Routes": "app_route",
            "DependencyManifests": "dependency_manifest",
            "DependencyItems": "dependency_item",
            "Artifacts": "project_artifact",
            "Commits": "git_commit",
            "Coverage": "source_byte_coverage",
        }
        for node, table in tables.items():
            lines.append(f"  DB --> {node}[{node}: {count_table(con, table)}]")

        # Show route/file/sample edges.
        routes = con.execute("SELECT route_path, file_id FROM app_route LIMIT 20").fetchall()
        for i, (route, file_id) in enumerate(routes):
            rid = f"R{i}"
            fid = f"RF{i}"
            lines.append(f"  Routes --> {rid}[{route}]")
            lines.append(f"  {rid} --> {fid}[file_id:{file_id[:10]}]")

        deps = con.execute("SELECT package_name, version_spec FROM dependency_item LIMIT 25").fetchall()
        for i, (pkg, ver) in enumerate(deps):
            did = f"D{i}"
            safe = str(pkg).replace('"', "'")
            lines.append(f"  DependencyItems --> {did}[{safe}]")

        arts = con.execute("SELECT artifact_type, path FROM project_artifact LIMIT 25").fetchall()
        for i, (atype, path) in enumerate(arts):
            aid = f"A{i}"
            safe = str(path).replace('"', "'")[-60:]
            lines.append(f"  Artifacts --> {aid}[{atype}: {safe}]")

        lines += [
            "  Files --> FileVersions",
            "  FileVersions --> Lines",
            "  FileVersions --> Chunks",
            "  Chunks --> Symbols",
            "  Chunks --> Routes",
            "  Files --> DependencyManifests",
            "  DependencyManifests --> DependencyItems",
            "  Files --> Artifacts",
            "  Commits --> FileVersions",
        ]
    else:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
        for i, t in enumerate(tables[:40], start=1):
            rows = count_table(con, t)
            lines.append(f"  DB --> T{i}[{t}: {rows}]")
        lines += [
            "  DB --> SIG[source_structure_signature]",
            "  DB --> SIGNAL[extraction_signal]",
            "  DB --> FTS[search index]",
        ]

    con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

def write_master_mmd(router_db: Path, out: Path):
    lines = [
        "flowchart TD",
        "  PKG[One Upload Brain Package]",
        "  ENV[Embedded Env14 Public Model Law]",
        "  UOP[Embedded UOP Governance]",
        "  PROJ[Project Brain]",
        "  R[(project_router.sqlite)]",
        "  PKG --> ENV",
        "  PKG --> UOP",
        "  PKG --> PROJ",
        "  PROJ --> R",
    ]
    con = sqlite3.connect(router_db)
    rows = con.execute("SELECT lane_key, lane_label, sector_db_path FROM sector_registry WHERE active_bool=1 ORDER BY lane_label").fetchall()
    for i, (lane_key, label, dbpath) in enumerate(rows, start=1):
        node = f"S{i}"
        lines.append(f"  R --> {node}[{label}]")
        if lane_key in {"github", "local_code"}:
            lines.append(f"  {node} --> CODE{i}[files/versions/lines/chunks/symbols/routes/dependencies/artifacts]")
        else:
            lines.append(f"  {node} --> SEM{i}[sources/chunks/signatures/signals/search]")
    lines += [
        "  PROJ --> MMD[lane MMDs]",
        "  PROJ --> RECEIPTS[receipts]",
        "  PROJ --> FLASH[FLASH_ME_FIRST_SINGLE_PROMPT]",
    ]
    con.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

def build_package(brain_root: Path, package_zip: Path):
    package_zip.parent.mkdir(parents=True, exist_ok=True)
    if package_zip.exists():
        package_zip.unlink()
    with zipfile.ZipFile(package_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in brain_root.rglob("*"):
            if not p.is_file():
                continue
            if p.resolve() == package_zip.resolve():
                continue
            z.write(p, p.relative_to(brain_root).as_posix())
    return sha256_file(package_zip)

def build_universal_brain(workspace_dir: str, brain_name: str, sources: list[dict], progress: ProgressCallback | None = None) -> dict:
    active = [s for s in sources if s.get("active", True)]
    if not active:
        raise RuntimeError("NO_ACTIVE_SOURCES_LOADED")

    slug = slugify(brain_name)
    brain_root = brain_output_dir(workspace_dir, brain_name)
    router_db = brain_root / "project" / "project_router.sqlite"
    sectors_root = brain_root / "project" / "sectors"
    renders_root = brain_root / "project" / "topology"
    packages_root = brain_root / "packages"
    receipts_root = brain_root / "receipts"

    for d in [brain_root, sectors_root, renders_root, packages_root, receipts_root]:
        d.mkdir(parents=True, exist_ok=True)

    init_router(router_db, brain_name)

    # Copy embedded Env14 public model package into package root first.
    env14 = find_env14_resource_root()
    if not env14:
        raise RuntimeError("EMBEDDED_ENV15_RESOURCE_NOT_FOUND: compile with resources/public_model_env15")
    safe_copytree_contents(env14, brain_root)

    con = connect(router_db)
    con.execute("INSERT OR REPLACE INTO env14_resource_manifest VALUES(?,?,?,?,?)", ("env14", 1, str(env14), now(), "COPIED_INTO_BRAIN_PACKAGE_ROOT"))
    con.commit()
    con.close()

    total = max(len(active) * 5 + 8, 1)
    done = 0
    def step(stage: str, task: str, file: str = ""):
        nonlocal done
        done += 1
        pct = min(100, int(done * 100 / total))
        emit(progress, stage=stage, task=task, file=file, done=done, total=total, percent=pct)

    groups: dict[str, list[dict]] = {}
    for src in active:
        groups.setdefault(src["lane_key"], []).append(src)

    con = connect(router_db)

    for lane_key, group in groups.items():
        lane = get_lane(lane_key)
        sector_dir = sectors_root / lane_key
        sector_dir.mkdir(parents=True, exist_ok=True)
        sector_db = sector_dir / f"{lane_key}_sector_v001.sqlite"

        step("sector", f"building {lane['label']}", lane_key)

        if lane_key in {"github", "local_code"}:
            init_code_sector_schema(sector_db)
            for src in group:
                root = Path(src.get("path") or "")
                if not root.exists():
                    raise RuntimeError(f"CODE_SOURCE_NOT_FOUND: {root}")
                build_code_sector(sector_db, root, lane_key)
                src_hash = sha256_bytes(str(root).encode())
                con.execute("INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)", (src.get("source_id"), lane_key, lane["label"], src.get("source_type"), src.get("display_name"), str(root), src_hash, 1, now()))
                con.execute("INSERT OR REPLACE INTO source_active_state VALUES(?,?,?,?,?,?,?)", (uid("state"), src.get("source_id"), lane_key, 1, "ACTIVE", "loaded", now()))
        else:
            build_generic_sector(sector_db, group, lane_key)
            for src in group:
                p = src.get("path") or src.get("display_name") or lane_key
                src_hash = sha256_bytes((str(p) + str(src.get("text", ""))).encode())
                con.execute("INSERT OR REPLACE INTO source_registry VALUES(?,?,?,?,?,?,?,?,?)", (src.get("source_id"), lane_key, lane["label"], src.get("source_type"), src.get("display_name"), str(p), src_hash, 1, now()))
                con.execute("INSERT OR REPLACE INTO source_active_state VALUES(?,?,?,?,?,?,?)", (uid("state"), src.get("source_id"), lane_key, 1, "ACTIVE", "loaded", now()))

        sector_hash = sha256_file(sector_db)
        con.execute("INSERT OR REPLACE INTO sector_registry VALUES(?,?,?,?,?,?,?,?)", (uid("sector", lane_key), lane_key, lane["label"], str(sector_db), 1, "v001", sector_hash, now()))
        con.commit()

        mmd = renders_root / f"{lane_key}_lane.mmd"
        step("mmd", f"generating end-to-end {lane['label']} MMD", str(mmd))
        write_mmd_for_sector(sector_db, lane_key, mmd)

    con.close()

    master_mmd = renders_root / "project_master_topology.mmd"
    step("mmd", "generating master end-to-end topology", str(master_mmd))
    write_master_mmd(router_db, master_mmd)

    # Correct flash prompt using Env14 boot law + current brain fields.
    flash = brain_root / "FLASH_ME_FIRST_SINGLE_PROMPT.txt"
    flash.write_text(
        f"""UEPC-ENV15-FLASH-BOOT-001 | Mode: flash_env + validation | Category: one-upload env flash

CHAT_NAME:
[{brain_name}]

BRIEF_NATURE_OF_CHAT:
[SQLite Brain Builder V3 one-upload project brain package]

OPTIONAL_CHAT_LINEAGE_MD:
[NONE unless included in project/sectors/chat_lineage]

I uploaded one Env14 clean runpack ZIP. Do not ask for individual files. The ZIP is the env/package container.

First inspect the ZIP and read:
FLASH_ME_FIRST_SINGLE_PROMPT.txt
.uepc_env
.uepc_project
.uepc_profile
env/env_sqlite.sqlite
uop/uop_sqlite.sqlite
project/project_template.sqlite
project/project_router.sqlite
manifests/SCHEMA_INVENTORY.json
manifests/INTERNAL_HASH_MANIFEST.txt
receipts/last_exit_slip.txt

Treat the chat window as display only. Durable state is inside the package.

If OPTIONAL_CHAT_LINEAGE_MD is provided, ingest it only as PROJECT_CHAT_HISTORY_SOURCE / CHAT_HISTORY_DELTA_SOURCE.
Do not mutate env law from chat lineage unless the user explicitly says mode=flash_env.

Run compact entry slip, continue from package pointers, and preserve full internal receipts.

CURRENT USER TASK AFTER FLASH:
[Continue from the project brain package sectors. Read project/project_router.sqlite, project/sectors, and project/topology/project_master_topology.mmd before answering.]
""",
        encoding="utf-8"
    )

    receipt = receipts_root / "build_receipt.md"
    receipt.write_text(f"# Build Receipt\n\nbrain={brain_name}\ncreated={now()}\nsources={len(active)}\nenv14_integrated=yes\ncode_assets_metadata_only=yes\n", encoding="utf-8")

    package_zip = packages_root / f"{slug}_one_upload_package_v001.zip"
    step("package", "building one-upload package with embedded Env14", str(package_zip))
    pkg_hash = build_package(brain_root, package_zip)

    con = connect(router_db)
    con.execute("INSERT OR REPLACE INTO package_manifest VALUES(?,?,?,?)", (uid("pkg"), str(package_zip), now(), pkg_hash))
    con.commit()
    con.close()

    emit(progress, stage="done", task="complete", file=str(package_zip), done=total, total=total, percent=100)

    return {
        "brain_root": str(brain_root),
        "router_db": str(router_db),
        "package_zip": str(package_zip),
        "package_hash": pkg_hash,
    }
