"""Users, their login sessions and their chats, in their own SQLite file, apart from the read-only sales data.

Only the api container reads and writes it. An admin creates users (python -m investigator add-user <name>);
passwords are stored as salted scrypt hashes, login tokens as sha256. A chat is a chain of runs linked by
parent_run_id, and each run carries the conversation state the agent continues from. Every read filters by user,
so one user cannot list, open or continue another user's chats. HISTORY_DB overrides the path.
ponytail: one api instance writes this file; move it to Postgres when several replicas must share it.
"""
import hashlib
import hmac
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
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                                  password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_sha256 TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users,
                                     created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users,
                                 parent_run_id TEXT, created_at TEXT NOT NULL, question TEXT NOT NULL,
                                 status TEXT NOT NULL, record TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_by_user ON runs (user_id, created_at);
"""
SCRYPT = {"n": 2**14, "r": 8, "p": 1}  # the hashlib defaults' safe setting: about 16 MB and 50 ms per hash


@contextmanager
def _db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB, timeout=5)) as con, con:  # commits on success
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        yield con


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)


def add_user(name: str, password: str) -> int:
    """Raises sqlite3.IntegrityError if the name is taken."""
    salt = secrets.token_bytes(16)
    with _db() as con:
        return con.execute("INSERT INTO users (name, password_hash, created_at) VALUES (?, ?, ?)",
                           (name, f"{salt.hex()}:{_scrypt(password, salt).hex()}", _now())).lastrowid


def login(name: str, password: str) -> str | None:
    """A new session token, or None. The hash is computed even for an unknown name, so timing does not tell
    which names exist."""
    with _db() as con:
        row = con.execute("SELECT user_id, password_hash FROM users WHERE name = ?", (name,)).fetchone()
    salt, _, digest = (row[1] if row else f"{'0' * 32}:").partition(":")
    ok = hmac.compare_digest(_scrypt(password, bytes.fromhex(salt)).hex(), digest) and row is not None
    if not ok:
        return None
    token = secrets.token_urlsafe(32)
    with _db() as con:  # ponytail: tokens never expire; add expiry and a login rate limit beyond 127.0.0.1
        con.execute("INSERT INTO sessions VALUES (?, ?, ?)", (_sha(token), row[0], _now()))
    return token


def logout(token: str) -> None:
    with _db() as con:
        con.execute("DELETE FROM sessions WHERE token_sha256 = ?", (_sha(token),))


def user_for_token(token: str) -> dict | None:
    with _db() as con:
        row = con.execute("SELECT u.user_id, u.name FROM sessions s JOIN users u USING (user_id) "
                          "WHERE s.token_sha256 = ?", (_sha(token),)).fetchone()
    return row and {"user_id": row[0], "name": row[1]}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
        rows = con.execute(f"SELECT {', '.join(keys)} FROM runs WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
                           (user_id,)).fetchall()
    return [dict(zip(keys, row)) for row in rows]
