from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlite_brain_builder.runtime.env15_project_schema import (
    ENV15_PHYSICAL_SECTORS,
    verify_env15_live_project,
)
from sqlite_brain_builder.runtime.env15_locked_read import (
    LOCKED_READ_ARCHIVE_SHA256,
    read_chatgpt_gemini_authority_member,
    supplied_env_uop_database_rows,
    write_supplied_env_uop_provenance_receipt,
)


class Env15TopologyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TopologyArtifact:
    topology_id: str
    mmd_path: str
    mmd_sha256: str
    required_nodes: tuple[str, ...]
    node_count: int
    edge_count: int
    semantic_status: str
    svg_path: str | None = None
    svg_sha256: str | None = None
    png_path: str | None = None
    png_sha256: str | None = None
    hd_png_path: str | None = None
    hd_png_sha256: str | None = None
    render_status: str = "NOT_RENDERED"


REQUIRED_NODES = {
    "ENV": ("USER_PROMPT", "ENV_ENTRY", "ENV_DB", "SOURCE_TRUTH", "MODE_GATES", "UOP_ROUTE", "PROJECT_ROUTE", "ENV_RELOCK", "ENV_EXIT"),
    "UOP": ("USER_PROMPT", "UOP_ENTRY", "UOP_DB", "OPERATOR_REGISTRY", "HUMAN_GATE", "PROJECT_ROUTE", "UOP_RECEIPT", "UOP_EXIT"),
    "PROJECT": ("USER_PROMPT", "PROJECT_ENTRY", "PROJECT_ROUTER", "SECTOR_REGISTRY", "MUTATION_GRANT", "PRIOR_SNAPSHOT", "SECTOR_WRITE", "DUAL_RECEIPTS", "RELOCK", "RESULT_SNAPSHOT", "MMD_RENDER", "PACKAGE_VALIDATE", "PROJECT_EXIT"),
}

DERIVED_TOPOLOGY_EDGES = {
    "ENV": (
        ("USER_PROMPT", "ENV_ENTRY"),
        ("ENV_ENTRY", "ENV_DB"),
        ("ENV_DB", "SOURCE_TRUTH"),
        ("SOURCE_TRUTH", "MODE_GATES"),
        ("MODE_GATES", "UOP_ROUTE"),
        ("UOP_ROUTE", "PROJECT_ROUTE"),
        ("PROJECT_ROUTE", "ENV_RELOCK"),
        ("ENV_RELOCK", "ENV_EXIT"),
    ),
    "UOP": (
        ("USER_PROMPT", "UOP_ENTRY"),
        ("UOP_ENTRY", "UOP_DB"),
        ("UOP_DB", "OPERATOR_REGISTRY"),
        ("OPERATOR_REGISTRY", "HUMAN_GATE"),
        ("HUMAN_GATE", "PROJECT_ROUTE"),
        ("PROJECT_ROUTE", "UOP_RECEIPT"),
        ("UOP_RECEIPT", "UOP_EXIT"),
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _label(value: Any, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = text.replace('"', "'").replace("[", "(").replace("]", ")").replace("|", "/")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _table_facts(database: Path, selected: tuple[str, ...] = ()) -> dict[str, Any]:
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        populated = []
        for table in tables:
            try:
                count = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.DatabaseError:
                count = -1
            if count:
                populated.append((table, count))
        selected_counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in selected if table in tables
        }
        return {
            "table_count": len(tables),
            "populated": populated,
            "selected": selected_counts,
            "database_sha256": _sha256(database),
            "database_size_bytes": database.stat().st_size,
        }
    finally:
        connection.close()


def _header(direction: str = "LR") -> list[str]:
    if direction not in {"LR", "TD"}:
        raise Env15TopologyError(f"UNSUPPORTED_FLOW_DIRECTION:{direction}")
    return [
        f"flowchart {direction}",
        "  classDef root fill:#111827,stroke:#111827,color:#fff;",
        "  classDef law fill:#f3e8ff,stroke:#6d28d9,color:#111;",
        "  classDef db fill:#dcfce7,stroke:#166534,color:#111;",
        "  classDef gate fill:#ffedd5,stroke:#c2410c,color:#111;",
        "  classDef receipt fill:#e0f2fe,stroke:#0369a1,color:#111;",
        "  classDef output fill:#fef3c7,stroke:#92400e,color:#111;",
    ]


def _supplied_mmd_with_live_project_amendment(root: Path, topology_id: str) -> str:
    member = "env/env_mmd.mmd" if topology_id == "ENV" else "uop/uop_mmd.mmd"
    source_bytes = read_chatgpt_gemini_authority_member(member)
    source = source_bytes.decode("utf-8-sig").rstrip() + "\n"
    if topology_id == "ENV":
        # These are the only two obsolete Project-lock labels in the supplied
        # Env MMD. Preserve the supplied graph and every other additive delta,
        # then correct the Project classification in place.
        source = source.replace(
            "E15BASE[Env + UOP + project base open mode=ro&immutable=1]",
            "E15BASE[Env + UOP exact locked authority + Project mutable sector live router]",
        ).replace(
            "E15OTHER[all other 13 project sectors preserved read-only by default]",
            "E15OTHER[all other 13 Project sectors mutable only by named one-turn grant]",
        )
        database = root / "env" / "env_sqlite.sqlite"
        amendment = [
            "%% EVIDENCE LANE ADDITIVE PROJECT MUTABLE SECTOR CORRECTION",
            "  subgraph EL_PROJECT_MUTABLE_AUTHORITY[PROJECT MUTABLE SECTOR / LIVE ROUTER]",
            '    USER_PROMPT["user prompt / exact source request"]',
            '    ENV_ENTRY["Env15 supplied MMD base / Env locked authority"]',
            f'    ENV_DB["env_sqlite.sqlite / exact source sha256={_sha256(database).upper()}"]',
            '    SOURCE_TRUTH["supplied Env15 package + current registered sources"]',
            '    MODE_GATES["mode / source / HIL gates"]',
            '    UOP_ROUTE["UOP locked governance authority"]',
            '    PROJECT_ROUTE["PROJECT_MUTABLE_SECTOR / live governed sector graph"]',
            '    ENV_RELOCK["named mutation receipt + validation + relock"]',
            '    ENV_EXIT["candidate only / explicit human HIL required"]',
            "    USER_PROMPT --> ENV_ENTRY --> ENV_DB --> SOURCE_TRUTH --> MODE_GATES",
            "    MODE_GATES --> UOP_ROUTE --> PROJECT_ROUTE --> ENV_RELOCK --> ENV_EXIT",
            "  end",
        ]
    else:
        database = root / "uop" / "uop_sqlite.sqlite"
        amendment = [
            "%% EVIDENCE LANE ADDITIVE PROJECT MUTABLE SECTOR CORRECTION",
            "  subgraph EL_UOP_PROJECT_MUTABLE_AUTHORITY[UOP TO PROJECT MUTABLE SECTOR]",
            '    USER_PROMPT["user prompt after Env entry"]',
            '    UOP_ENTRY["UOP supplied MMD base / locked governance authority"]',
            f'    UOP_DB["uop_sqlite.sqlite / exact source sha256={_sha256(database).upper()}"]',
            '    OPERATOR_REGISTRY["public operator registry"]',
            '    HUMAN_GATE["human gate / no autonomous promotion"]',
            '    PROJECT_ROUTE["PROJECT_MUTABLE_SECTOR / named governed lane"]',
            '    UOP_RECEIPT["operator activation + gate receipt"]',
            '    UOP_EXIT["return governed candidate to Env"]',
            "    USER_PROMPT --> UOP_ENTRY --> UOP_DB --> OPERATOR_REGISTRY --> HUMAN_GATE",
            "    HUMAN_GATE --> PROJECT_ROUTE --> UOP_RECEIPT --> UOP_EXIT",
            "  end",
        ]
    source_sha = hashlib.sha256(source_bytes).hexdigest().upper()
    return source + f"%% SUPPLIED_{topology_id}_MMD_BASE_SHA256={source_sha}\n" + "\n".join(amendment) + "\n"


def _env_mmd(root: Path) -> str:
    return _supplied_mmd_with_live_project_amendment(root, "ENV")


def _uop_mmd(root: Path) -> str:
    return _supplied_mmd_with_live_project_amendment(root, "UOP")


def _derived_authority_law(root: Path, topology_id: str) -> str:
    member_by_id = {
        "ENV": "env/env_sqlite.sqlite",
        "UOP": "uop/uop_sqlite.sqlite",
    }
    member = member_by_id[topology_id]
    expected = {
        name: (size, digest)
        for name, size, digest in supplied_env_uop_database_rows()
    }[member]
    database = root / Path(member)
    facts = _table_facts(database)
    title = "Environment" if topology_id == "ENV" else "Universal Operator Profile"
    other = "UOP" if topology_id == "ENV" else "Env"
    source_law_member = "env/env_law.md" if topology_id == "ENV" else "uop/uop_law.md"
    source_law_bytes = read_chatgpt_gemini_authority_member(source_law_member)
    source_law = source_law_bytes.decode("utf-8-sig").rstrip() + "\n"
    amendment = (
        f"\n# Evidence Lane {title} live Project mutable-sector amendment\n\n"
        f"SUPPLIED_LAW_BASE_SHA256={hashlib.sha256(source_law_bytes).hexdigest().upper()}\n"
        f"SOURCE_ARCHIVE_SHA256={LOCKED_READ_ARCHIVE_SHA256}\n"
        f"SOURCE_MEMBER={member}\n"
        f"SOURCE_MEMBER_SHA256={expected[1]}\n"
        f"SOURCE_MEMBER_BYTES={expected[0]}\n"
        f"INSTALLED_SQLITE_SHA256={facts['database_sha256'].upper()}\n"
        f"INSTALLED_SQLITE_BYTES={facts['database_size_bytes']}\n"
        f"SQLITE_TABLE_COUNT={facts['table_count']}\n\n"
        f"- The exact supplied {topology_id} SQLite bytes are the locked read-only authority.\n"
        f"- This law, the {topology_id} MMD, DOT, and root pointer are deterministic projections of that SQLite authority and the live governed Project router.\n"
        "- Project authority is the current governed sector graph; there is no separate locked Project container.\n"
        f"- {topology_id} cannot override {other} or Project truth.\n"
        "- Chat Lineage and Research are append-only services; every other Project lane requires an explicit named one-turn HIL grant, validation receipt, and relock.\n"
        "- Receipts and successful validation never promote a candidate; explicit human HIL remains required.\n"
        "- Hidden chain-of-thought is not stored. Only visible outputs, source identities, hashes, tool results, and receipts may be retained.\n"
    )
    return source_law + amendment


def _derived_dot(root: Path, topology_id: str) -> str:
    member = "env/env_mmd.dot" if topology_id == "ENV" else "uop/uop_mmd.dot"
    source_bytes = read_chatgpt_gemini_authority_member(member)
    source = source_bytes.decode("utf-8-sig").rstrip()
    if topology_id == "ENV":
        source = source.replace(
            'E15BASE [label="Env + UOP + project base open mode=ro&immutable=1"];',
            'E15BASE [label="Env + UOP exact locked authority + Project mutable sector live router"];',
        ).replace(
            'E15OTHER [label="all other 13 project sectors preserved read-only by default"];',
            'E15OTHER [label="all other 13 Project sectors mutable only by named one-turn grant"];',
        )
    closing = source.rfind("}")
    if closing < 0:
        raise Env15TopologyError(f"{topology_id}_SUPPLIED_DOT_ROOT_CLOSE_MISSING")
    database = root / (
        "env/env_sqlite.sqlite" if topology_id == "ENV" else "uop/uop_sqlite.sqlite"
    )
    amendment = [
        "",
        "// EVIDENCE LANE ADDITIVE PROJECT MUTABLE SECTOR CORRECTION",
        f'EL_{topology_id}_AUTHORITY [label="{topology_id} exact supplied authority\\nsha256={_sha256(database).upper()}"];',
    ]
    amendment.extend(
        f'{node} [label="{node}"];' for node in REQUIRED_NODES[topology_id]
    )
    amendment.append(f"EL_{topology_id}_AUTHORITY -> USER_PROMPT;")
    amendment.extend(
        f"{source_node} -> {target_node};"
        for source_node, target_node in DERIVED_TOPOLOGY_EDGES[topology_id]
    )
    corrected = source[:closing].rstrip() + "\n" + "\n".join(amendment) + "\n}\n"
    return corrected


def _write_derived_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
    encoded = text.encode("utf-8")
    if not path.is_file() or path.read_bytes() != encoded:
        path.write_bytes(encoded)
    path.chmod(path.stat().st_mode & ~stat.S_IWRITE)


def _code_graph_lines(root: Path, sector_rows: list[tuple[str, str]]) -> list[str]:
    """Render complete counts and proof with a bounded representative graph.

    V5.9 correctly treated SQLite as the complete project truth and rendered a
    compact sample from it.  The superseded implementation expanded every file,
    symbol, import, and route into Mermaid, producing an unreadable megagraph.
    This implementation still validates every governed file and reports all
    canonical/enhanced row totals, while limiting display nodes to twelve files
    and two related objects of each kind per displayed file.
    """

    lines = [
        "  subgraph CODE_GRAPH[complete code files, symbols, imports, assets, and routes]",
        "    direction TB",
    ]
    totals = {
        "source_files": 0,
        "symbols": 0,
        "imports": 0,
        "routes": 0,
        "text_files": 0,
        "binary_metadata_files": 0,
        "exact_bytes": 0,
        "coverage_errors": 0,
        "canonical_source_files": 0,
        "canonical_code_files": 0,
        "canonical_chunks": 0,
        "canonical_symbols": 0,
        "canonical_imports": 0,
        "canonical_routes": 0,
        "canonical_dependencies": 0,
        "canonical_artifacts": 0,
        "canonical_git_commits": 0,
    }
    code_sector_count = 0
    for sector_index, (sector_id, relative) in enumerate(sector_rows):
        database = root / Path(relative)
        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if "code_file_snapshot" not in tables:
                continue
            code_sector_count += 1
            columns = {row[1] for row in connection.execute("PRAGMA table_info(code_file_snapshot)")}
            content_expression = "content_kind" if "content_kind" in columns else "'TEXT_INDEXED'"
            files = connection.execute(
                "SELECT file_id,source_id,relative_path,language,size_bytes,sha256,"
                f"{content_expression} FROM code_file_snapshot ORDER BY source_id,relative_path,file_id"
            ).fetchall()
            symbols = connection.execute(
                "SELECT symbol_id,file_id,symbol_kind,qualified_name FROM code_symbol ORDER BY file_id,start_line,symbol_id"
            ).fetchall() if "code_symbol" in tables else []
            imports = connection.execute(
                "SELECT import_id,file_id,imported_module,imported_name,line_number FROM code_import "
                "ORDER BY file_id,line_number,import_id"
            ).fetchall() if "code_import" in tables else []
            routes = connection.execute(
                "SELECT boundary_id,file_id,route_or_api FROM code_route_api_boundary ORDER BY file_id,route_or_api,boundary_id"
            ).fetchall() if "code_route_api_boundary" in tables else []
            canonical_table_names = {
                "source_file": "canonical_source_files",
                "code_file": "canonical_code_files",
                "code_chunk": "canonical_chunks",
                "code_symbol": "canonical_symbols",
                "code_import_edge": "canonical_imports",
                "app_route": "canonical_routes",
                "dependency_item": "canonical_dependencies",
                "project_artifact": "canonical_artifacts",
                "git_commit": "canonical_git_commits",
            }
            canonical_counts = {
                total_name: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table, total_name in canonical_table_names.items()
                if table in tables
            }
            coverage = {
                str(file_id): (str(status), int(byte_count), str(sha256))
                for file_id, status, byte_count, sha256 in connection.execute(
                    "SELECT file_id,coverage_status,byte_count,sha256 FROM source_byte_coverage"
                )
            } if "source_byte_coverage" in tables else {}
            exact = {
                str(file_id): (int(byte_count or 0), int(chunk_count or 0))
                for file_id, byte_count, chunk_count in connection.execute(
                    "SELECT file_id,SUM(byte_end_exclusive-byte_start),COUNT(*) "
                    "FROM code_exact_byte_chunk GROUP BY file_id"
                )
            } if "code_exact_byte_chunk" in tables else {}
        finally:
            connection.close()
        if not files:
            continue
        sector_node = f"CODE_SECTOR_{sector_index:02d}"
        lines.append(
            f'    {sector_node}["{_label(sector_id)} / SQLite complete truth / files={len(files)}"]:::db'
        )
        file_nodes: dict[str, str] = {}
        file_facts: dict[str, tuple[Any, ...]] = {}
        for file_id, _source_id, relative_path, language, size_bytes, sha256, content_kind in files:
            file_key = str(file_id)
            file_facts[file_key] = (
                file_id, _source_id, relative_path, language, size_bytes, sha256, content_kind
            )
            kind = str(content_kind or "TEXT_INDEXED")
            expected_size = int(size_bytes or 0)
            proof = coverage.get(file_key)
            coverage_ok = bool(proof and proof[1] == expected_size and proof[2] == str(sha256))
            exact_size, exact_chunks = exact.get(file_key, (0, 0))
            if kind == "TEXT_INDEXED":
                totals["text_files"] += 1
                reconstruction_ok = exact_chunks > 0 and exact_size == expected_size
                totals["exact_bytes"] += exact_size
            else:
                totals["binary_metadata_files"] += 1
                reconstruction_ok = exact_chunks == 0
            if not (coverage_ok and reconstruction_ok):
                totals["coverage_errors"] += 1
        priority_file_ids: list[str] = []
        for row in (*routes, *symbols, *imports):
            candidate = str(row[1])
            if candidate in file_facts and candidate not in priority_file_ids:
                priority_file_ids.append(candidate)
        for row in files:
            candidate = str(row[0])
            if candidate not in priority_file_ids:
                priority_file_ids.append(candidate)
        selected_file_ids = priority_file_ids[:12]
        previous_file_node = sector_node
        for file_index, file_key in enumerate(selected_file_ids):
            file_id, _source_id, relative_path, language, size_bytes, sha256, content_kind = file_facts[file_key]
            node = f"CODE_FILE_{sector_index:02d}_{file_index:02d}"
            file_nodes[file_key] = node
            kind = str(content_kind or "TEXT_INDEXED")
            expected_size = int(size_bytes or 0)
            proof = coverage.get(file_key)
            exact_size, exact_chunks = exact.get(file_key, (0, 0))
            coverage_ok = bool(proof and proof[1] == expected_size and proof[2] == str(sha256))
            reconstruction_ok = (
                exact_chunks > 0 and exact_size == expected_size
                if kind == "TEXT_INDEXED"
                else exact_chunks == 0
            )
            proof_state = "PASS" if coverage_ok and reconstruction_ok else "FAIL"
            lines += [
                (
                    f'    {node}["{_label(relative_path, 150)} / {_label(language, 30)} / '
                    f'{_label(kind, 32)} / bytes={expected_size} / proof={proof_state}"]:::output'
                ),
                f"    {previous_file_node} --> {node}",
            ]
            previous_file_node = node
        related_counts: dict[tuple[str, str], int] = {}
        for symbol_index, (_symbol_id, file_id, symbol_type, symbol_name) in enumerate(symbols):
            parent = file_nodes.get(str(file_id))
            key = (str(file_id), "symbol")
            if not parent or related_counts.get(key, 0) >= 2:
                continue
            related_counts[key] = related_counts.get(key, 0) + 1
            node = f"CODE_SYMBOL_{sector_index:02d}_{symbol_index:04d}"
            lines += [
                f'    {node}["{_label(symbol_name, 90)} / {_label(symbol_type, 40)}"]:::law',
                f"    {parent} --> {node}",
            ]
        for import_index, (_edge_id, file_id, target, import_type, line_number) in enumerate(imports):
            parent = file_nodes.get(str(file_id))
            key = (str(file_id), "import")
            if not parent or related_counts.get(key, 0) >= 2:
                continue
            related_counts[key] = related_counts.get(key, 0) + 1
            node = f"CODE_IMPORT_{sector_index:02d}_{import_index:04d}"
            lines += [
                f'    {node}["{_label(target, 110)} / {_label(import_type, 45)} / line={int(line_number or 0)}"]:::gate',
                f"    {parent} --> {node}",
            ]
        for route_index, (_route_id, file_id, route_path) in enumerate(routes):
            parent = file_nodes.get(str(file_id))
            key = (str(file_id), "route")
            if not parent or related_counts.get(key, 0) >= 2:
                continue
            related_counts[key] = related_counts.get(key, 0) + 1
            node = f"CODE_ROUTE_{sector_index:02d}_{route_index:04d}"
            lines += [
                f'    {node}["{_label(route_path, 120)}"]:::receipt',
                f"    {parent} --> {node}",
            ]
        totals["source_files"] += len(files)
        totals["symbols"] += len(symbols)
        totals["imports"] += len(imports)
        totals["routes"] += len(routes)
        for name, count in canonical_counts.items():
            totals[name] += count
    if not totals["source_files"]:
        # Mermaid's ELK/Dagre handoff can stall indefinitely on an empty
        # subgraph. Keep the zero-file state explicit and renderable while the
        # SQLite counts remain the complete authority.
        lines.append(
            '    CODE_GRAPH_EMPTY["No code files supplied / SQLite counts remain authoritative"]:::receipt'
        )
    scope = "COMPLETE" if totals["source_files"] and totals["coverage_errors"] == 0 else "INCOMPLETE"
    lines += [
        "  end",
        (
            f'  CODE_TOPOLOGY_COVERAGE["scope={scope} / code_sectors={code_sector_count} / '
            f'source_files={totals["source_files"]} / text_files={totals["text_files"]} / '
            f'binary_metadata_files={totals["binary_metadata_files"]} / exact_bytes={totals["exact_bytes"]} / '
            f'symbols={totals["symbols"]} / imports={totals["imports"]} / routes={totals["routes"]} / '
            f'coverage_errors={totals["coverage_errors"]} / rendered_file_sample<=12"]:::receipt'
        ),
        (
            f'  V59_CANONICAL_TOTALS["V5.9 canonical compatibility / '
            f'source_file={totals["canonical_source_files"]} / code_file={totals["canonical_code_files"]} / '
            f'code_chunk={totals["canonical_chunks"]} / code_symbol={totals["canonical_symbols"]} / '
            f'code_import_edge={totals["canonical_imports"]} / app_route={totals["canonical_routes"]} / '
            f'dependency_item={totals["canonical_dependencies"]} / project_artifact={totals["canonical_artifacts"]} / '
            f'git_commit={totals["canonical_git_commits"]}"]:::receipt'
        ),
        "  SECTOR_REGISTRY --> CODE_TOPOLOGY_COVERAGE",
        "  CODE_TOPOLOGY_COVERAGE --> V59_CANONICAL_TOTALS",
    ]
    if totals["source_files"]:
        lines.append("  CODE_TOPOLOGY_COVERAGE --> CODE_GRAPH")
    return lines


def _project_mmd(root: Path) -> str:
    project = root / "project"
    router_facts = _table_facts(project / "project_router.sqlite", ("sector_registry", "sector_mutation_grant", "sector_mutation_receipt", "brain_snapshot_registry"))
    # The project master follows the readable V5.9 hierarchy: the full facts
    # remain in SQLite, while the representative render flows top-to-bottom.
    # A left-to-right root made the two code sectors and their related nodes
    # collapse into an extremely wide, visually unusable strip.
    lines = _header("TD") + [
        '  USER_PROMPT["user prompt / selected intake lane"]:::root',
        '  PROJECT_ENTRY["Env/UOP-governed live Project sector state"]:::law',
        f'  PROJECT_ROUTER["project_router.sqlite / tables={router_facts["table_count"]}"]:::db',
        '  SECTOR_REGISTRY["14 physical sectors / 18 logical intake lanes"]:::db',
        "  USER_PROMPT --> PROJECT_ENTRY --> PROJECT_ROUTER --> SECTOR_REGISTRY",
        "  subgraph SECTORS[real sector databases and current row counts]",
        "    direction TB",
    ]
    previous = "SECTOR_REGISTRY"
    router = sqlite3.connect(project / "project_router.sqlite")
    try:
        sector_rows = router.execute("SELECT sector_id,sqlite_path FROM sector_registry ORDER BY sector_id").fetchall()
    finally:
        router.close()
    for index, (sector_id, relative) in enumerate(sector_rows):
        database = root / Path(relative)
        facts = _table_facts(database)
        row_total = sum(count for _, count in facts["populated"] if count > 0)
        node = f"SECTOR_{index:02d}"
        lines += [
            f'    {node}["{_label(sector_id)} / tables={facts["table_count"]} / populated_rows={row_total}"]:::db',
            f"    {previous} --> {node}",
        ]
        previous = node
    lines += [
        "  end",
    ]
    lines += _code_graph_lines(root, [(str(sector_id), str(relative)) for sector_id, relative in sector_rows])
    lines += [
        '  MUTATION_GRANT["named one-turn grant / Chat Lineage + Research append services"]:::gate',
        '  PRIOR_SNAPSHOT["prior immutable snapshot + file hash"]:::receipt',
        '  SECTOR_WRITE["one transaction in exact named sector"]:::gate',
        '  DUAL_RECEIPTS["sector logical-state receipt + router byte-hash receipt"]:::receipt',
        '  RELOCK["close writer / read-only reopen / integrity + FK validation"]:::gate',
        '  RESULT_SNAPSHOT["resulting immutable snapshot / active head"]:::receipt',
        '  MMD_RENDER["prompt-rooted Env + UOP + Project MMD / SVG / PNG"]:::output',
        '  PACKAGE_VALIDATE["ChatGPT + Gemini provider-readable hash validation"]:::output',
        '  PROJECT_EXIT["registered brain / continuation and rollback pointers"]:::output',
        f"  {previous} --> MUTATION_GRANT --> PRIOR_SNAPSHOT --> SECTOR_WRITE --> DUAL_RECEIPTS",
        "  DUAL_RECEIPTS --> RELOCK --> RESULT_SNAPSHOT --> MMD_RENDER --> PACKAGE_VALIDATE --> PROJECT_EXIT",
    ]
    return "\n".join(lines) + "\n"


def _validate_text(topology_id: str, text: str) -> tuple[int, int]:
    missing = [node for node in REQUIRED_NODES[topology_id] if not re.search(rf"^\s*{re.escape(node)}(?:\[|\()", text, flags=re.MULTILINE)]
    if missing:
        raise Env15TopologyError(f"{topology_id}_MMD_REQUIRED_NODES_MISSING:{','.join(missing)}")
    node_count = len(re.findall(r"^\s*[A-Z][A-Z0-9_]*\s*(?:\[|\()", text, flags=re.MULTILINE))
    edge_count = text.count("-->")
    # The user-supplied Env15 MMD authorities are top-down. Preserve that base
    # direction; only the generated live Project master is independently TD.
    expected_direction = "TD"
    if not text.startswith(f"flowchart {expected_direction}") or edge_count == 0:
        raise Env15TopologyError(f"{topology_id}_MMD_FLOW_INVALID")
    return node_count, edge_count


def _topology_relationships(root: Path) -> dict[str, Any]:
    router = sqlite3.connect(root / "project" / "project_router.sqlite")
    try:
        sectors = [
            {"sector_id": row[0], "sqlite_path": row[1]}
            for row in router.execute("SELECT sector_id,sqlite_path FROM sector_registry ORDER BY sector_id")
        ]
    finally:
        router.close()
    return {
        "ENV": {"pointer": ".uepc_env", "sqlite": "env/env_sqlite.sqlite", "routes_to": ["UOP", "PROJECT"]},
        "UOP": {"pointer": ".uepc_profile", "sqlite": "uop/uop_sqlite.sqlite", "routes_to": ["PROJECT"]},
        "PROJECT": {"pointer": ".uepc_project", "sqlite": "project/project_router.sqlite", "registered_sectors": sectors},
    }


def _write_correction_contract(root: Path) -> Path:
    path = root / "receipts" / "ENV15_MMD_CORRECTION_CONTRACT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Env15 MMD correction contract\n\n"
        "1. Preserve the exact supplied Env and UOP SQLite bytes as the only locked read authorities.\n"
        "2. Use the exact supplied Env/UOP law and MMD bytes as derivation bases; append only the bounded live Project mutable-sector correction.\n"
        "3. Correct DOT and pointers so PROJECT_MUTABLE_SECTOR is the only Project authority classification.\n"
        "4. Read Project facts from the live router and governed sector SQLite databases; no separate locked Project container exists.\n"
        "5. Generate project/topology/project_master_topology.mmd from the complete live Project state.\n"
        "6. Validate complete SQLite coverage while rendering a bounded V5.9-style top-down representative graph.\n"
        "7. Render and validate derived Env/UOP SVG and PNG plus generated Project SVG, normal PNG, and HD-PNG.\n"
        "8. Hash every database and derived artifact and bind them in the supplied-authority provenance receipt.\n"
        "9. Resolve Project truth directly through the live router, canonical lane registry, lineage head, and sector databases.\n",
        encoding="utf-8",
    )
    return path


def _derived_topology_artifact(topology_id: str, mmd_path: Path) -> TopologyArtifact:
    text = mmd_path.read_text(encoding="utf-8")
    node_count, edge_count = _validate_text(topology_id, text)
    return TopologyArtifact(
        topology_id=topology_id,
        mmd_path=str(mmd_path),
        mmd_sha256=_sha256(mmd_path),
        required_nodes=REQUIRED_NODES[topology_id],
        node_count=node_count,
        edge_count=edge_count,
        semantic_status=f"SQLITE_DERIVED_{topology_id}_AUTHORITY_PASS",
    )


def generate_env15_topologies(brain_root: str | Path) -> dict[str, TopologyArtifact]:
    root = Path(brain_root).resolve()
    verification = verify_env15_live_project(root)
    if not verification.passed:
        raise Env15TopologyError("ENV15_PROJECT_NOT_VALID:" + ";".join(verification.errors))
    if not (root / "project" / "lineage" / "LINEAGE_HEAD.json").is_file():
        from sqlite_brain_builder.runtime.universal_lane_authority import (
            write_universal_lane_authority,
        )

        write_universal_lane_authority(root, brain_name=root.name)
    for topology_id, directory, law_name, mmd_name, dot_name in (
        ("ENV", "env", "env_law.md", "env_mmd.mmd", "env_mmd.dot"),
        ("UOP", "uop", "uop_law.md", "uop_mmd.mmd", "uop_mmd.dot"),
    ):
        _write_derived_text(
            root / directory / law_name,
            _derived_authority_law(root, topology_id),
        )
        _write_derived_text(
            root / directory / mmd_name,
            _env_mmd(root) if topology_id == "ENV" else _uop_mmd(root),
        )
        _write_derived_text(
            root / directory / dot_name,
            _derived_dot(root, topology_id),
        )
    project_path = root / "project" / "topology" / "project_master_topology.mmd"
    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_text = _project_mmd(root)
    node_count, edge_count = _validate_text("PROJECT", project_text)
    project_path.write_text(project_text, encoding="utf-8")
    result: dict[str, TopologyArtifact] = {
        "ENV": _derived_topology_artifact("ENV", root / "env" / "env_mmd.mmd"),
        "UOP": _derived_topology_artifact("UOP", root / "uop" / "uop_mmd.mmd"),
        "PROJECT": TopologyArtifact(
            "PROJECT",
            str(project_path),
            _sha256(project_path),
            REQUIRED_NODES["PROJECT"],
            node_count,
            edge_count,
            "SQLITE_COMPLETE_BOUNDED_PROJECT_MASTER_PASS",
        ),
    }
    correction_contract = _write_correction_contract(root)
    relationships = _topology_relationships(root)
    provenance = write_supplied_env_uop_provenance_receipt(
        root,
        require_complete_derivation=True,
    )
    if provenance["status"] != "PASS":
        raise Env15TopologyError(
            "ENV_UOP_DERIVATION_PROVENANCE_FAILED:"
            + ";".join(provenance["errors"])
        )
    receipt = {
        "contract": "EVIDENCE_LANE_ENV_UOP_SQLITE_DERIVED_LIVE_PROJECT_TOPOLOGY_V1",
        "generation_status": "ENV_UOP_SQLITE_DERIVATION_AND_PROJECT_MMD_COMPLETE_RENDER_NOT_STARTED",
        "supplied_mmd_base_sha256": {
            "ENV": hashlib.sha256(
                read_chatgpt_gemini_authority_member("env/env_mmd.mmd")
            ).hexdigest().upper(),
            "UOP": hashlib.sha256(
                read_chatgpt_gemini_authority_member("uop/uop_mmd.mmd")
            ).hexdigest().upper(),
        },
        "mmd_derivation_law": (
            "EXACT_SUPPLIED_BASE_PLUS_BOUNDED_PROJECT_MUTABLE_SECTOR_CORRECTION"
        ),
        "correction_contract_path": str(correction_contract),
        "correction_contract_sha256": _sha256(correction_contract),
        "supplied_authority_provenance": provenance,
        "relationships": relationships,
        "topologies": {
        key: {
            "mmd_path": value.mmd_path,
            "mmd_sha256": value.mmd_sha256,
            "required_nodes": value.required_nodes,
            "node_count": value.node_count,
            "edge_count": value.edge_count,
            "semantic_status": value.semantic_status,
            "render_status": value.render_status,
        }
        for key, value in result.items()
        },
    }
    receipt_path = root / "receipts" / "ENV15_PROMPT_ROOTED_MMD_RECEIPT.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def render_env15_topologies(brain_root: str | Path) -> dict[str, TopologyArtifact]:
    generated = generate_env15_topologies(brain_root)
    renderer = shutil.which("mmdc.cmd") or shutil.which("mmdc.exe") or shutil.which("mmdc")
    if not renderer:
        raise Env15TopologyError("ENV15_MMD_RENDERER_NOT_FOUND")
    result: dict[str, TopologyArtifact] = {}
    for topology_id in ("ENV", "UOP"):
        artifact = generated[topology_id]
        mmd = Path(artifact.mmd_path)
        svg = mmd.with_suffix(".svg")
        png = mmd.with_suffix(".png")
        for output, dimensions in ((svg, None), (png, (1920, 1080))):
            command = [renderer, "-i", str(mmd), "-o", str(output), "-b", "white"]
            if dimensions:
                command += ["-w", str(dimensions[0]), "-H", str(dimensions[1])]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                raise Env15TopologyError(
                    f"{topology_id}_RENDER_FAILED:{completed.stderr[-1000:]}"
                )
        if "<svg" not in svg.read_text(encoding="utf-8", errors="replace").casefold():
            raise Env15TopologyError(f"{topology_id}_SVG_BYTE_VALIDATION_FAILED")
        if png.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise Env15TopologyError(f"{topology_id}_PNG_BYTE_VALIDATION_FAILED")
        result[topology_id] = TopologyArtifact(
            **{
                **artifact.__dict__,
                "svg_path": str(svg),
                "svg_sha256": _sha256(svg),
                "png_path": str(png),
                "png_sha256": _sha256(png),
                "render_status": f"SQLITE_DERIVED_{topology_id}_MMD_TO_SVG_TO_PNG_PASS",
            }
        )

    project = generated["PROJECT"]
    mmd = Path(project.mmd_path)
    svg = mmd.with_suffix(".svg")
    png = mmd.with_suffix(".png")
    hd_png = mmd.with_name(mmd.stem + "_HD.png")
    for output, dimensions in ((svg, None), (png, (1920, 1080)), (hd_png, (7680, 4320))):
        command = [renderer, "-i", str(mmd), "-o", str(output), "-b", "white"]
        if dimensions:
            command += ["-w", str(dimensions[0]), "-H", str(dimensions[1])]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise Env15TopologyError(f"PROJECT_RENDER_FAILED:{completed.stderr[-1000:]}")
    if "<svg" not in svg.read_text(encoding="utf-8", errors="replace").casefold():
        raise Env15TopologyError("PROJECT_SVG_BYTE_VALIDATION_FAILED")
    for output in (png, hd_png):
        if output.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise Env15TopologyError(f"PROJECT_PNG_BYTE_VALIDATION_FAILED:{output.name}")
    result["PROJECT"] = TopologyArtifact(
        **{
            **project.__dict__,
            "svg_path": str(svg),
            "svg_sha256": _sha256(svg),
            "png_path": str(png),
            "png_sha256": _sha256(png),
            "hd_png_path": str(hd_png),
            "hd_png_sha256": _sha256(hd_png),
            "render_status": "MMD_TO_SVG_TO_PNG_TO_HD_PNG_PASS",
        }
    )
    relationships = _topology_relationships(Path(brain_root).resolve())
    manifest = {
        "contract": "ENV15_SQLITE_BACKED_TOPOLOGY_RENDER_V1",
        "status": "PASS",
        "relationships": relationships,
        "topologies": {
            key: {
                "mmd_path": item.mmd_path, "mmd_sha256": item.mmd_sha256,
                "svg_path": item.svg_path, "svg_sha256": item.svg_sha256,
                "png_path": item.png_path, "png_sha256": item.png_sha256,
                "hd_png_path": item.hd_png_path, "hd_png_sha256": item.hd_png_sha256,
                "render_status": item.render_status,
            }
            for key, item in result.items()
        },
    }
    manifest_path = Path(brain_root).resolve() / "receipts" / "ENV15_TOPOLOGY_RENDER_MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


__all__ = [
    "Env15TopologyError",
    "REQUIRED_NODES",
    "TopologyArtifact",
    "generate_env15_topologies",
    "render_env15_topologies",
]
