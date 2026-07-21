#!/usr/bin/env python3
"""Safe FTS5 query wrapper for Env15 chat_lineage.

Quotes each user token so punctuation such as V5.5, deep-ml, paths, and
reserved FTS operators are treated as literals. Uses parameter binding.
"""
from __future__ import annotations
import re
import sqlite3
from pathlib import Path


def quote_fts_literal(token: str) -> str:
    token = token.replace('"', '""').strip()
    return f'"{token}"'


def build_safe_match_query(text: str) -> str:
    tokens = [t for t in re.split(r'\s+', text.strip()) if t]
    if not tokens:
        return '""'
    return ' AND '.join(quote_fts_literal(t) for t in tokens)


def search(db_path: str | Path, text: str, limit: int = 50):
    query = build_safe_match_query(text)
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            'SELECT turn_id, record_type, content FROM turn_fts WHERE turn_fts MATCH ? LIMIT ?',
            (query, int(limit)),
        ).fetchall()
    finally:
        con.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('db')
    parser.add_argument('query')
    args = parser.parse_args()
    for row in search(args.db, args.query):
        print(row)
