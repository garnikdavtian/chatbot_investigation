"""Users and their chats, in their own SQLite file, apart from the read-only sales data.

Only the app writes it. Each browser gets its own random app key on its first visit (POST /api/session; not
the LLM key). The page keeps it and sends it on every call; only its sha256 is stored. Every read filters by
user, so one user cannot list, open or continue another user's chats. Nobody signs in or sees a key.
HISTORY_DB overrides the path (the container keeps it on a volume).
ponytail: one app instance writes this file; move it to Postgres when several replicas must share it.
"""
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from investigator.report import ROOT

DB = Path(os.environ.get("HISTORY_DB") or ROOT / "data" / "history.sqlite")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, key_sha256 TEXT NOT NULL UNIQUE,
                                  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users,
                                 parent_run_id TEXT, created_at TEXT NOT NULL, question TEXT NOT NULL,
                                 status TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_by_user ON runs (user_id, created_at);
"""


@contextmanager
def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=5)) as con, con:  # commits on success
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        yield con


def _sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def add_user() -> str:
    """A new anonymous user; returns their key, which only the caller ever holds."""
    key = secrets.token_urlsafe(32)
    with _db() as con:
        con.execute("INSERT INTO users (key_sha256, created_at) VALUES (?, ?)",
                    (_sha(key), datetime.now(UTC).isoformat(timespec="seconds")))
    return key


def user_for_key(key: str) -> int | None:
    with _db() as con:
        row = con.execute("SELECT user_id FROM users WHERE key_sha256 = ?", (_sha(key),)).fetchone()
    return row and row[0]


def save(run: dict, user_id: int) -> None:
    with _db() as con:
        con.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run["run_id"], user_id, run["parent_run_id"], run["created_at"], run["question"],
                     run["status"], json.dumps(run, ensure_ascii=False)))


def load(run_id: str, user_id: int) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT record FROM runs WHERE run_id = ? AND user_id = ?", (run_id, user_id)).fetchone()
    return row and json.loads(row[0])


def list_runs(user_id: int) -> list[dict]:
    keys = ("run_id", "created_at", "question", "status", "parent_run_id")
    with _db() as con:
        rows = con.execute(f"SELECT {', '.join(keys)} FROM runs WHERE user_id = ? ORDER BY created_at DESC, run_id DESC",
                           (user_id,)).fetchall()
    return [dict(zip(keys, row)) for row in rows]
