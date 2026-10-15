import json

import pytest
from fastapi.testclient import TestClient

from fake_ms import FakeMicrosoft
from jarvis_server import create_app

WA_ENV = {"AI_PROVIDER": "offline", "WHATSAPP_ACCESS_TOKEN": "wa-token", "WHATSAPP_PHONE_NUMBER_ID": "1098765"}


@pytest.fixture
def fake():
    return FakeMicrosoft()  # also fakes graph.facebook.com


def client(tmp_path, fake, env):
    app = create_app(env=env, data_dir=tmp_path, http=fake.client())
    return app, TestClient(app)


def chat(c, text):
    return c.post("/chat", json={"text": text, "session_id": "s"}).json()


def test_not_configured_uses_phone_handoff_and_says_so(tmp_path, fake):
    app, c = client(tmp_path, fake, {"AI_PROVIDER": "offline"})
    st = c.get("/integrations/status").json()["whatsapp_business"]
    assert st["state"] == "NOT_CONFIGURED" and "WHATSAPP_ACCESS_TOKEN" in st["detail"]
    out = chat(c, "whatsapp +919912345678 saying hi")
    assert out["plan"]["steps"][0]["tool"] == "send_whatsapp"  # pre-filled chat, user taps send
    # A model asking for the business API anyway gets NOT_CONFIGURED; nothing is sent.
    app.state.provider.respond = lambda sid, t: {"reply": "Sent!", "steps": [{"tool": "whatsapp_business_send", "args": [
        {"name": "to", "value": "919912345678"}, {"name": "message", "value": "hi"}]}]}
    out = chat(c, "x")
    assert out["error_code"] == "NOT_CONFIGURED" and "plan" not in out and fake.wa_sent == []


def test_share_sheet_fallback_needs_no_grant(tmp_path, fake):
    _, c = client(tmp_path, fake, {"AI_PROVIDER": "offline"})
    out = chat(c, "share meet me at the library at 5")
    assert out["action"] == {"type": "share_text", "value": "meet me at the library at 5", "params": {}}


def test_business_send_needs_grant_and_confirmation_and_never_claims_delivery(tmp_path, fake):
    app, c = client(tmp_path, fake, WA_ENV)
    plan = chat(c, "whatsapp +919912345678 saying running late")["plan"]
    assert plan["steps"][0]["tool"] == "whatsapp_business_send"
    assert c.post(f"/plans/{plan['id']}/approve").json()["plan"]["status"] == "blocked"
    c.post("/permissions/grant", json={"capability": "whatsapp", "targets": ["919912345678"]})
    r = c.post(f"/plans/{plan['id']}/next").json()
    assert r["plan"]["status"] == "awaiting_confirmation" and fake.wa_sent == []
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    step = r["plan"]["steps"][0]
    assert r["plan"]["status"] == "completed" and step["verification"] == "whatsapp_api_accepted"
    assert "delivery not confirmed" in step["message"]
    assert fake.wa_sent == [{"messaging_product": "whatsapp", "recipient_type": "individual", "to": "919912345678",
                             "type": "text", "text": {"preview_url": False, "body": "running late"}}]
    assert "wa-token" not in (tmp_path / "audit.jsonl").read_text() + json.dumps(r)


def test_outside_24h_window_is_permanent_with_reason(tmp_path, fake):
    _, c = client(tmp_path, fake, WA_ENV)
    c.post("/permissions/grant", json={"capability": "whatsapp"})
    fake.wa_mode = "outside_window"
    plan = chat(c, "whatsapp +919912345678 saying hi")["plan"]
    c.post(f"/plans/{plan['id']}/approve")
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    assert r["plan"]["status"] == "step_failed" and "24 hours" in r["plan"]["steps"][0]["message"]


def test_timeout_is_uncertain_and_not_resent(tmp_path, fake):
    _, c = client(tmp_path, fake, WA_ENV)
    c.post("/permissions/grant", json={"capability": "whatsapp"})
    fake.wa_mode = "timeout"
    plan = chat(c, "whatsapp +919912345678 saying hi")["plan"]
    c.post(f"/plans/{plan['id']}/approve")
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    assert r["plan"]["status"] == "awaiting_verification" and len(fake.wa_sent) == 1
    assert c.post(f"/plans/{plan['id']}/next").status_code == 409
    assert len(fake.wa_sent) == 1


def test_bad_token_is_auth_failure(tmp_path, fake):
    _, c = client(tmp_path, fake, {**WA_ENV, "WHATSAPP_ACCESS_TOKEN": "wrong"})
    c.post("/permissions/grant", json={"capability": "whatsapp"})
    plan = chat(c, "whatsapp +919912345678 saying hi")["plan"]
    c.post(f"/plans/{plan['id']}/approve")
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    assert r["plan"]["status"] == "step_failed" and "AUTH_FAILED" in r["plan"]["steps"][0]["message"]
    assert "wrong" not in json.dumps(r)


def test_contact_names_stay_on_phone_handoff(tmp_path, fake):
    _, c = client(tmp_path, fake, WA_ENV)
    out = chat(c, "whatsapp Mom saying hi")  # business API needs a number; names go through the phone
    assert out["plan"]["steps"][0]["tool"] == "send_whatsapp"
