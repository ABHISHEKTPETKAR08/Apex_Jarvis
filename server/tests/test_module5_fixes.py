"""Regression tests for the on-device bug reports (open_app args, cancellation, Microsoft sign-in polling)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from fake_ms import FakeMicrosoft
from graph import GraphClient
from jarvis_server import create_app
from permissions import PermissionStore
from plans import PlanManager
from providers import ALL_PARAMS, SCHEMA
from secrets_store import SecretStore
from storage import LocalJsonStore


def args(**given):
    """What the structured-output schema produces: every field present, unused ones null."""
    return {n: given.get(n) for n in ALL_PARAMS}


@pytest.fixture
def app(tmp_path):
    return create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path)


def chat_with_model(app, model_out, text="x"):
    app.state.provider.respond = lambda sid, t: model_out
    return TestClient(app).post("/chat", json={"text": text, "session_id": "s"}).json()


# ---- open_app argument schema -----------------------------------------------

def test_schema_is_per_tool_so_args_cannot_mix():
    variants = SCHEMA["properties"]["steps"]["items"]["anyOf"]
    by_tool = {v["properties"]["tool"]["enum"][0]: v["properties"]["args"] for v in variants}
    assert set(by_tool["set_timer"]["properties"]) == {"seconds"}  # not duration_minutes (seen on device)
    assert set(by_tool["send_whatsapp"]["properties"]) == {"to", "message"}  # not body/who (seen on device)
    assert set(by_tool["open_app"]["properties"]) == {"app"}
    assert all(v["properties"]["args"]["additionalProperties"] is False for v in variants)


@pytest.mark.parametrize("bad,err", [
    ({"tool": "set_timer", "args": {"duration_minutes": "1"}}, "unexpected"),
    ({"tool": "send_whatsapp", "args": {"who": "Anand", "body": "hi"}}, "unexpected"),
])
def test_cross_tool_argument_names_are_rejected(app, bad, err):
    out = chat_with_model(app, {"reply": "Done!", "steps": [bad]})
    assert out["action"]["type"] == "none" and err in out["error"] and "Done" not in out["reply"]


def test_missing_detail_becomes_a_question(app):
    out = chat_with_model(app, {"reply": "Sending.", "steps": [
        {"tool": "send_whatsapp", "args": {"to": "Anand SJBIT", "message": None}}]})
    assert out["reply"] == "I need one more detail, sir: message text." and "plan" not in out


@pytest.mark.parametrize("name", ["YouTube", "Among Us", "com.google.android.youtube"])
def test_open_app_with_schema_args(app, name):
    out = chat_with_model(app, {"reply": f"Opening {name}.", "steps": [{"tool": "open_app", "args": args(app=name)}]})
    assert out["action"] == {"type": "open_app", "value": name, "params": {}}


def test_old_bug_shape_is_rejected_without_claiming_success(app):
    # The exact failing output from the phone: the app name was put in the argument *name*.
    out = chat_with_model(app, {"reply": "Opening YouTube, sir.",
                                "steps": [{"tool": "open_app", "args": [{"name": "YouTube", "value": "YouTube"}]}]})
    assert out["action"]["type"] == "none" and "unexpected argument" in out["error"]
    assert "Opening" not in out["reply"]


def test_missing_app_name_rejected(app):
    out = chat_with_model(app, {"reply": "Opening.", "steps": [{"tool": "open_app", "args": args()}]})
    assert "missing required argument 'app'" in out["error"]


def test_timer_alarm_and_photo_through_schema_args(app):
    c = TestClient(app)
    assert chat_with_model(app, {"reply": "", "steps": [{"tool": "set_timer", "args": args(seconds="300")}]})["action"] \
        == {"type": "set_timer", "value": "300", "params": {}}
    assert chat_with_model(app, {"reply": "", "steps": [{"tool": "set_alarm", "args": args(time="06:30")}]})["action"][
        "value"] == "06:30"
    photo = {"reply": "Opening the camera.", "steps": [{"tool": "take_photo", "args": args()}]}
    denied = chat_with_model(app, photo)
    assert denied["action"]["type"] == "none" and "camera" in denied["error"]  # grant enforced
    c.post("/permissions/grant", json={"capability": "camera"})
    assert chat_with_model(app, photo)["action"]["type"] == "camera_capture"


def test_question_needs_no_tool(app):
    out = chat_with_model(app, {"reply": "Paris, sir.", "steps": []}, "capital of France?")
    assert out == {"reply": "Paris, sir.", "action": {"type": "none", "value": "", "params": {}}}


# ---- camera cancellation ------------------------------------------------------

def test_user_cancel_is_cancelled_not_failed_and_can_retry():
    perms = PermissionStore()
    perms.grant("camera")
    pm = PlanManager(perms)
    plan = pm.create("s", "photos", [{"tool": "take_photo", "args": {}}, {"tool": "take_photo", "args": {}}])
    r = pm.approve(plan.id)
    r = pm.report(plan.id, 0, r["dispatch"]["dispatch_id"], "cancelled", message="Photo cancelled")
    assert r["plan"]["steps"][0]["status"] == "cancelled" and "cancelled" in r["plan"]["note"]
    assert r["plan"]["steps"][0]["error_class"] is None
    assert pm.retry(plan.id, 0)["dispatch"] is not None


# ---- Microsoft sign-in polling --------------------------------------------------

def graph(tmp_path, handler):
    secrets = SecretStore(LocalJsonStore(tmp_path), {})
    return GraphClient({"MS_GRAPH_CLIENT_ID": "cid", "MS_GRAPH_TENANT": "contoso-tenant"}, secrets,
                       http=httpx.Client(transport=httpx.MockTransport(handler)), start_thread=False)


def test_connecting_status_explains_and_counts_down(tmp_path):
    g = graph(tmp_path, FakeMicrosoft().handle)
    g.start_device_flow()
    st = g.status()
    assert st["state"] == "CONNECTING" and st["tenant"] == "contoso-tenant" and 0 < st["expires_in"] <= 900


def test_microsoft_error_code_is_reported_not_left_connecting(tmp_path):
    def handler(req):
        if req.url.path.endswith("/devicecode"):
            return FakeMicrosoft().handle(req)
        return httpx.Response(400, json={"error": "invalid_client",
                                         "error_description": "AADSTS7000218: The request body must contain client_assertion. Trace ID: x"})
    g = graph(tmp_path, handler)
    g.start_device_flow()
    assert g.poll_device_flow() == "failed"
    st = g.status()
    assert st["state"] == "NOT_CONNECTED" and "AADSTS7000218" in st["last_error"] and "client_assertion" not in st["last_error"]


def test_new_connect_supersedes_old_poller(tmp_path):
    g = graph(tmp_path, FakeMicrosoft().handle)
    g.start_device_flow()
    first = g._flow["seq"]
    g.start_device_flow()
    assert g._flow["seq"] == first + 1
    g._poll_loop(first)  # the old poller exits immediately instead of polling the new code
    assert g.status()["state"] == "CONNECTING"


def test_poller_crash_ends_flow_instead_of_hanging(tmp_path, monkeypatch):
    g = graph(tmp_path, FakeMicrosoft().handle)
    g.start_device_flow()
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(g, "poll_device_flow", lambda: (_ for _ in ()).throw(KeyError("x")))
    g._poll_loop(g._flow["seq"])
    assert g.status()["state"] == "NOT_CONNECTED" and "polling failed" in g.status()["last_error"]


def test_prompt_tells_model_open_means_open_app():
    from providers import build_system
    s = build_system()
    assert "always use open_app" in s and "Never substitute a web search" in s


@pytest.mark.parametrize("value,ok", [
    ("YouTube", True), ("Among Us", True), ("Among Us.", True), ("Google Play Store", True),
    ("com.google.android.youtube", True), ("Photos & Videos", True),
    ("Among Us}}]}]}**.Password", False),  # corrupted model output seen on the phone
    ("", False), ("x" * 61, False),
])
def test_open_app_name_validation(app, value, ok):
    out = chat_with_model(app, {"reply": "Opening.", "steps": [{"tool": "open_app", "args": {"app": value}}]})
    assert (out["action"]["type"] == "open_app") is ok
    if not ok:
        assert "Opening" not in out["reply"]
