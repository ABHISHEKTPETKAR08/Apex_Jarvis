import pytest

from permissions import Capability, PermissionError_, PermissionStore


class Clock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return PermissionStore(path=tmp_path / "perms.json", clock=clock)


@pytest.mark.parametrize("cap", [c.value for c in Capability])
def test_default_deny_for_all_six_capabilities(store, cap):
    from permissions import SCOPES

    for scope in SCOPES[Capability(cap)]:
        assert store.authorize(cap, scope).allowed is False


def test_grant_is_scoped(store):
    store.grant("calendar", ["read"])
    assert store.authorize("calendar", "read").allowed
    assert not store.authorize("calendar", "write").allowed


def test_grant_expires(store, clock):
    store.grant("camera", ttl_seconds=60)
    assert store.authorize("camera", "capture").allowed
    clock.t += 61
    assert not store.authorize("camera", "capture").allowed
    assert store.list() == []
    assert store.list(include_inactive=True)[0]["active"] is False


def test_revoke_by_id_and_capability(store):
    g = store.grant("email")
    store.revoke(g.id)
    assert not store.authorize("email", "send").allowed
    store.grant("whatsapp")
    store.grant("whatsapp")
    assert store.revoke_capability("whatsapp") == 2
    assert not store.authorize("whatsapp", "send").allowed


def test_target_restricted_grant(store):
    store.grant("whatsapp", targets=["+919900000000"])
    assert store.authorize("whatsapp", "send", "+919900000000").allowed
    assert not store.authorize("whatsapp", "send", "+911111111111").allowed
    assert not store.authorize("whatsapp", "send").allowed


@pytest.mark.parametrize("kwargs", [
    {"capability": "sms"},
    {"capability": "email", "scopes": ["delete"]},
    {"capability": "camera", "ttl_seconds": 0},
    {"capability": "camera", "ttl_seconds": 10**9},
])
def test_invalid_grants_rejected(store, kwargs):
    with pytest.raises(PermissionError_):
        store.grant(**kwargs)


def test_unknown_capability_or_scope_denied(store):
    store.grant("speaker")
    assert not store.authorize("sms", "send").allowed
    assert not store.authorize("speaker", "listen").allowed


def test_grants_persist_and_corrupt_file_denies(tmp_path, clock):
    path = tmp_path / "perms.json"
    PermissionStore(path=path, clock=clock).grant("microphone")
    assert PermissionStore(path=path, clock=clock).authorize("microphone", "listen").allowed
    path.write_text("{not json")
    assert not PermissionStore(path=path, clock=clock).authorize("microphone", "listen").allowed


def test_phone_targets_match_regardless_of_formatting(store):
    store.grant("whatsapp", targets=["+91 99123 45678"])
    assert store.authorize("whatsapp", "send", "919912345678").allowed
    assert store.authorize("whatsapp", "send", "+91-9912345678").allowed
    assert not store.authorize("whatsapp", "send", "919912345679").allowed
    store.grant("email", ["send"], targets=["Bob@Example.com"])
    assert store.authorize("email", "send", "bob@example.com").allowed
