from __future__ import annotations
import sqlite3
from pathlib import Path
import importlib.resources as resources


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def apply_schema(con: sqlite3.Connection, package: str, name: str) -> None:
    sql = resources.files(package).joinpath(name).read_text(encoding="utf-8")
    con.executescript(sql)
    con.commit()


def integrity_ok(path: str | Path) -> bool:
    con = sqlite3.connect(path)
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
    finally:
        con.close()
