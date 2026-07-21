from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir


KEEP_MMD_STEMS = {
    "local_code_lane",
    "project_master_topology",
}


def safe_label(value, limit=120):
    text = str(value or "").replace("\\", "/").replace('"', "'")
    text = re.sub(r"[\[\]{}<>|`]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text or "none"


def node_id(prefix, value):
    return f"{prefix}_{hashlib.sha1(str(value).encode('utf-8', errors='ignore')).hexdigest()[:10]}"


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


def write_text(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cleanup_topology(topology: Path):
    if not topology.exists():
        return

    # Remove old long-strip MMDs and their renders.
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        stem = p.stem
        base = stem
        for suffix in ["_CRYSTAL", "_HD", "_MEGA"]:
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        if base not in KEEP_MMD_STEMS:
            try:
                p.unlink()
            except Exception:
                pass


def role_count(con, role_name):
    try:
        return con.execute("SELECT COUNT(*) FROM code_file_role WHERE role_name=?", (role_name,)).fetchone()[0]
    except Exception:
        return 0


def role_samples(con, role_name, limit=4):
    return rows(
        con,
        """
        SELECT f.canonical_path
        FROM code_file_role r
        JOIN code_file f ON f.file_id = r.file_id
        WHERE r.role_name=?
        ORDER BY f.canonical_path
        LIMIT ?
        """,
        (role_name, limit),
    )


def artifact_type_counts(con, limit=8):
    return rows(
        con,
        """
        SELECT artifact_type, COUNT(*), SUM(size_bytes)
        FROM project_artifact
        GROUP BY artifact_type
        ORDER BY COUNT(*) DESC, artifact_type
        LIMIT ?
        """,
        (limit,),
    )


def route_groups(con):
    data = rows(con, "SELECT route_path, route_type FROM app_route ORDER BY route_path")
    groups = {
        "API routes": [],
        "Page routes": [],
        "Artifact/data pages": [],
        "Other routes": [],
    }
    for path, rtype in data:
        p = str(path or "").lower()
        if "api" in p or str(rtype).upper().startswith("API"):
            groups["API routes"].append(path)
        elif "artifact" in p or "data" in p or "model" in p:
            groups["Artifact/data pages"].append(path)
        elif str(rtype).upper().startswith("PAGE"):
            groups["Page routes"].append(path)
        else:
            groups["Other routes"].append(path)
    return groups


def dependency_groups(con, limit=12):
    return rows(
        con,
        """
        SELECT package_name, version_spec
        FROM dependency_item
        ORDER BY package_name
        LIMIT ?
        """,
        (limit,),
    )


def write_universal_code_workflow_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)

    total_sources = count(con, "source_file")
    total_code = count(con, "code_file")
    total_chunks = count(con, "code_chunk")
    total_symbols = count(con, "code_symbol")
    total_routes = count(con, "app_route")
    total_imports = count(con, "code_import_edge")
    total_deps = count(con, "dependency_item")
    total_artifacts = count(con, "project_artifact")
    total_commits = count(con, "git_commit")

    roles = [
        ("UI_PAGE_ROUTE", "UI page / app route layer"),
        ("UI_UX_COMPONENT", "UI/UX component layer"),
        ("API_BACKEND_ROUTE", "API backend route layer"),
        ("BACKEND_PROCESS", "Backend process layer"),
        ("SERVICE_OR_UTILITY", "Service / utility layer"),
        ("DATA_DB_LAYER", "Data / DB / model layer"),
        ("CONFIG_BUILD_TOOLING", "Config / build tooling"),
        ("TEST_QA", "Test / QA layer"),
        ("DOCS_OR_NOTES", "Docs / notes layer"),
        ("ARTIFACT_PAYLOAD", "Artifact payload vault"),
        ("GENERAL_CODE", "General supporting code"),
    ]

    lines = [
        "flowchart LR",
        "  %% Universal coded-project workflow topology generated from SQLite brain.",
        "  %% SQLite remains full-detail truth. MMD is workflow map, not row dump.",
        "  classDef root fill:#111827,stroke:#111827,color:#ffffff,stroke-width:2px;",
        "  classDef intake fill:#e8f1ff,stroke:#245,color:#111,stroke-width:1px;",
        "  classDef ui fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef api fill:#fff4d6,stroke:#8a5a00,color:#111,stroke-width:1px;",
        "  classDef data fill:#e0f2fe,stroke:#0369a1,color:#111,stroke-width:1px;",
        "  classDef artifact fill:#f3e8ff,stroke:#635,color:#111,stroke-width:1px;",
        "  classDef package fill:#fef3c7,stroke:#92400e,color:#111,stroke-width:1px;",
        "",
        '  START["Local / GitHub coded project source"]:::root',
        f'  INTAKE["Source intake + hash + byte coverage\\nfiles={total_sources} | commits={total_commits}"]:::intake',
        f'  DB["local_code_sector_v001.sqlite\\ncode files={total_code} | chunks={total_chunks} | symbols={total_symbols}"]:::intake',
        '  ROLEMAP["Deterministic code role map\\npath + extension + content signals"]:::intake',
        "",
        "  START --> INTAKE --> DB --> ROLEMAP",
        "",
        "  subgraph APP_FLOW[Readable project workflow]",
        f'    UI_ROUTE["UI page / route surface\\n{role_count(con, "UI_PAGE_ROUTE")} files | routes={total_routes}"]:::ui',
        f'    UI_COMP["UI / UX components\\n{role_count(con, "UI_UX_COMPONENT")} files"]:::ui',
        f'    API_ROUTE["API / backend routes\\n{role_count(con, "API_BACKEND_ROUTE")} files"]:::api',
        f'    SERVICE["Services / utilities / backend process\\n{role_count(con, "SERVICE_OR_UTILITY") + role_count(con, "BACKEND_PROCESS")} files | imports={total_imports}"]:::api',
        f'    DATA["Data / DB / model / feature layer\\n{role_count(con, "DATA_DB_LAYER")} files"]:::data',
        f'    DEPS["Runtime dependency layer\\n{total_deps} package rows"]:::package',
        f'    ARTIFACTS["Artifact payload vault\\n{total_artifacts} preserved payload rows\\nno code chunking for image / GLB / video"]:::artifact',
        f'    CONFIG["Config / build tooling\\n{role_count(con, "CONFIG_BUILD_TOOLING")} files"]:::package',
        f'    TESTS["Tests / docs / notes\\n{role_count(con, "TEST_QA") + role_count(con, "DOCS_OR_NOTES")} files"]:::intake',
        "  end",
        "",
        "  ROLEMAP --> UI_ROUTE",
        "  ROLEMAP --> UI_COMP",
        "  ROLEMAP --> API_ROUTE",
        "  ROLEMAP --> SERVICE",
        "  ROLEMAP --> DATA",
        "  ROLEMAP --> CONFIG",
        "  ROLEMAP --> TESTS",
        "  ROLEMAP --> ARTIFACTS",
        "",
        "  UI_ROUTE --> UI_COMP",
        "  UI_COMP --> SERVICE",
        "  UI_COMP --> API_ROUTE",
        "  API_ROUTE --> SERVICE",
        "  SERVICE --> DATA",
        "  DATA --> ARTIFACTS",
        "  CONFIG --> DEPS",
        "  DEPS --> UI_ROUTE",
        "  DEPS --> API_ROUTE",
        "",
        "  subgraph ROUTE_SUMMARY[Route surface summary]",
    ]

    rgroups = route_groups(con)
    prev = "UI_ROUTE"
    for idx, (group, items) in enumerate(rgroups.items()):
        if not items:
            continue
        node = f"ROUTE_G_{idx}"
        sample = "\\n".join(safe_label(x, 45) for x in items[:4])
        label = f"{group}\\ncount={len(items)}"
        if sample:
            label += f"\\n{sample}"
        lines.append(f'    {node}["{safe_label(label, 240)}"]:::ui')
        lines.append(f"    {prev} --> {node}")
        prev = node
    lines.append("  end")

    lines += [
        "",
        "  subgraph ROLE_SAMPLES[Role sample files, details remain in SQLite]",
    ]

    previous_role = "ROLEMAP"
    for idx, (role, label) in enumerate(roles):
        n = role_count(con, role)
        if n == 0:
            continue
        sample_rows = role_samples(con, role, 3)
        sample = "\\n".join(safe_label(x[0], 52) for x in sample_rows)
        node = f"ROLE_{idx}"
        node_label = f"{label}\\n{n} files"
        if sample:
            node_label += f"\\n{sample}"
        css = "artifact" if role == "ARTIFACT_PAYLOAD" else ("ui" if role.startswith("UI") else ("api" if "API" in role or "BACKEND" in role or "SERVICE" in role else "intake"))
        lines.append(f'    {node}["{safe_label(node_label, 260)}"]:::{css}')
        lines.append(f"    {previous_role} --> {node}")
        previous_role = node
    lines.append("  end")

    lines += [
        "",
        "  subgraph DEP_SUMMARY[Dependency summary]",
    ]

    previous_dep = "DEPS"
    for idx, (name, version) in enumerate(dependency_groups(con, 14)):
        node = f"DEP_{idx}"
        label = f"{name}\\n{version}"
        lines.append(f'    {node}["{safe_label(label, 120)}"]:::package')
        lines.append(f"    {previous_dep} --> {node}")
        previous_dep = node
    lines.append("  end")

    lines += [
        "",
        "  subgraph ARTIFACT_SUMMARY[Artifact payload summary]",
    ]

    previous_art = "ARTIFACTS"
    for idx, (atype, n, size_sum) in enumerate(artifact_type_counts(con, 12)):
        node = f"ART_{idx}"
        label = f"{atype}\\ncount={n} | bytes={size_sum or 0}"
        lines.append(f'    {node}["{safe_label(label, 140)}"]:::artifact')
        lines.append(f"    {previous_art} --> {node}")
        previous_art = node
    lines.append("  end")

    lines += [
        "",
        "  subgraph HANDOFF[Project brain handoff]",
        '    ROUTER["project/project_router.sqlite"]:::intake',
        '    SECTOR["project/sectors/local_code/local_code_sector_v001.sqlite"]:::intake',
        '    VAULT["project/artifacts/code_asset_vault"]:::artifact',
        '    TOPO["project/topology/local_code_lane.mmd + SVG + CRYSTAL PNG"]:::package',
        '    PACKAGE["one-upload package project/ section"]:::package',
        "  end",
        "",
        "  DB --> ROUTER",
        "  DB --> SECTOR",
        "  ARTIFACTS --> VAULT",
        "  ROLEMAP --> TOPO",
        "  ROUTER --> PACKAGE",
        "  SECTOR --> PACKAGE",
        "  VAULT --> PACKAGE",
    ]

    write_text(out, lines)
    con.close()


def write_project_master_mmd(router_db: Path, out: Path):
    con = sqlite3.connect(router_db)
    sectors = rows(con, "SELECT lane_key, lane_label, sector_db_path FROM sector_registry WHERE active_bool=1 ORDER BY lane_label")
    sources = rows(con, "SELECT lane_label, source_type, display_name, path FROM source_registry WHERE active_bool=1 ORDER BY lane_label, display_name")

    lines = [
        "flowchart TD",
        "  %% Project master topology. Non-code sectors are DB/pointer fill targets, not MMD workflows.",
        "  classDef root fill:#111827,stroke:#111827,color:#ffffff,stroke-width:2px;",
        "  classDef db fill:#e8f1ff,stroke:#245,color:#111,stroke-width:1px;",
        "  classDef code fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef package fill:#fef3c7,stroke:#92400e,color:#111,stroke-width:1px;",
        '  ROOT["Generated project brain"]:::root',
        '  ROUTER["project/project_router.sqlite"]:::db',
        '  POINTERS["project/pointers/*.json"]:::db',
        '  SECTORS["project/sectors/*.sqlite"]:::db',
        '  CODEFLOW["code workflow topology\\nonly code gets MMD/SVG/PNG"]:::code',
        '  PACKAGE["one-upload package\\nEnv/UOP locked + project fillable DBs"]:::package',
        "  ROOT --> ROUTER",
        "  ROUTER --> POINTERS",
        "  ROUTER --> SECTORS",
        "  SECTORS --> CODEFLOW",
        "  ROOT --> PACKAGE",
        "",
        "  subgraph SECTOR_POINTERS[Sector DBs available for public model fill]",
    ]

    previous = "SECTORS"
    for idx, (lane_key, label, path) in enumerate(sectors):
        node = f"SEC_{idx}"
        if lane_key in {"local_code", "github"}:
            label_text = f"{label}\\nworkflow MMD exists"
            css = "code"
        else:
            label_text = f"{label}\\nDB + pointer only\\nno MMD"
            css = "db"
        lines.append(f'    {node}["{safe_label(label_text, 140)}"]:::{css}')
        lines.append(f"    {previous} --> {node}")
        previous = node

    lines.append("  end")

    if sources:
        lines += ["", "  subgraph ACTIVE_SOURCE_INTAKE[Selected sources used in this build]"]
        prev = "ROUTER"
        for idx, (lane, stype, display, path) in enumerate(sources[:20]):
            node = f"SRC_{idx}"
            label = f"{lane}\\n{stype}\\n{display or path}"
            lines.append(f'    {node}["{safe_label(label, 200)}"]:::db')
            lines.append(f"    {prev} --> {node}")
            prev = node
        lines.append("  end")

    write_text(out, lines)
    con.close()


def write_text(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def regenerate_topology_from_existing_brain(workspace_dir: str, brain_name: str):
    brain_root = brain_output_dir(workspace_dir, brain_name)
    topology = brain_root / "project" / "topology"
    router = brain_root / "project" / "project_router.sqlite"
    code_db = brain_root / "project" / "sectors" / "local_code" / "local_code_sector_v001.sqlite"

    if not code_db.exists():
        raise RuntimeError(f"CODE_DB_NOT_FOUND: {code_db}")
    if not router.exists():
        raise RuntimeError(f"ROUTER_DB_NOT_FOUND: {router}")

    cleanup_topology(topology)

    write_universal_code_workflow_mmd(code_db, topology / "local_code_lane.mmd")
    write_project_master_mmd(router, topology / "project_master_topology.mmd")

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "mmds": [str(p) for p in sorted(topology.glob("*.mmd"))],
    }


def write_code_project_mmd(db: Path, out: Path):
    topology = out.parent
    cleanup_topology(topology)
    write_universal_code_workflow_mmd(db, topology / "local_code_lane.mmd")


def write_master_mmd(router_db: Path, out: Path):
    cleanup_topology(out.parent)
    write_project_master_mmd(router_db, out)


def write_mmd(db: Path, lane_key: str, out: Path):
    if lane_key in {"local_code", "github"}:
        write_code_project_mmd(db, out)
    return
