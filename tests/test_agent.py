"""The investigation loop with scripted model responses (source "scripted", no API calls)."""
import hashlib
import itertools
import json
import shutil
import sqlite3

import pytest

from investigator import report
from investigator.agent import investigate, replay
from investigator.llm import LLMError, ScriptedLLM

AUG, SEP, OCT = "2026-08-01", "2026-09-01", "2026-10-01"
MONTHLY = """WITH g AS (SELECT substr(order_date, 1, 7) AS month, sum(amount_cents) AS gross FROM orders GROUP BY month),
r AS (SELECT substr(refund_date, 1, 7) AS month, sum(amount_cents) AS refunds FROM refunds GROUP BY month)
SELECT month, gross, coalesce(refunds, 0) AS refunds, gross - coalesce(refunds, 0) AS net
FROM g LEFT JOIN r USING (month) WHERE month IN ('2026-08', '2026-09') ORDER BY month"""
SEGMENT_REFUNDS = """SELECT c.segment, sum(rf.amount_cents) AS refunds FROM refunds rf
JOIN orders o ON o.order_id = rf.order_id JOIN customers c ON c.customer_id = o.customer_id
WHERE rf.refund_date >= '2026-09-01' AND rf.refund_date < '2026-10-01' GROUP BY c.segment ORDER BY c.segment"""
NAIVE_JOIN = """SELECT sum(o.amount_cents) AS gross FROM orders o LEFT JOIN refunds r ON r.order_id = o.order_id
WHERE o.order_date >= '2026-09-01' AND o.order_date < '2026-10-01'"""
ids = itertools.count(1)


def tool(name, args):
    return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{next(ids)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}}


def sql(query, purpose="check"):
    return tool("run_sql", {"purpose": purpose, "sql": query})


def fig(metric, start, end, value, query_id, segment=None):
    return {"metric": metric, "period_start": start, "period_end_exclusive": end, "segment": segment,
            "value_cents": value, "query_id": query_id}


def submit(*figures, summary="Net sales fell by 8500 cents, from 191500 cents to 183000 cents.", kind="observed"):
    return tool("submit_report", {"summary": summary, "findings": [{"kind": kind, "statement": "Totals.", "metrics": list(figures)}],
                                  "assumptions": ["Calendar months, UTC."], "open_questions": []})


GOOD = submit(fig("net", AUG, SEP, 191500, "q1"), fig("net", SEP, OCT, 183000, "q1"), fig("refunds", SEP, OCT, 16000, "q2", "small"))


def scripted(*calls):
    return ScriptedLLM(list(calls), source="scripted")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def db(tmp_path):
    copy = tmp_path / "db.sqlite"
    shutil.copy("data/investigation.sqlite", copy)
    return copy


def test_happy_path_is_verified(db):
    steps = []  # what the web UI streams as progress
    run = investigate("Why did net sales change?", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), GOOD), db,
                      on_step=lambda r: steps.append((len(r["queries"]), r["report"] is not None)))
    assert run["status"] == "verified", run["verification"]
    assert steps == [(1, False), (2, False), (2, True)]
    assert run["queries"][0]["rows"] == [["2026-08", 194000, 2500, 191500], ["2026-09", 204000, 21000, 183000]]
    assert {f["status"] for f in run["verification"]["figures"]} == {"ok"}
    assert {c["source"] for c in run["model_calls"]} == {"scripted"}
    assert "$1,830.00" in report.to_markdown(run)


def test_chart_is_checked_against_the_cited_result(db):
    def charted(**chart):
        call = submit(fig("net", AUG, SEP, 191500, "q1"), fig("net", SEP, OCT, 183000, "q1"))
        args = json.loads(call["message"]["tool_calls"][0]["function"]["arguments"])
        args["chart"] = {"kind": "line", "title": "Net by month", "query_id": "q1", "x": "month",
                         "y": ["net"], "group": None, "unit": "cents", **chart}
        call["message"]["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        return call
    run = investigate("Plot net sales by month", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), charted()), db)
    assert run["status"] == "verified" and run["report"]["chart"]["y"] == ["net"]
    assert "## Chart" in report.to_markdown(run)
    bad = charted(y=["month"]), charted(y=["profit"])  # text values, then a column the query never returned
    run = investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), *bad), db)
    assert run["status"] == "unverified"
    assert "must be numbers" in run["repairs"][0]["verification"]["issues"][0]
    assert "['profit']" in run["verification"]["issues"][0]
    assert "chart" not in investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), GOOD), db)["report"]
    one_bar = charted(query_id="q2", x="segment", y=["refunds"])
    run = investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS + " DESC LIMIT 1"), one_bar, GOOD), db)
    assert "single value" in run["repairs"][0]["verification"]["issues"][0] and run["status"] == "verified"


FAN_OUT = submit(fig("gross", SEP, OCT, 209000, "q1"), summary="Gross was 209000 cents.")


def test_fan_out_join_is_caught(db):
    """O3 has two refunds: joining refunds to orders counts its 5000 twice (209000 instead of 204000)."""
    run = investigate("q", scripted(sql(NAIVE_JOIN), sql(MONTHLY), FAN_OUT, FAN_OUT), db)
    assert run["status"] == "unverified" and len(run["repairs"]) == 1
    figure = run["verification"]["figures"][0]
    assert (figure["status"], figure["expected_cents"]) == ("query_error", 204000)


def test_repair_round_fixes_a_wrong_figure_without_leaking_the_answer(db):
    fixed = submit(fig("gross", SEP, OCT, 204000, "q2"), summary="Gross was 204000 cents.")
    run = investigate("q", scripted(sql(NAIVE_JOIN), sql(MONTHLY), FAN_OUT, fixed), db)
    feedback = run["messages"][7]["content"]
    assert "does not apply the metric rules" in feedback and "204000" not in feedback
    assert run["status"] == "verified" and run["repairs"][0]["verification"]["figures"][0]["status"] == "query_error"


def test_figure_must_be_in_the_cited_result(db):
    cited_wrong = submit(fig("net", SEP, OCT, 183000, "q2"), summary="Net was 183000 cents.")
    run = investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), cited_wrong, cited_wrong), db)
    assert run["status"] == "unverified"
    assert run["verification"]["figures"][0]["status"] == "unsupported"


def test_prose_money_is_checked(db):
    dollars = submit(fig("net", SEP, OCT, 183000, "q1"), summary="Net fell by $85 or 9999 cents.")
    run = investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), dollars, dollars), db)
    issues = " ".join(run["verification"]["issues"])
    assert run["status"] == "unverified" and '"$"' in issues and "9999" in issues


def test_write_is_rejected_and_failed_queries_count_toward_the_limit(db):
    before = sha(db)
    llm = scripted(sql("DELETE FROM refunds"), *[sql("SELEC oops")] * 5, sql(MONTHLY), GOOD, GOOD)
    run = investigate("q", llm, db)
    assert sha(db) == before
    assert [q["error"] for q in run["queries"]] == ["rejected"] + ["sql"] * 5  # the 7th was not run
    assert run["limit_reached"] and run["status"] == "incomplete"
    assert llm.seen[-1]["tool_choice"] == {"type": "function", "function": {"name": "submit_report"}}


def test_submit_needs_a_follow_up_query(db):
    run = investigate("q", scripted(sql(MONTHLY), GOOD, sql(SEGMENT_REFUNDS), GOOD), db)
    assert run["status"] == "verified" and len(run["model_calls"]) == 4
    assert "follow-up" in run["messages"][5]["content"]


def test_invalid_report_gets_a_repair_turn(db):
    run = investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), tool("submit_report", {"summary": "x"}), GOOD), db)
    assert run["status"] == "verified"
    assert "findings" in run["messages"][7]["content"]


def test_empty_result_and_model_failure_are_visible(db):
    run = investigate("q", scripted(sql("SELECT * FROM refunds WHERE refund_date >= '2027-01-01'"), LLMError("timeout")), db)
    assert "empty result" in run["messages"][3]["content"]
    assert run["status"] == "failed" and "timeout" in run["error"] and len(run["queries"]) == 1


def test_no_tool_call_ends_incomplete_at_turn_limit(db):
    text = {"message": {"role": "assistant", "content": "Sales fell."}}
    run = investigate("q", scripted(*[text] * 3), db, max_turns=3)
    assert run["status"] == "incomplete" and run["report"] is None


def test_data_contract_violation_stops_before_any_model_call(db):
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO refunds VALUES ('RF99', 'O404', '2026-09-03', 100)")
    llm = scripted(GOOD)
    run = investigate("q", llm, db)
    assert run["status"] == "failed" and "unknown order O404" in run["error"] and llm.seen == []


def test_follow_up_carries_the_parent_context(db):
    parent = investigate("Why did net sales change?", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), GOOD), db)
    llm = scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), GOOD)
    run = investigate("Which segment drove it?", llm, db, parent=parent)
    first = llm.seen[0]["messages"][1]["content"]
    assert run["parent_run_id"] == parent["run_id"]
    assert "Why did net sales change?" in first and "Follow-up question: Which segment drove it?" in first


def test_replay_reproduces_and_detects_a_data_change(db, tmp_path):
    run = investigate("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), GOOD), db)
    saved = report.load(report.save(run, tmp_path / "runs").stem, tmp_path / "runs")
    new, diffs = replay(saved, db_path=db)
    assert diffs == [] and new["status"] == "verified"
    assert {c["source"] for c in new["model_calls"]} == {"replay"}

    with sqlite3.connect(db) as con:  # one more September refund
        con.execute("INSERT INTO refunds VALUES ('RF99', 'O18', '2026-09-03', 100)")
    new, diffs = replay(saved, db_path=db)
    assert {"db_sha changed since recording", "q1 result differs"} <= set(diffs)
    assert "status verified -> failed" in diffs  # the new data triggers a repair round the recording never had
