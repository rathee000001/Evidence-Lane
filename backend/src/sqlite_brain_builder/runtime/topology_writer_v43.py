from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir


def safe_label(value, limit=160):
    text = str(value or "").replace("\\", "/").replace('"', "'")
    text = re.sub(r"[\[\]{}<>|`]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text or "none"


def count(con, table):
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def rows(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except Exception:
        return []


def header(title):
    # IMPORTANT: no YAML/frontmatter. Mermaid parser must see flowchart first.
    return [
        "flowchart TB",
        f'  TITLE["{safe_label(title, 220)}"]',
        "  classDef root fill:#111827,stroke:#111827,color:#ffffff,stroke-width:2px;",
        "  classDef db fill:#e8f1ff,stroke:#245,color:#111,stroke-width:1px;",
        "  classDef role fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef route fill:#fff4d6,stroke:#8a5a00,color:#111,stroke-width:1px;",
        "  classDef artifact fill:#f3e8ff,stroke:#635,color:#111,stroke-width:1px;",
        "  classDef warn fill:#ffe8e8,stroke:#922,color:#111,stroke-width:1px;",
        "",
    ]


def write_text(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cleanup_non_code_mmds(topology: Path):
    if not topology.exists():
        return
    keep_prefixes = ("local_code", "github", "project_master")
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        if not p.stem.startswith(keep_prefixes):
            try:
                p.unlink()
            except Exception:
                pass


def write_code_overview_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)
    role_counts = rows(con, "SELECT role_name, COUNT(*) FROM code_file_role GROUP BY role_name ORDER BY COUNT(*) DESC")

    lines = header("Code project overview from SQLite")
    lines += [
        '  ROOT["Code Project Brain"]:::root',
        '  DB["local_code_sector_v001.sqlite"]:::db',
        "  TITLE --> ROOT",
        "  ROOT --> DB",
        "  subgraph CORE_TABLES[SQLite code-sector table families]",
        f'    FILES["source_file / code_file\\nregistered={count(con,"source_file")} | code={count(con,"code_file")}"]:::db',
        f'    VERS["code_file_version\\n{count(con,"code_file_version")} versions"]:::db',
        f'    CHUNKS["code_chunk + code_fts\\n{count(con,"code_chunk")} chunks"]:::db',
        f'    ROLES["code_file_role\\n{count(con,"code_file_role")} role rows"]:::db',
        f'    SYMBOLS["code_symbol\\n{count(con,"code_symbol")} symbols"]:::db',
        f'    ROUTES["app_route\\n{count(con,"app_route")} routes"]:::db',
        f'    IMPORTS["code_import_edge\\n{count(con,"code_import_edge")} imports"]:::db',
        f'    DEPS["dependency_item\\n{count(con,"dependency_item")} deps"]:::db',
        f'    ARTS["project_artifact payload vault\\n{count(con,"project_artifact")} payloads"]:::artifact',
        "  end",
        "  DB --> FILES",
        "  FILES --> VERS",
        "  VERS --> CHUNKS",
        "  FILES --> ROLES",
        "  CHUNKS --> SYMBOLS",
        "  CHUNKS --> ROUTES",
        "  CHUNKS --> IMPORTS",
        "  FILES --> DEPS",
        "  FILES --> ARTS",
        "",
        "  subgraph ROLE_SUMMARY[Role summary]",
    ]

    prev = "ROLES"
    for idx, (role, n) in enumerate(role_counts):
        node = f"ROLE_SUM_{idx}"
        lines.append(f'    {node}["{safe_label(role)}\\n{n} files"]:::role')
        lines.append(f"    {prev} --> {node}")
        prev = node

    lines += [
        "  end",
        "",
        "  subgraph SECTION_OUTPUTS[Detailed topology files]",
        '    D1["local_code_roles_full.mmd"]:::db',
        '    D2["local_code_routes_workflow_full.mmd"]:::db',
        '    D3["local_code_dependencies_full.mmd"]:::db',
        '    D4["local_code_artifact_payload_full.mmd"]:::artifact',
        "  end",
        "  ROOT --> SECTION_OUTPUTS",
    ]

    write_text(out, lines)
    con.close()


def write_roles_full_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)

    data = rows(con, """
        SELECT r.role_name, f.canonical_path, f.language, f.extension, f.current_sha256
        FROM code_file_role r
        JOIN code_file f ON f.file_id = r.file_id
        ORDER BY r.role_name, f.canonical_path
    """)

    grouped = {}
    for role, path, lang, ext, digest in data:
        grouped.setdefault(role or "UNKNOWN", []).append((path, lang, ext, digest))

    lines = header("Full code role map")
    lines += [
        '  ROOT["All code files grouped by deterministic role"]:::root',
        '  RULE["Role is derived from path, extension, and deterministic content signals"]:::db',
        "  TITLE --> ROOT",
        "  ROOT --> RULE",
    ]

    prev_role = "RULE"
    for ridx, (role, items) in enumerate(sorted(grouped.items())):
        role_node = f"ROLE_{ridx}"
        lines.append(f'  subgraph SG_{ridx}["Role: {safe_label(role)}"]')
        lines.append(f'    {role_node}["{safe_label(role)}\\n{len(items)} files"]:::role')

        prev = role_node
        for i, (path, lang, ext, digest) in enumerate(items):
            node = f"F_{ridx}_{i}"
            label = f"{path}\\n{lang} {ext}\\nsha={str(digest)[:12]}"
            lines.append(f'    {node}["{safe_label(label, 240)}"]:::db')
            lines.append(f"    {prev} --> {node}")
            prev = node

        lines.append("  end")
        lines.append(f"  {prev_role} --> {role_node}")
        prev_role = role_node

    write_text(out, lines)
    con.close()


def write_routes_workflow_full_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)

    route_rows = rows(con, "SELECT route_path, route_type, file_id FROM app_route ORDER BY route_type, route_path")
    import_rows = rows(con, "SELECT from_path, import_target, import_type, line_number FROM code_import_edge ORDER BY from_path, line_number")
    symbol_rows = rows(con, "SELECT symbol_name, symbol_type, language, start_line FROM code_symbol ORDER BY language, symbol_type, symbol_name")

    lines = header("Routes imports symbols workflow")
    lines += [
        '  ROOT["Code workflow graph"]:::root',
        f'  ROUTE_TABLE["app_route\\n{len(route_rows)} routes"]:::route',
        f'  IMPORT_TABLE["code_import_edge\\n{len(import_rows)} imports"]:::db',
        f'  SYMBOL_TABLE["code_symbol\\n{len(symbol_rows)} symbols"]:::db',
        "  TITLE --> ROOT",
        "  ROOT --> ROUTE_TABLE",
        "  ROOT --> IMPORT_TABLE",
        "  ROOT --> SYMBOL_TABLE",
        "",
        "  subgraph ROUTES[Routes and API/page surfaces]",
    ]

    prev = "ROUTE_TABLE"
    for i, (route_path, route_type, file_id) in enumerate(route_rows):
        node = f"ROUTE_{i}"
        label = f"{route_type}\\n{route_path}\\nfile={str(file_id)[:10]}"
        lines.append(f'    {node}["{safe_label(label, 240)}"]:::route')
        lines.append(f"    {prev} --> {node}")
        prev = node
    lines.append("  end")

    lines += ["", "  subgraph IMPORTS[Import direction inside source code]"]
    prev = "IMPORT_TABLE"
    for i, (from_path, target, import_type, line_no) in enumerate(import_rows):
        node = f"IMP_{i}"
        label = f"{from_path}\\n{import_type} line {line_no}\\n-> {target}"
        lines.append(f'    {node}["{safe_label(label, 260)}"]:::db')
        lines.append(f"    {prev} --> {node}")
        prev = node
    lines.append("  end")

    lines += ["", "  subgraph SYMBOLS[Symbols components functions classes]"]
    prev = "SYMBOL_TABLE"
    for i, (name, stype, lang, line_no) in enumerate(symbol_rows):
        node = f"SYM_{i}"
        label = f"{stype} {name}\\n{lang} line {line_no}"
        lines.append(f'    {node}["{safe_label(label, 220)}"]:::role')
        lines.append(f"    {prev} --> {node}")
        prev = node
    lines.append("  end")

    write_text(out, lines)
    con.close()


def write_dependencies_full_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)

    dep_rows = rows(con, """
        SELECT m.ecosystem, m.path, d.package_name, d.version_spec
        FROM dependency_item d
        LEFT JOIN dependency_manifest m ON m.manifest_id = d.manifest_id
        ORDER BY m.ecosystem, d.package_name
    """)

    grouped = {}
    for ecosystem, manifest_path, package_name, version in dep_rows:
        grouped.setdefault(ecosystem or "unknown", []).append((manifest_path, package_name, version))

    lines = header("Dependency graph full package inventory")
    lines += [
        '  ROOT["Dependency graph from manifests"]:::root',
        f'  COUNT["dependency_item rows: {len(dep_rows)}"]:::db',
        "  TITLE --> ROOT",
        "  ROOT --> COUNT",
    ]

    prev_group = "COUNT"
    for eidx, (ecosystem, items) in enumerate(sorted(grouped.items())):
        eco_node = f"ECO_{eidx}"
        lines.append(f'  subgraph DEP_{eidx}["Ecosystem: {safe_label(ecosystem)}"]')
        lines.append(f'    {eco_node}["{safe_label(ecosystem)}\\n{len(items)} packages"]:::route')
        prev = eco_node
        for i, (manifest_path, package_name, version) in enumerate(items):
            node = f"DEP_{eidx}_{i}"
            label = f"{package_name}\\n{version}\\nmanifest={manifest_path}"
            lines.append(f'    {node}["{safe_label(label, 240)}"]:::route')
            lines.append(f"    {prev} --> {node}")
            prev = node
        lines.append("  end")
        lines.append(f"  {prev_group} --> {eco_node}")
        prev_group = eco_node

    write_text(out, lines)
    con.close()


def write_artifacts_full_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)

    art_rows = rows(con, """
        SELECT artifact_type, source_path, artifact_storage_path, size_bytes, artifact_sha256, semantic_status
        FROM project_artifact
        ORDER BY artifact_type, source_path
    """)

    lines = header("Artifact payload vault full preserved assets")
    lines += [
        '  ROOT["Artifact payload vault"]:::root',
        '  RULE["Image GLB video binary assets are preserved as payloads and hashes; not code chunked"]:::warn',
        f'  COUNT["project_artifact rows: {len(art_rows)}"]:::artifact',
        "  TITLE --> ROOT",
        "  ROOT --> RULE",
        "  ROOT --> COUNT",
        "",
        "  subgraph PAYLOADS[All preserved payloads]",
    ]

    prev = "COUNT"
    for i, (atype, source, stored, size_bytes, digest, status) in enumerate(art_rows):
        node = f"ART_{i}"
        label = f"{atype} | {size_bytes} bytes\\n{source}\\nsha={str(digest)[:16]}\\n{status}"
        lines.append(f'    {node}["{safe_label(label, 280)}"]:::artifact')
        lines.append(f"    {prev} --> {node}")
        prev = node

    lines.append("  end")
    write_text(out, lines)
    con.close()


def write_master_mmd(router_db: Path, out: Path):
    con = sqlite3.connect(router_db)

    sector_rows = rows(con, "SELECT lane_key, lane_label, sector_db_path FROM sector_registry WHERE active_bool=1 ORDER BY lane_label")
    source_rows = rows(con, "SELECT lane_label, source_type, display_name, path FROM source_registry WHERE active_bool=1 ORDER BY lane_label, display_name")

    lines = header("Project brain master topology router derived")
    lines += [
        '  ROOT["One project brain workspace"]:::root',
        '  ROUTER["project/project_router.sqlite"]:::db',
        '  PROJECT["project writable generated brain section"]:::db',
        '  TOPO["project/topology MMD SVG PNG"]:::db',
        '  ARTS["project/artifacts payload vault"]:::artifact',
        '  SECTORS["project/sectors SQLite files"]:::db',
        "  TITLE --> ROOT",
        "  ROOT --> PROJECT",
        "  PROJECT --> ROUTER",
        "  PROJECT --> SECTORS",
        "  PROJECT --> TOPO",
        "  PROJECT --> ARTS",
        "",
        "  subgraph ACTIVE_SECTORS[Active sectors from router]",
    ]

    prev = "SECTORS"
    for i, (lane_key, label, path) in enumerate(sector_rows):
        node = f"SEC_{i}"
        lines.append(f'    {node}["{safe_label(label)}\\n{safe_label(path, 180)}"]:::db')
        lines.append(f"    {prev} --> {node}")
        prev = node
        if lane_key in {"local_code", "github"}:
            code_node = f"CODE_GRAPH_{i}"
            lines.append(f'    {code_node}["code graph: files roles versions chunks symbols routes imports deps artifacts"]:::db')
            lines.append(f"    {node} --> {code_node}")
            lines.append(f"    {code_node} --> ARTS")
    lines.append("  end")

    lines += ["", "  subgraph ACTIVE_SOURCES[Active source intake rows]"]
    prev = "ROUTER"
    for i, (lane, source_type, display, path) in enumerate(source_rows):
        node = f"SRC_{i}"
        label = f"{lane}\\n{source_type}\\n{display or path}"
        lines.append(f'    {node}["{safe_label(label, 260)}"]:::role')
        lines.append(f"    {prev} --> {node}")
        prev = node
    lines.append("  end")

    write_text(out, lines)
    con.close()


def regenerate_topology_from_existing_brain(workspace_dir: str, brain_name: str):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    router = brain_root / "project" / "project_router.sqlite"
    code_db = brain_root / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"

    if not code_db.exists():
        raise RuntimeError(f"CODE_DB_NOT_FOUND: {code_db}")
    if not router.exists():
        raise RuntimeError(f"ROUTER_DB_NOT_FOUND: {router}")

    cleanup_non_code_mmds(topology)

    write_code_overview_mmd(code_db, topology / "local_code_lane.mmd")
    write_roles_full_mmd(code_db, topology / "local_code_roles_full.mmd")
    write_routes_workflow_full_mmd(code_db, topology / "local_code_routes_workflow_full.mmd")
    write_dependencies_full_mmd(code_db, topology / "local_code_dependencies_full.mmd")
    write_artifacts_full_mmd(code_db, topology / "local_code_artifact_payload_full.mmd")
    write_master_mmd(router, topology / "project_master_topology.mmd")

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "mmds": [str(p) for p in sorted(topology.glob("*.mmd"))],
    }


def write_code_project_mmd(db: Path, out: Path):
    topology = out.parent
    cleanup_non_code_mmds(topology)
    write_code_overview_mmd(db, topology / "local_code_lane.mmd")
    write_roles_full_mmd(db, topology / "local_code_roles_full.mmd")
    write_routes_workflow_full_mmd(db, topology / "local_code_routes_workflow_full.mmd")
    write_dependencies_full_mmd(db, topology / "local_code_dependencies_full.mmd")
    write_artifacts_full_mmd(db, topology / "local_code_artifact_payload_full.mmd")


def write_mmd(db: Path, lane_key: str, out: Path):
    if lane_key in {"local_code", "github"}:
        write_code_project_mmd(db, out)
    else:
        # Non-code lanes intentionally have no MMD.
        return
