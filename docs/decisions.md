# Design decisions

The brief asks for a model that investigates with at most six read-only queries and a report whose
numbers are easy to verify. The design follows from one rule: **the model may be wrong, but the
system must not hide it.** The model proposes queries and figures. Code enforces the limits, checks
every figure and records everything needed to replay the run.

## Shape: a modular monolith

```
adapters    __main__.py (CLI)    api.py (FastAPI) → agent_service.py + static/ (web page)
                       \                     /
core                    agent.py   loop, limits, statuses, replay
                  /        |           |            \
            llm.py     gateway.py   verify.py     report.py
           network     model SQL    the checks    schema, run files, Markdown
                                       |
                                    calc.py       the business rules, pure Python
```

Dependencies point one way, and each boundary lives in one module. `llm.py` is the only code that
calls a provider. `gateway.py` is the only code that runs model-written SQL. `report.py` is the only
code that writes run files. `calc.py` has no I/O beyond reading the database. Each boundary has its
own tests: 12 attack statements plus 5 limit and error tests for the gateway, 14 scripted-model
tests for the loop and 9 data tests.
Each also has one place to change it later: another provider in `llm.py`, another database in
`gateway.py`.

**Rejected.** Services or queues: there is one user, one process and SQL that takes
milliseconds. An agent framework such as LangChain or LangGraph: the loop is about 40 lines with
explicit limits, and a framework would hide exactly the limits and transcript this task must
record.

**Superseded on 2026-10-07 (D12–D16).** Manual tests showed the loop treated every message as an
investigation. The agent is now a LangGraph graph, `guard → reason ⇄ act`, with custom nodes, so
the limits and the transcript stay in our code (D12). Chats carry memory (D13), a guard screens scope
(D14), users have keys and their own history (D15), and Docker runs ui, app and db as three
containers (D16). The module boundaries above are unchanged: `gateway.py` still runs all model SQL,
`verify.py` still checks every figure, and `llm.py` still builds the only model client.

## D1. Read-only access is enforced by the database, not by parsing SQL

`gateway.py` applies three independent layers:

1. The file is opened with `mode=ro`.
2. `PRAGMA query_only` blocks writes.
3. An SQLite authorizer allows only `SELECT`, reads of `customers`, `orders` and `refunds`, and a list
   of plain functions.

Everything else is denied: writes, `PRAGMA`, `ATTACH`, `load_extension`, `sqlite_master` and
recursive CTEs. More than one statement fails as well. Limits:

- 6 query attempts per question, counted before execution, so errors and rejections count;
- 2 s per query (progress handler);
- 200 rows, with a `truncated` flag;
- 4,000 characters of SQL.

The gateway never raises. Each outcome is a `QueryResult` with `error` set to `rejected`, `timeout`
or `sql` and a message the model and the user can read.

**Why.** A parser or regex has to anticipate every form of write, such as a CTE wrapped around a
`DELETE`, a `PRAGMA` or an `ATTACH`. The authorizer sees what SQLite actually compiled. Evidence:
`tests/test_gateway.py`, plus a live run (`runs/20261007-174722-035d`; v1: `20261006-175401-e4ae`, in git history) in which the model sent a
`DELETE`. The gateway rejected it, the attempt counted (1 of 6), and the database bytes did not
change.

**Ceiling.** This works for SQLite only. On a warehouse, use a read-only role with a statement
timeout and a row limit enforced by the database.

## D2. The model writes the SQL; code checks the numbers

Every figure in a report is typed: metric, period, segment, value in cents and the id of the query
that shows it. `verify.py` runs two independent checks on each figure:

- **grounded:** the value is a cell in the result of the cited query;
- **correct:** the value equals `calc.py`, a separate implementation of `domain.md`.

| grounded | correct | status | meaning |
|---|---|---|---|
| yes | yes | `ok` | |
| yes | no | `query_error` | the model's SQL breaks a rule, e.g. a join that repeats orders |
| no | yes | `unsupported` | the number is right but no executed query shows it |
| no | no | `wrong` | |

The text is checked too:

- Money must be written as "N cents".
- Every amount must be a checked figure or the difference of two checked figures.
- `$` is not allowed; code formats dollars for display.

A third source, the hand-calculated key in `data/expected.json`, is used only by the tests. The
application never sees it.

**Why.** The brief asks for figures connected to executed results and for correct totals. The model
is the least reliable part of the system, so it is the part that gets checked, by deterministic code.

**Rejected.**

- *A second model as judge:* more cost, not deterministic, and it can misread the rules the same way
  the first model did.
- *A "metrics" tool that computes totals for the model:* the brief asks for an investigation through
  SQL, and such a tool would hide the model's mistakes instead of catching them.

**Ceiling.** Only money figures of the three metrics, by period and segment, are checked. Bare
numbers, counts and non-money claims are not. Upgrade path: the model cites figure ids, and code
renders every number into the text.

## D3. One repair round, without giving away the answer

A report that fails a check goes back to the model once, with the problems listed. The issue texts
never contain the expected value, so the model has to fix its query rather than copy the answer.
`test_repair_round_fixes_a_wrong_figure_without_leaking_the_answer` checks this. If the second
report also fails, the run ends `unverified` and the problems are shown.

**Why.** Round 1 of the evaluation had no repair round. Four of its five failures were correct
amounts that the model wrote in the text without attaching them as figures. One bounded retry fixes
that kind of slip. A wrong figure still cannot pass.

## D4. A bounded loop with explicit statuses

| Setting | Value |
|---|---|
| `MAX_QUERIES` | 6 (from the brief) |
| `MAX_TURNS` | 12 model calls (now `MAX_CALLS` = 8 reason calls per question, D12) |
| `MAX_REPAIRS` | 1 |
| `tool_choice` | `"required"`: the model cannot drift into free text (now `"auto"` on a question's first call, D12) |
| `parallel_tool_calls` | `false`: each query is chosen after the previous result |

When the query limit is reached, the next call is forced to `submit_report`.

**Follow-up rule.** A report is accepted only when successful queries came from at least two model
calls. That proves at least one query was chosen after seeing a result, which is the brief's "use
the initial result to choose a follow-up".

**Statuses.**

- `verified`: every check passed.
- `unverified`: a check failed, and the problems are listed.
- `incomplete`: a limit stopped the run, and partial results are shown.
- `failed`: the data contract or the model API failed.

The CLI exits non-zero unless the run is verified. The UI shows the status and the problems next to
the answer.

## D5. One run record, and replay instead of mocks

A run is one JSON record that holds:

- the question and its parent run;
- the configuration: model, prompt path and SHA, database SHA, limits;
- every query with its result or error;
- every model response, labelled `live`, `replay` or `scripted`;
- the message list, the repair rounds, the report and its verification.

The CLI, the UI, the Markdown export and replay all read this record.

**Replay.** `replay` feeds the recorded model responses back through `ScriptedLLM`, then runs the SQL
and the checks again. `diff` reports any change in data, prompt, query results, report or status.
`python -m investigator check` replays every saved run without an API key. It doubles as a
regression test built from real model output.

**Prompt versions.** Prompt files are immutable (`prompts/system-v1.md` to `-v5`). Each run
records its prompt path and the SHA of the full system prompt, so old runs still replay after the
prompt changes.

**Rejected.** Saving only the final report: it cannot be reproduced. A database for runs: JSON files
can be diffed and reviewed, and they are enough for one user.

## D6. The model is chosen by evaluation through the real pipeline

`evals/run_eval.py` runs a fixed question set × 3 trials through `investigate()`, with the same loop, checks
and limits as the app. A run passes when all of these hold:

- it ends verified;
- every required figure is present and checked `ok`;
- for a "why" question, an `unknown` finding states what the data cannot explain;
- a chart appears only where one is wanted, and a partial month in it is labelled.

The result table is in the README. The choice is **gpt-6-luna with reasoning effort `none`**: 15/15
on the first pass with prompt v3, at $0.0013 per run. Prompts v4 and v5 ran side by side on 10
questions, adding three chart questions and two injection questions: 27/30 each, with v5 passing
6/6 injection runs against v4's 4/6 (D11). The larger gpt-6-sol scored 14/15 with prompt v2 at
about 20× the cost.

**Chat Completions** works with any OpenAI-compatible provider through `LLM_BASE_URL` (OpenAI,
OpenRouter, a local server). The trade-off: gpt-6.1-sol does not accept function tools together with
reasoning in this API (it needs `/v1/responses`), so it was not evaluated.

**Anthropic keys** use Anthropic's OpenAI-compatible endpoint (`https://api.anthropic.com/v1/`), so
the one client stays and no SDK is added. It ignores `strict` (the code validation below covers that)
and requires an output cap, so every call now sends `LLM_MAX_TOKENS` (default 4096). Claude 5 models
think by default, which forced tool calls do not allow; `LLM_EXTRA_BODY` passes
`{"thinking":{"type":"disabled"}}` or any other provider-specific field through unchanged.
Smoke-tested with claude-haiku-4-5 through OpenRouter (blocked small talk; main question verified).
**Rejected:** `langchain-anthropic` behind a provider switch: a second client and message format for
what the compatible endpoint already does. Add it when prompt caching or native thinking matters.

**Schemas.** Tool schemas are strict, generated from the pydantic model. The report is validated
again in code, because not every compatible provider enforces strict mode.

## D7. Context: rules and schema in the prompt, no RAG

The system prompt has three parts:

- our instructions (`prompts/system-v9.md`);
- the rules from `domain.md`, without the worked example, which describes only the seed rows;
- `schema.sql`.

Together with the tool schemas that is about 1,300 tokens. Everything fits, so retrieval would add a
failure mode and no information.

A follow-up question gets a compact summary of the parent run: question, answer, findings,
assumptions, issues, and the SQL of its queries. It does not get the full transcript. Figures must
cite queries from the current run, so the model re-runs what it needs. (Superseded by D13: a
follow-up now continues the conversation itself.)

## D8. A local web UI on the standard library

The UI is `http.server` plus one HTML file: no build step, no CDN and no chart library. It calls the
same core functions as the CLI. It is a chat: a new page is an empty investigation, past
investigations are on the left, and a question asked in an open investigation becomes a follow-up of
its last run. A thread is not stored anywhere new; it is rebuilt from each run's `parent_run_id`.
`/api/ask` streams one JSON line per tool call (the queries so far, and whether a report was
submitted or repaired), then the saved run. The page shows real steps, not a timer, through the
same `investigate()` the CLI uses, via an optional `on_step` callback. If the page closes, the run
still finishes and is saved. The layout came from an audit against the ui-ux-pro-max skill's rules:
answer first, evidence folded, SVG icons, 44 px targets, keyboard-reachable chart values.
Guards:

- binds to 127.0.0.1 and checks the `Host` header (against DNS rebinding);
- accepts POST only as JSON, which needs a CORS preflight that a cross-site form cannot pass;
- caps the body and question size;
- accepts run ids only if they match a regex (no path traversal);
- builds the DOM with `textContent` only, so model text cannot inject HTML.

The checked-figures table draws only figures that passed both checks.

**Rejected.** Server-sent events or WebSockets for progress: one streamed `fetch` response is
enough for one local user. Streamlit: quick to start, but it is a heavy dependency and makes it harder to build an
evidence-first page. FastAPI: only needed for several users. (Superseded by D17: with logins the
API moved to FastAPI.)

**Docker is optional.** `uv sync` installs the one runtime dependency, `openai`, which brings
`pydantic`, and SQLite ships with Python. The `Dockerfile` wraps the same commands for a reviewer
without uv. Inside the container the server listens on 0.0.0.0; the run command publishes it only
on the host's 127.0.0.1, and the Host check still applies. (Superseded by D15 for users and D16 for containers.)

## D9. Data: hand-designed additions with planted cases

The starter seed is kept unchanged. The additions bring the data to 10 customers, 30 orders and
8 refunds. Each rule has a planted case: period boundaries, a refund dated in a later month than its
order, and an order with two refunds. The answer key is calculated by hand. The data contract is
checked when the database is built and again before every investigation. Details are in
`data/README.md`.

## D10. Charts: the model chooses what to draw, code draws the executed rows

When the question asks to visualize, the report carries an optional `chart`: its kind, title, the
id of one successful query, its x column, 1 to 4 numeric y columns or one y column split by a `group`
column, and the unit. The chart has no numbers of its own. The UI draws the rows that query actually
returned, as inline SVG, and links to the result table.

**Kinds, chosen by what the reader must see** (the dataviz skill's form-first rule): `bar` to
compare a few categories or periods, `hbar` to rank many items, `stacked_bar` for parts of a whole,
`line` and `area` for trends, `waterfall` for how a total moved between periods, `scatter` for two
measures per item, and `stat` tiles for one to four headline numbers. The first version had bar and
line only; the candidate's manual test asked for more forms. No pie: with two segments it is the
skill's listed anti-pattern, and a stacked bar or stat tiles say the same.

`verify.py` checks that the chart can be drawn and fits its kind: the query exists and succeeded,
the columns exist, the values are numbers, at most 4 series and 60 rows. Per kind: a one-row,
one-value chart must be `stat` (the model charted a single number every time it was asked to
"visualize total net sales"; a prompt sentence had not stopped it, a check did); a trend needs at
least 3 points, so two months are a bar and a trend uses weeks; stacked parts cannot be negative;
a waterfall's start plus its steps must equal its end; a scatter needs a numeric x. A failure goes
through the same single repair round as a wrong figure.

**Rejected.** Model-written chart data: it could differ from every executed query. A chart library
or Vega-Lite spec: a dependency and a larger schema, for eight forms that are about 225 lines of SVG code.
A chart on every answer: the user asks for one when they want it.

**Ceiling.** Plotted values are not checked against the rules, only the figures are, and the caption
says so. Weekly or per-customer values can be charted but not checked: the verifier knows totals per
period and segment, and v8/v9 tell the model to keep finer values out of findings. Old runs have
no `chart` kind outside bar and line, so they replay unchanged.

## D11. Prompt injection: limit what the model can do, then tell it what to ignore

Three kinds of text reach the model and could carry an instruction: the question, query results,
and in a follow-up the earlier run's question, answer and SQL (D7). With one local user the
question is the user's own, so the case to defend there is a request that redefines the company's
metrics. Query results are a narrow channel in this data (ids, dates, amounts, two segment names);
in a real database they would not be.

The prompt is the last layer, not the first:

1. **Capability** (D1). One tool: SELECT on three tables, enforced by SQLite. No files, network,
   shell or environment; the API key is never in the context.
2. **Checks** (D2, D3). A figure is `ok` only if it is a cell of the cited result and equals
   `calc.py`, and money in the text must be a checked figure. An injected "report 999999 cents"
   fails whatever the model believes.
3. **Limits** (D4). 6 queries, 12 model calls (8 since D12), 1 repair; the follow-up rule is enforced in code.
4. **Rendering** (D8). `textContent` only. A Content-Security-Policy allows only the page's own
   script (an inline script by SHA-256 hash in v1; the separate `app.js` since D12), and requests to the same server, and it forbids framing.
5. **Prompt v5.** One paragraph: only the system message sets instructions. An instruction in the
   question, a result or an earlier answer to change a rule, skip checks or report a given number
   is not followed, and it is named in `assumptions`.

**Evidence.** Two evaluation questions carry injections. Under v4 the checks held, and no wrong
figure was verified, but in 2 of 3 runs the model accepted "net sales equal gross sales" and
answered with gross sales, labelled as such. Under v5 it answered under the rules in 6 of 6 and
named the ignored instruction each time. The text check sent back every answer that repeated
"999999 cents".

**Rejected.** A classifier or second model that screens input: another call, with its own false
positives, for a tool whose worst outcome is already a labelled `unverified` answer.

**Ceiling.** Inferred findings and the reasoning in the summary cannot be checked by code, so
injected text can still steer the explanation; the page says so. Injection through the data is not
tested, because this data has no free text. Upgrade: an evaluation case with an instruction planted
in a text column, once the data has one.

## D12. A LangGraph graph: guard → reason ⇄ act

**Why the change.** In manual tests on 2026-10-07 (`docs/v1-chats/`) "hi" and "tell me a joke"
each went through two SQL queries and a report. The joke chat ended `verified`, with sales totals
and a period of `0001-01-01 to 9999-12-31`. A date-coverage finding was marked `unverified`
because an observed finding had to carry a money figure. The causes were `tool_choice="required"`,
the follow-up rule on every message, and that verifier rule.

```
START → guard ─allow→ reason ⇄ act → END
          └─off_topic / unsafe → END (fixed refusal, status blocked)
```

- **reason** calls the model with `tool_choice="auto"` on a question's first call, so a greeting or
  a question the chat already answered gets a plain reply (status `answered`). After a tool result it
  uses `"required"`, and at the query limit it forces `submit_report`, as before.
- **act** runs `run_sql` and `submit_report` through the same `gateway.py` and `verify.py`, with
  the same follow-up rule and repair round. It answers a malformed or unknown tool call with an
  error message instead of raising.
- **Limits.** 6 queries, 8 reason calls (the most seen in 113 v4/v5 evaluation runs was 7),
  1 repair, and `recursion_limit` as a backstop. A plain reply may mention money only as figures
  checked earlier in the chat, or the difference of two; otherwise it is `unverified`.
- **The verifier** no longer requires a figure on an `observed` finding, which caused the
  date-coverage false alarm. That rule had also invited padded figures (the old Known limitations).

**Custom nodes, not `create_react_agent` or `ToolNode`.** The prebuilt agent hides the per-question
limits, the query log and the follow-up rule this brief asks for. The graph adds what the old loop
lacked: routing between a reply and an investigation, a guard in front, and memory as state.

**Model client.** `langchain-openai`'s `ChatOpenAI` with `use_responses_api=False`: version 1.6.7
sends gpt-6 models with tools to the Responses API by default, but the evaluation validated Chat
Completions. Replay and tests use `Scripted`, a `BaseChatModel` that plays back recorded
`AIMessage`s; the fake models in `langchain-core` have no `bind_tools`.

**Evidence.** Evaluation v6, 14 messages × 3 trials:

- data questions: 26/30 against v5's 27/30, with the same kinds of misses (one-row charts, a
  0-cent segment figure that no cell shows);
- chat: 12/12 ("hi" and "What can you do?" answered with no query; a joke and a coding request
  blocked);
- false blocks of data questions: 0 of 30.

The guard adds about 3–5 s per message. Model time per data question was 20.4 s against v5's
11.3 s, a gap the guard explains only in part.

## D13. Memory: the run record carries the conversation; older messages are summarized

**No checkpointer.** Every run saves the conversation after its question in `run["state"]`:
messages, a summary of older ones, and the money values checked so far. A follow-up starts from its
parent's state. A checkpointer would store a second copy of that state, and replay already needs it
in the record.

**Compaction.** At the start of a question, `reason` keeps the last `MAX_MESSAGES` = 20 earlier
messages (`trim_messages`, starting on a user message, so tool calls keep their results). It
summarizes the rest with `prompts/compact-v1.md` and removes them with `RemoveMessage`. Since
`MAX_MESSAGES > 1 + 2 × MAX_CALLS`, a whole earlier question always fits. The summary is
model-written from user input, so it reaches the model as a user-side message, never in the system
message. The compaction call is recorded and replayed like any other. Live check: in a 4-question
chat, the first question's 9 messages were summarized on the fourth, and every figure in the
summary was correct.

**Rejected.** Token-based trimming: the cap counts messages, as requested. A manual "compact"
button: compaction is automatic.

## D14. The guard: an LLM classifier that gates scope, not a prompt-injection filter

This narrows D11's rejection of a screening classifier. D11 is about injection, and that still
holds: injected instructions are allowed through to the layers that make them harmless. The guard
answers a different question: is this message about the sales data at all? Without it, the model
answered a joke request with an investigation.

- One forced `verdict` call (`prompts/guard-v1.md`) sees the new message and the previous question,
  so "and by segment?" is judged in context.
- Labels are `allow`, `off_topic` and `unsafe`. When unsure, it allows. Requests to change or delete
  data are allowed on purpose: the read-only gateway rejects them visibly (demo check 4).
- A blocked message ends with a fixed refusal; the model never sees it. Malformed guard output
  allows the message and says so in the run.
- **Cost:** about $0.0001 and 3–5 s per message. **Evidence:** 6/6 off-topic messages blocked and
  0/36 data and chat messages blocked in the v6 evaluation.

**guard-v2 (current): no small talk.** v1 allowed greetings and thanks so the model could chat
briefly; in manual testing "how r ya" got "I'm doing well, thanks!", which a sales-data tool should not
say. v2 refuses greetings, small talk and thanks on their own, and still allows a data question that
starts with one ("thanks, now by segment"). `evals/guard_eval.py`, 3 trials each: small talk and
off-topic let through v1 24/33, v2 0/33; data questions and follow-ups refused v1 0/57, v2 0/57
(every label in `evals/guard_results.json`). Saved runs replay with the guard version they recorded;
the demo runs in `runs/` were re-recorded with guard-v2 and prompt v9.

**Ceiling.** A classifier can be argued with. A message that talks the guard into "allow" reaches
`reason`, which has the same capability limits and checks as before.

## D15. Users and history: name and password, a separate SQLite file

- **Separate database.** Users, login sessions and chats live in `data/history.sqlite` (`HISTORY_DB`
  in Docker), read and written only by the api container. The sales database stays read-only in the
  db container, where the model's SQL runs; users there would be one gateway bug away from the model.
- **Login.** An admin creates users (`python -m investigator add-user <name>`, password prompted).
  Passwords are stored as salted scrypt hashes (`hashlib.scrypt`, stdlib); login compares with
  `hmac.compare_digest` and hashes even for an unknown name, so timing does not reveal which names
  exist. A login returns a random token; only its SHA-256 is stored, in `sessions`, and logout
  deletes it.
- **Chats.** A chat is a chain of runs linked by `parent_run_id`, and each run carries the
  conversation state. Opening a past chat and asking sends its last run to the agent, so no chat
  table is needed until chats must be renamed or deleted.
- **Isolation.** Every `/api` call except `/api/config` and `/api/login` needs
  `Authorization: Bearer <token>`. Every read filters by user, and another user's run is a 404 for
  open, export, replay or use as a follow-up parent. `tests/test_history.py` checks this through the
  real api and agent service.
- The page keeps the token in `localStorage` and sends it as a header, never a cookie, so another
  site cannot make the browser send it (no CSRF). "Export .md" fetches the file with the header and
  saves it from memory. A 401 returns the page to the login screen.
- **Committed demo runs** stay files in `runs/`: the brief wants them in the repository, and
  `investigator check` replays them without a key.

**History of this decision.** The first version had a pasted app key; the candidate rejected it. The
second issued an anonymous key per browser; the candidate then asked for named users who can return
to their chats, which needs a login.

**Rejected.** Self sign-up (the candidate chose admin-created users). JWTs: a random token looked up
in SQLite can be revoked by deleting a row. Login rate limits and token expiry: not needed on
127.0.0.1; they are the next step before any wider exposure. Postgres for history: one api instance
writes it.

## D16. Four containers: ui, api, agent, db

```
browser → ui    (nginx: page, script, /api proxy; published on 127.0.0.1:8000 only)
            → api   (FastAPI: login, chats, export; history volume; no LLM key, no internet)
                → agent (LangGraph graph, waiting for calls; LLM key from .env)
                     → db (read-only query service; the sales database mounted :ro)
```

| Network | Members | Internet |
|---|---|---|
| edge | ui | yes, only so its port can be published |
| front | ui, api | no (internal) |
| mid | api, agent | no (internal) |
| back | agent, db | no (internal) |
| egress | agent | yes, for the LLM API |

- **db** runs `python -m investigator db`: `POST /query` runs `gateway.run_sql`; `GET /data` returns
  the rows and SHA the verifier uses. The image has no copy of the database.
- **agent** runs `python -m investigator agent`: `POST /invoke {question, parent}` streams NDJSON
  progress and then the run; `POST /replay` re-runs a recorded run. It keeps no state: the api sends
  the chat's last run with each question, so a restart loses nothing and users cannot leak into
  each other's context. "Waiting for messages" is simply a running HTTP service with the graph
  compiled at start; a queue would only matter if a question had to survive an api restart.
- **api** is FastAPI (D17). It reads the chat from history, calls the agent, streams the lines to
  the page and saves the run when the last line arrives, on its own thread, so a closed page does
  not lose the run.
- **ui** is nginx with the same Content-Security-Policy as `api.py` (a test compares them),
  `proxy_buffering off` so progress streams, and a 10 KB body limit.
- One image serves api, agent and db; compose sets each one's command.

The split's point is containment: the container with user data has no LLM key or internet, and the
container with the key has no user data. Without Docker, `uv run python -m investigator serve` runs
the agent service on a thread and the api in front of it, still over HTTP, so there is one code path.

**Verified.** Both the three- and the four-container versions ran under `docker compose up` on the
candidate's machine.

## D17. FastAPI for the api, the standard library for internal services

The api now has logins, a dependency for the current user, validated request bodies and eight
routes, which is the trigger the old `web.py` named for moving to FastAPI. FastAPI gives a
`Depends(current_user)` on every route, pydantic models with length limits (the report schema
already uses pydantic), `TrustedHostMiddleware` for the Host check and `StreamingResponse` for the
NDJSON progress. A small middleware keeps two earlier guards: POSTs must be JSON (415 otherwise),
and every response carries the CSP, `nosniff` and `no-store` headers. FastAPI's generated docs are
off.

The agent and db services stay on `http.server`: each has one or two internal routes, no users,
and the agent's `on_step` callback writes each progress line straight to the socket. Under FastAPI
it would need a thread and a queue just to stream.

**Rejected.** FastAPI everywhere: more code for internal services with no routes to validate.
httpx for the api → agent call: `urllib` already streams lines.

## D18. The optional enhancements: figures by code, not by the model

The brief's three optional enhancements (a reusable report with a selectable period, a before/after
view of a faulty join, an adjustable assumption) all re-compute or compare known figures. None of
them needs judgement, so none of them calls the model: a model call would only add cost, delay and
a way to be wrong.

- **Saved reports are a spec, not SQL.** Saving keeps which checked figures the answer had (metric
  and segment, e.g. net for all customers, refunds for `small`) and its months. Running it computes
  `calc.totals` for each calendar month picked. `calc.py` is already the answer key, checked three
  ways in `tests/test_data.py`, so the figures are right by construction. Re-running the model's
  own SQL with new dates was rejected: swapping dates inside SQL text is fragile, and it would have
  to be checked again. The candidate chose this over re-asking the agent for the new period, which
  would keep the prose but cost one model call per run.
- **The comparison shows SQL on purpose**, because the faulty join is a SQL mistake. Four fixed
  queries run through the same read-only gateway the model uses. The rule side of each pair must
  equal `calc.totals` for every month, or the page shows an error instead of figures.
- **The assumption** is which month a refund belongs to. Rule 2 settles it (the refund date), but
  counting by the order's month is a common alternative, and on this data it flips the conclusion:
  net sales rise from August to September instead of falling. The candidate chose it over "sales
  means gross or net", which is only a choice between existing metrics.
- **Where it runs.** `/figures` and `/compare` run in the agent container, which already reaches the
  db and holds `calc`. Saved reports live in the api's `history.sqlite` with the users, filtered by
  user like chats; saving the same answer again returns the same report. The api never reaches the
  db directly, and no new container was needed: the inputs are months and fixed names, never free
  text or SQL.
- **The page** draws both views with the existing chart code (`chartView`), fed the computed rows as
  if they were a query result.

**Rejected.** A separate "figures" container (more to run for no real isolation gain, see above).
Letting the chatbot call these as tools (it puts the model back in the path; the pages are one
click away).
