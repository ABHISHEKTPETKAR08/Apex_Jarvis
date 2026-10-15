import pytest

from permissions import PermissionStore
from plans import PlanError, PlanManager
from tools import REGISTRY, ToolValidationError, validate_step


class Clock:
    t = 1_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def perms(clock):
    return PermissionStore(clock=clock)


@pytest.fixture
def plans(perms, clock):
    return PlanManager(perms, clock=clock)


WA = {"tool": "send_whatsapp", "args": [{"name": "to", "value": "+919900000000"}, {"name": "message", "value": "On my way"}]}
PHOTO = {"tool": "take_photo", "args": []}
TIMER = {"tool": "set_timer", "args": {"seconds": "60"}}


def ok(plans, r, verification="activity_launched"):
    """Report the dispatched step as launched successfully."""
    d = r["dispatch"]
    return plans.report(r["plan"]["id"], d["step_id"], d["dispatch_id"], "succeeded", verification=verification)


# ---- tool validation ------------------------------------------------------

@pytest.mark.parametrize("name", ["run_shell", "exec_python", "os.system", "", None, "SEND_EMAIL"])
def test_unknown_tools_rejected(name):
    with pytest.raises(ToolValidationError, match="Unknown tool"):
        validate_step(name, {})


def test_no_code_execution_tools_registered():
    assert not {n for n in REGISTRY if any(w in n for w in ("shell", "exec", "eval", "python", "code", "script"))}


@pytest.mark.parametrize("tool,args,msg", [
    ("send_email", {"to": "not-an-email", "subject": "s", "body": "b"}, "invalid format"),
    ("send_email", {"to": "a@b.co", "subject": "s"}, "missing required"),
    ("set_alarm", {"time": "25:00"}, "invalid format"),
    ("set_timer", {"seconds": "60", "command": "rm -rf /"}, "unexpected"),
    ("create_calendar_event", {"title": "x", "start": "2026-02-30 10:00"}, "real date"),
    ("speak", {"text": "x" * 1001}, "longer than"),
    ("open_app", {"app": {"nested": 1}}, "must be a string"),
])
def test_invalid_args_rejected(tool, args, msg):
    with pytest.raises(ToolValidationError, match=msg):
        validate_step(tool, args)


def test_list_args_accepted():
    tool, args = validate_step("send_whatsapp", WA["args"])
    assert args == {"to": "+919900000000", "message": "On my way"}


def test_plan_with_unknown_tool_is_rejected_entirely(plans):
    with pytest.raises(ToolValidationError):
        plans.create("s", "do it", [TIMER, {"tool": "run_shell", "args": {"cmd": "whoami"}}])


def test_plan_step_limit(plans):
    with pytest.raises(ToolValidationError, match="at most"):
        plans.create("s", "too many", [TIMER] * 9)


# ---- approval / confirmation ---------------------------------------------

def test_plan_requires_approval_before_anything_runs(plans):
    plan = plans.create("s", "timer", [TIMER])
    assert plan.status == "awaiting_approval"
    with pytest.raises(PlanError, match="not been approved"):
        plans.advance(plan.id)


def test_send_requires_permission_then_explicit_confirmation(plans, perms):
    plan = plans.create("s", "whatsapp", [WA])
    r = plans.approve(plan.id)
    assert r["plan"]["status"] == "blocked" and r["dispatch"] is None  # default deny

    perms.grant("whatsapp")
    r = plans.advance(plan.id)  # resume re-authorizes
    assert r["plan"]["status"] == "awaiting_confirmation"
    assert r["confirmation"]["summary"] == "WhatsApp +919900000000: On my way"
    assert r["dispatch"] is None

    r = plans.confirm(plan.id, 0)
    assert r["plan"]["status"] == "awaiting_result"
    assert r["dispatch"]["action"]["type"] == "whatsapp_message"

    # Opening WhatsApp is not proof of sending: the user must verify.
    r = ok(plans, r)
    assert r["plan"]["status"] == "awaiting_verification" and r["verification"]["step_id"] == 0
    r = plans.verify(plan.id, 0, happened=True)
    assert r["plan"]["status"] == "completed" and r["plan"]["progress"] == {"done": 1, "total": 1}


def test_revocation_after_confirmation_blocks_dispatch(plans, perms):
    g = perms.grant("whatsapp")
    plan = plans.create("s", "whatsapp", [WA])
    plans.approve(plan.id)
    perms.revoke(g.id)
    r = plans.confirm(plan.id, 0)
    assert r["plan"]["status"] == "blocked" and r["dispatch"] is None


def test_expiry_mid_plan_blocks_next_step(plans, perms, clock):
    perms.grant("camera", ttl_seconds=30)
    plan = plans.create("s", "two photos", [PHOTO, PHOTO])
    r = plans.approve(plan.id)
    assert r["dispatch"]["step_id"] == 0
    clock.t += 31
    r = ok(plans, r)
    assert r["plan"]["status"] == "blocked" and r["plan"]["progress"]["done"] == 1


def test_retry_reauthorizes_and_requires_fresh_confirmation(plans, perms):
    g = perms.grant("whatsapp")
    plan = plans.create("s", "whatsapp", [WA])
    plans.approve(plan.id)
    r = plans.confirm(plan.id, 0)
    r = plans.report(plan.id, 0, r["dispatch"]["dispatch_id"], "failed", "permanent", message="WhatsApp not installed")
    assert r["plan"]["status"] == "step_failed"
    with pytest.raises(PlanError):
        plans.advance(plan.id)

    r = plans.retry(plan.id, 0)
    assert r["plan"]["status"] == "awaiting_confirmation"  # confirmation reset

    perms.revoke(g.id)
    r = plans.confirm(plan.id, 0)
    assert r["plan"]["status"] == "blocked"


def test_multi_step_progress_and_non_consequential_steps_skip_confirmation(plans, perms):
    perms.grant("camera")
    plan = plans.create("s", "photo then timer", [PHOTO, TIMER])
    r = plans.approve(plan.id)
    assert r["dispatch"]["action"]["type"] == "camera_capture"
    r = ok(plans, r)
    assert r["dispatch"]["action"] == {"type": "set_timer", "value": "60", "params": {}}
    assert r["plan"]["progress"] == {"done": 1, "total": 2}
    r = ok(plans, r)
    assert r["plan"]["status"] == "completed"


@pytest.mark.parametrize("call", ["confirm", "report", "retry"])
def test_out_of_order_calls_rejected(plans, call):
    plan = plans.create("s", "timer", [TIMER])
    r = plans.approve(plan.id)
    ok(plans, r)  # completed
    with pytest.raises(PlanError):
        if call == "report":
            plans.report(plan.id, 0, "some-other-dispatch", "succeeded", verification="x")
        else:
            getattr(plans, call)(plan.id, 0)


def test_cancel(plans):
    plan = plans.create("s", "timer", [TIMER])
    r = plans.cancel(plan.id)
    assert r["plan"]["status"] == "cancelled"
    with pytest.raises(PlanError):
        plans.approve(plan.id)
