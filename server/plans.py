"""Multi-step plans (tasks): validate -> approval -> per-step authorization -> final confirmation ->
phone execution -> verified outcome.

Reliability rules
- Every state change is a transaction on a copy of the plan: audit events are written, the plan is
  persisted, and only then does the in-memory state change. If storage fails, nothing happens and no
  action is dispatched (fail closed).
- Before every dispatch (first run, resume, retry, re-dispatch after restart) the plan approval, the
  capability grant and, for consequential steps, a fresh confirmation for *this attempt* are re-checked.
- Each dispatch has a unique dispatch_id (plan-step-attempt). Results are accepted once per dispatch_id;
  duplicates are ignored, stale ids rejected.
- "transient" failures mean the phone knows nothing happened: retried automatically with bounded
  exponential backoff. "uncertain" outcomes, hand-offs to other apps (sends, calendar events) and stale
  dispatches of non-idempotent tools are never retried blindly: the user must verify what happened.
"""
import copy
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from audit import Auditor, scrub
from permissions import PermissionStore
from storage import PersistenceError, Storage
from tools import ToolValidationError, get_tool, validate_step

MAX_STEPS = 8
MAX_ATTEMPTS = 3  # automatic attempts per step for transient failures
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
APPROVAL_TTL = 24 * 3600
CONFIRMATION_TTL = 10 * 60
RESULT_TIMEOUT = 120  # a dispatch with no result after this long is treated as stale
MAX_CHECKPOINTS = 60

# Plan states
AWAITING_APPROVAL = "awaiting_approval"
RUNNING = "running"
AWAITING_CONFIRMATION = "awaiting_confirmation"
AWAITING_RESULT = "awaiting_result"
AWAITING_VERIFICATION = "awaiting_verification"
RETRY_SCHEDULED = "retry_scheduled"
BLOCKED = "blocked"  # a step lacks permission; resume after granting
STEP_FAILED = "step_failed"
COMPLETED = "completed"
CANCELLED = "cancelled"

TERMINAL = {COMPLETED, CANCELLED}
OUTCOMES = {"succeeded", "handed_off", "failed", "uncertain", "cancelled"}  # cancelled = by the user
ERROR_CLASSES = {"transient", "permanent"}


def backoff_delay(attempt: int) -> float:
    """Delay before automatic attempt `attempt + 1`: 2s, 4s, 8s ... capped at 60s."""
    return min(BACKOFF_CAP, BACKOFF_BASE * 2 ** max(0, attempt - 1))


class PlanError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class Step:
    id: int
    tool: str
    args: dict
    status: str = "pending"
    # pending|awaiting_confirmation|blocked|dispatched|retry_scheduled|handed_off|uncertain|succeeded|failed|cancelled
    attempts: int = 0
    dispatch_id: Optional[str] = None
    dispatched_at: Optional[float] = None
    dispatch_boot: Optional[str] = None
    confirmed_at: Optional[float] = None
    confirmed_for_attempt: Optional[int] = None
    next_attempt_at: Optional[float] = None
    error_class: Optional[str] = None
    verification: Optional[str] = None
    message: str = ""
    last_result_dispatch: Optional[str] = None
    checkpoints: list = field(default_factory=list)
    provider_ref: dict = field(default_factory=dict)  # e.g. Outlook draft id, saved before the side effect

    def to_dict(self) -> dict:
        tool = get_tool(self.tool)
        return {
            "id": self.id,
            "tool": self.tool,
            "args": self.args,
            "summary": tool.summarize(self.args),
            "status": self.status,
            "consequential": tool.consequential,
            "capability": tool.capability.value if tool.capability else None,
            "scope": tool.scope,
            "attempts": self.attempts,
            "dispatch_id": self.dispatch_id,
            "error_class": self.error_class,
            "verification": self.verification,
            "next_attempt_at": self.next_attempt_at,
            "message": self.message,
            "checkpoints": self.checkpoints,
        }


@dataclass
class Plan:
    id: str
    session_id: str
    goal: str
    steps: list
    status: str = AWAITING_APPROVAL
    created_at: float = 0.0
    updated_at: float = 0.0
    approved_at: Optional[float] = None
    finished_at: Optional[float] = None
    note: str = ""

    def to_dict(self) -> dict:
        done = sum(1 for s in self.steps if s.status == "succeeded")
        retry_at = next((s.next_attempt_at for s in self.steps if s.status == "retry_scheduled"), None)
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "note": self.note,
            "progress": {"done": done, "total": len(self.steps)},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_at": self.approved_at,
            "finished_at": self.finished_at,
            "retry_at": retry_at,
            "steps": [s.to_dict() for s in self.steps],
        }

    def to_record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, rec: dict) -> "Plan":
        fields = cls.__dataclass_fields__
        step_fields = Step.__dataclass_fields__
        data = {k: v for k, v in rec.items() if k in fields}
        data["steps"] = [Step(**{k: v for k, v in s.items() if k in step_fields}) for s in rec.get("steps", [])]
        return cls(**data)


class _Txn:
    def __init__(self):
        self.events: list[tuple] = []
        self.approvals: list[dict] = []
        self.dispatch: Optional[dict] = None
        self.confirmation: Optional[dict] = None


class PlanManager:
    def __init__(
        self,
        permissions: PermissionStore,
        clock: Callable[[], float] = time.time,
        storage: Optional[Storage] = None,
        executor=None,
    ):
        self.permissions = permissions
        self.executor = executor  # runs server-side tools: executor.run(tool, args, ExecContext) -> outcome
        self.clock = clock
        self.storage = storage
        self.auditor = Auditor(storage, clock) if storage is not None else None
        self.boot_id = uuid.uuid4().hex[:8]  # dispatches from a previous run are stale
        self._plans: dict[str, Plan] = {}
        self._lock = threading.RLock()
        self.load_error: Optional[str] = None
        if storage is not None:
            try:
                for rec in storage.load_all("tasks"):
                    plan = Plan.from_record(rec)
                    self._plans[plan.id] = plan
            except PersistenceError as e:
                self.load_error = str(e)  # every later write fails too, so nothing can be dispatched

    # ---- queries --------------------------------------------------------
    def get(self, plan_id: str) -> Plan:
        with self._lock:
            plan = self._plans.get(plan_id)
        if plan is None:
            raise PlanError(f"No plan '{plan_id}'", 404)
        return plan

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            plans = sorted(self._plans.values(), key=lambda p: p.created_at, reverse=True)[:limit]
            return [p.to_dict() for p in plans]

    # ---- creation -------------------------------------------------------
    def create(self, session_id: str, goal: str, raw_steps: list) -> Plan:
        """Validates every step first; an invalid or unknown tool rejects the whole plan."""
        if not raw_steps:
            raise ToolValidationError("A plan needs at least one step")
        if len(raw_steps) > MAX_STEPS:
            raise ToolValidationError(f"A plan can have at most {MAX_STEPS} steps")
        steps = []
        for i, raw in enumerate(raw_steps):
            if not isinstance(raw, dict):
                raise ToolValidationError(f"Step {i + 1} is malformed")
            tool, args = validate_step(raw.get("tool"), raw.get("args"))
            steps.append(Step(id=i, tool=tool.name, args=args))
        now = self.clock()
        plan = Plan(uuid.uuid4().hex[:12], session_id, goal.strip()[:300] or "Task", steps,
                    created_at=now, updated_at=now)
        for s in steps:
            self._checkpoint(s, "pending")
        txn = _Txn()
        txn.events.append(("plan_created", None, {"steps": ",".join(s.tool for s in steps)}))
        with self._lock:
            self._commit(plan, txn)
        return plan

    # ---- lifecycle ------------------------------------------------------
    def approve(self, plan_id: str) -> dict:
        def fn(plan: Plan, txn: _Txn):
            if plan.status != AWAITING_APPROVAL:
                raise PlanError(f"Plan is {plan.status}, not awaiting approval")
            now = self.clock()
            plan.approved_at = now
            txn.approvals.append({"kind": "plan_approval", "plan_id": plan.id, "step_id": None, "at": now,
                                  "expires_at": now + APPROVAL_TTL})
            txn.events.append(("plan_approved", None, {}))
            plan.status = RUNNING
            self._advance(plan, txn)

        return self._drain(self._txn(plan_id, fn))

    def cancel(self, plan_id: str) -> dict:
        def fn(plan: Plan, txn: _Txn):
            if plan.status in TERMINAL:
                raise PlanError(f"Plan is already {plan.status}")
            for s in plan.steps:
                # A dispatched step may already be running on the phone: keep it so a late result is recorded.
                if s.status in ("pending", "awaiting_confirmation", "blocked", "retry_scheduled", "failed"):
                    self._checkpoint(s, "cancelled")
            plan.status = CANCELLED
            plan.finished_at = self.clock()
            plan.note = "Cancelled."
            txn.events.append(("plan_cancelled", None, {}))

        return self._txn(plan_id, fn)

    def advance(self, plan_id: str) -> dict:
        """Resume. Re-checks approval and permissions before anything is dispatched."""

        def fn(plan: Plan, txn: _Txn):
            if plan.status == AWAITING_APPROVAL:
                raise PlanError("Plan has not been approved")
            if plan.status in TERMINAL:
                raise PlanError(f"Plan is {plan.status}")
            if plan.status == STEP_FAILED:
                raise PlanError("A step failed; retry it or cancel the plan")
            if plan.status == AWAITING_VERIFICATION:
                raise PlanError("Confirm what happened with the current step first")
            if plan.status == AWAITING_RESULT:
                self._resume_dispatched(plan, txn)
                return
            if plan.status == RETRY_SCHEDULED:
                step = next(s for s in plan.steps if s.status == "retry_scheduled")
                if self.clock() < (step.next_attempt_at or 0):
                    raise PlanError(f"Retry not before {step.next_attempt_at:.0f}", 425)
            txn.events.append(("plan_resumed", None, {"from": plan.status}))
            plan.status = RUNNING
            self._advance(plan, txn)

        return self._drain(self._txn(plan_id, fn))

    def confirm(self, plan_id: str, step_id: int) -> dict:
        """Explicit final confirmation for this attempt of a consequential step, then resume."""

        def fn(plan: Plan, txn: _Txn):
            step = self._step(plan, step_id)
            if plan.status != AWAITING_CONFIRMATION or step.status != "awaiting_confirmation":
                raise PlanError("This step is not awaiting confirmation")
            now = self.clock()
            step.confirmed_at = now
            step.confirmed_for_attempt = step.attempts + 1
            txn.approvals.append({"kind": "step_confirmation", "plan_id": plan.id, "step_id": step.id, "at": now,
                                  "attempt": step.confirmed_for_attempt, "expires_at": now + CONFIRMATION_TTL})
            txn.events.append(("step_confirmed", step.id, {"attempt": step.confirmed_for_attempt}))
            plan.status = RUNNING
            self._advance(plan, txn)

        return self._drain(self._txn(plan_id, fn))

    def report(
        self,
        plan_id: str,
        step_id: int,
        dispatch_id: Optional[str],
        outcome: str,
        error_class: Optional[str] = None,
        verification: Optional[str] = None,
        message: str = "",
    ) -> dict:
        """Phone reports what happened for one dispatch. Duplicate reports are ignored."""
        return self._drain(self._report(plan_id, step_id, dispatch_id, outcome, error_class, verification, message))

    def _report(
        self,
        plan_id: str,
        step_id: int,
        dispatch_id: Optional[str],
        outcome: str,
        error_class: Optional[str] = None,
        verification: Optional[str] = None,
        message: str = "",
        internal: bool = False,
    ) -> dict:
        if outcome not in OUTCOMES:
            raise PlanError(f"outcome must be one of {sorted(OUTCOMES)}", 422)
        if outcome == "failed":
            error_class = error_class or "permanent"
            if error_class not in ERROR_CLASSES:
                raise PlanError(f"error_class must be one of {sorted(ERROR_CLASSES)}", 422)

        with self._lock:
            current = self.get(plan_id)
            cur_step = self._step(current, step_id)
            if get_tool(cur_step.tool).runs_on == "server" and not internal:
                raise PlanError("This step runs on the server; the phone can't report its result")
            if dispatch_id and dispatch_id == cur_step.last_result_dispatch:
                return self._result(current)  # duplicate report: already applied, no change

        def fn(plan: Plan, txn: _Txn):
            step = self._step(plan, step_id)
            if dispatch_id is not None and dispatch_id != step.dispatch_id:
                raise PlanError("Stale dispatch_id: this result is for an earlier attempt")
            if get_tool(step.tool).runs_on == "server" and not internal:
                raise PlanError("This step runs on the server; the phone can't report its result")
            # A late result for a step marked uncertain (restart/timeout) is real evidence: accept it.
            late_for_uncertain = step.status == "uncertain" and dispatch_id == step.dispatch_id
            if step.status != "dispatched" and not late_for_uncertain:
                raise PlanError("This step was not dispatched")
            tool = get_tool(step.tool)
            step.last_result_dispatch = step.dispatch_id
            step.message = scrub(message, 500)
            reported = outcome
            if reported == "succeeded" and tool.user_verifies:
                reported = "handed_off"  # another app finishes this; only the user can confirm it happened
            if reported == "succeeded" and not (verification or "").strip():
                raise PlanError("A succeeded result must say how it was verified", 422)

            if plan.status == CANCELLED:
                self._checkpoint(step, {"succeeded": "succeeded", "failed": "failed"}.get(reported, "uncertain"))
                txn.events.append(("late_result_after_cancel", step.id, {"outcome": reported}))
                return

            if reported == "succeeded":
                step.verification = scrub(verification, 100)
                self._checkpoint(step, "succeeded")
                txn.events.append(("step_succeeded", step.id, {"verification": step.verification}))
                plan.status = RUNNING
                self._advance(plan, txn)
            elif reported == "cancelled":
                self._checkpoint(step, "cancelled")
                plan.status = STEP_FAILED
                plan.note = f"You cancelled step {step.id + 1}. Retry it or cancel the plan."
                txn.events.append(("step_cancelled_by_user", step.id, {}))
            elif reported in ("handed_off", "uncertain"):
                self._checkpoint(step, reported)
                plan.status = AWAITING_VERIFICATION
                plan.note = ("Opened in the other app. Did it actually happen?" if reported == "handed_off"
                             else "The outcome is unknown. Did it happen?")
                txn.events.append((f"step_{reported}", step.id, {}))
            else:
                step.error_class = error_class
                if error_class == "transient" and step.attempts < MAX_ATTEMPTS:
                    delay = backoff_delay(step.attempts)
                    step.next_attempt_at = self.clock() + delay
                    self._checkpoint(step, "retry_scheduled")
                    plan.status = RETRY_SCHEDULED
                    plan.note = f"Temporary failure; retrying step {step.id + 1} in {delay:.0f}s."
                    txn.events.append(("step_retry_scheduled", step.id, {"delay": delay, "attempt": step.attempts}))
                else:
                    self._checkpoint(step, "failed")
                    plan.status = STEP_FAILED
                    why = "gave up after retries" if error_class == "transient" else (step.message or "failed")
                    plan.note = f"Step {step.id + 1} failed: {why}"
                    txn.events.append(("step_failed", step.id, {"error_class": error_class, "attempt": step.attempts}))

        return self._txn(plan_id, fn)

    def verify(self, plan_id: str, step_id: int, happened: bool) -> dict:
        """The user states whether a handed-off or uncertain step actually happened."""

        def fn(plan: Plan, txn: _Txn):
            step = self._step(plan, step_id)
            if plan.status != AWAITING_VERIFICATION or step.status not in ("handed_off", "uncertain"):
                raise PlanError("This step is not awaiting verification")
            if happened:
                step.verification = "user_confirmed"
                self._checkpoint(step, "succeeded")
                txn.events.append(("step_verified", step.id, {"happened": True}))
                plan.status = RUNNING
                self._advance(plan, txn)
            else:
                step.error_class = "permanent"
                step.message = "User reported it did not happen."
                self._checkpoint(step, "failed")
                plan.status = STEP_FAILED
                plan.note = f"Step {step.id + 1} did not happen."
                txn.events.append(("step_verified", step.id, {"happened": False}))

        return self._drain(self._txn(plan_id, fn))

    def retry(self, plan_id: str, step_id: int) -> dict:
        """Manual retry of a failed or blocked step: re-authorized and re-confirmed like a first run."""

        def fn(plan: Plan, txn: _Txn):
            step = self._step(plan, step_id)
            if plan.status in TERMINAL or step.status not in ("failed", "blocked", "cancelled"):
                raise PlanError("Only failed, blocked or cancelled steps can be retried")
            step.confirmed_at = None
            step.confirmed_for_attempt = None
            step.next_attempt_at = None
            self._checkpoint(step, "pending")
            txn.events.append(("step_retry_requested", step.id, {}))
            plan.status = RUNNING
            self._advance(plan, txn)

        return self._drain(self._txn(plan_id, fn))

    # ---- server-executed steps ------------------------------------------
    def _drain(self, result: dict) -> dict:
        """Runs server-side dispatches until the plan needs the phone or the user, or finishes."""
        for _ in range(MAX_STEPS * (MAX_ATTEMPTS + 1)):
            d = result.get("dispatch")
            if not d or d.get("runs_on") != "server":
                return result
            plan_id = result["plan"]["id"]
            out = self._execute_server(plan_id, d)
            result = self._report(plan_id, d["step_id"], d["dispatch_id"], out.get("outcome", "uncertain"),
                                  out.get("error_class"), out.get("verification"), out.get("message", ""),
                                  internal=True)
        return result

    def _execute_server(self, plan_id: str, d: dict) -> dict:
        from integrations import ExecContext

        plan = self.get(plan_id)
        step = plan.steps[d["step_id"]]
        tool = get_tool(step.tool)
        if self.executor is None:
            return {"outcome": "failed", "error_class": "permanent", "message": "NOT_CONFIGURED: no integrations"}
        ctx = ExecContext(plan.id, step.id, d["dispatch_id"], plan.session_id, dict(step.provider_ref),
                          lambda ref: self._save_ref(plan_id, step.id, d["dispatch_id"], ref))
        try:
            return self.executor.run(step.tool, dict(step.args), ctx)
        except Exception as e:  # executor bug or failed ref checkpoint: never assume it didn't happen
            if tool.idempotent:
                return {"outcome": "failed", "error_class": "transient", "message": f"Internal error: {type(e).__name__}"}
            return {"outcome": "uncertain", "message": f"Internal error: {type(e).__name__}"}

    def _save_ref(self, plan_id: str, step_id: int, dispatch_id: str, ref: dict):
        """Checkpoints a provider reference before the next side effect. Raises if it can't be saved."""

        def fn(plan: Plan, txn: _Txn):
            step = self._step(plan, step_id)
            if step.dispatch_id != dispatch_id or step.status != "dispatched":
                raise PlanError("Step is no longer running this dispatch")
            step.provider_ref.update(ref)
            txn.events.append(("provider_ref_saved", step.id, {"keys": ",".join(sorted(ref))}))

        self._txn(plan_id, fn)

    # ---- internals ------------------------------------------------------
    def _resume_dispatched(self, plan: Plan, txn: _Txn):
        step = next(s for s in plan.steps if s.status == "dispatched")
        now = self.clock()
        stale = step.dispatch_boot != self.boot_id or now - (step.dispatched_at or 0) > RESULT_TIMEOUT
        if not stale:
            raise PlanError("Waiting for the phone to report the current step's result")
        tool = get_tool(step.tool)
        if not tool.idempotent:
            # Might already have happened: never re-send or re-apply blindly.
            self._checkpoint(step, "uncertain")
            plan.status = AWAITING_VERIFICATION
            plan.note = "I lost track of this step (restart or timeout). Did it happen?"
            txn.events.append(("step_uncertain_on_resume", step.id, {}))
            return
        if not self._checks_pass(plan, step, tool, txn):
            return
        # Same dispatch_id: a phone that already ran it answers from its ledger instead of running it again.
        step.dispatched_at = now
        step.dispatch_boot = self.boot_id
        plan.status = AWAITING_RESULT
        txn.events.append(("step_redispatched", step.id, {"dispatch_id": step.dispatch_id}))
        txn.dispatch = self._dispatch(step, tool)

    def _checks_pass(self, plan: Plan, step: Step, tool, txn: _Txn) -> bool:
        """Approval + capability re-check. Pauses the plan and returns False if either fails."""
        now = self.clock()
        if plan.approved_at is None or now - plan.approved_at > APPROVAL_TTL:
            plan.approved_at = None
            plan.status = AWAITING_APPROVAL
            plan.note = "The approval for this plan expired. Approve it again to continue."
            txn.events.append(("approval_expired", step.id, {}))
            return False
        if tool.capability:
            target = step.args.get(tool.target_param) if tool.target_param else None
            decision = self.permissions.authorize(tool.capability.value, tool.scope, target)
            if not decision.allowed:
                self._checkpoint(step, "blocked")
                plan.status = BLOCKED
                plan.note = decision.reason
                txn.events.append(("permission_denied", step.id, {"capability": tool.capability.value,
                                                                  "scope": tool.scope}))
                return False
        return True

    def _advance(self, plan: Plan, txn: _Txn):
        step = next((s for s in plan.steps if s.status != "succeeded"), None)
        if step is None:
            plan.status = COMPLETED
            plan.finished_at = self.clock()
            plan.note = "All steps completed and verified."
            txn.events.append(("plan_completed", None, {}))
            return
        tool = get_tool(step.tool)
        if not self._checks_pass(plan, step, tool, txn):
            return

        if tool.consequential:
            now = self.clock()
            valid = (step.confirmed_at is not None and step.confirmed_for_attempt == step.attempts + 1
                     and now - step.confirmed_at <= CONFIRMATION_TTL)
            if not valid:
                step.confirmed_at = None
                step.confirmed_for_attempt = None
                self._checkpoint(step, "awaiting_confirmation")
                plan.status = AWAITING_CONFIRMATION
                plan.note = "Confirm before I do this."
                txn.confirmation = {"step_id": step.id, "summary": tool.summarize(step.args)}
                return

        step.attempts += 1
        step.dispatch_id = f"{plan.id}-{step.id}-{step.attempts}"
        step.dispatched_at = self.clock()
        step.dispatch_boot = self.boot_id
        step.next_attempt_at = None
        step.error_class = None
        self._checkpoint(step, "dispatched")
        plan.status = AWAITING_RESULT
        plan.note = f"Running step {step.id + 1} of {len(plan.steps)}."
        txn.events.append(("step_dispatched", step.id, {"tool": step.tool, "attempt": step.attempts,
                                                         "dispatch_id": step.dispatch_id, "_args": step.args}))
        txn.dispatch = self._dispatch(step, tool)

    @staticmethod
    def _dispatch(step: Step, tool) -> dict:
        if tool.runs_on == "server":
            return {"step_id": step.id, "dispatch_id": step.dispatch_id, "runs_on": "server"}
        return {"step_id": step.id, "dispatch_id": step.dispatch_id, "action": tool.to_action(step.args)}

    def _txn(self, plan_id: str, fn) -> dict:
        with self._lock:
            plan = copy.deepcopy(self.get(plan_id))
            txn = _Txn()
            fn(plan, txn)
            plan.updated_at = self.clock()
            self._commit(plan, txn)
            return self._result(plan, txn)

    def _commit(self, plan: Plan, txn: _Txn):
        """Audit, persist, then publish. Any storage failure aborts the transition."""
        try:
            if self.storage is not None:
                for event, step_id, details in txn.events:
                    details = dict(details)
                    args = details.pop("_args", None)
                    self.auditor.record(event, plan.id, step_id, args=args, status=plan.status, **details)
                for a in txn.approvals:
                    a = {"id": uuid.uuid4().hex[:16], **a}
                    self.storage.save("approvals", a["id"], a)
                self.storage.save("tasks", plan.id, plan.to_record())
        except PersistenceError as e:
            raise PlanError(f"Not saved, nothing was done: {e}", 503) from e
        self._plans[plan.id] = plan

    def _result(self, plan: Plan, txn: Optional[_Txn] = None) -> dict:
        verification = None
        if plan.status == AWAITING_VERIFICATION:
            step = next((s for s in plan.steps if s.status in ("handed_off", "uncertain")), None)
            if step:
                verification = {"step_id": step.id, "summary": get_tool(step.tool).summarize(step.args)}
        return {
            "plan": plan.to_dict(),
            "dispatch": txn.dispatch if txn else None,
            "confirmation": txn.confirmation if txn else None,
            "verification": verification,
        }

    def _step(self, plan: Plan, step_id: int) -> Step:
        if not isinstance(step_id, int) or not 0 <= step_id < len(plan.steps):
            raise PlanError(f"No step {step_id}", 404)
        return plan.steps[step_id]

    def _checkpoint(self, step: Step, status: str):
        step.status = status
        step.checkpoints.append({"status": status, "t": self.clock(), "attempt": step.attempts})
        del step.checkpoints[:-MAX_CHECKPOINTS]
