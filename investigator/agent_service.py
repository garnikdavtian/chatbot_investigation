"""The agent container: the compiled graph waits here for the api container's calls. It keeps no state between
calls; the api sends the chat's last run with each question and saves what comes back.

  GET  /config                     model, limits, and whether new questions can be asked
  POST /invoke {question, parent}  NDJSON: {"progress": ...} after each graph step, then {"run": ...}
  POST /replay {run, parent}       {"status", "diffs"}: the recorded responses re-run, no LLM call

No auth: only the api container can reach it (an internal compose network). It holds the LLM key and reaches
the sales data through the db service; it never sees passwords or other users' chats.
ponytail: stdlib http.server, like the db service; on_step writes each line straight to the socket.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from investigator import agent, gateway
from investigator.llm import LLMError, live_model

MAX_BODY_BYTES = 5_000_000  # a question plus its chat's last run record


def config() -> dict:
    return {"live": bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL")),
            "model": os.environ.get("LLM_MODEL"), "max_queries": agent.MAX_QUERIES,
            "max_messages": agent.MAX_MESSAGES, "max_rows": gateway.MAX_ROWS, "timeout_s": gateway.TIMEOUT_S}


def progress(run: dict) -> dict:
    return {"guard": run["guard"] and run["guard"]["label"],
            "queries": [{"id": q["id"], "purpose": q["purpose"], "error": q["error"], "rows": len(q["rows"])}
                        for q in run["queries"]],
            "submitted": run["report"] is not None, "repairs": len(run["repairs"])}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/config":
            return self._json(404, {"error": "not found"})
        self._json(200, config())

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY_BYTES:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(size))
        except ValueError:
            return self._json(400, {"error": "body is not valid JSON"})
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
                         on_step=lambda r: self._line({"progress": progress(r)}))
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
