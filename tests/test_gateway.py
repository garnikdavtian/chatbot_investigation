"""Red-team the read-only SQL tool. Every attack must be rejected and leave the database byte-identical."""
import hashlib
import shutil

import pytest

from investigator.gateway import run_sql

ATTACKS = [
    "DROP TABLE orders",
    "DELETE FROM refunds",
    "INSERT INTO customers VALUES ('X', 'y')",
    "UPDATE orders SET amount_cents = 0",
    "CREATE TEMP TABLE t(x)",
    "ATTACH DATABASE 'evil.db' AS e",
    "PRAGMA writable_schema = ON",
    "VACUUM INTO 'copy.db'",
    "SELECT 1; DROP TABLE orders",
    "SELECT * FROM sqlite_master",
    "SELECT load_extension('x')",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r",
]


@pytest.fixture
def db(tmp_path):
    copy = tmp_path / "db.sqlite"
    shutil.copy("data/investigation.sqlite", copy)
    return copy


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_select_returns_columns_and_rows(db):
    r = run_sql(db, "SELECT substr(order_date, 1, 7) AS month, sum(amount_cents) AS gross FROM orders GROUP BY month")
    assert r.ok and r.columns == ["month", "gross"]
    assert r.rows == [["2026-08", 194000], ["2026-09", 204000], ["2026-10", 14000]]


@pytest.mark.parametrize("sql", ATTACKS)
def test_attack_is_rejected_and_db_unchanged(db, sql, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # any file an attack might create would land here
    before = sha(db)
    r = run_sql(db, sql)
    assert r.error == "rejected", r.message
    assert sha(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["db.sqlite"]


def test_runaway_query_times_out(db):
    r = run_sql(db, "SELECT count(*) FROM orders a, orders b, orders c, orders d, orders e, orders f", timeout_s=0.3)
    assert r.error == "timeout"


def test_row_cap_truncates(db):
    r = run_sql(db, "SELECT a.order_id FROM orders a, orders b", max_rows=50)
    assert r.ok and r.truncated and len(r.rows) == 50


def test_bad_sql_is_reported_not_raised(db):
    r = run_sql(db, "SELEC oops")
    assert r.error == "sql" and "syntax error" in r.message


def test_oversized_sql_rejected(db):
    assert run_sql(db, "SELECT 1" + " " * 5000).error == "rejected"
