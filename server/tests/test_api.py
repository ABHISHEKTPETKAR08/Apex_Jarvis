import pytest
from fastapi.testclient import TestClient

from jarvis_server import create_app


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path))


def chat(client, text):
    r = client.post("/chat", json={"text": text, "session_id": "t"})
    assert r.status_code == 200
    return r.json()


@pytest.mark.parametrize("text,action", [
    ("open youtube", {"type": "open_app", "value": "youtube"}),
    ("search for weather in bangalore", {"type": "web_search", "value": "weather in bangalore"}),
    ("set a timer for 5 minutes", {"type": "set_timer", "value": "300"}),
    ("alarm at 6:30 am", {"type": "set_alarm", "value": "06:30"}),
    ("call mom", {"type": "dial", "value": "mom"}),
    ("open github.com", {"type": "open_url", "value": "https://github.com"}),
])
def test_existing_offline_phone_actions_preserved(client, text, action):
    out = chat(client, text)
    assert {k: out["action"][k] for k in ("type", "value")} == action
    assert "plan" not in out


def test_health_and_status_never_leak_secrets(tmp_path):
    env = {"AI_PROVIDER": "azure_openai", "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com/",
           "AZURE_OPENAI_API_KEY": "topsecret", "AZURE_OPENAI_DEPLOYMENT_NAME": "d", "AZURE_OPENAI_API_VERSION": "2024-10-21"}
    c = TestClient(create_app(env=env, data_dir=tmp_path))
    h = c.get("/health").json()
    assert (h["mode"], h["configured"], h["storage"], h["storage_ok"]) == ("azure_openai", True, "local_json", True)
    assert "topsecret" not in c.get("/provider/status").text


def test_misconfigured_provider_does_not_fall_back(tmp_path):
    c = TestClient(create_app(env={"AI_PROVIDER": "azure_openai"}, data_dir=tmp_path))
    assert c.get("/health").json()["configured"] is False
    out = c.post("/chat", json={"text": "open youtube"}).json()
    assert out["action"]["type"] == "none" and "not configured" in out["error"]


def test_capability_action_denied_by_default_then_granted(client):
    out = chat(client, "take a photo")
    assert out["action"]["type"] == "none" and "camera" in out["error"]
    g = client.post("/permissions/grant", json={"capability": "camera", "ttl_seconds": 3600}).json()
    assert chat(client, "take a photo")["action"]["type"] == "camera_capture"
    client.post("/permissions/revoke", json={"grant_id": g["id"]})
    assert chat(client, "take a photo")["action"]["type"] == "none"


def test_whatsapp_goes_through_plan_approval_and_confirmation(client):
    out = chat(client, "whatsapp +919900000000 saying running late")
    assert out["action"]["type"] == "none"
    plan = out["plan"]
    assert plan["status"] == "awaiting_approval" and plan["steps"][0]["consequential"] is True

    r = client.post(f"/plans/{plan['id']}/approve").json()
    assert r["plan"]["status"] == "blocked"

    client.post("/permissions/grant", json={"capability": "whatsapp", "scopes": ["send"]})
    r = client.post(f"/plans/{plan['id']}/next").json()
    assert r["confirmation"]["summary"] == "WhatsApp +919900000000: running late"

    r = client.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    assert r["dispatch"]["action"]["params"] == {"to": "+919900000000", "message": "running late"}

    r = client.post(f"/plans/{plan['id']}/steps/0/result", json={
        "dispatch_id": r["dispatch"]["dispatch_id"], "outcome": "succeeded", "verification": "activity_launched"}).json()
    assert r["plan"]["status"] == "awaiting_verification"
    r = client.post(f"/plans/{plan['id']}/steps/0/verify", json={"happened": True}).json()
    assert r["plan"]["status"] == "completed"


def test_plan_errors_are_http_errors(client):
    assert client.post("/plans/nope/approve").status_code == 404
    plan = chat(client, "whatsapp +919900000000 saying hi")["plan"]
    assert client.post(f"/plans/{plan['id']}/next").status_code == 409
    assert client.post(f"/plans/{plan['id']}/steps/7/confirm").status_code == 404


def test_unknown_tool_from_model_is_never_executed(tmp_path):
    app = create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path)
    app.state.provider.respond = lambda sid, text: {
        "reply": "Done.", "steps": [{"tool": "run_shell", "args": [{"name": "cmd", "value": "rm -rf /"}]}]}
    c = TestClient(app)
    out = c.post("/chat", json={"text": "x"}).json()
    assert out["action"]["type"] == "none" and "Unknown tool" in out["error"] and "plan" not in out


def test_actions_authorize_endpoint(client):
    assert client.post("/actions/authorize", json={"type": "open_app", "value": "x"}).json()["allowed"] is True
    assert client.post("/actions/authorize", json={"type": "camera_capture"}).json()["allowed"] is False
    assert client.post("/actions/authorize", json={"type": "compose_email", "value": "a@b.co"}).json()["allowed"] is False
    client.post("/permissions/grant", json={"capability": "camera"})
    assert client.post("/actions/authorize", json={"type": "camera_capture"}).json()["allowed"] is True


def test_grant_validation(client):
    assert client.post("/permissions/grant", json={"capability": "sms"}).status_code == 400
    assert client.post("/permissions/revoke", json={}).status_code == 400
    caps = client.get("/permissions").json()["capabilities"]
    assert set(caps) == {"email", "whatsapp", "camera", "microphone", "speaker", "calendar"}
