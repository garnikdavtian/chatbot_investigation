"""Read-only SQL tool: the trust boundary for SQL written by the model.

Read-only is enforced by the database itself, at three independent layers:
  1. the file is opened read-only (mode=ro),
  2. PRAGMA query_only blocks any write,
  3. an authorizer allows only SELECT on the three tables and a list of plain functions
     (recursive CTEs are denied too: they read a name that is not one of the tables).
Plus a time limit, a row cap and an SQL length cap. Every outcome is a QueryResult, never an exception.

With Docker the database lives in its own container, mounted read-only, on a network with no internet:
`python -m investigator db` serves run_sql over HTTP, and the app sets DB_URL to reach it. query() and
snapshot() take a file path or that URL, so the agent does not care where the database is.
"""
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from investigator import calc

TABLES = {"customers", "orders", "refunds"}
FUNCTIONS = {
    # aggregates and scalars
    "abs", "avg", "coalesce", "concat", "concat_ws", "count", "group_concat", "ifnull", "iif", "instr", "length",
    "lower", "ltrim", "max", "min", "nullif", "printf", "format", "replace", "round", "rtrim", "sign",
    "string_agg", "substr", "substring", "sum", "total", "trim", "typeof", "upper",
    # dates and pattern matching (LIKE and GLOB are function calls in SQLite)
    "date", "datetime", "julianday", "strftime", "time", "unixepoch", "like", "glob",
    # window functions
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead",
    "first_value", "last_value", "nth_value",
}
MAX_ROWS = 200
TIMEOUT_S = 2.0
MAX_SQL_CHARS = 4000


@dataclass
class QueryResult:
    sql: str
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False  # True when the row cap cut the result
    elapsed_ms: int = 0
    error: str | None = None  # "rejected" | "timeout" | "sql"
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return asdict(self)


def _authorize(action, arg1, arg2, _db, _trigger):
    allowed = (
        action == sqlite3.SQLITE_SELECT
        or (action == sqlite3.SQLITE_READ and arg1 in TABLES)
        or (action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FUNCTIONS)
    )
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY


def run_sql(db_path, sql: str, max_rows: int = MAX_ROWS, timeout_s: float = TIMEOUT_S) -> QueryResult:
    result = QueryResult(sql=sql)
    if len(sql) > MAX_SQL_CHARS:
        result.error, result.message = "rejected", f"SQL longer than {MAX_SQL_CHARS} characters"
        return result

    started = time.monotonic()
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        con.set_authorizer(_authorize)
        con.set_progress_handler(lambda: time.monotonic() - started > timeout_s, 10_000)
        cur = con.execute(sql)
        result.columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
        result.truncated = len(rows) > max_rows
        result.rows = [list(r) for r in rows[:max_rows]]
    except (sqlite3.Error, sqlite3.Warning) as e:
        msg = str(e)
        if msg == "interrupted":
            result.error, result.message = "timeout", f"query exceeded {timeout_s}s and was stopped"
        elif any(k in msg for k in ("not authorized", "authorization denied", "prohibited", "readonly", "one statement")):
            result.error, result.message = "rejected", f"blocked by the read-only policy ({msg}); allowed: one SELECT on {sorted(TABLES)}"
        else:
            result.error, result.message = "sql", msg
    finally:
        con.close()
        result.elapsed_ms = round((time.monotonic() - started) * 1000)
    return result


def _remote(db) -> bool:
    return str(db).startswith(("http://", "https://"))


def query(db, sql: str) -> QueryResult:
    if not _remote(db):
        return run_sql(db, sql)
    req = urllib.request.Request(f"{db}/query", json.dumps({"sql": sql}).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S + 5) as r:
            return QueryResult(**json.load(r))
    except (OSError, ValueError) as e:  # unreachable, timed out, or not a QueryResult: still counts as an attempt
        return QueryResult(sql=sql, error="unavailable", message=f"the database service did not answer: {e}")


def snapshot(db) -> tuple[str, dict]:
    """The database's sha and all rows, for the data contract and the verifier.
    ponytail: ships the three tables whole; compute calc.totals in the db service when the data grows."""
    if _remote(db):
        with urllib.request.urlopen(f"{db}/data", timeout=10) as r:
            body = json.load(r)
        return body["sha"], body["data"]
    return hashlib.sha256(Path(db).read_bytes()).hexdigest()[:12], calc.load_db(db)


def serve(db_path, port: int = 8001, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """POST /query {"sql"} -> QueryResult; GET /data -> {"sha", "data"}. No auth: it is reachable only on the
    compose network, and every query goes through run_sql on a read-only file."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/data":
                return self._json(404, {"error": "not found"})
            sha, data = snapshot(db_path)
            self._json(200, {"sha": sha, "data": data})

        def do_POST(self):
            size = int(self.headers.get("Content-Length") or 0)
            if self.path != "/query" or size > 4 * MAX_SQL_CHARS:
                return self._json(404 if self.path != "/query" else 413, {"error": "POST /query {\"sql\": ...}"})
            try:
                sql = json.loads(self.rfile.read(size)).get("sql")
            except (ValueError, AttributeError):
                sql = None
            if not isinstance(sql, str):
                return self._json(400, {"error": "POST /query {\"sql\": ...}"})
            self._json(200, run_sql(db_path, sql).to_dict())

        def _json(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)
