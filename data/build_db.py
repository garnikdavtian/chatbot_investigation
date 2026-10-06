"""Build data/investigation.sqlite from the supplied schema and seed plus data/additions.json.

Deterministic: the JSON files are the source of truth and the database is a build output.
Run: uv run python -m data.build_db
"""
import json
import sqlite3
from pathlib import Path

from investigator.calc import load_json, validate

DATA = Path(__file__).resolve().parent
SOURCES = (DATA / "starter/seed.json", DATA / "additions.json")


def build(out: Path = DATA / "investigation.sqlite") -> Path:
    problems = validate(load_json(*SOURCES))
    if problems["errors"]:
        raise ValueError(f"data contract failed: {problems['errors']}")

    out.unlink(missing_ok=True)
    con = sqlite3.connect(out)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript((DATA / "starter/schema.sql").read_text())
        for src in SOURCES:
            for table, rows in json.loads(src.read_text()).items():
                cols = list(rows[0])
                con.executemany(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows],
                )
        con.commit()
    finally:
        con.close()
    return out


if __name__ == "__main__":
    print(f"built {build()}")
