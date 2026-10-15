import json

import pytest
from fastapi.testclient import TestClient

from fake_ms import FakeMicrosoft
from integrations import ExecContext
from jarvis_server import create_app


class Clock:
    def __init__(self):
        self.t = 2_000_000.0

    def __call__(self):
        return self.t


BASE_ENV = {"AI_PROVIDER": "offline", "MS_GRAPH_CLIENT_ID": "11111111-2222-3333-4444-555555555555"}


@pytest.fixture
def ms():
    return FakeMicrosoft()


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, ms, clock, env=None):
    app = create_app(env={**BASE_ENV, **(env or {})}, data_dir=tmp_path, clock=clock, http=ms.client())
    app.state.integrations.sleep = lambda s: None
    return app, TestClient(app)


def connect(c, app):
    flow = c.post("/integrations/graph/connect").json()
    assert flow["user_code"] == "ABCD-1234"
    g = app.state.integrations.graph
    assert g.poll_device_flow() == "pending"
    assert g.poll_device_flow() == "connected"


def chat(c, text):
    return c.post("/chat", json={"text": text, "session_id": "s"}).json()


# ---- configuration / sign-in ------------------------------------------------

def test_not_configured_without_client_id(tmp_path, ms, clock):
    app = create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path, clock=clock, http=ms.client())
    c = TestClient(app)
    assert c.get("/integrations/status").json()["microsoft_graph"]["state"] == "NOT_CONFIGURED"
    r = c.post("/integrations/graph/connect")
    assert r.status_code == 503 and r.json()["detail"]["code"] == "NOT_CONFIGURED"
    # A model asking for the tool anyway gets NOT_CONFIGURED, never fake mail.
    app.state.provider.respond = lambda sid, t: {"reply": "", "steps": [{"tool": "email_summarize", "args": []}]}
    out = chat(c, "summarize my email")
    assert out["error_code"] == "NOT_CONFIGURED" and "data" not in out
    assert ms.calls == []


def test_device_code_sign_in_and_status_never_exposes_tokens(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    assert c.get("/integrations/status").json()["microsoft_graph"]["state"] == "NOT_CONNECTED"
    connect(c, app)
    st = c.get("/integrations/status").json()["microsoft_graph"]
    assert st["state"] == "CONNECTED" and st["account"] == "a***@example.com"
    assert "rt1" not in json.dumps(st) and "at1" not in json.dumps(st)
    assert "rt1" in (tmp_path / "secrets.json").read_text()  # plaintext without JARVIS_SECRET_KEY
    assert st["tokens_encrypted"] is False


def test_tokens_encrypted_with_secret_key(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock, {"JARVIS_SECRET_KEY": "correct horse battery staple"})
    connect(c, app)
    raw = (tmp_path / "secrets.json").read_text()
    assert "rt1" not in raw and "at1" not in raw
    assert c.get("/integrations/status").json()["microsoft_graph"]["tokens_encrypted"] is True


def test_expired_refresh_token_returns_not_configured(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["read"]})
    clock.t += 7200  # access token expired
    ms.refresh_revoked = True
    out = chat(c, "summarize my email")
    assert out["error_code"] == "NOT_CONFIGURED"
    assert c.get("/integrations/status").json()["microsoft_graph"]["state"] == "NOT_CONNECTED"


# ---- read / summarize ------------------------------------------------------

def test_summarize_requires_read_grant_and_returns_real_data(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    out = chat(c, "summarize my unread email")
    assert "permission" in out["reply"] and not any("inbox" in x for x in ms.calls)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["read"]})
    out = chat(c, "summarize my unread email")
    assert out["summary_source"] == "rule_based"
    assert "1 messages, 1 unread" in out["reply"] and "Priya" in out["reply"]
    assert out["data"]["messages"] == [{"from": "Priya", "subject": "Project review",
                                        "received": "2026-10-15T09:00:00Z", "unread": True}]
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "server_read" in audit and "Project review" not in audit


def test_ai_summary_used_when_provider_has_a_model(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["read"]})
    app.state.integrations.summarizer = lambda instruction, text: "Priya wants to move the review." if "Priya" in text else None
    out = chat(c, "check my email")
    assert out["summary_source"] == "ai" and "Priya wants to move the review." in out["reply"]


# ---- draft / send ------------------------------------------------------------

def send_plan(c):
    out = chat(c, "email bob@example.com about Lunch saying see you at 1")
    plan = out["plan"]
    assert plan["steps"][0]["tool"] == "email_send"
    return plan["id"]


def test_send_needs_grant_approval_and_confirmation_then_verifies(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    pid = send_plan(c)
    r = c.post(f"/plans/{pid}/approve").json()
    assert r["plan"]["status"] == "blocked" and ms.messages == {}  # default deny
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["send"]})
    r = c.post(f"/plans/{pid}/next").json()
    assert r["plan"]["status"] == "awaiting_confirmation" and ms.sends == 0
    assert r["confirmation"]["summary"] == 'Send email to bob@example.com — "Lunch": see you at 1'
    r = c.post(f"/plans/{pid}/steps/0/confirm").json()
    assert r["dispatch"] is None  # executed on the server, not on the phone
    step = r["plan"]["steps"][0]
    assert r["plan"]["status"] == "completed" and step["verification"] == "graph_sent_items"
    assert ms.sends == 1 and list(ms.messages.values())[0]["isDraft"] is False


def test_throttled_send_is_retried_only_after_fresh_confirmation(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["send"]})
    pid = send_plan(c)
    c.post(f"/plans/{pid}/approve")
    ms.send_mode = "429"
    r = c.post(f"/plans/{pid}/steps/0/confirm").json()
    assert r["plan"]["status"] == "retry_scheduled" and ms.sends == 0
    ms.send_mode = "ok"
    clock.t += 2
    r = c.post(f"/plans/{pid}/next").json()
    assert r["plan"]["status"] == "awaiting_confirmation" and ms.sends == 0
    r = c.post(f"/plans/{pid}/steps/0/confirm").json()
    assert r["plan"]["status"] == "completed" and ms.sends == 1
    assert len(ms.messages) == 1  # the saved draft was reused, not duplicated


def test_timeout_after_send_is_uncertain_not_retried(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["send"]})
    pid = send_plan(c)
    c.post(f"/plans/{pid}/approve")
    ms.send_mode = "timeout_after_apply"
    r = c.post(f"/plans/{pid}/steps/0/confirm").json()
    assert r["plan"]["status"] == "awaiting_verification" and r["plan"]["steps"][0]["status"] == "uncertain"
    assert c.post(f"/plans/{pid}/next").status_code == 409
    assert ms.sends == 1


def test_send_executor_detects_already_sent_draft(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    ms.messages["draft9"] = {"id": "draft9", "isDraft": False, "subject": "x"}
    ctx = ExecContext("p", 0, "p-0-2", "s", {"draft_id": "draft9"}, lambda ref: None)
    out = app.state.integrations.run("email_send", {"to": "bob@example.com", "subject": "x", "body": "y"}, ctx)
    assert out["outcome"] == "succeeded" and ms.sends == 0


def test_send_not_leaving_drafts_is_uncertain(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    ms.send_mode = "stay_draft"
    ctx = ExecContext("p", 0, "p-0-1", "s", {}, lambda ref: None)
    out = app.state.integrations.run("email_send", {"to": "bob@example.com", "subject": "x", "body": "y"}, ctx)
    assert out["outcome"] == "uncertain"


def test_draft_needs_approval_but_never_sends(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["draft"]})
    out = chat(c, "draft an email to bob@example.com about Notes saying here they are")
    assert out["plan"]["status"] == "awaiting_approval" and ms.messages == {}
    r = c.post(f"/plans/{out['plan']['id']}/approve").json()
    assert r["plan"]["status"] == "completed" and r["plan"]["steps"][0]["verification"] == "graph_draft_readback"
    assert ms.sends == 0 and list(ms.messages.values())[0]["isDraft"] is True


def test_phone_cannot_report_server_step(tmp_path, ms, clock):
    app, c = make(tmp_path, ms, clock)
    connect(c, app)
    c.post("/permissions/grant", json={"capability": "email", "scopes": ["send"]})
    pid = send_plan(c)
    c.post(f"/plans/{pid}/approve")
    ms.send_mode = "timeout_after_apply"
    r = c.post(f"/plans/{pid}/steps/0/confirm").json()
    did = r["plan"]["steps"][0]["dispatch_id"]
    resp = c.post(f"/plans/{pid}/steps/0/result", json={"dispatch_id": did, "outcome": "succeeded",
                                                        "verification": "x"})
    assert resp.status_code == 409
