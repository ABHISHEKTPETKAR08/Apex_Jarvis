"""Module-4 data (OAuth tokens) follows the Module-3 storage rules: Atlas only when configured, no fallback."""
import json

from fastapi.testclient import TestClient

from fake_ms import FakeMicrosoft
from jarvis_server import create_app
from test_reliability import FakeMongo

GRAPH = {"AI_PROVIDER": "offline", "MS_GRAPH_CLIENT_ID": "11111111-2222-3333-4444-555555555555"}
ATLAS = {"MONGODB_URI": "mongodb+srv://u:p@cluster0.example.mongodb.net", "MONGODB_DB_NAME": "jarvis_t"}


def sign_in(app, c):
    c.post("/integrations/graph/connect")
    g = app.state.integrations.graph
    return [g.poll_device_flow(), g.poll_device_flow()]


def test_tokens_go_to_atlas_when_configured(tmp_path):
    ms, mongo = FakeMicrosoft(), FakeMongo()
    env = {**GRAPH, **ATLAS, "JARVIS_SECRET_KEY": "k3y"}
    app = create_app(env=env, data_dir=tmp_path, http=ms.client(), mongo_client=mongo)
    c = TestClient(app)
    assert sign_in(app, c)[-1] == "connected"
    docs = list(mongo["jarvis_t"]["secrets"].docs.values())
    assert len(docs) == 1 and "enc" in docs[0] and "rt1" not in json.dumps(docs)
    assert not list(tmp_path.iterdir())  # nothing written locally
    # restart against the same Atlas: still connected
    app2 = create_app(env=env, data_dir=tmp_path, http=ms.client(), mongo_client=mongo)
    assert TestClient(app2).get("/integrations/status").json()["microsoft_graph"]["state"] == "CONNECTED"


def test_atlas_unavailable_sign_in_fails_cleanly_without_local_fallback(tmp_path):
    ms = FakeMicrosoft()
    app = create_app(env={**GRAPH, "MONGODB_URI": "https://not-mongo"}, data_dir=tmp_path, http=ms.client())
    c = TestClient(app)
    assert sign_in(app, c)[-1] == "failed"
    st = c.get("/integrations/status").json()
    assert st["microsoft_graph"]["state"] == "NOT_CONNECTED" and "couldn't be stored" in st["microsoft_graph"]["last_error"]
    assert st["storage"] == "mongodb_atlas"
    assert not list(tmp_path.iterdir())


def test_local_storage_when_atlas_not_configured(tmp_path):
    ms = FakeMicrosoft()
    app = create_app(env={**GRAPH, "MONGODB_URI": "<mongodb+srv://USER:PASSWORD@CLUSTER>"}, data_dir=tmp_path,
                     http=ms.client())
    c = TestClient(app)
    assert sign_in(app, c)[-1] == "connected"
    assert (tmp_path / "secrets.json").exists()
    assert c.get("/storage/status").json()["backend"] == "local_json"
