"""Read-only SQL tool: the trust boundary for SQL written by the model.

Read-only is enforced by the database itself, at three independent layers:
  1. the file is opened read-only (mode=ro),
  2. PRAGMA query_only blocks any write,
  3. an authorizer allows only SELECT on the three tables and a list of plain functions
     (recursive CTEs are denied too: they read a name that is not one of the tables).
Plus a time limit, a row cap and an SQL length cap. Every outcome is a QueryResult, never an exception.
"""
import sqlite3
import time
from dataclasses import asdict, dataclass, field

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
