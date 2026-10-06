"""The investigation loop: the model chooses the queries; code runs them and checks the report.

  question -> model -run_sql-> gateway (read-only) -> result -> model -> ... -submit_report-> verify -> run
Bounded by MAX_QUERIES query attempts per question (errors count) and MAX_TURNS model calls.
A report that fails a check goes back to the model once (MAX_REPAIRS) with the problems listed.
The run dict is the one record of an investigation: it is saved, exported, shown and replayed.
Status: verified | unverified (a check failed) | incomplete (a limit stopped it) | failed (data or model error).
"""
import hashlib
import json
import secrets
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path

import openai
from pydantic import ValidationError

from investigator import calc, gateway
from investigator.llm import LLMError, ScriptedLLM
from investigator.report import ROOT, Report
from investigator.verify import verify

DB = ROOT / "data" / "investigation.sqlite"
PROMPT = ROOT / "prompts" / "system-v5.md"  # versions are immutable: saved runs replay with their own
MAX_QUERIES = 6
MAX_TURNS = 12
MAX_REPAIRS = 1

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
FORCE_SUBMIT = {"type": "function", "function": {"name": "submit_report"}}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def system_prompt(prompt_path=PROMPT) -> str:
    """Our instructions + the supplied business rules + the schema, so familiar metric names cannot
    override this company's definitions. The worked example is left out: it describes the seed rows only."""
    guide = Path(prompt_path).read_text().strip()
    rules = (ROOT / "data" / "starter" / "domain.md").read_text().split("## Worked example")[0].strip()
    schema = (ROOT / "data" / "starter" / "schema.sql").read_text().strip()
    return f"{guide}\n\n{rules}\n\n## Schema\n\n```sql\n{schema}\n```"


def parent_context(parent: dict) -> str:
    rep, ver = parent["report"] or {}, parent["verification"] or {}
    lines = [f"Earlier investigation {parent['run_id']} ({parent['status']}).",
             f"Earlier question: {parent['question']}",
             f"Earlier answer: {rep.get('summary', 'none, it ended without a report')}"]
    lines += [f"- {f['kind']}: {f['statement']}" for f in rep.get("findings", [])]
    lines += [f"- assumption: {a}" for a in rep.get("assumptions", [])]
    lines += [f"- verification issue: {i}" for i in ver.get("issues", [])]
    lines.append("Earlier queries (reuse what worked; cite only the queries you run now):")
    lines += [f"- {q['purpose']}" + (f" (failed: {q['error']})" if q["error"] else "") + f"\n{q['sql']}"
              for q in parent["queries"]]
    return "\n".join(lines)


def investigate(question: str, llm, db_path=DB, parent: dict | None = None, prompt_path=PROMPT,
                max_queries: int = MAX_QUERIES, max_turns: int = MAX_TURNS, on_step=None) -> dict:
    """on_step(run), if given, is called after every tool call, so a UI can show progress."""
    prompt = system_prompt(prompt_path)
    run = {
        "run_id": datetime.now(UTC).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "question": question,
        "parent_run_id": parent["run_id"] if parent else None,
        "status": None, "error": None, "limit_reached": False,
        "config": {**llm.config, "prompt": Path(prompt_path).resolve().relative_to(ROOT).as_posix(),
                   "prompt_sha": sha(prompt.encode()), "db_sha": sha(Path(db_path).read_bytes()),
                   "max_queries": max_queries, "max_turns": max_turns, "max_repairs": MAX_REPAIRS,
                   "max_rows": gateway.MAX_ROWS, "timeout_s": gateway.TIMEOUT_S},
        "data_check": None, "report": None, "verification": None, "repairs": [],
        "queries": [], "model_calls": [],
        "messages": [{"role": "system", "content": prompt},
                     {"role": "user", "content": f"{parent_context(parent)}\n\nFollow-up question: {question}"
                      if parent else question}],
    }
    data = calc.load_db(db_path)
    run["data_check"] = calc.validate(data)
    if run["data_check"]["errors"]:
        return _end(run, "failed", "data contract violated: " + "; ".join(run["data_check"]["errors"]))

    try:
        for _ in range(max_turns):
            call = llm.complete(run["messages"], TOOLS, FORCE_SUBMIT if run["limit_reached"] else "required")
            run["model_calls"].append(call)
            run["messages"].append(call["message"])
            tool_calls = call["message"].get("tool_calls") or []
            if not tool_calls:
                run["messages"].append({"role": "user", "content": "Reply with a tool call: run_sql, or submit_report when done."})
            for tc in tool_calls:
                run["messages"].append({"role": "tool", "tool_call_id": tc["id"],
                                        "content": _handle(run, tc, data, db_path, max_queries)})
                if on_step:
                    on_step(run)
                if run["report"]:
                    if run["limit_reached"] or not _followed_up(run):
                        return _end(run, "incomplete", "stopped by the query limit, so the report is partial")
                    return _end(run, "verified" if run["verification"]["ok"] else "unverified")
    except LLMError as e:
        return _end(run, "failed", f"model call failed: {e}")
    return _end(run, "incomplete", f"no accepted report after {max_turns} model calls")


def _end(run: dict, status: str, error: str | None = None) -> dict:
    run["status"], run["error"] = status, error
    return run


def _followed_up(run: dict) -> bool:
    """Successful queries from at least two model calls: the later one was chosen after seeing a result."""
    return len({q["call"] for q in run["queries"] if q["error"] is None}) >= 2


def _handle(run: dict, tc: dict, data: dict, db_path, max_queries: int) -> str:
    name = tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"])
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        return "Error: tool arguments must be a JSON object. Nothing was run."
    if name == "run_sql":
        return _query(run, args, db_path, max_queries)
    if name == "submit_report":
        return _submit(run, args, data, max_queries)
    return f"Error: unknown tool {name!r}. Use run_sql or submit_report."


def _query(run: dict, args: dict, db_path, max_queries: int) -> str:
    sql = args.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return 'Error: run_sql needs {"purpose": "...", "sql": "SELECT ..."}. Nothing was run.'
    if len(run["queries"]) >= max_queries:
        run["limit_reached"] = True
        return (f"Not run: all {max_queries} query attempts are used. Call submit_report with what you have "
                "and put what is missing in open_questions.")
    r = gateway.run_sql(db_path, sql)
    q = {"id": f"q{len(run['queries']) + 1}", "purpose": str(args.get("purpose", "")),
         "call": len(run["model_calls"]) - 1, **r.to_dict()}
    run["queries"].append(q)
    out = {"query_id": q["id"], "attempts_used": f"{len(run['queries'])} of {max_queries}"}
    if r.error:
        out |= {"error": r.error, "message": r.message}
    else:
        out |= {"columns": r.columns, "rows": r.rows, "truncated": r.truncated}
        if not r.rows:
            out["note"] = "empty result: no rows matched"
    if len(run["queries"]) == max_queries:
        out["limit"] = "that was the last query attempt: call submit_report next"
    return json.dumps(out)


def _submit(run: dict, args: dict, data: dict, max_queries: int) -> str:
    if not _followed_up(run) and len(run["queries"]) < max_queries:
        return ("Not accepted yet: use a query result to choose at least one follow-up query, "
                "for example gross sales vs refunds or a segment breakdown, then submit.")
    try:
        report = Report.model_validate(args)
    except ValidationError as e:
        return "Not accepted, fix the report: " + "; ".join(
            f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
    rep = report.model_dump()
    if rep["chart"] is None:
        del rep["chart"]  # absent = no chart, so runs saved before charts existed replay unchanged
    verification = verify(rep, run["queries"], data)
    if not verification["ok"] and len(run["repairs"]) < MAX_REPAIRS:
        run["repairs"].append({"report": rep, "verification": verification})
        return ("Not accepted: the checks found problems. Fix them and call submit_report again; you may run "
                "more queries if attempts remain.\n- " + "\n- ".join(verification["issues"]))
    run["report"], run["verification"] = rep, verification
    return "Report accepted."


def replay(run: dict, parent: dict | None = None, db_path=DB) -> tuple[dict, list]:
    """Re-run a saved investigation on its recorded model responses: no API key; SQL and checks run again."""
    model = {k: run["config"].get(k) for k in ("base_url", "model", "reasoning_effort")}
    new = investigate(run["question"], ScriptedLLM(run["model_calls"], "replay", model), db_path, parent,
                      ROOT / run["config"]["prompt"], run["config"]["max_queries"], run["config"]["max_turns"])
    return new, diff(run, new)


def diff(old: dict, new: dict) -> list:
    """What differs between a saved run and its replay. Empty means it reproduced."""
    out = [f"{k} changed since recording" for k in ("prompt_sha", "db_sha") if old["config"][k] != new["config"][k]]
    for a, b in zip_longest(old["queries"], new["queries"]):
        if a is None or b is None or any(a[k] != b[k] for k in ("sql", "columns", "rows", "truncated", "error")):
            out.append(f"{(a or b)['id']} result differs")
    if old["status"] != new["status"]:
        out.append(f"status {old['status']} -> {new['status']}")
    out += [f"{k} differs" for k in ("report", "verification") if old[k] != new[k]]
    return out
