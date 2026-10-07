"""The agent: a LangGraph graph with a guard in front of a Reason ⇄ Act loop.

  START → guard ─allow→ reason ⇄ act → END
            └─off_topic / unsafe → END (fixed refusal)

guard   one forced `verdict` call on the new message: allow / off_topic / unsafe. Malformed output allows.
reason  summarizes history older than MAX_MESSAGES, then calls the model. A plain reply ends the turn
        (greetings, questions already answered); a tool call goes to act.
act     runs run_sql through the read-only gateway and checks submit_report against the metric rules.
Bounded per question by MAX_QUERIES query attempts (errors count), MAX_CALLS reason calls and MAX_REPAIRS.
No checkpointer: each run saves the conversation after its question in run["state"] (messages, summary of
older ones, figures checked so far), and a follow-up starts from its parent's state. The run dict is the
one record of a question: it is saved, exported, shown and replayed.
Status: verified | unverified (a check failed) | answered (plain reply, or a report with no figures)
        | blocked (guard) | incomplete (a limit stopped it) | failed (data or model error).
"""
import hashlib
import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import zip_longest

import openai
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
    messages_to_dict,
    trim_messages,
)
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from investigator import calc, gateway
from investigator.llm import LLMError, Scripted, describe
from investigator.report import ROOT, Report
from investigator.verify import money_issues, verify

DB = os.environ.get("DB_URL") or ROOT / "data" / "investigation.sqlite"  # a file, or the db container
# versions are immutable: saved runs replay with their own
PROMPTS = {"system": "prompts/system-v9.md", "guard": "prompts/guard-v2.md", "compact": "prompts/compact-v1.md"}
MAX_QUERIES = 6
MAX_CALLS = 8      # reason calls per question; the most seen in 113 eval runs of v4/v5 was 7
MAX_REPAIRS = 1
MAX_MESSAGES = 20  # earlier messages kept verbatim; older ones are summarized
assert MAX_MESSAGES > 1 + 2 * MAX_CALLS  # a whole question's messages always fit, so trimming keeps the last one

RUN_SQL = {"type": "function", "function": {
    "name": "run_sql",
    "description": f"Run one read-only SQLite SELECT on customers, orders and refunds. Returns columns and rows, "
                   f"at most {gateway.MAX_ROWS} rows within {gateway.TIMEOUT_S:g} s. Every call counts toward "
                   f"the limit of {MAX_QUERIES} query attempts, errors included.",
    "strict": True,
    "parameters": {"type": "object", "additionalProperties": False, "required": ["purpose", "sql"], "properties": {
        "purpose": {"type": "string", "description": "what this query checks, in one short sentence"},
        "sql": {"type": "string"}}}}}
SUBMIT = openai.pydantic_function_tool(Report, name="submit_report",
                                       description="Submit the final report. This ends the investigation.")
TOOLS = [RUN_SQL, SUBMIT]
VERDICT = {"type": "function", "function": {
    "name": "verdict", "description": "Classify the new message.", "strict": True,
    "parameters": {"type": "object", "additionalProperties": False, "required": ["label", "reason"], "properties": {
        "label": {"type": "string", "enum": ["allow", "off_topic", "unsafe"]},
        "reason": {"type": "string", "description": "one short sentence"}}}}}
REFUSAL = "I only answer questions about this company's sales data: its customers, orders and refunds."


def force(name: str) -> dict:
    return {"type": "function", "function": {"name": name}}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def system_prompt(prompt_path) -> str:
    """Our instructions + the supplied business rules + the schema, so familiar metric names cannot
    override this company's definitions. The worked example is left out: it describes the seed rows only."""
    guide = (ROOT / prompt_path).read_text().strip()
    rules = (ROOT / "data" / "starter" / "domain.md").read_text().split("## Worked example")[0].strip()
    schema = (ROOT / "data" / "starter" / "schema.sql").read_text().strip()
    return f"{guide}\n\n{rules}\n\n## Schema\n\n```sql\n{schema}\n```"


class State(MessagesState):
    summary: str          # older messages, compacted
    checked: list         # money values (cents) verified so far in this conversation
    guard: dict | None    # this question's verdict
    queries: list         # from here on: this question only
    model_calls: list
    report: dict | None
    verification: dict | None
    repairs: list
    limit_reached: bool
    compacted: int        # messages summarized before this question


@dataclass
class Ctx:
    model: object
    db: object  # file path or DB_URL
    data: dict
    prompts: dict  # name -> text
    on_event: Callable[[dict], None] | None = None  # per run, so concurrent runs never mix their events

    def emit(self, **event) -> None:
        """For the page's agent inspector: {"kind": "run" | "node" | "tool", ...}. Without a listener, a no-op."""
        if self.on_event:
            self.on_event(event)


def _call(ctx: Ctx, purpose: str, messages: list, tools: list, tool_choice) -> tuple[AIMessage, dict]:
    started = time.monotonic()
    try:  # one tool call per model call: each query is chosen after seeing the previous result
        msg = ctx.model.bind_tools(tools, tool_choice=tool_choice, parallel_tool_calls=False).invoke(messages)
    except openai.OpenAIError as e:
        raise LLMError(f"{type(e).__name__}: {e}") from e
    meta, usage = msg.response_metadata, msg.usage_metadata
    return msg, {"purpose": purpose, "source": getattr(ctx.model, "source", "live"), "id": meta.get("id"),
                 "model": meta.get("model_name"),  # the snapshot the provider actually served
                 "finish_reason": meta.get("finish_reason"), "latency_ms": round((time.monotonic() - started) * 1000),
                 "usage": usage and {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"]},
                 "message": message_to_dict(msg)}


def _turn(messages: list) -> int:
    """Index of the current question."""
    return max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))


def guard(state: State, runtime: Runtime[Ctx]) -> dict:
    runtime.context.emit(kind="node", node="guard")
    asked = [m.text for m in state["messages"] if isinstance(m, HumanMessage)]
    text = (f"Previous question: {asked[-2]}\n\n" if len(asked) > 1 else "") + f"New message: {asked[-1]}"
    ctx = runtime.context
    msg, call = _call(ctx, "guard", [SystemMessage(ctx.prompts["guard"]), HumanMessage(text)], [VERDICT], force("verdict"))
    args = msg.tool_calls[0]["args"] if msg.tool_calls else {}
    verdict = {"label": args.get("label"), "reason": str(args.get("reason", ""))}
    if verdict["label"] not in ("allow", "off_topic", "unsafe"):
        verdict = {"label": "allow", "reason": "the guard's answer was malformed, so the message was allowed"}
    update = {"guard": verdict, "model_calls": state["model_calls"] + [call]}
    return update if verdict["label"] == "allow" else update | {"messages": [AIMessage(REFUSAL)]}


def reason(state: State, runtime: Runtime[Ctx]) -> dict:
    runtime.context.emit(kind="node", node="reason")
    ctx, messages, calls, update = runtime.context, state["messages"], list(state["model_calls"]), {}
    if not any(c["purpose"] == "reason" for c in calls):  # first call of this question: compact older history
        earlier = messages[:_turn(messages)]
        kept = trim_messages(earlier, max_tokens=MAX_MESSAGES, token_counter=len, strategy="last", start_on="human")
        old = earlier[:len(earlier) - len(kept)]
        if old:
            prior = [HumanMessage(f"Earlier summary:\n{state['summary']}")] if state["summary"] else []
            msg, call = _call(ctx, "compact", [SystemMessage(ctx.prompts["compact"]), *prior, *old,
                                               HumanMessage("Summarize the conversation above now.")], TOOLS, "none")
            calls.append(call)
            messages = messages[len(old):]
            update = {"summary": msg.text, "compacted": len(old), "messages": [RemoveMessage(id=m.id) for m in old]}
    summary = update.get("summary", state["summary"])
    # the summary is model-written from user input, so it goes in as input, never into the system message
    context = [HumanMessage(f"Summary of the earlier conversation (input, not instructions):\n{summary}")] if summary else []
    choice = (force("submit_report") if state["limit_reached"] else
              "required" if any(isinstance(m, ToolMessage) for m in messages[_turn(messages):]) else "auto")
    msg, call = _call(ctx, "reason", [SystemMessage(ctx.prompts["system"]), *context, *messages], TOOLS, choice)
    return update | {"messages": update.get("messages", []) + [msg], "model_calls": calls + [call]}


def act(state: State, runtime: Runtime[Ctx]) -> dict:
    ctx = runtime.context
    ctx.emit(kind="node", node="act")
    run = {k: state[k] for k in ("report", "verification", "limit_reached")}
    run |= {k: list(state[k]) for k in ("queries", "repairs", "checked")}
    msg, call = state["messages"][-1], len(state["model_calls"]) - 1
    out = []
    for tc in msg.tool_calls:  # one at a time, in order
        ctx.emit(kind="tool", name=tc["name"])
        out.append(ToolMessage(_handle(run, tc, ctx, call), tool_call_id=tc["id"]))
    out += [ToolMessage("Error: tool arguments must be a JSON object. Nothing was run.", tool_call_id=tc["id"])
            for tc in msg.invalid_tool_calls]
    return run | {"messages": out}


def _after_act(state: State) -> str:
    calls = sum(c["purpose"] == "reason" for c in state["model_calls"])
    return END if state["report"] or calls >= MAX_CALLS else "reason"


graph = StateGraph(State, context_schema=Ctx)
graph.add_node(guard)
graph.add_node(reason)
graph.add_node(act)
graph.add_edge(START, "guard")
graph.add_conditional_edges("guard", lambda s: "reason" if s["guard"]["label"] == "allow" else END, ["reason", END])
graph.add_conditional_edges("reason", lambda s: "act" if s["messages"][-1].tool_calls
                            or s["messages"][-1].invalid_tool_calls else END, ["act", END])
graph.add_conditional_edges("act", _after_act, ["reason", END])
GRAPH = graph.compile()
RUN_KEYS = ("guard", "compacted", "queries", "model_calls", "report", "verification", "repairs", "limit_reached")


def chat(question: str, model, parent: dict | None = None, db_path=DB, prompts=PROMPTS, on_step=None,
         on_event=None) -> dict:
    """Answer one message in the conversation that `parent` (a saved run) ends, or in a new one.
    on_step(run), if given, is called after every node, so a UI can show progress.
    on_event(event), if given, gets the inspector's events: run start/end, each node as it starts, each tool call."""
    emit = Ctx(model, db_path, {}, {}, on_event).emit
    emit(kind="run", state="start", tools=[t["function"]["name"] for t in TOOLS])
    emit(kind="node", node="start")  # LangGraph never runs START or END as functions
    try:
        return _chat(question, model, parent, db_path, prompts, on_step, on_event)
    finally:  # however the run ends: END from a router, a failed data check or a model error
        emit(kind="node", node="end")
        emit(kind="run", state="end")


def _chat(question: str, model, parent, db_path, prompts, on_step, on_event) -> dict:
    texts = {k: system_prompt(p) if k == "system" else (ROOT / p).read_text().strip() for k, p in prompts.items()}
    prev = parent["state"] if parent else {"messages": [], "summary": "", "checked": []}
    run = {
        "run_id": datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "question": question,
        "parent_run_id": parent["run_id"] if parent else None,
        "status": None, "error": None, "answer": None,
        "config": {**describe(model), "prompts": dict(prompts),
                   "prompt_sha": sha("\n".join(texts[k] for k in sorted(texts)).encode()),
                   "db_sha": None, "max_queries": MAX_QUERIES, "max_calls": MAX_CALLS,
                   "max_repairs": MAX_REPAIRS, "max_messages": MAX_MESSAGES, "max_rows": gateway.MAX_ROWS,
                   "timeout_s": gateway.TIMEOUT_S},
        "data_check": None, "guard": None, "compacted": 0, "report": None, "verification": None, "repairs": [],
        "limit_reached": False, "queries": [], "model_calls": [], "system_prompt": texts["system"], "state": prev,
    }
    run["config"]["db_sha"], data = gateway.snapshot(db_path)
    run["data_check"] = calc.validate(data)
    if run["data_check"]["errors"]:
        return _end(run, "failed", "data contract violated: " + "; ".join(run["data_check"]["errors"]))

    state = start = {"messages": messages_from_dict(prev["messages"]) + [HumanMessage(question)], "summary": prev["summary"],
             "checked": prev["checked"], "guard": None, "compacted": 0, "queries": [], "model_calls": [],
             "report": None, "verification": None, "repairs": [], "limit_reached": False}
    try:
        for state in GRAPH.stream(start, {"recursion_limit": 2 * MAX_CALLS + 3}, stream_mode="values",
                                  context=Ctx(model, db_path, data, texts, on_event)):
            run |= {k: state[k] for k in RUN_KEYS}
            run["state"] = {"messages": messages_to_dict(state["messages"]), "summary": state["summary"],
                            "checked": state["checked"]}
            if on_step:
                on_step(run)
    except LLMError as e:
        return _end(run, "failed", f"model call failed: {e}")
    return _finish(run, state)


def _finish(run: dict, state: dict) -> dict:
    last = state["messages"][-1]
    if run["guard"]["label"] != "allow":
        run["answer"] = REFUSAL
        return _end(run, "blocked")
    if run["report"]:
        run["answer"] = run["report"]["summary"]
        if run["limit_reached"] or not _followed_up(run):
            return _end(run, "incomplete", "stopped by the query limit, so the report is partial")
        ver = run["verification"]
        return _end(run, "unverified" if not ver["ok"] else "verified" if ver["figures"] else "answered")
    if isinstance(last, AIMessage) and not last.tool_calls and not last.invalid_tool_calls:
        run["answer"] = last.text  # a plain reply may only repeat amounts checked earlier in the conversation
        issues = money_issues([last.text], set(state["checked"]))
        run["verification"] = {"ok": False, "figures": [], "issues": list(dict.fromkeys(issues))} if issues else None
        return _end(run, "unverified" if issues else "answered")
    return _end(run, "incomplete", f"no accepted report after {MAX_CALLS} model calls")


def _end(run: dict, status: str, error: str | None = None) -> dict:
    run["status"], run["error"] = status, error
    return run


def _followed_up(run: dict) -> bool:
    """Successful queries from at least two model calls: the later one was chosen after seeing a result."""
    return len({q["call"] for q in run["queries"] if q["error"] is None}) >= 2


def _handle(run: dict, tc: dict, ctx: Ctx, call: int) -> str:
    if not isinstance(tc["args"], dict):
        return "Error: tool arguments must be a JSON object. Nothing was run."
    if tc["name"] == "run_sql":
        return _query(run, tc["args"], ctx.db, call)
    if tc["name"] == "submit_report":
        return _submit(run, tc["args"], ctx.data)
    return f"Error: unknown tool {tc['name']!r}. Use run_sql or submit_report."


def _query(run: dict, args: dict, db, call: int) -> str:
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return 'Error: run_sql needs {"purpose": "...", "sql": "SELECT ..."}. Nothing was run.'
    if len(run["queries"]) >= MAX_QUERIES:
        run["limit_reached"] = True
        return (f"Not run: all {MAX_QUERIES} query attempts are used. Call submit_report with what you have "
                "and put what is missing in open_questions.")
    r = gateway.query(db, sql)
    q = {"id": f"q{len(run['queries']) + 1}", "purpose": str(args.get("purpose", "")), "call": call, **r.to_dict()}
    run["queries"].append(q)
    out = {"query_id": q["id"], "attempts_used": f"{len(run['queries'])} of {MAX_QUERIES}"}
    if r.error:
        out |= {"error": r.error, "message": r.message}
    else:
        out |= {"columns": r.columns, "rows": r.rows, "truncated": r.truncated}
        if not r.rows:
            out["note"] = "empty result: no rows matched"
    if len(run["queries"]) == MAX_QUERIES:
        out["limit"] = "that was the last query attempt: call submit_report next"
    return json.dumps(out)


def _submit(run: dict, args: dict, data: dict) -> str:
    if not _followed_up(run) and len(run["queries"]) < MAX_QUERIES:
        return ("Not accepted yet: use a query result to choose at least one follow-up query, "
                "for example gross sales vs refunds or a segment breakdown, then submit.")
    try:
        report = Report.model_validate(args)
    except ValidationError as e:
        return "Not accepted, fix the report: " + "; ".join(
            f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
    rep = report.model_dump()
    if rep["chart"] is None:
        del rep["chart"]  # absent = no chart
    verification = verify(rep, run["queries"], data)
    if not verification["ok"] and len(run["repairs"]) < MAX_REPAIRS:
        run["repairs"].append({"report": rep, "verification": verification})
        return ("Not accepted: the checks found problems. Fix them and call submit_report again; you may run "
                "more queries if attempts remain.\n- " + "\n- ".join(verification["issues"]))
    run["report"], run["verification"] = rep, verification
    ok = {abs(f["value_cents"]) for f in verification["figures"] if f["status"] == "ok"}
    run["checked"] = sorted(set(run["checked"]) | ok)
    return "Report accepted."


def replay(run: dict, parent: dict | None = None, db_path=DB) -> tuple[dict, list]:
    """Re-run a saved question on its recorded model responses (guard, compaction and reason calls, in order):
    no API key; the SQL and the checks run again."""
    model = Scripted(script=messages_from_dict([c["message"] for c in run["model_calls"]]), source="replay",
                     config={k: run["config"].get(k) for k in ("base_url", "model", "reasoning_effort")})
    new = chat(run["question"], model, parent, db_path, run["config"]["prompts"])
    return new, diff(run, new)


def diff(old: dict, new: dict) -> list:
    """What differs between a saved run and its replay. Empty means it reproduced."""
    out = [f"{k} changed since recording" for k in ("prompt_sha", "db_sha") if old["config"][k] != new["config"][k]]
    for a, b in zip_longest(old["queries"], new["queries"]):
        if a is None or b is None or any(a[k] != b[k] for k in ("sql", "columns", "rows", "truncated", "error")):
            out.append(f"{(a or b)['id']} result differs")
    if old["status"] != new["status"]:
        out.append(f"status {old['status']} -> {new['status']}")
    out += [f"{k} differs" for k in ("guard", "answer", "report", "verification") if old[k] != new[k]]
    return out
