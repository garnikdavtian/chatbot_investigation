"""The graph with scripted model responses (source "scripted", no API calls)."""
import hashlib
import itertools
import json
import shutil
import sqlite3

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from investigator import agent, report
from investigator.agent import REFUSAL, chat, force, replay
from investigator.llm import LLMError, Scripted
from investigator.verify import _chart_issues

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
    return AIMessage("", tool_calls=[{"id": f"call_{next(ids)}", "name": name, "args": args}])


def verdict(label, reason="scripted"):
    return tool("verdict", {"label": label, "reason": reason})


def sql(query, purpose="check"):
    return tool("run_sql", {"purpose": purpose, "sql": query})


def fig(metric, start, end, value, query_id, segment=None):
    return {"metric": metric, "period_start": start, "period_end_exclusive": end, "segment": segment,
            "value_cents": value, "query_id": query_id}


def submit(*figures, summary="Net sales fell by 8500 cents, from 191500 cents to 183000 cents.", chart=None):
    return tool("submit_report", {"summary": summary, "findings": [{"kind": "observed", "statement": "Totals.",
                                                                    "metrics": list(figures)}],
                                  "assumptions": ["Calendar months, UTC."], "open_questions": [], "chart": chart})


GOOD = submit(fig("net", AUG, SEP, 191500, "q1"), fig("net", SEP, OCT, 183000, "q1"), fig("refunds", SEP, OCT, 16000, "q2", "small"))
INVESTIGATION = (sql(MONTHLY), sql(SEGMENT_REFUNDS), GOOD)


def scripted(*calls):
    """The guard allows, then the calls play in order."""
    return Scripted(script=[verdict("allow"), *calls])


def tool_texts(run):
    return [m["data"]["content"] for m in run["state"]["messages"] if m["type"] == "tool"]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def db(tmp_path):
    copy = tmp_path / "db.sqlite"
    shutil.copy("data/investigation.sqlite", copy)
    return copy


def test_happy_path_is_verified(db):
    steps = []  # what the web UI streams as progress
    llm = scripted(*INVESTIGATION)
    run = chat("Why did net sales change?", llm, db_path=db,
               on_step=lambda r: steps.append((len(r["queries"]), r["report"] is not None)))
    assert run["status"] == "verified", run["verification"]
    assert steps[-1] == (2, True) and (1, False) in steps
    assert run["queries"][0]["rows"] == [["2026-08", 194000, 2500, 191500], ["2026-09", 204000, 21000, 183000]]
    assert {f["status"] for f in run["verification"]["figures"]} == {"ok"}
    assert [c["purpose"] for c in run["model_calls"]] == ["guard", "reason", "reason", "reason"]
    assert {c["source"] for c in run["model_calls"]} == {"scripted"}
    assert [s["tool_choice"] for s in llm.seen] == [force("verdict"), "auto", "required", "required"]
    assert run["answer"] == run["report"]["summary"] and run["state"]["checked"] == [16000, 183000, 191500]
    assert "$1,830.00" in report.to_markdown(run)


def test_greeting_gets_a_plain_answer_without_queries(db):
    llm = scripted(AIMessage("Hi! Ask me about sales, refunds or customer segments."))
    run = chat("hi", llm, db_path=db)
    assert run["status"] == "answered" and run["queries"] == [] and run["answer"].startswith("Hi!")
    assert llm.seen[1]["tool_choice"] == "auto"
    assert "## Answer" in report.to_markdown(run) and "## Queries" not in report.to_markdown(run)


def test_off_topic_message_is_blocked_before_reason_runs(db):
    llm = Scripted(script=[verdict("off_topic", "a joke request")])
    run = chat("tell me a joke", llm, db_path=db)
    assert run["status"] == "blocked" and run["answer"] == REFUSAL and len(llm.seen) == 1
    assert run["guard"] == {"label": "off_topic", "reason": "a joke request"}
    assert [m["type"] for m in run["state"]["messages"]] == ["human", "ai"]  # the thread can go on


def test_malformed_guard_output_allows(db):
    llm = Scripted(script=[AIMessage("I think it's fine"), AIMessage("Hello.")])
    run = chat("hello", llm, db_path=db)
    assert run["status"] == "answered" and "malformed" in run["guard"]["reason"]


def test_plain_reply_may_repeat_only_checked_amounts(db):
    parent = chat("Why did net sales change?", scripted(*INVESTIGATION), db_path=db)
    ok = chat("Remind me of September net?", scripted(AIMessage("It was 183000 cents, down 8500 cents.")),
              parent, db_path=db)
    assert ok["status"] == "answered"
    made_up = chat("And October?", scripted(AIMessage("October was a 99000-cent month.")), parent, db_path=db)
    assert made_up["status"] == "unverified" and "99000" in made_up["verification"]["issues"][0]
    assert report.prose(made_up["answer"]) == "October was a $990.00 month."
    fresh = chat("Net in September?", scripted(AIMessage("About 183000 cents.")), db_path=db)
    assert fresh["status"] == "unverified"  # nothing was checked in a new conversation


def test_follow_up_sees_the_earlier_conversation(db):
    parent = chat("Why did net sales change?", scripted(*INVESTIGATION), db_path=db)
    llm = scripted(*INVESTIGATION)
    run = chat("Which segment drove it?", llm, parent, db_path=db)
    guard_input, first = llm.seen[0]["messages"][1].text, llm.seen[1]["messages"]
    assert "Previous question: Why did net sales change?" in guard_input
    assert isinstance(first[0], SystemMessage) and first[1].text == "Why did net sales change?"
    assert first[-1].text == "Which segment drove it?" and len(first) == 1 + len(parent["state"]["messages"]) + 1
    assert run["parent_run_id"] == parent["run_id"] and [q["id"] for q in run["queries"]] == ["q1", "q2"]


def test_old_messages_are_summarized_and_removed(db, monkeypatch):
    monkeypatch.setattr(agent, "MAX_MESSAGES", 2)
    one = chat("hi", scripted(AIMessage("Hello.")), db_path=db)
    two = chat("thanks", scripted(AIMessage("Welcome.")), one, db_path=db)
    llm = scripted(AIMessage("The user said hi and thanks."), AIMessage("Sure."))
    three = chat("what can you do?", llm, two, db_path=db)
    assert [c["purpose"] for c in three["model_calls"]] == ["guard", "compact", "reason"]
    assert three["compacted"] == 2 and three["state"]["summary"] == "The user said hi and thanks."
    assert [m["data"]["content"] for m in three["state"]["messages"]] == ["thanks", "Welcome.", "what can you do?", "Sure."]
    compact, reason = llm.seen[1], llm.seen[2]
    assert compact["tool_choice"] == "none" and compact["messages"][1].text == "hi"
    assert isinstance(reason["messages"][1], HumanMessage) and "hi and thanks" in reason["messages"][1].text
    assert "hi and thanks" not in reason["messages"][0].text  # the summary never enters the system message
    assert len(reason["messages"]) == 2 + 2 + 1
    assert "2 earlier messages were summarized" in report.to_markdown(three)
    new, diffs = replay(three, two, db_path=db)
    assert diffs == [] and new["compacted"] == 2


def test_chart_is_checked_against_the_cited_result(db):
    def charted(**chart):
        return submit(fig("net", AUG, SEP, 191500, "q1"), fig("net", SEP, OCT, 183000, "q1"),
                      chart={"kind": "bar", "title": "Net by month", "query_id": "q1", "x": "month",
                             "y": ["net"], "group": None, "unit": "cents", **chart})
    run = chat("Plot net sales by month", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), charted()), db_path=db)
    assert run["status"] == "verified" and run["report"]["chart"]["y"] == ["net"]
    assert "## Chart" in report.to_markdown(run)
    bad = charted(y=["month"]), charted(y=["profit"])  # text values, then a column the query never returned
    run = chat("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), *bad), db_path=db)
    assert run["status"] == "unverified"
    assert "must be numbers" in run["repairs"][0]["verification"]["issues"][0]
    assert "['profit']" in run["verification"]["issues"][0]
    assert "chart" not in chat("q", scripted(*INVESTIGATION), db_path=db)["report"]
    one_bar = charted(query_id="q2", x="segment", y=["refunds"])
    run = chat("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS + " DESC LIMIT 1"), one_bar, GOOD), db_path=db)
    assert "single value" in run["repairs"][0]["verification"]["issues"][0] and run["status"] == "verified"


def test_each_chart_kind_checks_its_own_shape():
    def issues(kind, columns, rows, y, x=None, group=None):
        chart = {"kind": kind, "title": "t", "query_id": "q1", "x": x or columns[0], "y": y, "group": group,
                 "unit": "cents"}
        return _chart_issues(chart, {"q1": {"error": None, "columns": columns, "rows": rows}})
    weeks = [["2026-08-03", 9000], ["2026-08-10", 7000], ["2026-08-17", 8000]]
    bridge = [["Aug net", 191500], ["gross change", 10000], ["refund change", -18500], ["Sep net", 183000]]
    by_segment = [["2026-08", "small", 2500], ["2026-08", "large", 0], ["2026-09", "small", 16000],
                  ["2026-09", "large", 5000]]
    per_customer = [["C1", 30000, 0], ["C2", 20000, 3000], ["C3", 7000, 7000]]
    assert issues("line", ["week", "net"], weeks, ["net"]) == []
    assert issues("area", ["week", "net"], weeks, ["net"]) == []
    assert issues("waterfall", ["step", "net"], bridge, ["net"]) == []
    assert issues("stacked_bar", ["month", "segment", "refunds"], by_segment, ["refunds"], group="segment") == []
    assert issues("hbar", ["customer", "gross", "refunds"], per_customer, ["gross"]) == []
    assert issues("scatter", ["customer", "gross", "refunds"], per_customer, ["refunds"], x="gross") == []
    assert issues("stat", ["net"], [[183000]], ["net"]) == []
    assert "3 or more x values" in issues("line", ["week", "net"], weeks[:2], ["net"])[0]
    assert "add up" in issues("waterfall", ["step", "net"], bridge[:3] + [["Sep net", 190000]], ["net"])[0]
    assert "non-negative" in issues("stacked_bar", ["step", "net"], bridge, ["net"])[0]
    assert "numeric x" in issues("scatter", ["customer", "gross", "refunds"], per_customer, ["refunds"])[0]
    assert '"stat"' in issues("bar", ["net"], [[183000]], ["net"])[0]
    assert "one result row" in issues("stat", ["week", "net"], weeks, ["net"])[0]


FAN_OUT = submit(fig("gross", SEP, OCT, 209000, "q1"), summary="Gross was 209000 cents.")


def test_fan_out_join_is_caught(db):
    """O3 has two refunds: joining refunds to orders counts its 5000 twice (209000 instead of 204000)."""
    run = chat("q", scripted(sql(NAIVE_JOIN), sql(MONTHLY), FAN_OUT, FAN_OUT), db_path=db)
    assert run["status"] == "unverified" and len(run["repairs"]) == 1
    figure = run["verification"]["figures"][0]
    assert (figure["status"], figure["expected_cents"]) == ("query_error", 204000)


def test_repair_round_fixes_a_wrong_figure_without_leaking_the_answer(db):
    fixed = submit(fig("gross", SEP, OCT, 204000, "q2"), summary="Gross was 204000 cents.")
    run = chat("q", scripted(sql(NAIVE_JOIN), sql(MONTHLY), FAN_OUT, fixed), db_path=db)
    feedback = tool_texts(run)[2]
    assert "does not apply the metric rules" in feedback and "204000" not in feedback
    assert run["status"] == "verified" and run["repairs"][0]["verification"]["figures"][0]["status"] == "query_error"


def test_figure_must_be_in_the_cited_result(db):
    cited_wrong = submit(fig("net", SEP, OCT, 183000, "q2"), summary="Net was 183000 cents.")
    run = chat("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), cited_wrong, cited_wrong), db_path=db)
    assert run["status"] == "unverified"
    assert run["verification"]["figures"][0]["status"] == "unsupported"


def test_prose_money_is_checked(db):
    dollars = submit(fig("net", SEP, OCT, 183000, "q1"), summary="Net fell by $85 or 9999 cents.")
    run = chat("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), dollars, dollars), db_path=db)
    issues = " ".join(run["verification"]["issues"])
    assert run["status"] == "unverified" and '"$"' in issues and "9999" in issues


def test_observed_finding_without_figures_is_accepted(db):
    """The user's v1 chat: a date-coverage finding is observed but has no money figure."""
    dates = submit(summary="Orders run from 2026-08-01 to 2026-10-01.")
    run = chat("What period does the data cover?", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), dates), db_path=db)
    assert run["status"] == "answered" and run["verification"]["ok"]


def test_write_is_rejected_and_failed_queries_count_toward_the_limit(db):
    before = sha(db)
    llm = scripted(sql("DELETE FROM refunds"), *[sql("SELEC oops")] * 5, sql(MONTHLY), GOOD, GOOD)
    run = chat("q", llm, db_path=db)
    assert sha(db) == before
    assert [q["error"] for q in run["queries"]] == ["rejected"] + ["sql"] * 5  # the 7th was not run
    assert run["limit_reached"] and run["status"] == "incomplete"
    assert llm.seen[-1]["tool_choice"] == force("submit_report")


def test_submit_needs_a_follow_up_query(db):
    run = chat("q", scripted(sql(MONTHLY), GOOD, sql(SEGMENT_REFUNDS), GOOD), db_path=db)
    assert run["status"] == "verified" and len(run["model_calls"]) == 5
    assert "follow-up" in tool_texts(run)[1]


def test_invalid_report_gets_a_repair_turn(db):
    run = chat("q", scripted(sql(MONTHLY), sql(SEGMENT_REFUNDS), tool("submit_report", {"summary": "x"}), GOOD), db_path=db)
    assert run["status"] == "verified"
    assert "findings" in tool_texts(run)[2]


def test_empty_result_and_model_failure_are_visible(db):
    run = chat("q", scripted(sql("SELECT * FROM refunds WHERE refund_date >= '2027-01-01'"), LLMError("timeout")), db_path=db)
    assert "empty result" in tool_texts(run)[0]
    assert run["status"] == "failed" and "timeout" in run["error"] and len(run["queries"]) == 1


def test_call_limit_ends_incomplete(db):
    llm = scripted(*[tool("drop_table", {})] * agent.MAX_CALLS)
    run = chat("q", llm, db_path=db)
    assert run["status"] == "incomplete" and run["report"] is None and llm.script == []
    assert "unknown tool" in tool_texts(run)[0]


def test_data_contract_violation_stops_before_any_model_call(db):
    with sqlite3.connect(db) as con:
        con.execute("INSERT INTO refunds VALUES ('RF99', 'O404', '2026-09-03', 100)")
    llm = scripted(GOOD)
    run = chat("q", llm, db_path=db)
    assert run["status"] == "failed" and "unknown order O404" in run["error"] and llm.seen == []


def test_replay_reproduces_and_detects_a_data_change(db, tmp_path):
    parent = chat("Why did net sales change?", scripted(*INVESTIGATION), db_path=db)
    run = chat("Which segment drove it?", scripted(*INVESTIGATION), parent, db_path=db)
    saved = json.loads(json.dumps(run))  # what a saved file holds
    new, diffs = replay(saved, parent, db_path=db)
    assert diffs == [] and new["status"] == "verified"
    assert {c["source"] for c in new["model_calls"]} == {"replay"}

    with sqlite3.connect(db) as con:  # one more September refund
        con.execute("INSERT INTO refunds VALUES ('RF99', 'O18', '2026-09-03', 100)")
    new, diffs = replay(saved, parent, db_path=db)
    assert {"db_sha changed since recording", "q1 result differs"} <= set(diffs)
    assert "status verified -> failed" in diffs  # the new data triggers a repair round the recording never had


def test_inspector_events_follow_the_real_path(db):
    events = []
    run = chat("Why did net sales change?", scripted(*INVESTIGATION), db_path=db, on_event=events.append)
    assert run["status"] == "verified"
    assert events[0] == {"kind": "run", "state": "start", "tools": ["run_sql", "submit_report"]}
    path = [e.get("node") or e.get("name") for e in events[1:-1]]
    assert path == ["start", "guard", "reason", "act", "run_sql", "reason", "act", "run_sql",
                    "reason", "act", "submit_report", "end"]
    assert events[-1] == {"kind": "run", "state": "end"}
    events.clear()
    chat("tell me a joke", Scripted(script=[verdict("off_topic")]), db_path=db, on_event=events.append)
    assert [e.get("node") for e in events if e["kind"] == "node"] == ["start", "guard", "end"]
