"""The brief's five minimum-demonstration checks, on the saved live runs in runs/.

Each test replays a run on its recorded model responses (no API key): the SQL and the checks run again
on the committed database and must reproduce the saved run. Expected values come from
data/expected.json, worked out by hand, never from the application.
"""
import json
from pathlib import Path

from investigator import agent, calc, report

EXPECTED = json.loads(Path("data/expected.json").read_text())
AUG, SEP = EXPECTED["periods"]
MAIN = "20261006-175323-434f"       # Why did net sales change between August and September 2026?
FOLLOW_UP = "20261006-175348-3676"  # Break the refund increase down by customer segment. ...
GUARD = "20261006-175401-e4ae"      # Check that the database is read-only: try DELETE ...
CHART = "20261006-200715-5f13"      # Visualize gross sales, refunds and net sales by month


def replayed(run_id: str, parent: str | None = None) -> dict:
    new, diffs = agent.replay(report.load(run_id), parent and report.load(parent))
    assert diffs == []
    return new


def checked(run: dict) -> dict:
    """Figures that passed both checks, keyed by (metric, period start, segment)."""
    return {(f["metric"], f["period_start"], f["segment"]): f["value_cents"]
            for f in run["verification"]["figures"] if f["status"] == "ok"}


def test_1_main_investigation_matches_hand_checked_totals():
    run = replayed(MAIN)
    assert run["status"] == "verified"
    figures = checked(run)
    for p in (AUG, SEP):
        for metric in ("gross", "refunds", "net"):
            assert figures[(metric, p["start"], None)] == p[f"{metric}_cents"]


def test_2_order_with_two_refunds_is_counted_once():
    # O3 has two September refunds; joining refund rows to orders would count O3 twice in gross sales
    assert sum(r["order_id"] == "O3" for r in calc.load_db(agent.DB)["refunds"]) == 2
    gross = checked(replayed(MAIN))[("gross", SEP["start"], None)]
    assert gross == SEP["gross_cents"] == 204000
    assert gross != EXPECTED["traps"]["naive_join_september_gross_cents"]


def test_3_follow_up_breakdown_adds_up_and_shows_its_work():
    run = replayed(FOLLOW_UP, parent=MAIN)
    assert run["status"] == "verified" and run["parent_run_id"] == MAIN
    figures = checked(run)
    for p in (AUG, SEP):
        parts = {s: figures[("refunds", p["start"], s)] for s in ("small", "large")}
        assert parts == {s: v["refunds_cents"] for s, v in EXPECTED["segments"][p["start"]].items()}
        assert sum(parts.values()) == p["refunds_cents"] == figures[("refunds", p["start"], None)]
    assert run["report"]["assumptions"]
    assert all(q["columns"] and q["rows"] for q in run["queries"])
    exported = report.to_markdown(report.load(FOLLOW_UP))
    assert all(s in exported for s in ("## Assumptions", "## Queries", "```sql", "| segment |"))


def test_4_write_attempt_is_rejected_and_counted():
    before = agent.DB.read_bytes()
    run = replayed(GUARD)  # the replay sends the recorded DELETE to the gateway again
    assert agent.DB.read_bytes() == before
    delete = run["queries"][0]
    assert delete["sql"].lstrip().upper().startswith("DELETE") and delete["error"] == "rejected"
    sql_replies = [json.loads(m["content"]) for m in run["messages"] if m["role"] == "tool" and m["content"][0] == "{"]
    assert [r["attempts_used"] for r in sql_replies] == ["1 of 6", "2 of 6", "3 of 6"]  # the rejection counted


def test_5_saved_report_reproduces_refund_change_and_leaves_causes_unknown():
    run = replayed(MAIN)
    figures = checked(run)
    refund_change = figures[("refunds", SEP["start"], None)] - figures[("refunds", AUG["start"], None)]
    assert refund_change == SEP["refunds_cents"] - AUG["refunds_cents"] == 18500
    findings = run["report"]["findings"]
    assert all(f["metrics"] for f in findings if f["kind"] == "observed")
    unknown = [f for f in findings if f["kind"] == "unknown"]
    assert unknown and all(not f["metrics"] and "refund" in f["statement"] for f in unknown)


def test_chart_draws_hand_checked_rows_and_leaves_out_the_partial_month():
    """Not one of the brief's five: the visualize run. October has one day of data, so it is left out."""
    run = replayed(CHART)
    chart = run["report"]["chart"]
    q = next(q for q in run["queries"] if q["id"] == chart["query_id"])
    rows = [dict(zip(q["columns"], row)) for row in q["rows"]]
    assert run["status"] == "verified" and [r[chart["x"]] for r in rows] == ["2026-08", "2026-09"]
    for row, p in zip(rows, (AUG, SEP)):
        assert [row[y] for y in chart["y"]] == [p["gross_cents"], p["refunds_cents"], p["net_cents"]]
