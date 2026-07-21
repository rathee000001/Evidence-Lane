from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from sqlite_brain_builder.runtime.path_policy import brain_output_dir


KEEP_MMD_STEMS = {"local_code_lane", "project_master_topology"}


def safe(value, limit=140):
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


def cleanup_topology(topology: Path):
    if not topology.exists():
        return
    for p in list(topology.glob("*.mmd")) + list(topology.glob("*.svg")) + list(topology.glob("*.png")):
        stem = p.stem
        for suffix in ["_CRYSTAL", "_4K", "_HD", "_MEGA"]:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
        if stem not in KEEP_MMD_STEMS:
            try:
                p.unlink()
            except Exception:
                pass


def write_text(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def role_count(con, role_name):
    try:
        return con.execute("SELECT COUNT(*) FROM code_file_role WHERE role_name=?", (role_name,)).fetchone()[0]
    except Exception:
        return 0


def route_rows(con):
    data = rows(
        con,
        """
        SELECT r.route_path, r.route_type, r.file_id, f.canonical_path
        FROM app_route r
        LEFT JOIN code_file f ON f.file_id = r.file_id
        ORDER BY r.route_type, r.route_path
        """
    )

    if data:
        return data

    # Fallback if app_route table is empty but files exist.
    return rows(
        con,
        """
        SELECT f.canonical_path, 'ROUTE_CANDIDATE', f.file_id, f.canonical_path
        FROM code_file f
        WHERE f.canonical_path LIKE '%/page.%'
           OR f.canonical_path LIKE '%/route.%'
           OR f.canonical_path LIKE '%/api/%'
           OR f.canonical_path LIKE 'app/%'
           OR f.canonical_path LIKE 'src/app/%'
        ORDER BY f.canonical_path
        """
    )


def route_import_targets(con, file_path, limit=4):
    return rows(
        con,
        """
        SELECT import_target, import_type
        FROM code_import_edge
        WHERE from_path=?
        ORDER BY line_number
        LIMIT ?
        """,
        (file_path, limit),
    )


def route_symbols(con, file_id, limit=3):
    return rows(
        con,
        """
        SELECT symbol_name, symbol_type
        FROM code_symbol
        WHERE file_id=?
        ORDER BY start_line
        LIMIT ?
        """,
        (file_id, limit),
    )


def dependency_rows(con, limit=12):
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


def artifact_type_counts(con, limit=12):
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


def write_route_first_code_mmd(db: Path, out: Path):
    con = sqlite3.connect(db)

    routes = route_rows(con)

    total_sources = count(con, "source_file")
    total_code = count(con, "code_file")
    total_routes = len(routes)
    total_symbols = count(con, "code_symbol")
    total_imports = count(con, "code_import_edge")
    total_deps = count(con, "dependency_item")
    total_artifacts = count(con, "project_artifact")
    total_chunks = count(con, "code_chunk")

    lines = [
        "flowchart LR",
        "  %% Route-first coded-project topology generated from SQLite.",
        "  %% SQLite DB contains full detail. MMD is workflow map for fast understanding.",
        "  classDef root fill:#111827,stroke:#111827,color:#ffffff,stroke-width:2px;",
        "  classDef route fill:#fff4d6,stroke:#8a5a00,color:#111,stroke-width:1px;",
        "  classDef code fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef backend fill:#e0f2fe,stroke:#0369a1,color:#111,stroke-width:1px;",
        "  classDef artifact fill:#f3e8ff,stroke:#635,color:#111,stroke-width:1px;",
        "  classDef db fill:#e8f1ff,stroke:#245,color:#111,stroke-width:1px;",
        "  classDef package fill:#fef3c7,stroke:#92400e,color:#111,stroke-width:1px;",
        "",
        '  PROJECT["Coded project source"]:::root',
        f'  SQLITE["local_code_sector_v001.sqlite\\nfiles={total_sources} | code={total_code} | chunks={total_chunks} | symbols={total_symbols}"]:::db',
        f'  ROUTE_SURFACE["Route/page surface\\nroutes={total_routes}"]:::route',
        f'  CODE_ROLES["Code role map\\nUI={role_count(con,"UI_UX_COMPONENT")} | API={role_count(con,"API_BACKEND_ROUTE")} | service={role_count(con,"SERVICE_OR_UTILITY") + role_count(con,"BACKEND_PROCESS")} | data={role_count(con,"DATA_DB_LAYER")}"]:::code',
        f'  DEPS["Dependencies/tooling\\n{total_deps} packages"]:::package',
        f'  ARTIFACTS["Artifact payload vault\\n{total_artifacts} payloads preserved\\nimages/GLB/video not code-chunked"]:::artifact',
        "",
        "  PROJECT --> SQLITE",
        "  SQLITE --> ROUTE_SURFACE",
        "  SQLITE --> CODE_ROLES",
        "  SQLITE --> DEPS",
        "  SQLITE --> ARTIFACTS",
        "",
        "  ROUTE_SURFACE --> CODE_ROLES",
        "  CODE_ROLES --> DEPS",
        "  CODE_ROLES --> ARTIFACTS",
        "",
        "  subgraph ROUTES[Routes/pages as primary workflow nodes]",
        "    direction TB",
    ]

    previous = "ROUTE_SURFACE"
    for idx, (route_path, route_type, file_id, file_path) in enumerate(routes):
        route_node = f"ROUTE_{idx}"
        file_node = f"ROUTE_FILE_{idx}"
        symbol_node = f"ROUTE_SYMBOL_{idx}"
        import_node = f"ROUTE_IMPORT_{idx}"

        route_label = f"{route_type}\\n{route_path}"
        file_label = f"route file\\n{file_path or file_id}"

        syms = route_symbols(con, file_id, 3)
        sym_text = "\\n".join(f"{stype}:{name}" for name, stype in syms) if syms else "symbols in SQLite"

        imps = route_import_targets(con, file_path or "", 4)
        imp_text = "\\n".join(f"{itype}:{target}" for target, itype in imps) if imps else "imports in SQLite"

        lines.append(f'    {route_node}["{safe(route_label, 180)}"]:::route')
        lines.append(f'    {file_node}["{safe(file_label, 180)}"]:::code')
        lines.append(f'    {symbol_node}["{safe(sym_text, 180)}"]:::code')
        lines.append(f'    {import_node}["{safe(imp_text, 180)}"]:::backend')
        lines.append(f"    {previous} --> {route_node}")
        lines.append(f"    {route_node} --> {file_node}")
        lines.append(f"    {file_node} --> {symbol_node}")
        lines.append(f"    {file_node} --> {import_node}")
        lines.append(f"    {import_node} --> CODE_ROLES")
        previous = route_node

    lines.append("  end")

    lines += [
        "",
        "  subgraph CODE_LAYERS[Code layer clusters]",
        "    direction TB",
        f'    UI_PAGE["UI page/app route files\\n{role_count(con,"UI_PAGE_ROUTE")} files"]:::route',
        f'    UI_COMP["UI/UX components\\n{role_count(con,"UI_UX_COMPONENT")} files"]:::code',
        f'    API_LAYER["API/backend routes\\n{role_count(con,"API_BACKEND_ROUTE")} files"]:::backend',
        f'    SERVICE_LAYER["services/utilities/backend process\\n{role_count(con,"SERVICE_OR_UTILITY") + role_count(con,"BACKEND_PROCESS")} files"]:::backend',
        f'    DATA_LAYER["data/DB/model layer\\n{role_count(con,"DATA_DB_LAYER")} files"]:::db',
        f'    CONFIG_LAYER["config/build tooling\\n{role_count(con,"CONFIG_BUILD_TOOLING")} files"]:::package',
        "  end",
        "",
        "  CODE_ROLES --> UI_PAGE",
        "  UI_PAGE --> UI_COMP",
        "  UI_COMP --> API_LAYER",
        "  API_LAYER --> SERVICE_LAYER",
        "  SERVICE_LAYER --> DATA_LAYER",
        "  CONFIG_LAYER --> DEPS",
        "  DATA_LAYER --> ARTIFACTS",
        "",
        "  subgraph DEPENDENCY_SUMMARY[Dependency/tooling summary]",
        "    direction TB",
    ]

    prev_dep = "DEPS"
    for idx, (name, version) in enumerate(dependency_rows(con, 14)):
        node = f"DEP_{idx}"
        label = f"{name}\\n{version}"
        lines.append(f'    {node}["{safe(label, 120)}"]:::package')
        lines.append(f"    {prev_dep} --> {node}")
        prev_dep = node
    lines.append("  end")

    lines += [
        "",
        "  subgraph ARTIFACT_SUMMARY[Artifact vault summary]",
        "    direction TB",
    ]

    prev_art = "ARTIFACTS"
    for idx, (atype, n, size_sum) in enumerate(artifact_type_counts(con, 12)):
        node = f"ART_{idx}"
        label = f"{atype}\\ncount={n} | bytes={size_sum or 0}"
        lines.append(f'    {node}["{safe(label, 140)}"]:::artifact')
        lines.append(f"    {prev_art} --> {node}")
        prev_art = node
    lines.append("  end")

    lines += [
        "",
        "  subgraph HANDOFF[Code brain outputs]",
        "    direction TB",
        '    ROUTER["project/project_router.sqlite"]:::db',
        '    CODE_DB["project/sectors/local_code/local_code_sector_v001.sqlite"]:::db',
        '    VAULT["project/artifacts/code_asset_vault"]:::artifact',
        '    TOPOLOGY["project/topology/local_code_lane.mmd + svg + 4k/crystal png"]:::package',
        "  end",
        "",
        "  SQLITE --> CODE_DB",
        "  SQLITE --> ROUTER",
        "  ARTIFACTS --> VAULT",
        "  ROUTE_SURFACE --> TOPOLOGY",
    ]

    write_text(out, lines)
    con.close()


def write_project_master_mmd(router_db: Path, out: Path):
    con = sqlite3.connect(router_db)

    sectors = rows(con, "SELECT lane_key, lane_label, sector_db_path FROM sector_registry WHERE active_bool=1 ORDER BY lane_label")

    lines = [
        "flowchart TD",
        "  %% Project master topology. This is package/sector structure, not code workflow.",
        "  classDef root fill:#111827,stroke:#111827,color:#ffffff,stroke-width:2px;",
        "  classDef locked fill:#ffe8e8,stroke:#922,color:#111,stroke-width:1px;",
        "  classDef fill fill:#e8f1ff,stroke:#245,color:#111,stroke-width:1px;",
        "  classDef code fill:#eaffea,stroke:#275,color:#111,stroke-width:1px;",
        "  classDef pkg fill:#fef3c7,stroke:#92400e,color:#111,stroke-width:1px;",
        "",
        '  PACKAGE["One-upload package"]:::root',
        '  LOCKED["locked public model law\\nenv + uop + project_template_locked"]:::locked',
        '  PROJECT["generated project brain\\nfillable sector DBs + pointers"]:::fill',
        '  ROUTER["project/project_router.sqlite"]:::fill',
        '  POINTERS["project/pointers/*.json"]:::fill',
        '  SECTORS["project/sectors/*.sqlite"]:::fill',
        '  CODE_MMD["code workflow topology only\\nlocal_code_lane.mmd/svg/png"]:::code',
        "",
        "  PACKAGE --> LOCKED",
        "  PACKAGE --> PROJECT",
        "  PROJECT --> ROUTER",
        "  PROJECT --> POINTERS",
        "  PROJECT --> SECTORS",
        "  PROJECT --> CODE_MMD",
        "",
        "  subgraph SECTOR_DB_POINTERS[Predefined sector DBs and pointers]",
        "    direction TB",
    ]

    previous = "SECTORS"
    for idx, (lane_key, label, path) in enumerate(sectors):
        node = f"SEC_{idx}"
        if lane_key in {"local_code", "github"}:
            text = f"{label}\\nDB + pointer + code MMD"
            css = "code"
        else:
            text = f"{label}\\nempty/fillable DB + pointer\\nno MMD"
            css = "fill"
        lines.append(f'    {node}["{safe(text, 150)}"]:::{css}')
        lines.append(f"    {previous} --> {node}")
        previous = node

    lines.append("  end")

    lines += [
        "",
        '  EXPORT["export result\\nEnv/UOP locked + generated project section"]:::pkg',
        "  LOCKED --> EXPORT",
        "  PROJECT --> EXPORT",
    ]

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

    write_route_first_code_mmd(code_db, topology / "local_code_lane.mmd")
    write_project_master_mmd(router, topology / "project_master_topology.mmd")

    return {
        "brain_root": str(brain_root),
        "topology": str(topology),
        "mmds": [str(p) for p in sorted(topology.glob("*.mmd"))],
    }


def write_code_project_mmd(db: Path, out: Path):
    cleanup_topology(out.parent)
    write_route_first_code_mmd(db, out.parent / "local_code_lane.mmd")


def write_master_mmd(router_db: Path, out: Path):
    cleanup_topology(out.parent)
    write_project_master_mmd(router_db, out)


def write_mmd(db: Path, lane_key: str, out: Path):
    if lane_key in {"local_code", "github"}:
        write_code_project_mmd(db, out)
    return
