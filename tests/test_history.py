"""Login, chats and the api -> agent call: passwords and tokens are stored hashed, no user can reach another's
chats, and a question goes through the agent service and is saved with its chat."""
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from investigator import agent, agent_service, api, history
from investigator.llm import Scripted

ALLOW = AIMessage("", tool_calls=[{"id": "g", "name": "verdict", "args": {"label": "allow", "reason": "ok"}}])


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "DB", tmp_path / "history.sqlite")
    srv = agent_service.serve(port=0)  # the agent container, on a thread
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(api, "AGENT_URL", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setattr(agent_service, "live_model", lambda: Scripted(script=[ALLOW, AIMessage("Hello.")]))
    yield TestClient(api.app, base_url="http://127.0.0.1")
    srv.shutdown()


def login(client, name, password="correct horse"):
    history.add_user(name, password)
    r = client.post("/api/login", json={"name": name, "password": password})
    assert r.status_code == 200 and r.json()["name"] == name
    return {"Authorization": f"Bearer {r.json()['token']}"}


def ask(client, auth, question="hi", parent=None):
    r = client.post("/api/ask", headers=auth, json={"question": question, "parent_run_id": parent})
    assert r.status_code == 200, r.text
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert "progress" in lines[0] and "run" in lines[-1]
    return lines[-1]["run"]


def test_a_question_goes_through_the_agent_and_a_chat_continues_from_its_last_run(client):
    alice = login(client, "alice")
    first = ask(client, alice)
    assert first["status"] == "answered" and first["answer"] == "Hello."
    second = ask(client, alice, "hi again", first["run_id"])
    assert second["parent_run_id"] == first["run_id"]
    # the agent got the first turn's conversation with the second question
    assert [m["data"]["content"] for m in second["state"]["messages"] if m["type"] == "human"] == ["hi", "hi again"]
    assert [r["run_id"] for r in client.get("/api/runs", headers=alice).json()] == [second["run_id"], first["run_id"]]


def test_users_see_only_their_own_chats(client):
    alice, bob = login(client, "alice"), login(client, "bob")
    rid = ask(client, alice)["run_id"]
    ask(client, bob)

    assert client.get("/api/config").status_code == 200  # no login needed
    for auth in ({}, {"Authorization": "Bearer wrong"}):
        assert client.get("/api/runs", headers=auth).status_code == 401
        assert client.post("/api/ask", headers=auth, json={"question": "hi"}).status_code == 401
    assert client.post("/api/login", json={"name": "alice", "password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"name": "nobody", "password": "x"}).status_code == 401
    assert len(client.get("/api/runs", headers=alice).json()) == 1
    assert client.get(f"/api/runs/{rid}", headers=alice).status_code == 200
    for path in (f"/api/runs/{rid}", f"/api/runs/{rid}/report.md"):
        assert client.get(path, headers=bob).status_code == 404, path
    for path, body in ((f"/api/runs/{rid}/replay", {}), ("/api/ask", {"question": "and?", "parent_run_id": rid})):
        assert client.post(path, headers=bob, json=body).status_code == 404, path
    r = client.post(f"/api/runs/{rid}/replay", headers=alice, json={})
    assert r.status_code == 200 and r.json()["diffs"] == []


def test_secrets_are_stored_only_as_hashes_and_logout_ends_the_session(client):
    auth = login(client, "alice")
    token = auth["Authorization"].split()[1]
    with sqlite3.connect(history.DB) as con:
        password_hash = con.execute("SELECT password_hash FROM users").fetchone()[0]
        token_sha = con.execute("SELECT token_sha256 FROM sessions").fetchone()[0]
    assert "correct horse" not in password_hash and token not in token_sha and len(token_sha) == 64
    # JSON only, like every POST: another site's form cannot log in or ask
    assert client.post("/api/login", content="name=alice", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    assert client.post("/api/ask", headers=auth, json={"question": " "}).status_code == 400
    assert client.post("/api/logout", headers=auth, json={}).status_code == 200
    assert client.get("/api/runs", headers=auth).status_code == 401
    assert client.get("/api/runs", headers={"Host": "evil.example"}).status_code == 400  # DNS rebinding


def test_agent_unavailable_is_a_503(client, monkeypatch):
    auth = login(client, "alice")
    monkeypatch.setattr(api, "AGENT_URL", "http://127.0.0.1:9")
    r = client.post("/api/ask", headers=auth, json={"question": "hi"})
    assert r.status_code == 503 and "agent" in r.json()["error"]
    assert agent.MAX_QUERIES  # the agent module itself is untouched by the split


EXPECTED = json.loads(Path("data/expected.json").read_text())


def saved_answer(user_id, figures, run_id="r1"):
    """A saved answer with checked figures, as the agent records it; no LLM needed."""
    history.save({"run_id": run_id, "parent_run_id": None, "created_at": "2026-10-07T12:00:00+00:00",
                  "question": "Why are sales down?", "status": "verified", "verification": {"figures": figures}}, user_id)
    return run_id


def figure(metric, start, end, segment=None, status="ok"):
    return {"metric": metric, "period_start": start, "period_end_exclusive": end, "segment": segment, "status": status}


def test_a_saved_report_reruns_its_checked_figures_for_any_months(client):
    alice, bob = login(client, "alice"), login(client, "bob")
    alice_id = history.user_for_token(alice["Authorization"].split()[1])["user_id"]
    rid = saved_answer(alice_id, [figure("net", "2026-08-01", "2026-09-01"), figure("net", "2026-09-01", "2026-10-01"),
                                  figure("refunds", "2026-09-01", "2026-10-01", "small"),
                                  figure("gross", "2026-09-01", "2026-10-01", status="wrong")])  # not reused
    report_id = client.post("/api/reports", headers=alice, json={"run_id": rid}).json()["report_id"]
    assert client.post("/api/reports", headers=alice, json={"run_id": rid}).json()["report_id"] == report_id  # no duplicate
    saved = client.get("/api/reports", headers=alice).json()
    assert [(r["report_id"], r["source_run_id"]) for r in saved] == [(report_id, rid)]  # the page links back to the answer
    assert client.get("/api/reports", headers=bob).json() == []

    r = client.post(f"/api/reports/{report_id}/run", headers=alice, json={"from_month": "2026-08", "to_month": "2026-09"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["spec"] == {"columns": [["all", "net"], ["small", "refunds"]],
                                      "from_month": "2026-08", "to_month": "2026-09"}
    assert body["columns"] == ["month", "net_cents", "small_refunds_cents"]
    p, seg = EXPECTED["periods"], EXPECTED["segments"]
    assert body["rows"] == [["2026-08", p[0]["net_cents"], seg["2026-08-01"]["small"]["refunds_cents"]],
                            ["2026-09", p[1]["net_cents"], seg["2026-09-01"]["small"]["refunds_cents"]]]

    for months in ({"from_month": "2026-13", "to_month": "2026-13"}, {"from_month": "2026-09", "to_month": "2026-08"}):
        assert client.post(f"/api/reports/{report_id}/run", headers=alice, json=months).status_code == 400
    assert client.post(f"/api/reports/{report_id}/run", headers=bob, json={"from_month": "2026-08", "to_month": "2026-09"}).status_code == 404
    assert client.post("/api/reports", headers=bob, json={"run_id": rid}).status_code == 404
    unchecked = saved_answer(alice_id, [figure("net", "2026-08-01", "2026-09-01", status="wrong")], "r2")
    assert client.post("/api/reports", headers=alice, json={"run_id": unchecked}).status_code == 400


def test_compare_shows_the_planted_traps_next_to_the_rules(client):
    assert client.get("/api/compare").status_code == 401
    body = client.get("/api/compare", headers=login(client, "alice")).json()
    sep = {k: dict(zip(c["columns"], next(r for r in c["rows"] if r[0] == "2026-09"))) for k, c in body.items()}
    traps = EXPECTED["traps"]
    assert sep["faulty_join"]["correct_gross_cents"] == EXPECTED["periods"][1]["gross_cents"]
    assert sep["faulty_join"]["naive_join_gross_cents"] == traps["naive_join_september_gross_cents"]
