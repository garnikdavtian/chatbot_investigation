"""Command line for the investigator.

  uv run --env-file .env python -m investigator ask "Why did net sales change from August to September 2026?"
  uv run --env-file .env python -m investigator ask "Which segment drove it?" --parent <run_id>   # same chat
  uv run python -m investigator replay <run_id>    # recorded responses, no API key
  uv run python -m investigator check              # replay every saved run, exit 1 on any difference
  uv run python -m investigator add-user alice          # asks for a password; users log in on the page
  uv run python -m investigator add-user alice --demo-chats   # ... and gives them the saved demo chats in runs/
  uv run python -m investigator add-user admin --password admin123   # no prompt (start.sh's local default login)
  uv run --env-file .env python -m investigator serve   # web UI on http://127.0.0.1:8000 (api + agent)
"""
import argparse
import getpass
import json
import sqlite3
import sys

from investigator import agent, report
from investigator.llm import LLMError, live_model


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="investigator", description="Chat with a bounded agent over the sales database.")
    sub = p.add_subparsers(dest="cmd", required=True)
    ask = sub.add_parser("ask", help="ask the live model a question")
    ask.add_argument("question")
    ask.add_argument("--parent", help="run id of the earlier message in the same chat")
    sub.add_parser("replay", help="replay one saved run without an API key").add_argument("run_id")
    sub.add_parser("check", help="replay every saved run; exit 1 if any result differs")
    serve = sub.add_parser("serve", help="web UI on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 only inside a container")
    agent_p = sub.add_parser("agent", help="serve the agent over HTTP (the agent container)")
    agent_p.add_argument("--port", type=int, default=8002)
    agent_p.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 only inside a container")
    add_user = sub.add_parser("add-user", help="create a user who can log in to the web UI")
    add_user.add_argument("name")
    add_user.add_argument("--demo-chats", action="store_true",
                          help="also give the user the saved demo chats in runs/ (each run can belong to one user)")
    add_user.add_argument("--password", help="set it without a prompt (visible in the process list: local use only)")
    db = sub.add_parser("db", help="serve the read-only query tool over HTTP (the db container)")
    db.add_argument("--port", type=int, default=8001)
    db.add_argument("--host", default="127.0.0.1", help="bind address; 0.0.0.0 only inside a container")
    a = p.parse_args(argv)

    if a.cmd == "serve":
        from investigator.api import serve
        serve(a.port, a.host)
        return 0

    if a.cmd == "agent":
        from investigator import agent_service
        print(f"agent service on port {a.port}, model {agent_service.config()['model']}")
        agent_service.serve(a.port, a.host).serve_forever()
        return 0

    if a.cmd == "add-user":
        from investigator import history
        if history.user_exists(a.name):  # checked before the prompt, so a rerun of start.sh asks nothing
            sys.exit(f"a user named {a.name!r} already exists")
        password = a.password or getpass.getpass(f"password for {a.name}: ")
        if len(password) < 8 or (not a.password and password != getpass.getpass("again: ")):
            sys.exit("passwords must match and be at least 8 characters")
        try:
            user_id = history.add_user(a.name, password)
        except sqlite3.IntegrityError:
            sys.exit(f"a user named {a.name!r} already exists")
        demo = 0
        for path in sorted(report.RUNS.glob("*.json")) if a.demo_chats else []:
            try:
                history.save(json.loads(path.read_text()), user_id)
                demo += 1
            except sqlite3.IntegrityError:
                pass  # run ids are unique: another user already has this demo chat
        print(f"created {a.name}" + (f" with {demo} demo chat messages" if a.demo_chats else "") + "; they can log in on the web UI")
        return 0

    if a.cmd == "db":
        from investigator import gateway
        print(f"read-only query service for {agent.DB} on port {a.port}")
        gateway.serve(agent.DB, a.port, a.host).serve_forever()
        return 0

    if a.cmd == "ask":
        parent = report.load(a.parent) if a.parent else None
        try:
            model = live_model()
        except LLMError as e:
            sys.exit(str(e))
        run = agent.chat(a.question, model, parent)
        path = report.save(run)
        print(f"{run['status']}: " + report.prose(run["answer"] or run["error"]))
        for issue in (run["verification"] or {}).get("issues", []):
            print(f"  ! {report.prose(issue)}")
        print(f"saved {path.relative_to(report.ROOT)} and .md")
        return 0 if run["status"] in ("verified", "answered", "blocked") else 1

    runs = [report.load(a.run_id)] if a.cmd == "replay" else [report.load(f.stem) for f in sorted(report.RUNS.glob("*.json"))]
    differs = 0
    for run in runs:
        parent = report.load(run["parent_run_id"]) if run["parent_run_id"] else None
        new, diffs = agent.replay(run, parent)
        print(f"{run['run_id']}  {new['status']:<10}  " + ("reproduced" if not diffs else "DIFFERS: " + "; ".join(diffs)))
        differs += bool(diffs)
    return 1 if differs else 0


if __name__ == "__main__":
    sys.exit(main())
