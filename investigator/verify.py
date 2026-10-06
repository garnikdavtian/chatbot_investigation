"""Check a submitted report against the executed queries and the metric rules.

Two independent checks per figure:
  grounded - the value appears in the result of the query it cites;
  correct  - calc.py, an independent implementation of domain.md, gives the same value.
  grounded + correct = ok; grounded only = query_error (e.g. a join that repeats orders);
  correct only = unsupported (right number, but no cited evidence); neither = wrong.
Issue texts go back to the model in the repair round, so they say what failed but never the
expected value: the model must fix its query, not copy the answer. Expected values stay in
`figures`, for people.
"""
import re

from investigator import calc

CENTS = re.compile(r"(-?\d[\d,]*)\s*cents\b")
WHY = {"query_error": "is in the cited result, but that query does not apply the metric rules for this metric, "
                      "period and segment",
       "unsupported": "is not in the result of the cited query",
       "wrong": "is not in the cited result and does not match the metric rules"}


def verify(report: dict, queries: list, data: dict) -> dict:
    by_id = {q["id"]: q for q in queries}
    figures, issues = [], []
    for i, finding in enumerate(report["findings"]):
        if finding["kind"] == "observed" and not finding["metrics"]:
            issues.append(f"finding {i + 1} is marked observed but has no figures")
        for m in finding["metrics"]:
            q = by_id.get(m["query_id"])
            grounded = bool(q) and q["error"] is None and any(
                cell == m["value_cents"] for row in q["rows"] for cell in row)
            start, end = m["period_start"], m["period_end_exclusive"]
            expected = (calc.totals(data, start, end, m["segment"])[f"{m['metric']}_cents"]
                        if calc.is_iso_day(start) and calc.is_iso_day(end) else None)
            correct = expected == m["value_cents"]
            status = ("ok" if correct else "query_error") if grounded else ("unsupported" if correct else "wrong")
            figures.append({"finding": i, **m, "expected_cents": expected, "status": status})
            if status != "ok":
                why = WHY[status] if expected is not None else "has a period date that is not YYYY-MM-DD"
                issues.append(f"finding {i + 1}: {m['metric']} {start} to {end} (excluded), "
                              f"{m['segment'] or 'all customers'} = {m['value_cents']} cents [{m['query_id']}] {why}")

    # ponytail: only amounts written as "N cents" are checked; bare numbers are not. Upgrade path:
    # have the model reference figures by id and render the numbers into the text in code.
    ok_values = {abs(f["value_cents"]) for f in figures if f["status"] == "ok"}
    allowed = ok_values | {abs(a - b) for a in ok_values for b in ok_values}
    for text in [report["summary"]] + [f["statement"] for f in report["findings"]]:
        if "$" in text:
            issues.append('the text writes money with "$"; write amounts as integer cents')
        for raw in CENTS.findall(text):
            if abs(int(raw.replace(",", ""))) not in allowed:
                issues.append(f"the text mentions {raw} cents, which is not a checked figure or the difference "
                              "of two: attach it to a finding with the query that shows it, or remove it")

    if report.get("chart"):
        issues += _chart_issues(report["chart"], by_id)
    if not figures:
        issues.append("the report has no figures to check")
    return {"ok": not issues, "figures": figures, "issues": list(dict.fromkeys(issues))}


def _chart_issues(c: dict, by_id: dict) -> list:
    """The chart draws the cited result's rows as they are, so check only that they can be drawn."""
    q = by_id.get(c["query_id"])
    if not q or q["error"] is not None or not q["rows"]:
        return [f"the chart cites {c['query_id']}, which has no result rows to draw"]
    missing = [col for col in [c["x"], *c["y"], c["group"]] if col and col not in q["columns"]]
    if missing:
        return [f"the chart names columns {missing} that are not in the result of {c['query_id']}"]
    if not 1 <= len(c["y"]) <= 4 or (c["group"] and len(c["y"]) != 1):
        return ["the chart needs 1 to 4 y columns, or exactly one y column with group"]
    if c["group"] and len({row[q["columns"].index(c["group"])] for row in q["rows"]}) > 4:
        return [f"the chart's group column {c['group']} has more than 4 values; a chart shows at most 4 series"]
    ys = [q["columns"].index(col) for col in c["y"]]
    if not all(isinstance(row[i], int | float) for row in q["rows"] for i in ys):
        return [f"the chart's y columns must be numbers in every row of {c['query_id']}"]
    if len(q["rows"]) == 1 and len(c["y"]) == 1:
        return [("the chart would draw a single value, which compares nothing: set chart to null and say in "
                 "assumptions why a chart does not help")]
    if len(q["rows"]) > 60:
        return [f"the chart would draw {len(q['rows'])} rows; aggregate to at most 60"]
    return []
