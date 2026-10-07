"""The agent container: the compiled graph waits here for the api container's calls. It keeps no state between
calls; the api sends the chat's last run with each question and saves what comes back.

  GET  /config                     model, limits, and whether new questions can be asked
  POST /invoke {question, parent}  NDJSON: {"event": ...} for the inspector, {"progress": ...} after each graph step,
                                   then {"run": ...}
  POST /replay {run, parent}       {"status", "diffs"}: the recorded responses re-run, no LLM call
  POST /figures {from_month, to_month}  gross, refunds and net per month, all customers and per segment (calc, no LLM)
  GET  /compare                    the faulty join and the refund-month assumption against the rules (fixed SQL, no LLM)

No auth: only the api container can reach it (an internal compose network). It holds the LLM key and reaches
the sales data through the db service; it never sees passwords or other users' chats.
ponytail: stdlib http.server, like the db service; on_step writes each line straight to the socket.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from investigator import agent, calc, gateway
from investigator.llm import LLMError, live_model

MAX_BODY_BYTES = 5_000_000  # a question plus its chat's last run record


def config() -> dict:
    return {"live": bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL")),
            "model": os.environ.get("LLM_MODEL"), "max_queries": agent.MAX_QUERIES,
            "tools": [t["function"]["name"] for t in agent.TOOLS],
            "max_messages": agent.MAX_MESSAGES, "max_rows": gateway.MAX_ROWS, "timeout_s": gateway.TIMEOUT_S}


def progress(run: dict) -> dict:
    return {"guard": run["guard"] and run["guard"]["label"],
            "queries": [{"id": q["id"], "purpose": q["purpose"], "error": q["error"], "rows": len(q["rows"])}
                        for q in run["queries"]],
            "submitted": run["report"] is not None, "repairs": len(run["repairs"])}


# Our SQL, not the model's: each pair is the rule and a known way to get it wrong, run through the same read-only
# gateway. The rule side must equal calc.totals for every month, or nothing is shown.
GROSS = "SELECT substr(order_date, 1, 7) AS month, sum(amount_cents) FROM orders GROUP BY 1"
GROSS_NAIVE_JOIN = ("SELECT substr(o.order_date, 1, 7) AS month, sum(o.amount_cents) FROM orders o\n"
                    "LEFT JOIN refunds r ON r.order_id = o.order_id GROUP BY 1")
REFUNDS = "SELECT substr(refund_date, 1, 7) AS month, sum(amount_cents) FROM refunds GROUP BY 1"
REFUNDS_BY_ORDER_MONTH = ("SELECT substr(o.order_date, 1, 7) AS month, sum(r.amount_cents) FROM refunds r\n"
                          "JOIN orders o ON o.order_id = r.order_id GROUP BY 1")


def figures(first: str, last: str) -> dict:
    _, data = gateway.snapshot(agent.DB)
    return {"months": [{"month": m, "all": calc.totals(data, *calc.month_bounds(m)),
                        **calc.by_segment(data, *calc.month_bounds(m))} for m in calc.months(first, last)]}


def compare() -> dict:
    """Raises RuntimeError if a query fails or the rule side disagrees with calc: a bug, never shown as figures."""
    _, data = gateway.snapshot(agent.DB)
    by_month = {}
    for sql in (GROSS, GROSS_NAIVE_JOIN, REFUNDS, REFUNDS_BY_ORDER_MONTH):
        r = gateway.query(agent.DB, sql)
        if not r.ok:
            raise RuntimeError(f"comparison query failed: {r.message}")
        by_month[sql] = dict(r.rows)
    gross, naive, refunds, by_order = (by_month[q] for q in (GROSS, GROSS_NAIVE_JOIN, REFUNDS, REFUNDS_BY_ORDER_MONTH))
    months = sorted(set(gross) | set(refunds))
    for m in months:
        rule = calc.totals(data, *calc.month_bounds(m))
        if (gross.get(m, 0), refunds.get(m, 0)) != (rule["gross_cents"], rule["refunds_cents"]):
            raise RuntimeError(f"comparison SQL disagrees with the metric rules for {m}")
    return {
        "faulty_join": {
            "sql_rule": GROSS, "sql_alt": GROSS_NAIVE_JOIN,
            "columns": ["month", "correct_gross_cents", "naive_join_gross_cents", "difference_cents"],
            "rows": [[m, gross.get(m, 0), naive.get(m, 0), naive.get(m, 0) - gross.get(m, 0)] for m in months]},
        "refund_month": {
            "sql_rule": REFUNDS, "sql_alt": REFUNDS_BY_ORDER_MONTH,
            "columns": ["month", "gross_cents", "refunds_by_refund_date_cents", "refunds_by_order_date_cents",
                        "net_by_refund_date_cents", "net_by_order_date_cents"],
            "rows": [[m, gross.get(m, 0), refunds.get(m, 0), by_order.get(m, 0), gross.get(m, 0) - refunds.get(m, 0),
                      gross.get(m, 0) - by_order.get(m, 0)] for m in months]},
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/config":
            return self._json(200, config())
        if self.path != "/compare":
            return self._json(404, {"error": "not found"})
        try:
            self._json(200, compare())
        except RuntimeError as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY_BYTES:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(size))
        except ValueError:
            return self._json(400, {"error": "body is not valid JSON"})
        if self.path == "/figures":
            try:
                return self._json(200, figures(body.get("from_month"), body.get("to_month")))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
        if self.path == "/replay":
            new, diffs = agent.replay(body["run"], body.get("parent"))
            return self._json(200, {"status": new["status"], "diffs": diffs})
        if self.path != "/invoke":
            return self._json(404, {"error": "not found"})
        try:
            model = live_model()
        except LLMError as e:
            return self._json(503, {"error": str(e)})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        run = agent.chat(body["question"], model, body.get("parent"),
                         on_step=lambda r: self._line({"progress": progress(r)}),
                         on_event=lambda e: self._line({"event": e}))
        self._line({"run": run})

    def _line(self, data) -> None:
        self.wfile.write(json.dumps(data).encode() + b"\n")
        self.wfile.flush()

    def _json(self, code: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8002, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)
