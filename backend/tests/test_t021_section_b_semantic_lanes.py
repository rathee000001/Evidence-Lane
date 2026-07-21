from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlite_brain_builder.runtime.env15_project_schema import resolve_env15_sector
from sqlite_brain_builder.runtime.stable_runtime_v53 import build_brain


SEMANTIC_FIXTURES = {
    "discussion": (
        "Decision: Keep the governed router\nDelta: Add stable workflow edges\nGate: No package without validation\n",
        ("discussion_decision", "discussion_delta", "discussion_hard_gate"),
    ),
    "analysis": (
        "Claim: The index is deterministic\nEvidence: Identical replay adds zero rows\nRisk: Files may change during intake\nAlternative: Snapshot before mutation\n",
        ("analysis_supporting_evidence", "analysis_risk", "analysis_alternative"),
    ),
    "plan": (
        "Phase: Backend repair\nMilestone: All lane tests pass\nDependency: Env15 schema\nStatus: planned -> complete\n",
        ("plan_phase", "plan_milestone", "plan_dependency", "plan_status"),
    ),
    "mode": (
        "Scope: Project sectors only\nGate: Validate before relock\nAllow: Deterministic indexing\nBlock: Hidden mutation\nSupersede: Env14 -> Env15\n",
        ("mode_scope", "mode_gate", "mode_allowed_action", "mode_blocked_action", "mode_supersede_ledger"),
    ),
}


def _counts(database: Path, tables: tuple[str, ...]) -> dict[str, int]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in (*tables, "mutation_receipt")
        }
    finally:
        connection.close()


def test_discussion_analysis_plan_and_mode_have_structured_idempotent_projections(tmp_path: Path) -> None:
    sources = [
        {
            "source_id": f"source_{lane}",
            "lane_key": lane,
            "display_name": f"{lane} fixture",
            "text": text,
            "active": True,
        }
        for lane, (text, _tables) in SEMANTIC_FIXTURES.items()
    ]
    first = build_brain(str(tmp_path), "Semantic Lane Brain", sources, generate_mmd=False)
    _, database = resolve_env15_sector(first["brain_root"], "discussion")
    all_tables = tuple(table for _text, tables in SEMANTIC_FIXTURES.values() for table in tables)
    before = _counts(database, all_tables)
    second = build_brain(str(tmp_path), "Semantic Lane Brain", sources, generate_mmd=False)
    after = _counts(database, all_tables)

    assert before == after
    assert second["incremental"]["all_sources_unchanged"] is True
    assert before["mutation_receipt"] == 4
    assert all(before[table] >= 1 for table in all_tables)

    connection = sqlite3.connect(database)
    try:
        evidence = connection.execute(
            "SELECT statement,relation_target,state FROM analysis_supporting_evidence"
        ).fetchone()
        status = connection.execute("SELECT statement,relation_target,state FROM plan_status").fetchone()
        supersession = connection.execute(
            "SELECT statement,relation_target,state FROM mode_supersede_ledger"
        ).fetchone()
    finally:
        connection.close()
    assert evidence == ("Identical replay adds zero rows", "The index is deterministic", "SUPPORTS_CLAIM")
    assert status == ("planned -> complete", "complete", "STATUS_TRANSITION")
    assert supersession == ("Env14 -> Env15", "Env14", "SUPERSEDES")
