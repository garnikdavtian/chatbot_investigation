"""The api container: FastAPI over the users' chats, calling the agent container for every question.

  POST /api/login {name, password}  -> {"token", "name"} (users are created by an admin: add-user)
  POST /api/logout                  ends this token's session
  GET  /api/config                  model, limits, and whether new questions can be asked (no login needed)
  GET  /api/runs                    your chats' messages, newest first
  GET  /api/runs/{id}               one saved run record
  GET  /api/runs/{id}/report.md     Markdown export
  POST /api/ask {question, parent_run_id}  NDJSON progress, then the run; the agent gets the chat's last run
  POST /api/runs/{id}/replay        the recorded responses re-run by the agent -> {"status", "diffs"}
  POST /api/reports {run_id}        save an answer's checked figures as a report -> {"report_id"}
  GET  /api/reports                 your saved reports
  POST /api/reports/{id}/run {from_month, to_month}  the report's figures per month -> {"report", "columns", "rows"}
  GET  /api/compare                 the faulty join against the rules

Every other call needs "Authorization: Bearer <token>". Another user's run is a 404, so run ids reveal nothing.
A header, not a cookie: another site cannot make the browser send it, so no CSRF. This container holds the
users and chats but no LLM key; the agent container holds the key but no users.
"""
import json
import os
import queue
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from investigator import calc, history, report

STATIC = Path(__file__).parent / "static"
AGENT_URL = os.environ.get("AGENT_URL") or "http://127.0.0.1:8002"
# Only the page's own script file runs, it talks only to this server, and no other site can frame it.
# docker/nginx.conf sends the same header.
CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

app = FastAPI(title="Data Investigator API", docs_url=None, redoc_url=None, openapi_url=None)
# Host check: blocks DNS-rebinding pages from reaching this server
app.add_middleware(TrustedHostMiddleware, allowed_hosts=os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost").split(","))


@app.middleware("http")
async def headers(request: Request, call_next):
    if request.method == "POST" and request.headers.get("content-type", "").split(";")[0] != "application/json":
        # JSON only: a cross-site form cannot send it without a CORS preflight, which this server never allows
        return JSONResponse({"error": "send application/json"}, 415)
    response = await call_next(request)
    response.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                             "Cache-Control": "no-store"})
    return response


@app.exception_handler(StarletteHTTPException)
def http_error(_, e: StarletteHTTPException):
    return JSONResponse({"error": e.detail}, e.status_code)


@app.exception_handler(RequestValidationError)
def bad_request(_, e: RequestValidationError):
    first = e.errors()[0]
    return JSONResponse({"error": f"{'.'.join(map(str, first['loc'][1:]))}: {first['msg']}"}, 400)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Ask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    parent_run_id: str | None = None


class SaveReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str


class Months(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_month: str
    to_month: str


METRICS = ("gross", "refunds", "net")


def current_user(authorization: str = Header("")) -> dict:
    scheme, _, token = authorization.partition(" ")
    user = history.user_for_token(token.strip()) if scheme == "Bearer" and token.strip() else None
    if user is None:
        raise HTTPException(401, "log in first")
    return {**user, "token": token.strip()}


User = Annotated[dict, Depends(current_user)]


def saved_run(run_id: str, user: dict) -> dict:
    run = history.load(run_id, user["user_id"])
    if run is None:
        raise HTTPException(404, f"no saved run {run_id!r}")
    return run


def call_agent(path: str, body: dict | None = None, timeout: float = 300):
    """The open response from the agent container; HTTPException 503 if it is down or refuses."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(AGENT_URL + path, data, {"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise HTTPException(503, json.load(e).get("error", "the agent refused the request")) from e
    except OSError as e:
        raise HTTPException(503, f"the agent service did not answer: {e}") from e


@app.get("/")
def page():
    return FileResponse(STATIC / "index.html")


@app.get("/app.js")
def script():
    return FileResponse(STATIC / "app.js", media_type="text/javascript")


@app.post("/api/login")
def login(body: Login):
    token = history.login(body.name, body.password)
    if token is None:
        raise HTTPException(401, "wrong name or password")
    return {"token": token, "name": body.name}


@app.post("/api/logout")
def logout(user: User):
    history.logout(user["token"])
    return {}


@app.get("/api/config")
def config():
    with call_agent("/config", timeout=5) as r:
        return json.load(r)


@app.get("/api/runs")
def runs(user: User):
    return history.list_runs(user["user_id"])


@app.get("/api/runs/{run_id}")
def run(run_id: str, user: User):
    return saved_run(run_id, user)


@app.get("/api/runs/{run_id}/report.md")
def markdown(run_id: str, user: User):
    return Response(report.to_markdown(saved_run(run_id, user)), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'})


@app.post("/api/ask")
def ask(body: Ask, user: User):
    parent = saved_run(body.parent_run_id, user) if body.parent_run_id else None
    r = call_agent("/invoke", {"question": body.question, "parent": parent})
    lines = queue.Queue()

    def pump():  # on its own thread: if the page goes away, the run is still read to the end and saved
        with r:
            for line in r:
                if line.startswith(b'{"run"'):
                    history.save(json.loads(line)["run"], user["user_id"])
                lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return StreamingResponse(iter(lines.get, None), media_type="application/x-ndjson")


@app.post("/api/runs/{run_id}/replay")
def replay(run_id: str, user: User):
    run = saved_run(run_id, user)
    parent = saved_run(run["parent_run_id"], user) if run["parent_run_id"] else None
    with call_agent("/replay", {"run": run, "parent": parent}) as r:
        return json.load(r)


@app.post("/api/reports")
def save_report(body: SaveReport, user: User):
    run = saved_run(body.run_id, user)
    figs = [f for f in (run.get("verification") or {}).get("figures", []) if f["status"] == "ok"]
    if not figs:
        raise HTTPException(400, "this answer has no checked figures to reuse")
    seen = {(f["segment"] or "all", f["metric"]) for f in figs}
    last_day = max(date.fromisoformat(f["period_end_exclusive"]) for f in figs) - timedelta(days=1)
    spec = {"columns": sorted(seen, key=lambda c: (c[0] != "all", c[0], METRICS.index(c[1]))),
            "from_month": min(f["period_start"] for f in figs)[:7],
            "to_month": last_day.isoformat()[:7]}
    return {"report_id": history.save_report(user["user_id"], run["question"][:200], spec, run["run_id"])}


@app.get("/api/reports")
def reports(user: User):
    return history.list_reports(user["user_id"])


@app.post("/api/reports/{report_id}/run")
def run_report(report_id: int, body: Months, user: User):
    saved = history.load_report(report_id, user["user_id"])
    if saved is None:
        raise HTTPException(404, f"no saved report {report_id}")
    try:
        calc.months(body.from_month, body.to_month)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    with call_agent("/figures", body.model_dump(), timeout=30) as r:
        months = json.load(r)["months"]
    cols = saved["spec"]["columns"]
    return {"report": saved,
            "columns": ["month"] + [f"{metric}_cents" if seg == "all" else f"{seg}_{metric}_cents" for seg, metric in cols],
            "rows": [[m["month"]] + [m.get(seg, {}).get(f"{metric}_cents", 0) for seg, metric in cols] for m in months]}


@app.get("/api/compare")
def compare(user: User):
    with call_agent("/compare", timeout=30) as r:
        return json.load(r)


def serve(port: int = 8000, host: str = "127.0.0.1") -> None:
    """Without AGENT_URL (local use) the agent service runs on a thread of this process, still over HTTP.
    host 0.0.0.0 is for a container only, published on the host's 127.0.0.1; the Host check still applies."""
    import uvicorn

    from investigator import agent_service
    if not os.environ.get("AGENT_URL"):
        threading.Thread(target=agent_service.serve().serve_forever, daemon=True).start()
    print(f"Data Investigator on http://127.0.0.1:{port} (Ctrl+C stops it)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
