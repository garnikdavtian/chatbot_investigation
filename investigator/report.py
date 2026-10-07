"""The report contract the model must fill, and run storage and export.

The model never formats money: it writes integer cents ("183000 cents") and this module renders
dollars, because a model run in the selection probe mixed cents and dollars in its prose.
"""
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: Literal["gross", "refunds", "net"]
    period_start: str = Field(description="YYYY-MM-DD, included")
    period_end_exclusive: str = Field(description="YYYY-MM-DD, excluded")
    segment: str | None = Field(description="customer segment, or null for all customers")
    value_cents: int = Field(description="integer cents, exactly as shown in a cell of the cited query result")
    query_id: str = Field(description="id of the query whose result shows this value, e.g. q2")


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["observed", "inferred", "unknown"] = Field(
        description="observed: shown by a query result; inferred: drawn from observed figures; "
                    "unknown: the data cannot establish it")
    statement: str = Field(description='one or two sentences; write money as "<integer> cents"')
    metrics: list[Metric] = Field(description="the figures this finding relies on")


class Chart(BaseModel):
    """What to draw, not the numbers: the UI draws the rows of the cited query's executed result."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["bar", "line"] = Field(description="bar to compare categories or segments; line for a trend "
                                                     "over three or more ordered periods")
    title: str
    query_id: str = Field(description="id of the successful query whose result rows the chart draws")
    x: str = Field(description="result column for the x axis, e.g. a month or a segment")
    y: list[str] = Field(description="1 to 4 numeric result columns, one series each")
    group: str | None = Field(description="optional result column whose values (at most 4) split rows into "
                                          "series, e.g. segment; then give exactly one y column. null otherwise")
    unit: Literal["cents", "count"] = Field(description="unit of the y values")


class Report(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(description='2-4 sentences that answer the question; write money as "<integer> cents"')
    findings: list[Finding]
    assumptions: list[str] = Field(description="periods, units and every other assumption you made")
    open_questions: list[str] = Field(description="what the data cannot answer, and follow-ups worth running")
    chart: Chart | None = Field(default=None, description="a chart of one query result, only when the question "
                                                          "asks to visualize, plot or chart; else null")


def fmt_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def prose(text: str) -> str:
    """Render '183000 cents' as '$1,830.00'."""
    return re.sub(r"(-?\d[\d,]*)\s*cents\b", lambda m: fmt_cents(int(m.group(1).replace(",", ""))), text)


def save(run: dict, runs_dir: Path = RUNS) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run['run_id']}.json"
    path.write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n")
    path.with_suffix(".md").write_text(to_markdown(run))
    return path


RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")


def load(run_id: str, runs_dir: Path = RUNS) -> dict:
    if not RUN_ID.fullmatch(run_id):  # ids reach here from the CLI and from URLs: no path tricks
        raise FileNotFoundError(f"not a run id: {run_id!r}")
    return json.loads((runs_dir / f"{run_id}.json").read_text())


STATUS = {"verified": "✅ verified", "unverified": "⚠️ unverified", "answered": "💬 answered", "blocked": "🚫 blocked",
          "incomplete": "⏸ incomplete", "failed": "❌ failed"}
FIGURE = {"ok": "✅", "query_error": "❌ query computed a wrong value", "unsupported": "⚠️ not in the cited result",
          "wrong": "❌ wrong and not in the cited result"}


def _table(columns: list, rows: list) -> str:
    if not columns:
        return "_no columns_"
    lines = ["| " + " | ".join(map(str, columns)) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def to_markdown(run: dict) -> str:
    """A self-contained record another person can check: question, definitions, assumptions,
    queries, results and explanation."""
    cfg, rep, ver, check = run["config"], run["report"], run["verification"] or {}, run["data_check"] or {}
    served = sorted({c["model"] for c in run["model_calls"] if c.get("model")})
    sources = ", ".join(sorted({c["source"] for c in run["model_calls"]})) or "none"
    purposes = ", ".join(f"{n} {p}" for p in ("guard", "compact", "reason")
                         if (n := sum(c["purpose"] == p for c in run["model_calls"])))
    status = STATUS.get(run["status"], run["status"])
    status += f" — {run['error']}" if run["error"] else ""
    status += f" (after {len(run['repairs'])} repair round)" if run["repairs"] else ""
    model = f"`{cfg.get('model')}`"
    model += f" served as `{', '.join(served)}`" if served and served != [cfg.get("model")] else ""
    out = [f"# Investigation {run['run_id']}", "",
           f"**Question:** {run['question']}  ",
           f"**Status:** {status}  ",
           (f"**Model:** {model}, reasoning effort {cfg.get('reasoning_effort')}; {len(run['model_calls'])} model calls "
            f"({purposes}; {sources}); {len(run['queries'])} of {cfg['max_queries']} query attempts  "),
           (f"**Data:** db `{cfg['db_sha']}`, contract check: {len(check.get('errors', []))} errors, "
            f"{len(check.get('warnings', []))} warnings · **Prompts:** {', '.join(f'`{p}`' for p in cfg['prompts'].values())} "
            f"`{cfg['prompt_sha']}` · "
            f"{run['created_at']}  ")]
    if run["parent_run_id"]:
        out.append(f"**Follow-up of:** [{run['parent_run_id']}]({run['parent_run_id']}.md)  ")
    if run["guard"]:
        out.append(f"**Guard:** {run['guard']['label']} — {run['guard']['reason']}  ")
    if run["compacted"]:
        out.append(f"**Memory:** {run['compacted']} earlier messages were summarized before this question")
    if not rep and run["answer"] is not None:
        out += ["", "## Answer", "", prose(run["answer"])]
        out += ["", "## Verification issues", ""] + [f"- {prose(i)}" for i in ver.get("issues", [])] if ver else []
    if rep:
        out += ["", "## Answer", "", prose(rep["summary"])]
        if rep.get("chart"):
            c = rep["chart"]
            series = ", ".join(c["y"]) + (f" by {c['group']}" if c["group"] else "")
            out += ["", "## Chart", "", (f"{c['kind']} chart \"{c['title']}\" of [{c['query_id']}]: x = {c['x']}, "
                                         f"y = {series} ({c['unit']}). The web UI draws it from that query's rows.")]
        out += ["", "## Findings", ""]
        for i, f in enumerate(rep["findings"]):
            out.append(f"- **{f['kind']}** — {prose(f['statement'])}")
            for fig in (x for x in ver.get("figures", []) if x["finding"] == i):
                seg = f", {fig['segment']}" if fig["segment"] else ""
                line = (f"  - {fig['metric']} {fig['period_start']} to {fig['period_end_exclusive']} (excl.){seg}: "
                        f"{fmt_cents(fig['value_cents'])} [{fig['query_id']}] {FIGURE[fig['status']]}")
                if fig["status"] != "ok" and fig["expected_cents"] is not None:
                    line += f" (rules give {fmt_cents(fig['expected_cents'])})"
                out.append(line)
        if ver.get("issues"):
            out += ["", "## Verification issues", ""] + [f"- {prose(i)}" for i in ver["issues"]]
        for title, items in (("Assumptions", rep["assumptions"]), ("Open questions", rep["open_questions"])):
            if items:
                out += ["", f"## {title}", ""] + [f"- {prose(x)}" for x in items]
    for n, r in enumerate(run["repairs"], 1):
        out += ["", f"## Repair round {n}", "", "The first report failed these checks and went back to the model:", ""]
        out += [f"- {prose(i)}" for i in r["verification"]["issues"]]
    out += ["", "## Queries", ""] if run["queries"] else []
    for q in run["queries"]:
        state = f"{q['error']}: {q['message']}" if q["error"] else f"{len(q['rows'])} rows"
        state += ", truncated" if q["truncated"] else ""
        out += [f"### {q['id']} — {q['purpose']} ({state}, {q['elapsed_ms']} ms)", "", "```sql", q["sql"], "```", ""]
        if not q["error"]:
            out += [_table(q["columns"], q["rows"]), ""]
    summary = "System prompt: instructions, business rules (data/starter/domain.md) and schema"
    out += ["## Definitions given to the model", "", f"<details><summary>{summary}</summary>", "",
            run["system_prompt"], "", "</details>", "",
            "## Reproduce", "", f"`uv run python -m investigator replay {run['run_id']}` (recorded responses, no API key)", ""]
    return "\n".join(out)
