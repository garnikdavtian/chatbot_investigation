"""Metric rules from data/starter/domain.md as plain Python: the independent answer key.

No SQL here, so it can check the SQL the model writes. Pure functions over row dicts.
"""
import json
import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

TABLES = ("customers", "orders", "refunds")


def load_json(*paths) -> dict:
    data = {t: [] for t in TABLES}
    for p in paths:
        loaded = json.loads(Path(p).read_text())
        for t in TABLES:
            data[t] += loaded.get(t, [])
    return data


def load_db(path) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {t: [dict(r) for r in con.execute(f"SELECT * FROM {t}")] for t in TABLES}
    finally:
        con.close()


def totals(data: dict, start: str, end: str, segment: str | None = None) -> dict:
    """Rule 2: orders by order_date, refunds by refund_date, half-open [start, end).
    Rule 3: each side summed on its own, never joined row-by-row.
    Rule 4: a refund takes the segment of the customer on its original order."""
    seg_of_customer = {c["customer_id"]: c["segment"] for c in data["customers"]}
    seg_of_order = {o["order_id"]: seg_of_customer[o["customer_id"]] for o in data["orders"]}

    def keep(day, order_id):
        return start <= day < end and (segment is None or seg_of_order[order_id] == segment)

    gross = sum(o["amount_cents"] for o in data["orders"] if keep(o["order_date"], o["order_id"]))
    refunds = sum(r["amount_cents"] for r in data["refunds"] if keep(r["refund_date"], r["order_id"]))
    return {"gross_cents": gross, "refunds_cents": refunds, "net_cents": gross - refunds}


def by_segment(data: dict, start: str, end: str) -> dict:
    return {s: totals(data, start, end, s) for s in sorted({c["segment"] for c in data["customers"]})}


def months(first: str, last: str, limit: int = 24) -> list[str]:
    """Calendar months first..last inclusive, as YYYY-MM. ValueError on a bad month, a reversed or too long range."""
    if not all(isinstance(m, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", m) for m in (first, last)) or first > last:
        raise ValueError("months must be YYYY-MM, the first not after the last")
    y, m = map(int, first.split("-"))
    out = []
    while f"{y:04d}-{m:02d}" <= last:
        if len(out) == limit:
            raise ValueError(f"at most {limit} months")
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_bounds(month: str) -> tuple[str, str]:
    """[first day, first day of the next month) for YYYY-MM: rule 2's half-open period."""
    y, m = map(int, month.split("-"))
    return f"{month}-01", f"{y + m // 12:04d}-{m % 12 + 1:02d}-01"


def is_iso_day(s) -> bool:
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _is_cents(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate(data: dict) -> dict:
    """Data contract checked before any investigation. Errors break a stated rule;
    warnings are suspicious but no rule covers them (see README ambiguity log)."""
    errors, warnings = [], []
    for table, key in (("customers", "customer_id"), ("orders", "order_id"), ("refunds", "refund_id")):
        dupes = [k for k, n in Counter(r[key] for r in data[table]).items() if n > 1]
        if dupes:
            errors.append(f"{table}: duplicate {key} {dupes}")

    customers = {c["customer_id"] for c in data["customers"]}
    orders = {o["order_id"]: o for o in data["orders"]}
    for o in data["orders"]:
        if o["customer_id"] not in customers:
            errors.append(f"{o['order_id']}: unknown customer {o['customer_id']}")
        if not is_iso_day(o["order_date"]):
            errors.append(f"{o['order_id']}: order_date {o['order_date']!r} is not YYYY-MM-DD")
        if not _is_cents(o["amount_cents"]):
            errors.append(f"{o['order_id']}: amount_cents {o['amount_cents']!r} is not a non-negative integer")

    refunded = Counter()
    for r in data["refunds"]:
        order = orders.get(r["order_id"])
        if order is None:
            errors.append(f"{r['refund_id']}: unknown order {r['order_id']}")
        if not is_iso_day(r["refund_date"]):
            errors.append(f"{r['refund_id']}: refund_date {r['refund_date']!r} is not YYYY-MM-DD")
        if not _is_cents(r["amount_cents"]):
            errors.append(f"{r['refund_id']}: amount_cents {r['amount_cents']!r} is not a non-negative integer")
            continue
        refunded[r["order_id"]] += r["amount_cents"]
        if order and is_iso_day(r["refund_date"]) and r["refund_date"] < order["order_date"]:
            warnings.append(f"{r['refund_id']}: refund_date is before its order_date")

    for order_id, cents in refunded.items():
        if order_id in orders and _is_cents(orders[order_id]["amount_cents"]) and cents > orders[order_id]["amount_cents"]:
            errors.append(f"{order_id}: refunds {cents} exceed order amount {orders[order_id]['amount_cents']}")
    return {"errors": errors, "warnings": warnings}
