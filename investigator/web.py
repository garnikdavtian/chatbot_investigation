"""Local web UI: one page and a small JSON API over the same core functions the CLI uses.

  uv run --env-file .env python -m investigator serve      # then open http://127.0.0.1:8000

  GET  /api/config                 model, limits, and whether new questions can be asked
  GET  /api/runs                   saved investigations, newest first
  GET  /api/runs/<id>              one saved run record
  GET  /api/runs/<id>/report.md    Markdown export
  POST /api/ask                    {"question": "...", "parent_run_id": null or "<id>"} -> the new run
  POST /api/runs/<id>/replay       re-run on the recorded responses -> {"status", "diffs"}

ponytail: stdlib http.server for one local user on 127.0.0.1. To share it, serve the same functions
from FastAPI/uvicorn behind authentication.
"""
import base64
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from investigator import agent, gateway, report
from investigator.llm import LiveLLM, LLMError

PAGE = Path(__file__).parent / "static" / "index.html"
MAX_QUESTION_CHARS = 500
MAX_BODY_BYTES = 10_000


def config() -> dict:
    return {"live": bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_MODEL")),
            "model": os.environ.get("LLM_MODEL"), "max_queries": agent.MAX_QUERIES,
            "max_rows": gateway.MAX_ROWS, "timeout_s": gateway.TIMEOUT_S}


def csp(page: bytes) -> str:
    """Only the page's own inline script runs (allowed by its hash), it talks only to this server,
    and no other site can frame it."""
    script = page.split(b"<script>")[1].split(b"</script>")[0]
    digest = base64.b64encode(hashlib.sha256(script).digest()).decode()
    return (f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def progress(run: dict) -> dict:
    return {"queries": [{"id": q["id"], "purpose": q["purpose"], "error": q["error"], "rows": len(q["rows"])}
                        for q in run["queries"]],
            "submitted": run["report"] is not None, "repairs": len(run["repairs"])}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self._path()
        if path is None:
            return
        if path == [""]:
            page = PAGE.read_bytes()
            return self._send(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": csp(page)})
        if path == ["api", "config"]:
            return self._json(200, config())
        if path == ["api", "runs"]:
            runs = [report.load(f.stem) for f in sorted(report.RUNS.glob("*.json"), reverse=True)]
            keys = ("run_id", "created_at", "question", "status", "parent_run_id")
            return self._json(200, [{k: r[k] for k in keys} for r in runs])
        if len(path) in (3, 4) and path[:2] == ["api", "runs"] and path[3:] in ([], ["report.md"]):
            run = self._run(path[2])
            if run and len(path) == 3:
                return self._json(200, run)
            if run:
                return self._send(200, report.to_markdown(run).encode(), "text/markdown; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{run["run_id"]}.md"'})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self._path()
        if path is None:
            return
        # JSON only: a cross-site form cannot send it without a CORS preflight, which this server never allows
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._json(415, {"error": "send application/json"})
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY_BYTES:
            return self._json(413, {"error": "request too large"})
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "body is not valid JSON"})
        if not isinstance(body, dict):
            return self._json(400, {"error": "body must be a JSON object"})

        if path == ["api", "ask"]:
            question = str(body.get("question") or "").strip()
            if not 0 < len(question) <= MAX_QUESTION_CHARS:
                return self._json(400, {"error": f"the question must be 1 to {MAX_QUESTION_CHARS} characters"})
            parent = self._run(body["parent_run_id"]) if body.get("parent_run_id") else None
            if body.get("parent_run_id") and parent is None:
                return
            try:
                llm = LiveLLM.from_env()
            except LLMError as e:
                return self._json(503, {"error": str(e)})
            # One JSON object per line: progress after every tool call, then the saved run.
            self.send_response(200)
            for k, v in {"Content-Type": "application/x-ndjson", "X-Content-Type-Options": "nosniff",
                         "Cache-Control": "no-store"}.items():
                self.send_header(k, v)
            self.end_headers()
            run = agent.investigate(question, llm, parent=parent, on_step=lambda r: self._line({"progress": progress(r)}))
            report.save(run)
            return self._line({"run": run})
        if len(path) == 4 and path[:2] == ["api", "runs"] and path[3] == "replay":
            run = self._run(path[2])
            parent = self._run(run["parent_run_id"]) if run and run["parent_run_id"] else None
            if run is None or (run["parent_run_id"] and parent is None):
                return
            new, diffs = agent.replay(run, parent)
            return self._json(200, {"status": new["status"], "diffs": diffs})
        self._json(404, {"error": "not found"})

    def _path(self) -> list | None:
        # Host check: blocks DNS-rebinding pages from reaching this local server
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            self._json(403, {"error": "local use only"})
            return None
        return self.path.split("?")[0].strip("/").split("/")

    def _run(self, run_id: str) -> dict | None:
        try:
            return report.load(run_id)
        except FileNotFoundError:
            self._json(404, {"error": f"no saved run {run_id!r}"})
            return None

    def _line(self, data) -> None:
        try:
            self.wfile.write(json.dumps(data).encode() + b"\n")
            self.wfile.flush()
        except OSError:
            pass  # the page went away: finish and save the run anyway

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def _send(self, code: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(code)
        for k, v in {"Content-Type": content_type, "Content-Length": str(len(body)),
                     "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def serve(port: int = 8000, host: str = "127.0.0.1") -> None:
    """host 0.0.0.0 is for a container only, published on the host's 127.0.0.1; the Host check still applies."""
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Data Investigator on http://127.0.0.1:{port} (Ctrl+C stops it)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
