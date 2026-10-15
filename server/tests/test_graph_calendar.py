import pytest
from fastapi.testclient import TestClient

from fake_ms import FakeMicrosoft
from integrations import ExecContext
from jarvis_server import create_app
from tools import ToolValidationError, validate_step


class Clock:
    def __init__(self):
        self.t = 2_000_000.0

    def __call__(self):
        return self.t


ENV = {"AI_PROVIDER": "offline", "MS_GRAPH_CLIENT_ID": "11111111-2222-3333-4444-555555555555"}


@pytest.fixture
def ms():
    m = FakeMicrosoft()
    m.events["evA"] = {"id": "evA", "subject": "Standup", "start": {"dateTime": "2026-10-20T09:00:00"},
                       "end": {"dateTime": "2026-10-20T09:30:00"}}
    return m


@pytest.fixture
def setup(tmp_path, ms):
    clock = Clock()
    app = create_app(env=ENV, data_dir=tmp_path, clock=clock, http=ms.client())
    app.state.integrations.sleep = lambda s: None
    c = TestClient(app)
    c.post("/integrations/graph/connect")
    g = app.state.integrations.graph
    g.poll_device_flow()
    g.poll_device_flow()
    return app, c, clock


def chat(c, text):
    return c.post("/chat", json={"text": text, "session_id": "s"}).json()


def test_agenda_needs_read_grant_and_lists_refs(setup, ms):
    app, c, _ = setup
    assert "permission" in chat(c, "what's on my calendar tomorrow")["reply"]
    c.post("/permissions/grant", json={"capability": "calendar", "scopes": ["read"]})
    app.state.provider.respond = lambda sid, t: {"reply": "", "steps": [
        {"tool": "calendar_agenda", "args": [{"name": "date", "value": "2026-10-20"}]}]}
    out = chat(c, "agenda")
    assert out["data"]["events"][0]["ref"] == "E1" and "E1: Standup at 09:00" in out["reply"]


def test_read_grant_does_not_allow_writes(setup, ms):
    app, c, _ = setup
    c.post("/permissions/grant", json={"capability": "calendar", "scopes": ["read"]})
    plan = chat(c, "add event Dentist on 2026-10-21 10:00")["plan"]
    r = c.post(f"/plans/{plan['id']}/approve").json()
    assert r["plan"]["status"] == "blocked" and ms.event_creates == 0


def test_create_event_confirmed_and_verified_by_readback(setup, ms):
    app, c, _ = setup
    c.post("/permissions/grant", json={"capability": "calendar", "scopes": ["write"]})
    plan = chat(c, "add event Dentist on 2026-10-21 10:00")["plan"]
    assert plan["steps"][0]["tool"] == "calendar_add_event"
    r = c.post(f"/plans/{plan['id']}/approve").json()
    assert r["plan"]["status"] == "awaiting_confirmation" and ms.event_creates == 0
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    assert r["plan"]["status"] == "completed"
    assert r["plan"]["steps"][0]["verification"] == "graph_event_readback"
    created = [e for e in ms.events.values() if e["subject"] == "Dentist"][0]
    assert created["start"]["dateTime"] == "2026-10-21T10:00:00" and created["end"]["dateTime"] == "2026-10-21T11:00:00"


def test_create_retry_uses_transaction_id_so_no_duplicate(setup, ms):
    app, _, _ = setup
    integ = app.state.integrations
    args = {"title": "Gym", "start": "2026-10-22 07:00"}
    ctx = ExecContext("planX", 0, "planX-0-1", "s", {}, lambda ref: None)
    assert integ.run("calendar_add_event", args, ctx)["outcome"] == "succeeded"
    ctx2 = ExecContext("planX", 0, "planX-0-2", "s", {}, lambda ref: None)  # e.g. after an uncertain timeout
    assert integ.run("calendar_add_event", args, ctx2)["outcome"] == "succeeded"
    assert ms.event_creates == 1


def test_update_event_by_ref_verified(setup, ms):
    app, c, _ = setup
    c.post("/permissions/grant", json={"capability": "calendar", "scopes": ["read", "write"]})
    app.state.provider.respond = lambda sid, t: {"reply": "", "steps": [
        {"tool": "calendar_agenda", "args": [{"name": "date", "value": "2026-10-20"}]}]}
    chat(c, "agenda")
    app.state.provider.respond = lambda sid, t: {"reply": "", "steps": [{"tool": "calendar_update_event", "args": [
        {"name": "event_ref", "value": "E1"}, {"name": "start", "value": "2026-10-20 10:15"}]}]}
    plan = chat(c, "move it")["plan"]
    c.post(f"/plans/{plan['id']}/approve")
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    assert r["plan"]["status"] == "completed"
    assert ms.events["evA"]["start"]["dateTime"] == "2026-10-20T10:15:00"
    assert ms.events["evA"]["end"]["dateTime"] == "2026-10-20T10:45:00"  # duration kept
    assert r["plan"]["steps"][0]["checkpoints"][-1]["status"] == "succeeded"


def test_update_ref_survives_restart_via_saved_ref(setup, ms):
    app, _, _ = setup
    integ = app.state.integrations
    ctx = ExecContext("p", 0, "p-0-1", "other-session", {"event_id": "evA"}, lambda ref: None)
    out = integ.run("calendar_update_event", {"event_ref": "E1", "title": "Daily standup"}, ctx)
    assert out["outcome"] == "succeeded" and ms.events["evA"]["subject"] == "Daily standup"


def test_unknown_ref_fails_without_touching_calendar(setup, ms):
    app, _, _ = setup
    ctx = ExecContext("p", 0, "p-0-1", "fresh", {}, lambda ref: None)
    out = app.state.integrations.run("calendar_update_event", {"event_ref": "E7", "title": "x"}, ctx)
    assert out["outcome"] == "failed" and out["error_class"] == "permanent"
    assert ms.events["evA"]["subject"] == "Standup"


def test_update_requires_a_change_and_valid_ref():
    with pytest.raises(ToolValidationError, match="at least one"):
        validate_step("calendar_update_event", {"event_ref": "E1"})
    with pytest.raises(ToolValidationError):
        validate_step("calendar_update_event", {"event_ref": "evA-raw-id", "title": "x"})


def test_without_graph_falls_back_to_phone_calendar(tmp_path):
    c = TestClient(create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path))
    out = c.post("/chat", json={"text": "add event Dentist on 2026-10-21 10:00"}).json()
    assert out["plan"]["steps"][0]["tool"] == "create_calendar_event"  # Android insert intent, user-verified
