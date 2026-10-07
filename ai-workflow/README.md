# AI workflow used during this exercise

The setup was deliberately small: default Claude Code, one third-party plugin (hooks only), and the
application's own prompt and model settings. Two skills guided the web UI only. No subagents or MCP
servers were used.
Machine-readable record: [`manifest.json`](manifest.json).

## Tools and models

**Development time**

| Component | Version or setting | Status |
|---|---|---|
| Claude Code (CLI, Linux) | 2.1.284 | used |
| Model | Claude Opus 5.5 (`claude-opus-5-5`, set as `"model": "opus"`) | used |
| Reasoning effort | `high` in `settings.json`; `max` was also set during the session, a session setting not saved in a file | used |
| Permission mode | `auto`, a session setting; there are no permission rules in `settings.json` | used |
| Context compaction | `autoCompactWindow: 300000` | used (the session was compacted) |
| ponytail plugin | 4.10.0, commit `e3ba2aa6f1e6f0bc4d69eb09c9f0d0a93af56156`, user scope, MIT | used (hooks) |
| Built-in tools | Bash, Read, Write, Edit, WebFetch, WebSearch, ToolSearch, TaskStop | default |
| Shell tools | uv 0.12.15, Python 3.13.15, pytest 9.1.1, ruff 0.16.10 (through `uvx`), sqlite3, curl, Chromium 152 (headless screenshots), Playwright 1.63.0 through `uv run --with playwright` (headless sign-in and chat check; not a project dependency), Docker Compose 5.5.1 (`docker compose config` only: no daemon); Node.js 26.8.1 runs the hooks | used |
| dataviz skill (bundled with Claude Code) | Claude Code 2.1.284 | used: chart palette and mark rules; its validator checked the palette against the page's surfaces |
| ui-ux-pro-max skill, third party, MIT | [repository](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill), commit `477bcb2`; cloned to a temporary directory, not installed | used at the candidate's request: its local search script and rule lists drove the UI audit (it makes no network calls). Its suggested web fonts were not adopted: the page loads nothing from a CDN |
| Subagents, MCP servers | none | not-used |

**Inside the application**

| Setting | Value | Where |
|---|---|---|
| Framework | LangGraph 1.2.14 (`StateGraph`: guard → reason ⇄ act), langchain-core and langchain-openai 1.6.7, openai 3.24.0; FastAPI 0.142.2 on uvicorn 0.54.0 for the api (httpx 0.28.1 for its tests) | `investigator/agent.py`, `investigator/api.py`, `pyproject.toml` |
| API | OpenAI Chat Completions through `ChatOpenAI(use_responses_api=False)`, any compatible endpoint. langchain-openai 1.6.7 otherwise switches gpt-6 models with tools to the Responses API, which the evaluation did not cover | `investigator/llm.py`, `LLM_BASE_URL` |
| Model | `gpt-6-luna` | `LLM_MODEL` |
| Reasoning effort | `none` (gpt-6 models accept function tools in Chat Completions only without reasoning) | `LLM_REASONING_EFFORT` |
| Temperature, max tokens | provider defaults (not set) | none |
| Tools | `run_sql`, `submit_report`; strict JSON schemas; `parallel_tool_calls=false`. `tool_choice`: `auto` on a question's first call (plain reply or tool), `required` after a tool result, `submit_report` forced at the query limit. The guard forces its `verdict` tool; compaction calls use `none` | `investigator/agent.py` |
| Client | timeout 60 s, 2 SDK retries | `investigator/llm.py` |
| Limits | 6 query attempts, 8 reason calls, 1 repair round per question; 20 earlier messages kept verbatim, older ones summarized; 200 rows, 2 s per query, 4,000 SQL characters | `agent.py`, `gateway.py` |

Every saved run records the model, base URL, reasoning effort, prompt paths and their SHA, database SHA
and limits under `config`.

## Configuration files

| What | Path in this repository | Original location |
|---|---|---|
| Application prompts, current | `prompts/system-v9.md`, `prompts/guard-v2.md`, `prompts/compact-v1.md` | same |
| Earlier system prompt versions | `prompts/system-v1.md` to `system-v8.md` | same; versions are immutable, and saved runs name the ones they used |
| Business rules and schema, appended to the prompt | `data/starter/domain.md`, `data/starter/schema.sql` | starter pack, unchanged |
| Tool definitions and report schema | `investigator/agent.py` (`RUN_SQL`, `SUBMIT`), `investigator/report.py` (`Report`) | same |
| Environment variable names | [`../.env.example`](../.env.example) | same |
| Claude Code settings, relevant part | `claude-code/settings.json` | `~/.claude/settings.json` (user scope) |
| ponytail hooks and the scripts they run | `claude-code/ponytail-4.10.0/hooks/` | `~/.claude/plugins/cache/ponytail/ponytail/4.10.0/hooks/` |
| Ruleset text the hooks inject | `claude-code/ponytail-4.10.0/skills/ponytail/SKILL.md` | same plugin |
| Plugin license | `claude-code/ponytail-4.10.0/LICENSE` | same plugin |

There is no project or user `CLAUDE.md`, no `.claude/` folder in the project, and no custom agent
definitions.

**Omitted or redacted (values not shown):**

- **`.env` (API keys).** Never committed; `.gitignore` excludes it.
- **The OpenRouter key.** It was used during development only, to check model availability and list
  prices. It is not needed to run or replay the application.
- **`autoMode.environment` in `settings.json`.** An auto-generated description of the developer's
  machine and unrelated local projects. It is unrelated to this exercise.
- **`theme` and `agentPushNotifEnabled`.** Terminal display settings.
- **Model settings for other models.** Not used here.
- **Claude Code auto-memory.** It contains notes about other projects. It is unrelated and was not
  used for this work.
- **Account-level claude.ai connectors.** They were available in the session but not used.
- **The full chat transcript.** Not required. The workflow example below and `docs/llm-usage.md`
  cover it.

### What the hooks do (read before enabling)

All three hooks are plain Node.js scripts that run locally. They make no network calls and start no
other processes.

| Event | Script | Effect |
|---|---|---|
| `SessionStart` (startup, resume, clear, compact) | `ponytail-activate.js` | Writes the mode flag `~/.claude/.ponytail-active` and prints the ruleset from `SKILL.md`, filtered to the active level (`full`), as hidden session context. If no status line is configured, it once writes `~/.claude/.ponytail-statusline-nudged` and asks Claude to offer status-line setup. That offer was not taken. |
| `SubagentStart` | `ponytail-subagent.js` | Gives subagents the same ruleset. No subagents were used. |
| `UserPromptSubmit` | `ponytail-mode-tracker.js` | Watches prompts for `/ponytail …` or "stop ponytail" and updates the mode flag. `/ponytail default <mode>` would save a default to `~/.config/ponytail/config.json`. That was not used. |

The effect on this exercise: the ruleset pushed toward the standard library and the fewest files, no
speculative abstractions, and `ponytail:` comments that name each deliberate shortcut's limit and
upgrade path. Examples are in `investigator/history.py`, `investigator/agent_service.py` and `investigator/verify.py`. The candidate
asked for this style explicitly.

## One workflow example

The candidate gave this instruction to Claude Code:

> "be the best across all the applicants in this project, but not overengineering, we must show also
> ability to find cheapest working ways but not underperforming"

**Configuration that shaped the result.** The ponytail hook ruleset (simplest working solution) and
the application prompt.

**How the result was checked.** Every change went through the same gates: scripted tests, a live
run, the verifier's status, then the evaluation in `evals/run_eval.py`, which runs the questions
(5 at first, 14 now: 10 data questions and 4 chat messages) × 3 trials through the real pipeline.
The rewritten page was also driven headless with Playwright; that check caught a sign-in bug
(the sidebar showed "null" after the model name).

**Correction.** The first live run ended `unverified`. The model's segment query joined without keys
and gave each segment the full refund total, and two of its queries failed with SQL errors. Prompt v2
added three SQL rules. Query errors fell from 9 of 39 to 0 of 36 for gpt-6-luna. Later, the rubric
also required an `unknown` finding for "why" questions. Re-scoring the saved runs showed v2 met it in
only 3 of 6. Prompt v3 brought it to 6 of 6. The full account, with excerpts, is in
[`../docs/llm-usage.md`](../docs/llm-usage.md).

**Correction: the architecture.** The candidate's manual test of v1 (`../docs/v1-chats/`) showed
that every message was forced through SQL: "hi" and "tell me a joke" each got two queries and a
report. Claude Code had optimized for the evaluation, which had no chat messages. The rewrite
(LangGraph guard → reason ⇄ act, memory, users, containers; later logins, FastAPI and a separate agent container) was the candidate's design; the
evaluation gained 4 chat cases so this cannot regress unseen.

## Reproduce or replay

**The application, without credentials.**

```bash
uv sync
uv run pytest                              # 64 tests, scripted and replayed model responses
uv run python -m investigator check        # replay every saved real run in runs/
uv run python -m evals.run_eval --rescore  # re-score the saved evaluation runs
uv run python -m investigator serve        # UI with your chats and replay, http://127.0.0.1:8000
```

**New model calls.** Copy `.env.example` to `.env`, set `LLM_API_KEY`, then run
`uv run --env-file .env python -m investigator ask "…"`.

**The development setup.** Install Claude Code 2.1.x, then:

```bash
claude plugin marketplace add https://github.com/dietrichgebert/ponytail.git
claude plugin install ponytail@ponytail
```

Then check that the installed version is 4.10.0 (commit above), or compare the installed hooks
with the snapshot in `claude-code/ponytail-4.10.0/`. Merge `claude-code/settings.json` into
`~/.claude/settings.json`. Read the hook table above before enabling the plugin.

## Decisions and limitations

- **Why this setup.** One general coding agent with a strong model was enough for a one-day,
  single-repository task. The plugin added a coding style; it added no capability. More agents,
  skills or hooks would have been configuration with nothing to do.
- **The model is the part that gets checked.** The application's correctness comes from
  deterministic checks around it (`verify.py`, `calc.py`, replay), not from trusting the model. The
  same applies to Claude Code's output, through tests, replay and the evaluation.
- **What I would change.** For a team, I would commit a project `.claude/settings.json` with
  explicit permission rules: allow `uv run`, `pytest` and `ruff`, and require a prompt for network
  and git push. The session's auto mode would no longer be needed. I would also add a CI job that
  runs `pytest`, `investigator check` and `ruff`.
