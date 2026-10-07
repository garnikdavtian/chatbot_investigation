"""Web UI: one page, its script, and a small JSON API over the same agent the CLI uses.

  uv run --env-file .env python -m investigator serve        # then open http://127.0.0.1:8000

  GET  /api/config                 model, limits, and whether new questions can be asked (no key needed)
  POST /api/session                {} -> {"key": "..."}: a new anonymous user (no key needed)
  GET  /api/runs                   your chats' messages, newest first
  GET  /api/runs/<id>              one saved run record
  GET  /api/runs/<id>/report.md    Markdown export
  POST /api/ask                    {"question": "...", "parent_run_id": null or "<id>"} -> NDJSON progress, then the run
  POST /api/runs/<id>/replay       re-run on the recorded responses -> {"status", "diffs"}

The page gets its key from /api/session on its first visit and keeps it, so nobody signs in. Every other /api
call needs "Authorization: Bearer <app key>". Another user's run is a 404, so run ids reveal nothing. A header,
not a cookie: another site cannot make the browser send it, so no CSRF.
ponytail: stdlib http.server; FastAPI/uvicorn when it needs more than a few routes or many users.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from investigator import agent, gateway, history, report
from investigator.llm import LLMError, live_model

STATIC = Path(__file__).parent / "static"
FILES = {"": ("index.html", "text/html; charset=utf-8"), "app.js": ("app.js", "text/javascript; charset=utf-8")}
# Only the page's own script file runs, it talks only to this server, and no other site can frame it.
# docker/nginx.conf sends the same header.
CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
MAX_QUESTION_CHARS = 500
MAX_BODY_BYTES = 10_000


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
        path = self._path()
        if path is None:
            return
        if len(path) == 1 and path[0] in FILES:
            name, content_type = FILES[path[0]]
            return self._send(200, (STATIC / name).read_bytes(), content_type)
        if path == ["api", "config"]:
            return self._json(200, config())
        user = self._user()
        if user is None:
            return
        if path == ["api", "runs"]:
            return self._json(200, history.list_runs(user))
        if len(path) in (3, 4) and path[:2] == ["api", "runs"] and path[3:] in ([], ["report.md"]):
            run = self._run(path[2], user)
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
        if path == ["api", "session"]:
            # ponytail: anyone who can reach the page can create users; add a per-IP limit if it is ever exposed
            return self._json(200, {"key": history.add_user()})
        user = self._user()
        if user is None:
            return

        if path == ["api", "ask"]:
            question = str(body.get("question") or "").strip()
            if not 0 < len(question) <= MAX_QUESTION_CHARS:
                return self._json(400, {"error": f"the question must be 1 to {MAX_QUESTION_CHARS} characters"})
            parent = self._run(str(body["parent_run_id"]), user) if body.get("parent_run_id") else None
            if body.get("parent_run_id") and parent is None:
                return
            try:
                model = live_model()
            except LLMError as e:
                return self._json(503, {"error": str(e)})
            # One JSON object per line: progress after every graph step, then the saved run.
            self.send_response(200)
            for k, v in {"Content-Type": "application/x-ndjson", "X-Content-Type-Options": "nosniff",
                         "Cache-Control": "no-store"}.items():
                self.send_header(k, v)
            self.end_headers()
            run = agent.chat(question, model, parent, on_step=lambda r: self._line({"progress": progress(r)}))
            history.save(run, user)
            return self._line({"run": run})
        if len(path) == 4 and path[:2] == ["api", "runs"] and path[3] == "replay":
            run = self._run(path[2], user)
            parent = self._run(run["parent_run_id"], user) if run and run["parent_run_id"] else None
            if run is None or (run["parent_run_id"] and parent is None):
                return
            new, diffs = agent.replay(run, parent)
            return self._json(200, {"status": new["status"], "diffs": diffs})
        self._json(404, {"error": "not found"})

    def _path(self) -> list | None:
        # Host check: blocks DNS-rebinding pages from reaching this server
        allowed = os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",")
        if self.headers.get("Host", "").rsplit(":", 1)[0] not in allowed:
            self._json(403, {"error": "host not allowed"})
            return None
        return self.path.split("?")[0].strip("/").split("/")

    def _user(self) -> int | None:
        scheme, _, key = self.headers.get("Authorization", "").partition(" ")
        user = history.user_for_key(key.strip()) if scheme == "Bearer" and key.strip() else None
        if user is None:
            self._json(401, {"error": "a valid app key is required"})
        return user

    def _run(self, run_id: str, user: int) -> dict | None:
        run = history.load(run_id, user)
        if run is None:
            self._json(404, {"error": f"no saved run {run_id!r}"})
        return run

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
        for k, v in {"Content-Type": content_type, "Content-Length": str(len(body)), "Content-Security-Policy": CSP,
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
