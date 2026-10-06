"""The answer key is checked three independent ways: by hand (data/expected.json),
in plain Python (calc.py) and in plain SQL. Supplied seed results are checked as given."""
import json
import sqlite3
from pathlib import Path

import pytest

from data.build_db import SOURCES, build
from investigator.calc import by_segment, load_db, load_json, totals, validate

DATA = Path("data")
EXPECTED = json.loads((DATA / "expected.json").read_text())
SEED_EXPECTED = json.loads((DATA / "starter/expected-seed-results.json").read_text())

# Correct pattern: sum each side on its own (rule 3), then combine.
SQL_TOTALS = """
SELECT (SELECT coalesce(sum(amount_cents), 0) FROM orders WHERE order_date >= :s AND order_date < :e),
       (SELECT coalesce(sum(amount_cents), 0) FROM refunds WHERE refund_date >= :s AND refund_date < :e)
"""
# The trap from domain.md: joining raw refund rows to orders repeats order amounts.
SQL_NAIVE_GROSS = """
SELECT sum(o.amount_cents) FROM orders o LEFT JOIN refunds r ON r.order_id = o.order_id
WHERE o.order_date >= :s AND o.order_date < :e
"""


def test_supplied_seed_db_matches_seed_json():
    assert load_db(DATA / "starter/seed.sqlite") == load_json(DATA / "starter/seed.json")


def test_calc_reproduces_supplied_seed_answer_key():
    seed = load_json(DATA / "starter/seed.json")
    for p in SEED_EXPECTED["periods"]:
        got = totals(seed, p["start"], p["end_exclusive"])
        assert got == {k: p[k] for k in ("gross_cents", "refunds_cents", "net_cents")}
    assert by_segment(seed, "2026-09-01", "2026-10-01") == SEED_EXPECTED["september_segments"]


def test_built_db_matches_json_sources(tmp_path):
    rebuilt = load_db(build(tmp_path / "x.sqlite"))
    assert rebuilt == load_json(*SOURCES) == load_db(DATA / "investigation.sqlite")


def test_data_contract_holds():
    assert validate(load_db(DATA / "investigation.sqlite")) == {"errors": [], "warnings": []}


@pytest.mark.parametrize("period", EXPECTED["periods"], ids=lambda p: p["start"])
def test_hand_key_equals_calc_equals_sql(period):
    data = load_db(DATA / "investigation.sqlite")
    want = {k: period[k] for k in ("gross_cents", "refunds_cents", "net_cents")}
    assert totals(data, period["start"], period["end_exclusive"]) == want

    con = sqlite3.connect(DATA / "investigation.sqlite")
    gross, refunds = con.execute(SQL_TOTALS, {"s": period["start"], "e": period["end_exclusive"]}).fetchone()
    assert {"gross_cents": gross, "refunds_cents": refunds, "net_cents": gross - refunds} == want


def test_segments_match_hand_key_and_sum_to_totals():
    data = load_db(DATA / "investigation.sqlite")
    for p in EXPECTED["periods"]:
        segs = by_segment(data, p["start"], p["end_exclusive"])
        assert segs == EXPECTED["segments"][p["start"]]
        for k in ("gross_cents", "refunds_cents", "net_cents"):
            assert sum(s[k] for s in segs.values()) == p[k]


def test_naive_join_trap_really_double_counts():
    con = sqlite3.connect(DATA / "investigation.sqlite")
    (naive,) = con.execute(SQL_NAIVE_GROSS, {"s": "2026-09-01", "e": "2026-10-01"}).fetchone()
    assert naive == EXPECTED["traps"]["naive_join_september_gross_cents"] != EXPECTED["periods"][1]["gross_cents"]


def test_validate_catches_broken_data():
    bad = {
        "customers": [{"customer_id": "C1", "segment": "small"}],
        "orders": [
            {"order_id": "O1", "customer_id": "C1", "order_date": "2026-09-05", "amount_cents": 100},
            {"order_id": "O2", "customer_id": "C9", "order_date": "05/09/2026", "amount_cents": 1.5},
        ],
        "refunds": [
            {"refund_id": "R1", "order_id": "O1", "refund_date": "2026-09-01", "amount_cents": 150},
            {"refund_id": "R2", "order_id": "O7", "refund_date": "2026-09-09", "amount_cents": 1},
        ],
    }
    report = validate(bad)
    text = " ".join(report["errors"])
    for needle in ("unknown customer C9", "not YYYY-MM-DD", "not a non-negative integer", "unknown order O7", "exceed order amount"):
        assert needle in text
    assert report["warnings"] == ["R1: refund_date is before its order_date"]
