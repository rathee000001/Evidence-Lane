from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir
from sqlite_brain_builder.runtime.sector_schema_v49 import cleanup_non_code_mmds

def safe(value, limit=90):
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

def write_text(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def role_count(con, role):
    try:
        return con.execute("SELECT COUNT(*) FROM code_file_role WHERE role_name=?", (role,)).fetchone()[0]
    except Exception:
        return 0

def route_rows(con):
    return rows(
        con,
        """
        SELECT r.route_path, r.route_type, r.file_id, f.canonical_path
        FROM app_route r
        LEFT JOIN code_file f ON f.file_id = r.file_id
        ORDER BY r.route_type, r.route_path
        """
    )

def import_targets(con, from_path, limit=4):
    return rows(
        con,
        """
        SELECT import_target, import_type
        FROM code_import_edge
        WHERE from_path=?
        ORDER BY line_number
        LIMIT ?
        """,
        (from_path, limit),
    )

def dependency_summary(con, limit=12):
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

def artifact_summary(con, limit=12):
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

def write_route_code_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)
    routes = route_rows(con)

    lines = [
        "flowchart LR",
        "  %% Code workflow only: routes/pages -> code used -> deps/tools -> artifacts.",
        "  classDef route fill:#fff4d6,stroke:#8a5a00,color:#111,stroke-width:1px;",
        "  classDef code fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef service fill:#e0f2fe,stroke:#0369a1,color:#111,stroke-width:1px;",
        "  classDef dep fill:#fef3c7,stroke:#92400e,color:#111,stroke-width:1px;",
        "  classDef artifact fill:#f3e8ff,stroke:#635,color:#111,stroke-width:1px;",
        "  classDef root fill:#111827,stroke:#111827,color:#fff,stroke-width:2px;",
        "",
        '  ROOT["coded project"]:::root',
        f'  ROUTES["routes / pages\\n{len(routes)} route nodes"]:::route',
        f'  UI["UI / UX code\\n{role_count(con,"UI_UX_COMPONENT")} files"]:::code',
        f'  API["API / backend code\\n{role_count(con,"API_BACKEND_ROUTE") + role_count(con,"BACKEND_PROCESS")} files"]:::service',
        f'  SERVICE["services / utilities\\n{role_count(con,"SERVICE_OR_UTILITY")} files"]:::service',
        f'  DATA["data / model / DB layer\\n{role_count(con,"DATA_DB_LAYER")} files"]:::service',
        f'  DEPS["tools + dependencies\\n{count(con,"dependency_item")} packages"]:::dep',
        f'  ART["artifact vault\\n{count(con,"project_artifact")} payload rows"]:::artifact',
        "",
        "  ROOT --> ROUTES",
        "  ROUTES --> UI",
        "  ROUTES --> API",
        "  UI --> SERVICE",
        "  API --> SERVICE",
        "  SERVICE --> DATA",
        "  SERVICE --> DEPS",
        "  DATA --> ART",
        "  UI --> ART",
        "",
        "  subgraph ROUTE_WORKFLOW[route/page nodes linked to code used]",
        "    direction TB",
    ]

    previous = "ROUTES"
    for idx, (route_path, route_type, file_id, file_path) in enumerate(routes[:80]):
        route_node = f"R{idx}"
        file_node = f"F{idx}"
        import_node = f"I{idx}"

        imports = import_targets(con, file_path or "", 4)
        import_label = "imports in SQLite"
        if imports:
            import_label = "\\n".join(f"{itype}: {target}" for target, itype in imports)

        lines.append(f'    {route_node}["{safe(route_path, 80)}"]:::route')
        lines.append(f'    {file_node}["{safe(file_path or file_id, 95)}"]:::code')
        lines.append(f'    {import_node}["{safe(import_label, 150)}"]:::service')
        lines.append(f"    {previous} --> {route_node}")
        lines.append(f"    {route_node} --> {file_node}")
        lines.append(f"    {file_node} --> {import_node}")
        lines.append(f"    {import_node} --> UI")
        lines.append(f"    {import_node} --> API")
        previous = route_node

    lines.append("  end")

    lines += ["", "  subgraph DEPENDENCIES[tools and dependencies]", "    direction TB"]
    previous = "DEPS"
    for idx, (name, version) in enumerate(dependency_summary(con, 14)):
        node = f"D{idx}"
        lines.append(f'    {node}["{safe(name, 70)}\\n{safe(version, 50)}"]:::dep')
        lines.append(f"    {previous} --> {node}")
        previous = node
    lines.append("  end")

    lines += ["", "  subgraph ARTIFACTS[artifact payload groups]", "    direction TB"]
    previous = "ART"
    for idx, (atype, n, size_sum) in enumerate(artifact_summary(con, 12)):
        node = f"A{idx}"
        lines.append(f'    {node}["{safe(atype, 70)}\\ncount={n} | bytes={size_sum or 0}"]:::artifact')
        lines.append(f"    {previous} --> {node}")
        previous = node
    lines.append("  end")

    write_text(out, lines)
    con.close()

def write_project_master_mmd(router_db: Path, out: Path):
    con = sqlite3.connect(router_db)
    sectors = rows(con, "SELECT lane_key, lane_label FROM sector_registry WHERE active_bool=1 ORDER BY lane_label")

    lines = [
        "flowchart TD",
        "  %% Project topology only: locked public model law + generated sector DBs/pointers.",
        "  classDef locked fill:#ffe8e8,stroke:#922,color:#111,stroke-width:1px;",
        "  classDef fill fill:#e8f1ff,stroke:#245,color:#111,stroke-width:1px;",
        "  classDef code fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef root fill:#111827,stroke:#111827,color:#fff,stroke-width:2px;",
        "",
        '  PKG["one-upload package"]:::root',
        '  LAW["locked env + uop + project template"]:::locked',
        '  GEN["generated project brain"]:::fill',
        '  PTR["sector pointers"]:::fill',
        '  DB["sector SQLite DBs"]:::fill',
        '  CODE["code workflow MMD only"]:::code',
        "",
        "  PKG --> LAW",
        "  PKG --> GEN",
        "  GEN --> PTR",
        "  GEN --> DB",
        "  DB --> CODE",
        "",
        "  subgraph SECTORS[fillable sector DBs]",
        "    direction TB",
    ]

    previous = "DB"
    for idx, (lane_key, label) in enumerate(sectors):
        node = f"S{idx}"
        if lane_key in {"local_code", "github"}:
            label_text = f"{label}\\nDB + pointer + code MMD"
            css = "code"
        else:
            label_text = f"{label}\\nDB + pointer\\nno MMD"
            css = "fill"
        lines.append(f'    {node}["{safe(label_text, 90)}"]:::{css}')
        lines.append(f"    {previous} --> {node}")
        previous = node

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
    write_route_code_mmd(code_db, topology / "local_code_lane.mmd")
    write_project_master_mmd(router, topology / "project_master_topology.mmd")
    return {"brain_root": str(brain_root), "topology": str(topology)}

def write_code_project_mmd(db: Path, out: Path):
    cleanup_non_code_mmds(out.parent)
    write_route_code_mmd(db, out.parent / "local_code_lane.mmd")

def write_master_mmd(router_db: Path, out: Path):
    cleanup_non_code_mmds(out.parent)
    write_project_master_mmd(router_db, out)

def write_mmd(db: Path, lane_key: str, out: Path):
    if lane_key in {"local_code", "github"}:
        write_code_project_mmd(db, out)
