from __future__ import annotations
from pathlib import Path
import re
from sqlite_brain_builder.core import uid, sha256_text, now
from sqlite_brain_builder.storage.sqlite_utils import connect, apply_schema


def parse_chat_text(text: str) -> dict:
    turns = []
    speaker = "unknown"
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, speaker
        if buf:
            t = "\n".join(buf).strip()
            if t:
                turns.append({"speaker": speaker, "text": t})
        buf = []

    for line in text.splitlines():
        m = re.match(r"^(User|Assistant|Human|AI|Praveen|ChatGPT)\s*[:：]", line.strip(), re.I)
        if m:
            flush()
            speaker = "user" if m.group(1).lower() in {"user", "human", "praveen"} else "assistant"
            buf.append(line.split(":", 1)[1].strip() if ":" in line else "")
        else:
            buf.append(line)
    flush()
    if not turns and text.strip():
        turns = [{"speaker": "transcript", "text": text.strip()}]

    deltas, gates, reqs, artifacts = [], [], [], []
    for i, t in enumerate(turns):
        lower = t["text"].lower()
        if any(k in lower for k in ["delta", "correction", "supersede", "new requirement", "hard gate"]):
            deltas.append({"turn": i, "text": t["text"][:1000]})
        if "hard gate" in lower or "mandatory" in lower:
            gates.append({"turn": i, "text": t["text"][:1000]})
        if "requirement" in lower or "must" in lower:
            reqs.append({"turn": i, "text": t["text"][:1000]})
        for m in re.finditer(r"[A-Za-z0-9_\- ]+\.(zip|sqlite|md|txt|png|svg|mmd)", t["text"]):
            artifacts.append({"turn": i, "text": m.group(0)})
    return {"turns": turns, "deltas": deltas, "hard_gates": gates, "requirements": reqs, "artifacts": artifacts}


def build_semantic_sector(db_path: str | Path, source_path: str | Path, lane: str = "Discussion", contract_text: str = "prompt\nresponse\ndecision\ndelta\nhard_gate\nartifact_reference\nnext_action") -> dict:
    source_path = Path(source_path)
    text = source_path.read_text(encoding="utf-8", errors="replace")
    con = connect(db_path)
    apply_schema(con, "sqlite_brain_builder.storage", "semantic_sector_schema.sql")
    source_id = uid("semantic_src")
    contract_id = uid("contract")
    con.execute(
        "INSERT INTO semantic_source VALUES(?,?,?,?,?,?)",
        (source_id, lane, source_path.suffix.lstrip(".").upper(), source_path.name, sha256_text(text), now()),
    )
    con.execute(
        "INSERT INTO lane_schema_contract VALUES(?,?,?,?,?,?)",
        (contract_id, lane, f"{lane} contract", contract_text, sha256_text(contract_text), now()),
    )
    for field in [x.strip() for x in contract_text.splitlines() if x.strip()]:
        con.execute("INSERT INTO lane_schema_field VALUES(?,?,?,?,?)", (uid("field"), contract_id, field, "TEXT", 0))
    parsed = parse_chat_text(text)
    for i, t in enumerate(parsed["turns"]):
        tid = uid("turn")
        h = sha256_text(t["text"])
        con.execute("INSERT INTO discussion_turn VALUES(?,?,?,?,?,?)", (tid, source_id, i, t["speaker"], t["text"], h))
        con.execute("INSERT INTO lineage_fts VALUES(?,?,?,?,?)", ("turn", tid, source_id, h, t["text"]))
    for d in parsed["deltas"]:
        did = uid("delta")
        h = sha256_text(d["text"])
        con.execute("INSERT INTO discussion_delta VALUES(?,?,?,?,?)", (did, source_id, None, d["text"], h))
        con.execute("INSERT INTO lineage_fts VALUES(?,?,?,?,?)", ("delta", did, source_id, h, d["text"]))
    for g in parsed["hard_gates"]:
        gid = uid("gate")
        h = sha256_text(g["text"])
        con.execute("INSERT INTO discussion_hard_gate VALUES(?,?,?,?,?)", (gid, source_id, None, g["text"], h))
        con.execute("INSERT INTO lineage_fts VALUES(?,?,?,?,?)", ("hard_gate", gid, source_id, h, g["text"]))
    con.commit()
    con.close()
    return {
        "source_id": source_id,
        "turns": len(parsed["turns"]),
        "deltas": len(parsed["deltas"]),
        "hard_gates": len(parsed["hard_gates"]),
        "requirements": len(parsed["requirements"]),
    }
