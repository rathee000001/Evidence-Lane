from __future__ import annotations
from pathlib import Path
from sqlite_brain_builder.core import uid, now, slugify
from sqlite_brain_builder.storage.sqlite_utils import connect, apply_schema
from sqlite_brain_builder.workspace.account import hash_password
from sqlite_brain_builder.runtime.path_policy import normalize_workspace_dir, brain_output_dir, workspace_db_path


def init_workspace(workspace_dir: str | Path, username: str = "local_user", password: str = "change-me") -> Path:
    root = normalize_workspace_dir(workspace_dir)

    # Workspace support folders stay at selected root.
    # Do NOT create root\brains; brain folders are direct children of root.
    for name in ["packages", "receipts", "renders", "logs", "review_queue", "exports"]:
        (root / name).mkdir(parents=True, exist_ok=True)

    db = workspace_db_path(root)
    con = connect(db)
    apply_schema(con, "sqlite_brain_builder.workspace", "workspace_schema.sql")

    existing_user = con.execute(
        "SELECT user_id FROM workspace_user WHERE username=? LIMIT 1",
        (username,),
    ).fetchone()
    if existing_user is None:
        salt, digest = hash_password(password)
        con.execute(
            "INSERT INTO workspace_user(user_id,username,password_salt,password_hash,created_at) VALUES(?,?,?,?,?)",
            (uid("user"), username, salt, digest, now()),
        )
    workspace_value = str(root)
    existing_workspace = con.execute(
        "SELECT value FROM workspace_settings WHERE key='workspace_dir'",
    ).fetchone()
    if existing_workspace is None or existing_workspace[0] != workspace_value:
        con.execute(
            "INSERT OR REPLACE INTO workspace_settings(key,value,updated_at) VALUES(?,?,?)",
            ("workspace_dir", workspace_value, now()),
        )
    con.commit()
    con.close()
    return db


def create_brain(workspace_dir: str | Path, brain_name: str) -> dict:
    root = normalize_workspace_dir(workspace_dir)
    db = workspace_db_path(root)
    con = connect(db)
    apply_schema(con, "sqlite_brain_builder.workspace", "workspace_schema.sql")

    brain_id = uid("brain")
    slug = slugify(brain_name)
    out = brain_output_dir(root, brain_name)
    out.mkdir(parents=True, exist_ok=True)

    con.execute(
        "INSERT INTO brain_project VALUES(?,?,?,?,?,?,?)",
        (brain_id, brain_name, slug, str(out), "ACTIVE", now(), now())
    )
    session_id = uid("session")
    con.execute(
        "INSERT INTO brain_session VALUES(?,?,?,?)",
        (session_id, brain_id, "Initial session", now())
    )
    con.execute(
        "INSERT OR REPLACE INTO brain_last_state_pointer VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (brain_id, session_id, None, None, None, None, None, "overview", str(out), "Add intake or build package", "ACTIVE", now())
    )
    con.commit()
    con.close()

    return {
        "brain_id": brain_id,
        "brain_name": brain_name,
        "brain_slug": slug,
        "output_dir": str(out),
        "session_id": session_id,
    }


def update_last_state(workspace_dir: str | Path, brain_id: str, **kwargs):
    db = workspace_db_path(workspace_dir)
    con = connect(db)
    row = con.execute("SELECT * FROM brain_last_state_pointer WHERE brain_id=?", (brain_id,)).fetchone()
    cols = [d[0] for d in con.execute("SELECT * FROM brain_last_state_pointer LIMIT 0").description]
    data = dict(zip(cols, row)) if row else {"brain_id": brain_id}
    data.update(kwargs)
    data["created_at"] = now()
    values = [data.get(c) for c in cols]
    con.execute("INSERT OR REPLACE INTO brain_last_state_pointer VALUES(%s)" % ",".join("?" for _ in cols), values)
    con.commit()
    con.close()


def get_last_state(workspace_dir: str | Path, brain_id: str) -> dict:
    con = connect(workspace_db_path(workspace_dir))
    cur = con.execute("SELECT * FROM brain_last_state_pointer WHERE brain_id=?", (brain_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    con.close()
    return dict(zip(cols, row)) if row else {}
