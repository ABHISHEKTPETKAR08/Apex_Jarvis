import json

import pytest
from fastapi.testclient import TestClient
from pymongo import errors as mongo_errors

import plans as plans_mod
from jarvis_server import create_app
from permissions import PermissionStore
from plans import PlanError, PlanManager, backoff_delay
from storage import LocalJsonStore, MongoStore, PersistenceError, UnavailableStore, build_storage


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FlakyStore(LocalJsonStore):
    """Local store whose writes can be made to fail."""

    fail_saves = False
    fail_audit = False

    def save(self, kind, doc_id, doc):
        if self.fail_saves:
            raise PersistenceError("disk full")
        super().save(kind, doc_id, doc)

    def append_audit(self, event):
        if self.fail_audit:
            raise PersistenceError("audit unavailable")
        super().append_audit(event)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    return FlakyStore(tmp_path)


def manager(store, clock):
    perms = PermissionStore(clock=clock, storage=store)
    return PlanManager(perms, clock=clock, storage=store), perms


PHOTO = {"tool": "take_photo", "args": []}
TIMER = {"tool": "set_timer", "args": {"seconds": "60"}}
WA = {"tool": "send_whatsapp", "args": {"to": "+919912345678", "message": "secret plans for tonight"}}


def launched(pm, r):
    d = r["dispatch"]
    return pm.report(r["plan"]["id"], d["step_id"], d["dispatch_id"], "succeeded", verification="activity_launched")


def fail(pm, r, error_class, message=""):
    d = r["dispatch"]
    return pm.report(r["plan"]["id"], d["step_id"], d["dispatch_id"], "failed", error_class, message=message)


# ---- transient / permanent failures ---------------------------------------

def test_backoff_is_exponential_and_bounded():
    assert [backoff_delay(a) for a in (1, 2, 3, 4, 10)] == [2, 4, 8, 16, 60]


def test_transient_failure_retries_with_backoff_then_gives_up(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    plan = pm.create("s", "photo", [PHOTO])
    r = pm.approve(plan.id)
    first_id = r["dispatch"]["dispatch_id"]

    r = fail(pm, r, "transient")
    assert r["plan"]["status"] == "retry_scheduled" and r["dispatch"] is None
    assert r["plan"]["retry_at"] == clock.t + 2
    with pytest.raises(PlanError) as e:
        pm.advance(plan.id)  # too early
    assert e.value.status_code == 425

    clock.t += 2
    r = pm.advance(plan.id)
    assert r["dispatch"]["dispatch_id"] != first_id and r["plan"]["steps"][0]["attempts"] == 2
    r = fail(pm, r, "transient")
    assert r["plan"]["retry_at"] == clock.t + 4

    clock.t += 4
    r = fail(pm, pm.advance(plan.id), "transient")  # attempt 3 = MAX_ATTEMPTS
    assert r["plan"]["status"] == "step_failed" and "gave up" in r["plan"]["note"]


def test_permanent_failure_is_not_retried(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    plan = pm.create("s", "photo", [PHOTO])
    r = fail(pm, pm.approve(plan.id), "permanent", "No camera app")
    assert r["plan"]["status"] == "step_failed" and r["plan"]["retry_at"] is None
    with pytest.raises(PlanError):
        pm.advance(plan.id)


# ---- permission / approval rechecks ----------------------------------------

def test_revocation_blocks_scheduled_retry(store, clock):
    pm, perms = manager(store, clock)
    g = perms.grant("camera")
    plan = pm.create("s", "photo", [PHOTO])
    fail(pm, pm.approve(plan.id), "transient")
    perms.revoke(g.id)
    clock.t += 10
    r = pm.advance(plan.id)
    assert r["plan"]["status"] == "blocked" and r["dispatch"] is None


def test_retry_of_send_needs_fresh_confirmation(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("whatsapp")
    plan = pm.create("s", "wa", [WA])
    pm.approve(plan.id)
    r = fail(pm, pm.confirm(plan.id, 0), "transient")  # WhatsApp couldn't open; nothing sent
    clock.t += 2
    r = pm.advance(plan.id)
    assert r["plan"]["status"] == "awaiting_confirmation" and r["dispatch"] is None


def test_expired_approval_requires_reapproval(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    plan = pm.create("s", "photo", [PHOTO])
    fail(pm, pm.approve(plan.id), "transient")
    clock.t += plans_mod.APPROVAL_TTL + 1
    r = pm.advance(plan.id)
    assert r["plan"]["status"] == "awaiting_approval" and r["dispatch"] is None
    r = pm.approve(plan.id)
    assert r["dispatch"] is not None


def test_stale_confirmation_is_not_reused(store, clock):
    pm, perms = manager(store, clock)
    plan = pm.create("s", "wa", [WA])
    pm.approve(plan.id)  # blocked: no grant yet
    perms.grant("whatsapp")
    pm.advance(plan.id)  # awaiting confirmation
    pm.confirm(plan.id, 0)
    # confirmed_for_attempt is consumed by that dispatch; a later attempt can't reuse it
    step = pm.get(plan.id).steps[0]
    assert step.confirmed_for_attempt == step.attempts


# ---- verified outcomes ----------------------------------------------------

def test_success_requires_verification_and_sends_need_user_check(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    perms.grant("whatsapp")
    plan = pm.create("s", "photo", [PHOTO])
    r = pm.approve(plan.id)
    with pytest.raises(PlanError) as e:
        pm.report(plan.id, 0, r["dispatch"]["dispatch_id"], "succeeded")
    assert e.value.status_code == 422

    plan = pm.create("s", "wa", [WA])
    pm.approve(plan.id)
    r = launched(pm, pm.confirm(plan.id, 0))
    assert r["plan"]["steps"][0]["status"] == "handed_off" and r["plan"]["status"] == "awaiting_verification"
    r = pm.verify(plan.id, 0, happened=False)
    assert r["plan"]["status"] == "step_failed"


# ---- restart / resume -----------------------------------------------------

def test_restart_resumes_idempotent_step_with_same_dispatch_id(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    plan = pm.create("s", "photo then timer", [PHOTO, TIMER])
    dispatch_id = pm.approve(plan.id)["dispatch"]["dispatch_id"]

    pm2, _ = manager(store, clock)  # server restart: state comes from storage
    assert pm2.get(plan.id).status == "awaiting_result"
    r = pm2.advance(plan.id)
    assert r["dispatch"]["dispatch_id"] == dispatch_id  # phone ledger dedups it
    r = launched(pm2, r)
    assert r["dispatch"]["action"]["type"] == "set_timer"


def test_restart_never_resends_non_idempotent_step(store, clock):
    pm, perms = manager(store, clock)
    plan = pm.create("s", "timers", [TIMER, TIMER])
    r = launched(pm, pm.approve(plan.id))
    in_flight = r["dispatch"]["dispatch_id"]

    pm2, _ = manager(store, clock)
    r = pm2.advance(plan.id)
    assert r["dispatch"] is None and r["plan"]["status"] == "awaiting_verification"
    assert r["plan"]["steps"][1]["status"] == "uncertain"
    with pytest.raises(PlanError):
        pm2.report(plan.id, 1, in_flight + "x", "succeeded", verification="x")
    r = pm2.verify(plan.id, 1, happened=True)
    assert r["plan"]["status"] == "completed"


def test_late_result_resolves_uncertain_step(store, clock):
    pm, _ = manager(store, clock)
    plan = pm.create("s", "timer", [TIMER])
    r = pm.approve(plan.id)
    pm2, _ = manager(store, clock)
    assert pm2.advance(plan.id)["plan"]["steps"][0]["status"] == "uncertain"
    r = launched(pm2, r)  # the phone's report finally arrives with the original dispatch_id
    assert r["plan"]["status"] == "completed"
    with pytest.raises(PlanError):
        pm2.report(plan.id, 0, "other-id", "succeeded", verification="x")


def test_dispatch_times_out_without_restart(store, clock):
    pm, _ = manager(store, clock)
    plan = pm.create("s", "timer", [TIMER])
    pm.approve(plan.id)
    with pytest.raises(PlanError):
        pm.advance(plan.id)  # still waiting
    clock.t += plans_mod.RESULT_TIMEOUT + 1
    assert pm.advance(plan.id)["plan"]["status"] == "awaiting_verification"


def test_cancel_during_dispatch_records_late_result(store, clock):
    pm, _ = manager(store, clock)
    plan = pm.create("s", "timers", [TIMER, TIMER])
    r = pm.approve(plan.id)
    pm.cancel(plan.id)
    r = launched(pm, r)
    assert r["plan"]["status"] == "cancelled" and r["dispatch"] is None
    assert [s["status"] for s in r["plan"]["steps"]] == ["succeeded", "cancelled"]


# ---- duplicates -----------------------------------------------------------

def test_duplicate_result_is_ignored(store, clock):
    pm, _ = manager(store, clock)
    plan = pm.create("s", "timers", [TIMER, TIMER])
    r1 = pm.approve(plan.id)
    r2 = launched(pm, r1)
    again = launched(pm, r1)  # same dispatch_id reported twice
    assert again["dispatch"] is None
    assert again["plan"]["steps"][1]["attempts"] == 1 == r2["plan"]["steps"][1]["attempts"]


def test_idempotency_key_replays_without_second_side_effect(tmp_path, clock):
    c = TestClient(create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path, clock=clock))
    plan = c.post("/chat", json={"text": "whatsapp +919912345678 saying hi"}).json()["plan"]
    h = {"Idempotency-Key": "approve-1"}
    a = c.post(f"/plans/{plan['id']}/approve", headers=h).json()
    b = c.post(f"/plans/{plan['id']}/approve", headers=h).json()
    assert b["idempotent_replay"] is True and b["plan"] == a["plan"]
    assert c.post(f"/plans/{plan['id']}/approve").status_code == 409  # without the key it's a real 2nd call
    events = [e["event"] for e in c.get(f"/tasks/{plan['id']}/audit").json()["events"]]
    assert events.count("plan_approved") == 1

    g1 = c.post("/permissions/grant", json={"capability": "camera"}, headers={"Idempotency-Key": "g"}).json()
    g2 = c.post("/permissions/grant", json={"capability": "camera"}, headers={"Idempotency-Key": "g"}).json()
    assert g1["id"] == g2["id"] and len(c.get("/permissions").json()["grants"]) == 1


# ---- persistence ----------------------------------------------------------

def test_state_survives_restart_including_grants_and_approvals(tmp_path, clock):
    c = TestClient(create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path, clock=clock))
    c.post("/permissions/grant", json={"capability": "whatsapp"})
    plan = c.post("/chat", json={"text": "whatsapp +919912345678 saying hi"}).json()["plan"]
    c.post(f"/plans/{plan['id']}/approve")

    c2 = TestClient(create_app(env={"AI_PROVIDER": "offline"}, data_dir=tmp_path, clock=clock))
    assert c2.get(f"/tasks/{plan['id']}").json()["plan"]["status"] == "awaiting_confirmation"
    assert c2.get("/permissions").json()["grants"][0]["capability"] == "whatsapp"
    approvals = json.loads((tmp_path / "approvals.json").read_text())
    assert [a["kind"] for a in approvals.values()] == ["plan_approval"]


def test_failed_save_changes_nothing_and_dispatches_nothing(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    plan = pm.create("s", "photo", [PHOTO])
    store.fail_saves = True
    with pytest.raises(PlanError) as e:
        pm.approve(plan.id)
    assert e.value.status_code == 503
    assert pm.get(plan.id).status == "awaiting_approval"
    store.fail_saves = False
    assert pm.approve(plan.id)["dispatch"] is not None


def test_failed_audit_blocks_dispatch(store, clock):
    pm, perms = manager(store, clock)
    perms.grant("camera")
    plan = pm.create("s", "photo", [PHOTO])
    store.fail_audit = True
    with pytest.raises(PlanError):
        pm.approve(plan.id)
    assert pm.get(plan.id).steps[0].attempts == 0


def test_grant_not_saved_is_not_active_and_revoke_applies_anyway(store, clock):
    perms = PermissionStore(clock=clock, storage=store)
    g = perms.grant("camera")
    store.fail_saves = True
    with pytest.raises(PersistenceError):
        perms.grant("email")
    assert not perms.authorize("email", "send").allowed
    with pytest.raises(PersistenceError):
        perms.revoke(g.id)
    assert not perms.authorize("camera", "capture").allowed  # fail safe


def test_unavailable_atlas_fails_closed_without_local_fallback(tmp_path):
    env = {"AI_PROVIDER": "offline", "MONGODB_URI": "https://not-mongo", "MONGODB_DB_NAME": "jarvis"}
    c = TestClient(create_app(env=env, data_dir=tmp_path))
    st = c.get("/storage/status").json()
    assert st["backend"] == "mongodb_atlas" and st["ok"] is False
    assert c.post("/permissions/grant", json={"capability": "camera"}).status_code == 503
    out = c.post("/chat", json={"text": "whatsapp +919912345678 saying hi"}).json()
    assert "plan" not in out and "couldn't save" in out["reply"]
    assert not list(tmp_path.glob("*.json"))  # nothing written locally


def test_local_store_reports_limitations_and_migrates_legacy_grants(tmp_path, clock):
    (tmp_path / "permissions.json").write_text(json.dumps([{
        "id": "abc", "capability": "camera", "scopes": ["capture"], "granted_at": clock.t,
        "expires_at": None, "revoked_at": None, "targets": None}]))
    store = build_storage({}, tmp_path)
    assert store.status()["backend"] == "local_json" and store.status()["limitations"]
    assert PermissionStore(clock=clock, storage=store).authorize("camera", "capture").allowed


# ---- MongoDB (fake client: no network) ------------------------------------

class FakeCursor(list):
    def sort(self, key, direction):
        return FakeCursor(sorted(self, key=lambda d: d.get(key, 0), reverse=direction < 0))

    def limit(self, n):
        return FakeCursor(self[:n])


class FakeColl:
    def __init__(self, client):
        self.client, self.docs = client, {}

    def _maybe_fail(self):
        if self.client.fail_times:
            self.client.fail_times -= 1
            raise mongo_errors.AutoReconnect("blip")

    def replace_one(self, flt, doc, upsert=False):
        self._maybe_fail()
        self.docs[flt["_id"]] = dict(doc)

    def insert_one(self, doc):
        self._maybe_fail()
        if doc["_id"] in self.docs:
            raise mongo_errors.DuplicateKeyError("dup")
        self.docs[doc["_id"]] = dict(doc)
        if self.client.fail_after_insert:  # write landed but the ack was lost
            self.client.fail_after_insert = False
            raise mongo_errors.AutoReconnect("ack lost")

    def find(self, q=None):
        self._maybe_fail()
        q = q or {}
        return FakeCursor(dict(d) for d in self.docs.values() if all(d.get(k) == v for k, v in q.items()))

    def delete_one(self, flt):
        self.docs.pop(flt["_id"], None)

    def create_index(self, *a, **k):
        pass


class FakeMongo:
    def __init__(self):
        self.fail_times = 0
        self.fail_after_insert = False
        self.dbs = {}
        self.admin = self

    def command(self, name):
        if self.fail_times:
            self.fail_times -= 1
            raise mongo_errors.ServerSelectionTimeoutError("no servers")
        return {"ok": 1}

    def __getitem__(self, name):
        client = self
        return self.dbs.setdefault(name, type("DB", (), {
            "_c": {}, "__getitem__": lambda s, k: s._c.setdefault(k, FakeColl(client))})())


def test_mongo_retries_transient_errors_with_backoff():
    fake, sleeps = FakeMongo(), []
    store = MongoStore("mongodb+srv://u:p@cluster0.example.mongodb.net", "jarvis", client=fake, sleep=sleeps.append)
    fake.fail_times = 2
    store.save("tasks", "t1", {"x": 1})
    assert sleeps == [0.2, 0.4] and store.load_all("tasks") == [{"x": 1}]
    fake.fail_times = 5
    with pytest.raises(PersistenceError, match="after 3 attempts"):
        store.save("tasks", "t2", {"x": 2})


def test_mongo_audit_retry_does_not_duplicate():
    fake = FakeMongo()
    store = MongoStore("mongodb://localhost", "jarvis", client=fake, sleep=lambda s: None)
    fake.fail_after_insert = True
    store.append_audit({"id": "e1", "t": 1, "plan_id": "p"})
    assert len(store.list_audit("p")) == 1


def test_mongo_end_to_end_and_status_hides_credentials(clock):
    fake = FakeMongo()
    env = {"AI_PROVIDER": "offline", "MONGODB_URI": "mongodb+srv://jarvis:hunter2@cluster0.example.mongodb.net/?x=1",
           "MONGODB_DB_NAME": "jarvis_test"}
    c = TestClient(create_app(env=env, clock=clock, mongo_client=fake))
    st = c.get("/storage/status").json()
    assert st == {"backend": "mongodb_atlas", "ok": True, "host": "cluster0.example.mongodb.net",
                  "database": "jarvis_test", "plans_load_error": None}
    c.post("/permissions/grant", json={"capability": "camera"})
    assert c.post("/chat", json={"text": "take a photo"}).json()["action"]["type"] == "camera_capture"

    c2 = TestClient(create_app(env=env, clock=clock, mongo_client=fake))  # restart on same "Atlas"
    assert c2.post("/chat", json={"text": "take a photo"}).json()["action"]["type"] == "camera_capture"


def test_mongo_unreachable_at_startup_is_unavailable(tmp_path):
    fake = FakeMongo()
    fake.fail_times = 10
    store = build_storage({"MONGODB_URI": "mongodb+srv://a:b@h.example.net"}, tmp_path, mongo_client=fake)
    assert isinstance(store, UnavailableStore) and "b@" not in json.dumps(store.status())


# ---- audit / history hygiene ----------------------------------------------

def test_audit_has_no_message_content_recipients_or_secrets(tmp_path, clock):
    env = {"AI_PROVIDER": "offline", "AZURE_OPENAI_API_KEY": "sk-shouldnotappear123456"}
    c = TestClient(create_app(env=env, data_dir=tmp_path, clock=clock))
    c.post("/permissions/grant", json={"capability": "whatsapp", "targets": ["+919912345678"]})
    plan = c.post("/chat", json={"text": "whatsapp +919912345678 saying secret plans for tonight"}).json()["plan"]
    c.post(f"/plans/{plan['id']}/approve")
    r = c.post(f"/plans/{plan['id']}/steps/0/confirm").json()
    c.post(f"/plans/{plan['id']}/steps/0/result", json={
        "dispatch_id": r["dispatch"]["dispatch_id"], "outcome": "failed", "error_class": "permanent",
        "message": "crash token=sk-abcdefghijklmnop1234"})

    raw = (tmp_path / "audit.jsonl").read_text() + json.dumps(c.get(f"/tasks/{plan['id']}/audit").json())
    for leaked in ("secret plans", "9912345678", "sk-abcdefghijklmnop1234", "sk-shouldnotappear"):
        assert leaked not in raw
    assert "**********78" in raw
    tasks = c.get("/tasks").json()["tasks"]
    assert len(tasks) == 1 and tasks[0]["id"] == plan["id"] and tasks[0]["created_at"] == clock.t
    assert "sk-abcdefghijklmnop1234" not in json.dumps(tasks)
