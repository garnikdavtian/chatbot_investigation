# LLM usage note

## Tools and models

| Where | Tool or model | Used for |
|---|---|---|
| Development | Claude Code 2.1.284 with Claude Opus 5.5 (`claude-opus-5-5`) | Planning, all code, tests, data additions, prompts, docs, running the evaluations |
| Development | ponytail plugin 4.10.0 for Claude Code (hooks) | Adds a "simplest working solution" ruleset to every session. See `ai-workflow/README.md` |
| Development | Skills: `dataviz` (bundled with Claude Code) and the third-party `ui-ux-pro-max` (commit `477bcb2`, at the candidate's request) | Chart palette and its validator; the UI/UX audit of the chat page |
| Development | Built-in Claude Code tools: shell, file edit, web fetch and search | uv, pytest, ruff (`uvx`), sqlite3, curl, headless Chromium for UI screenshots; provider docs, model lists and prices |
| Application | OpenAI Chat Completions, **`gpt-6-luna`**, reasoning effort `none`, through `langchain-openai` 1.6.7 in a LangGraph 1.2.14 graph | The guard, the investigating model and conversation summaries. Chosen by evaluation (see the README) |
| Evaluation only | `gpt-5.6-luna`, `gpt-6-sol` | Comparison and ceiling. `gpt-6.1-sol` was rejected: it needs the Responses API to use function tools with reasoning |

## What was generated

Claude Code generated everything outside `data/starter/`, which holds the starter pack unchanged:

- the application;
- the tests;
- the data additions and the hand-checked answer key;
- the prompts, the evaluation and the docs.

The candidate set the direction, reviewed the work and made the product decisions: which assignment
to take, a final UI without Streamlit, the review gates and, after testing v1 by hand, the move to a
LangGraph agent with a guard, memory, per-user keys and three containers (correction 7), and
later named logins, a FastAPI api and a separate agent container (correction 9). Claude Code ran every check
listed below.

## One representative instruction and workflow

> "be the best across all the applicants in this project, but not overengineering, we must show also
> ability to find cheapest working ways but not underperforming"

This instruction turned model choice into a measurement instead of a guess. `evals/run_eval.py`
runs fixed questions through the real pipeline (the same loop, checks and limits as the app). It
scores each run by the verifier and by the figures each answer needs. The cheapest model that matched
the larger one was kept. The same loop was used for every change:

```
build -> scripted tests -> live run -> verifier result -> evaluation -> fix -> re-record -> replay check
```

## One correction, with numbers

**1. The first live run.** The question was "Why did net sales change between August and September
2026?", asked of gpt-5.6-luna with prompt v1. The run ended `unverified`, and the verifier showed why:

```
q1 sql   circular reference: refunds        (a CTE named "refunds" shadowed the table)
q3 sql   ambiguous column name: c.segment
finding 2: refunds 2026-09-01..2026-10-01 small = 21000 cents [q4] is query_error (rules give 16000)
finding 2: refunds 2026-09-01..2026-10-01 large = 21000 cents [q4] is query_error (rules give 5000)
... 4 more segment figures
```

The overall totals were right. The segment query, however, joined without keys, so each segment
received the full refund total. The report looked plausible. The system caught the error and did
not present the answer as checked.

**2. The fix, in the prompt** (`prompts/system-v1.md` → `system-v2.md`). The first two rules target
the SQL errors; the third makes the model check its own breakdowns:

```
- Join only on keys: `refunds.order_id = orders.order_id` and `orders.customer_id = customers.customer_id`.
- Keep each query to one breakdown, and give CTEs names that differ from the table names.
- Before you report a breakdown, check that its parts add up to the overall totals. If they do not, fix the query.
```

Round 1 of the evaluation also showed that 4 of the 5 failures were correct amounts written in the
text without being attached as figures. Two more changes followed: one repair round, with problems
sent back to the model but never the expected values, and the prompt rule "Attach every amount you
mention to a finding's `metrics`".

**3. The effect, measured** (5 questions × 3 trials, `evals/results.json`):

| Model | Query errors, v1 | Query errors, v2 |
|---|---|---|
| gpt-5.6-luna | 8 of 38 | 2 of 34 |
| gpt-6-luna | 9 of 39 | **0 of 36** |

**4. A later improvement** (v2 → v3). The rubric was tightened so that a "why" question must also
get an `unknown` finding for what the data cannot explain (brief, check 5). Re-scoring the saved
runs, without new model calls, showed that v2 did this in only 3 of 6 "why" runs. A two-line prompt
change (`prompts/system-v3.md`) brought it to 6 of 6. v3 also keeps 15/15 with no query errors, at
the same cost.

**5. Charts** (v3 → v4). The first live chart run plotted August to October. The data covers one day
of October, so the chart showed sales collapsing. v4 tells the model to check date coverage and to
leave out or label a partial month, and the evaluation now checks that. The first v4 round (20/24)
also showed the model charting a single number in 3 of 3 runs when asked to "visualize total net
sales for September". A rule in `verify.py` now rejects a one-value chart; the repair round removed
the chart in 3 of 3, and v4 scored 24/24. The runs of both rounds stay in `evals/runs/`.

**6. Injection** (v4 → v5). Asked what protects against prompt injection, the honest answer was:
the design (read-only tool, checks in code, limits, plain-text rendering), but nothing in the
prompt, no evaluation case and no Content-Security-Policy. v5 adds one paragraph, the evaluation two
injection questions, and the page a hash-based CSP. Re-measured side by side on the same 10
questions, v4 accepted "net sales equal gross sales" in 2 of 3 runs, answering with gross sales;
v5 answered under the rules in 6 of 6. Both scored 27/30 overall.

**7. The candidate's manual test** (v1 → the graph, prompt v6). The candidate chatted with v1 by hand
(`docs/v1-chats/`). 3 of 4 chats went wrong, in ways the evaluation could not see, because all of
its questions were data questions:

- "hi" and "tell me a joke" were each forced through 2 SQL queries and a report; the joke chat even
  ended `verified`, with sales totals for `0001-01-01 to 9999-12-31`;
- a date-coverage finding was marked `unverified` only because an observed finding had to carry a
  money figure.

The causes were in the design: every message was an investigation (`tool_choice="required"`, the
follow-up rule, a figure on every observed finding). The rewrite (decisions D12–D16) routes each
message: a guard blocks off-topic ones, and the model may answer in plain text. The evaluation gained
4 chat cases. v6 result: chat 12/12, 0 false blocks of 30 data questions, data 26/30 against v5's
27/30, with the same kinds of misses. While building it, the live 4-question memory check exposed an
amount written as "10000-cent", which neither the money check nor the dollar rendering matched; the
shared pattern now accepts it, with a test.

**8. Chart kinds** (v6 → v9). The candidate's second manual test found only bar charts. Claude Code
added seven forms, per-kind checks and prompt v7, and four evaluation cases (weekly plot, waterfall,
customer ranking, headline numbers). The evaluation then showed two problems:

- v7 passed 7 of 12 chart runs. In the misses, the verifier had rejected per-customer, per-week and
  per-step amounts, because it only checks totals per period and segment. v8 tells the model which
  figures can be checked: 10/12.
- v7 also cost the main "why" question its `unknown` finding: 1/3, against v6's 3/3. A focused
  re-run (6 trials each, not saved) gave v6 6/6 and v8 3/6. The model now wrote the limit into
  `open_questions` instead. Rewording the prompt rule (v9) still gave 3/6, and removing either new
  chart bullet gave 4/6, so no single sentence caused it. Adding the rule to the `Finding.kind`
  schema description gave 5/6 in the focused run and 3/3 in the evaluation.

v9 result: data 27/30 (v6 26/30), chart forms 11/12, chat 12/12, 0 false blocks. The schema
change also applies to any re-run of older prompts, so the v6 row predates it.

**9. Logins and a separate agent** (anonymous keys → named users, D15–D17). The candidate asked for a
FastAPI api and a separate agent container, a login page, users with their chat sessions in the
database, and an agent "idle all the time waiting for the message". Claude Code built it and
corrected three points of the request: the idle agent is just a running HTTP service with no state
of its own; chat sessions are rows linked to a user, and they already existed as runs; and users
must not go in the sales database the model's SQL runs against. A login by name alone was flagged
as no protection; the candidate chose passwords and admin-created users. A browser test of the new
page failed at first because the page's CSP blocked the test tool's own script evaluation, which
confirmed the CSP; the test ran with the tool's CSP bypass.

## Checks of Claude Code's own output

- 61 tests, `ruff`, and a replay of every saved run after each change. The CSP was checked in a
  headless browser: the page and its chart render, and no violations are logged.
- The UI was reviewed through headless-browser screenshots at desktop and phone width. This caught
  links that were invisible in dark mode and chart labels that shrank to unreadable on a phone; both
  were fixed. The chart colours were checked with a colour-vision validator.
- A new test failed on its first run because it parsed plain-text tool replies as JSON. The fix was
  in the test; the code under test was correct.
- In v1's live DELETE run, the rule "an observed finding needs a figure" made the model attach an
  unrelated net-sales figure to the finding "the DELETE was rejected". The rule was removed with the
  rewrite (D12).
- After the rewrite, the sign-in screen was driven in headless Chromium: wrong key rejected, chat,
  blocked message, investigation, sign out. This caught "gpt-6-lunanull" in the sidebar, where
  `replaceChildren` printed a `null`; fixed.
