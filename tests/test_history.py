"""Users and chat history over HTTP: keys are stored hashed, and no user can reach another's chats."""
import json
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from langchain_core.messages import AIMessage

from investigator import agent, history, web
from investigator.llm import Scripted


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB", tmp_path / "history.sqlite")
    shutil.copy("data/investigation.sqlite", tmp_path / "db.sqlite")
    monkeypatch.setattr(agent, "DB", tmp_path / "db.sqlite")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def call(base, path, key=None, body=None):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None, headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def hello(user, parent=None):
    """A saved chat message of `user`, made with a scripted model."""
    script = [AIMessage("", tool_calls=[{"id": "g", "name": "verdict", "args": {"label": "allow", "reason": "ok"}}]),
              AIMessage("Hello.")]
    run = agent.chat("hi", Scripted(script=script), parent, db_path=agent.DB)
    history.save(run, user)
    return run


def session(base):
    """What the page does on its first visit: get this browser's own key."""
    status, body = call(base, "/api/session", body={})
    assert status == 200
    return json.loads(body)["key"]


def test_users_see_only_their_own_chats(server):
    alice, bob = session(server), session(server)
    assert alice != bob
    first = hello(history.user_for_key(alice))
    hello(history.user_for_key(alice), first)
    hello(history.user_for_key(bob))
    rid = first["run_id"]

    assert call(server, "/api/config")[0] == 200  # no key needed
    for key in (None, "wrong"):
        assert call(server, "/api/runs", key)[0] == 401
        assert call(server, "/api/ask", key, {"question": "hi"})[0] == 401
    assert len(json.loads(call(server, "/api/runs", alice)[1])) == 2
    assert len(json.loads(call(server, "/api/runs", bob)[1])) == 1
    assert call(server, f"/api/runs/{rid}", alice)[0] == 200
    for path, body in ((f"/api/runs/{rid}", None), (f"/api/runs/{rid}/report.md", None),
                       (f"/api/runs/{rid}/replay", {}), ("/api/ask", {"question": "and?", "parent_run_id": rid})):
        assert call(server, path, bob, body)[0] == 404, path
    status, body = call(server, f"/api/runs/{rid}/replay", alice, {})
    assert status == 200 and json.loads(body)["diffs"] == []


def test_keys_are_stored_only_as_hashes(server):
    key = session(server)
    with sqlite3.connect(history.DB) as con:
        stored = con.execute("SELECT key_sha256 FROM users").fetchone()[0]
    assert key not in stored and len(stored) == 64 and history.user_for_key(key) == 1
    # JSON only, like every POST: another site's form cannot create users
    req = urllib.request.Request(server + "/api/session", b"x=1", {"Content-Type": "application/x-www-form-urlencoded"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 415
