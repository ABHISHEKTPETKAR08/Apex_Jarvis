"""JARVIS brain: FastAPI server the Android client talks to.

POST /chat {"text", "session_id"} -> {"reply", "action", "plan"?}
  - one low-risk step   -> "action" for the phone to run immediately (authorized first if capability-gated)
  - several steps, or any step that sends/commits -> "plan" that needs approval and per-step confirmation
The AI provider is chosen explicitly with AI_PROVIDER in server/.env (see .env.example).
Storage is MongoDB Atlas when MONGODB_URI is set, otherwise local JSON files (see storage.py).
Mutating plan/permission endpoints accept an Idempotency-Key header; a repeated key returns the first response.
"""
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from audit import Auditor, mask_recipient
from graph import IntegrationError
from integrations import Integrations
from permissions import PermissionError_, PermissionStore, SCOPES
from plans import PlanError, PlanManager
from providers import ProviderError, build_provider
from storage import PersistenceError, build_storage
from tools import REGISTRY, ToolValidationError, validate_step

HERE = Path(__file__).resolve().parent
log = logging.getLogger("jarvis")

NONE_ACTION = {"type": "none", "value": "", "params": {}}
IDEMPOTENCY_TTL = 24 * 3600

# Phone action types that are capability-gated; the phone must ask /actions/authorize before re-running them.
ACTION_CAPABILITY = {
    t.to_action({p.name: "x" for p in t.params})["type"]: t
    for t in REGISTRY.values() if t.capability and t.runs_on == "phone"
}


def _ask_for_missing(e: Exception) -> Optional[str]:
    """'send_whatsapp: missing required argument 'message'' -> a question instead of a vague refusal."""
    import re as _re

    m = _re.match(r"(\w+): missing required argument '(\w+)'", str(e))
    if not m or m.group(1) not in REGISTRY:
        return None
    param = next((p for p in REGISTRY[m.group(1)].params if p.name == m.group(2)), None)
    return f"I need one more detail, sir: {param.description.lower()}." if param else None


def not_configured(what: str, detail: str) -> dict:
    return {"reply": f"{what} isn't set up yet, sir.", "action": NONE_ACTION, "error_code": "NOT_CONFIGURED",
            "error": detail}


class ChatIn(BaseModel):
    text: str
    session_id: str = "default"


class GrantIn(BaseModel):
    capability: str
    scopes: Optional[list[str]] = None
    ttl_seconds: Optional[float] = None
    targets: Optional[list[str]] = None


class RevokeIn(BaseModel):
    grant_id: Optional[str] = None
    capability: Optional[str] = None


class ResultIn(BaseModel):
    dispatch_id: str
    outcome: str  # succeeded | handed_off | failed | uncertain
    error_class: Optional[str] = None  # transient (nothing happened, safe to retry) | permanent
    verification: Optional[str] = None  # how success was observed, e.g. "activity_launched"
    message: str = ""


class VerifyIn(BaseModel):
    happened: bool


class TtsIn(BaseModel):
    text: str


class AuthorizeIn(BaseModel):
    type: str
    value: str = ""


def create_app(
    env: Optional[Mapping[str, str]] = None,
    data_dir: Optional[Path] = None,
    clock=None,
    mongo_client=None,
    storage=None,
    http=None,
    integrations=None,
) -> FastAPI:
    if env is None:
        load_dotenv(HERE / ".env")
        env = dict(os.environ)
    clock = clock or time.time
    data_dir = Path(data_dir) if data_dir else HERE / "data"
    storage = storage or build_storage(env, data_dir, mongo_client)
    storage_status = storage.status()
    if not storage_status["ok"]:
        log.error("Storage unavailable (%s): actions are disabled until it is fixed.", storage_status.get("error"))
    try:
        permissions = PermissionStore(clock=clock, storage=storage)
    except PersistenceError:
        permissions = PermissionStore(clock=clock)  # empty = everything denied
        permissions.storage = storage  # grants fail closed too
    integrations = integrations or Integrations(env, storage, http=http, clock=clock, start_threads=http is None)
    plans = PlanManager(permissions, clock=clock, storage=storage, executor=integrations)
    auditor = Auditor(storage, clock)
    provider, warning = build_provider(env)

    def tool_available(tool) -> bool:
        return integrations.available(tool.requires)

    provider.tool_filter = tool_available
    integrations.summarizer = provider.summarize
    if warning:
        log.warning(warning)
    log.info("AI provider: %s (configured=%s); storage: %s", provider.name, provider.configured, storage.backend)

    app = FastAPI(title="JARVIS")
    app.state.provider = provider
    app.state.permissions = permissions
    app.state.plans = plans
    app.state.storage = storage
    app.state.integrations = integrations

    # ---- idempotency ----------------------------------------------------
    idem_lock = threading.Lock()
    idem_cache: dict[str, dict] = {}
    try:
        for rec in storage.load_all("idempotency"):
            if clock() - rec.get("at", 0) < IDEMPOTENCY_TTL:
                idem_cache[rec["id"]] = rec
    except PersistenceError:
        pass

    def idempotent(request: Request, fn):
        """Replays the stored response for a repeated Idempotency-Key instead of running fn again."""
        key = request.headers.get("Idempotency-Key")
        if not key:
            return fn()
        if len(key) > 128:
            raise HTTPException(400, "Idempotency-Key too long")
        rec_id = hashlib.sha256(f"{request.method} {request.url.path} {key}".encode()).hexdigest()[:32]
        with idem_lock:
            cached = idem_cache.get(rec_id)
            if cached and clock() - cached["at"] < IDEMPOTENCY_TTL:
                return {**cached["response"], "idempotent_replay": True}
            response = fn()  # errors propagate and are not cached: a retry re-evaluates
            rec = {"id": rec_id, "at": clock(), "path": request.url.path, "response": response}
            idem_cache[rec_id] = rec
            try:
                storage.save("idempotency", rec_id, rec)
            except PersistenceError as e:
                log.warning("Idempotency record not persisted: %s", e)
            return response

    def plan_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PlanError as e:
            raise HTTPException(e.status_code, str(e))

    # ---- status ---------------------------------------------------------
    @app.get("/health")
    def health():
        st = storage.status()
        return {"ok": True, "mode": provider.name, "configured": provider.configured,
                "storage": st["backend"], "storage_ok": st["ok"]}

    @app.get("/provider/status")
    def provider_status():
        return {**provider.status(), "warning": warning}

    @app.post("/provider/check")
    def provider_check():
        return provider.check()

    @app.get("/storage/status")
    def storage_info():
        return {**storage.status(), "plans_load_error": plans.load_error}

    # ---- chat -----------------------------------------------------------
    @app.post("/chat")
    def chat(body: ChatIn):
        text = body.text.strip()
        if not text:
            return {"reply": "I didn't catch that, sir.", "action": NONE_ACTION}
        try:
            out = provider.respond(body.session_id, text)
        except ProviderError as e:
            return {"reply": "My AI provider isn't available right now, sir.", "action": NONE_ACTION,
                    "error": str(e), "error_class": "transient" if e.transient else "permanent"}
        reply, steps = out.get("reply", ""), out.get("steps") or []
        if not steps:
            return {"reply": reply, "action": NONE_ACTION}

        # Tools whose integration isn't set up are refused up front (NOT_CONFIGURED), never faked.
        for raw in steps:
            name = raw.get("tool") if isinstance(raw, dict) else None
            t = REGISTRY.get(str(name))
            if t is not None and not tool_available(t):
                return not_configured(t.requires, integrations.unavailable_reason(t.requires))

        if len(steps) == 1:
            try:
                tool, args = validate_step(steps[0].get("tool"), steps[0].get("args"))
            except (ToolValidationError, AttributeError) as e:
                log.warning("Rejected tool call: %s", e)
                return {"reply": _ask_for_missing(e) or "Sorry sir, I couldn't run that safely.",
                        "action": NONE_ACTION, "error": str(e)}
            if tool.runs_on == "server" and not tool.mutates and not tool.consequential:
                return server_read(reply, tool, args, body.session_id)
            if not tool.consequential and not tool.mutates:
                if tool.capability:
                    target = args.get(tool.target_param) if tool.target_param else None
                    decision = permissions.authorize(tool.capability.value, tool.scope, target)
                    if not decision.allowed:
                        return {"reply": f"I need {tool.capability.value} permission for that, sir.",
                                "action": NONE_ACTION, "error": decision.reason}
                try:
                    auditor.record("direct_action", None, None, args=args, tool=tool.name)
                except PersistenceError as e:
                    if tool.capability:  # gated actions must be on record before they run
                        return {"reply": "I can't record that action right now, so I won't run it, sir.",
                                "action": NONE_ACTION, "error": str(e)}
                    log.warning("Audit not recorded for %s: %s", tool.name, e)
                return {"reply": reply, "action": tool.to_action(args)}

        try:
            plan = plans.create(body.session_id, text, steps)
        except ToolValidationError as e:
            log.warning("Rejected plan: %s", e)
            return {"reply": _ask_for_missing(e) or "Sorry sir, I couldn't build a safe plan for that.",
                    "action": NONE_ACTION, "error": str(e)}
        except PlanError as e:
            return {"reply": "I couldn't save that plan, so I won't start it, sir.", "action": NONE_ACTION, "error": str(e)}
        return {"reply": reply, "action": NONE_ACTION, "plan": plan.to_dict()}

    def server_read(reply: str, tool, args: dict, session_id: str) -> dict:
        """Read-only provider call answered inline. Same grant + audit rules as any other action."""
        target = args.get(tool.target_param) if tool.target_param else None
        decision = permissions.authorize(tool.capability.value, tool.scope, target)
        if not decision.allowed:
            return {"reply": f"I need {tool.capability.value} {tool.scope} permission for that, sir.",
                    "action": NONE_ACTION, "error": decision.reason}
        try:
            auditor.record("server_read", None, None, args=args, tool=tool.name)
        except PersistenceError as e:
            return {"reply": "I can't record that right now, so I won't run it, sir.", "action": NONE_ACTION,
                    "error": str(e)}
        try:
            out = integrations.read(tool.name, args, session_id)
        except IntegrationError as e:
            if e.code == "NOT_CONFIGURED":
                return not_configured(tool.requires, str(e))
            return {"reply": "I couldn't reach that service just now, sir.", "action": NONE_ACTION,
                    "error": str(e), "error_code": e.code, "error_class": "transient" if e.transient else "permanent"}
        return {"reply": f"{reply} {out['text']}".strip(), "action": NONE_ACTION, "data": out["data"],
                "summary_source": out.get("source")}

    @app.post("/reset")
    def reset(body: ChatIn):
        provider.reset(body.session_id)
        return {"ok": True}

    # ---- integrations ---------------------------------------------------
    @app.get("/integrations/status")
    def integrations_status():
        return {**integrations.status(), "storage": storage.status()["backend"]}

    @app.post("/integrations/graph/connect")
    def graph_connect():
        """Starts Microsoft sign-in (device code). The user enters the code at the returned URL."""
        try:
            flow = integrations.graph.start_device_flow()
        except IntegrationError as e:
            raise HTTPException(503 if e.code == "NOT_CONFIGURED" else 502, {"code": e.code, "detail": str(e)})
        _audit_best_effort("graph_connect_started")
        return flow

    @app.post("/integrations/graph/disconnect")
    def graph_disconnect():
        try:
            integrations.graph.disconnect()
        except PersistenceError as e:
            raise HTTPException(503, str(e))
        _audit_best_effort("graph_disconnected")
        return {"state": integrations.graph.status()["state"]}

    # ---- Azure Speech (optional) ----------------------------------------
    def speech_error(e: IntegrationError) -> JSONResponse:
        status = {"NOT_CONFIGURED": 503, "BAD_INPUT": 400}.get(e.code, 429 if e.transient else 502)
        return JSONResponse({"code": e.code, "detail": str(e)}, status_code=status)

    def speech_allowed(capability: str, scope: str):
        d = permissions.authorize(capability, scope)
        if not d.allowed:
            return JSONResponse({"code": "PERMISSION_DENIED", "detail": d.reason}, status_code=403)
        return None

    @app.post("/speech/tts")
    def speech_tts(body: TtsIn):
        """Azure neural voice. Needs speaker:speak. The key never leaves the server."""
        if not integrations.speech.configured:
            return speech_error(IntegrationError("NOT_CONFIGURED", integrations.speech.status()["detail"]))
        if (denied := speech_allowed("speaker", "speak")) is not None:
            return denied
        if not body.text.strip():
            raise HTTPException(400, "text is empty")
        try:
            audio = integrations.speech.tts(body.text)
        except IntegrationError as e:
            return speech_error(e)
        _audit_best_effort("speech_tts", chars=len(body.text))
        return Response(content=audio, media_type="audio/mpeg")

    @app.post("/speech/stt")
    async def speech_stt(request: Request, language: Optional[str] = None):
        """Azure speech-to-text for a short WAV clip the user recorded. Needs microphone:listen. Audio isn't stored."""
        if not integrations.speech.configured:
            return speech_error(IntegrationError("NOT_CONFIGURED", integrations.speech.status()["detail"]))
        if (denied := speech_allowed("microphone", "listen")) is not None:
            return denied
        audio = await request.body()
        try:
            text = integrations.speech.stt(audio, language)
        except IntegrationError as e:
            return speech_error(e)
        _audit_best_effort("speech_stt", bytes=len(audio))
        return {"text": text}

    # ---- permissions ----------------------------------------------------
    @app.get("/permissions")
    def list_permissions(include_inactive: bool = False):
        return {
            "capabilities": {c.value: list(s) for c, s in SCOPES.items()},
            "grants": permissions.list(include_inactive),
        }

    @app.post("/permissions/grant")
    def grant(body: GrantIn, request: Request):
        def run():
            try:
                g = permissions.grant(body.capability, body.scopes, body.ttl_seconds, body.targets)
            except PermissionError_ as e:
                raise HTTPException(400, str(e))
            except PersistenceError as e:
                raise HTTPException(503, f"Grant not saved, so it is not active: {e}")
            _audit_best_effort("grant_created", grant_id=g.id, capability=g.capability, scopes=",".join(g.scopes),
                               ttl=body.ttl_seconds, targets=",".join(mask_recipient(t) for t in g.targets or []))
            return g.__dict__

        return idempotent(request, run)

    @app.post("/permissions/revoke")
    def revoke(body: RevokeIn, request: Request):
        def run():
            try:
                if body.grant_id:
                    permissions.revoke(body.grant_id)
                    n = 1
                elif body.capability:
                    n = permissions.revoke_capability(body.capability)
                else:
                    raise HTTPException(400, "Provide grant_id or capability")
            except PermissionError_ as e:
                raise HTTPException(400, str(e))
            except PersistenceError as e:
                raise HTTPException(503, f"Revoked for now, but not saved; it may return after a restart: {e}")
            _audit_best_effort("grant_revoked", grant_id=body.grant_id, capability=body.capability, count=n)
            return {"revoked": n}

        return idempotent(request, run)

    def _audit_best_effort(event: str, **details):
        try:
            auditor.record(event, **details)
        except PersistenceError as e:
            log.warning("Audit event %s not recorded: %s", event, e)

    @app.post("/actions/authorize")
    def authorize_action(body: AuthorizeIn):
        """Checked by the phone before re-running a capability-gated action outside a plan."""
        tool = ACTION_CAPABILITY.get(body.type)
        if tool is None:
            return {"allowed": True, "reason": "No capability required."}
        if tool.consequential:
            return {"allowed": False, "reason": "This action needs a confirmed plan; ask JARVIS again."}
        target = body.value if tool.target_param else None
        d = permissions.authorize(tool.capability.value, tool.scope, target)
        return {"allowed": d.allowed, "reason": d.reason}

    # ---- tasks (execution history) --------------------------------------
    @app.get("/tasks")
    def list_tasks(limit: int = 50):
        return {"tasks": plans.list(max(1, min(limit, 200)))}

    @app.get("/tasks/{plan_id}")
    def get_task(plan_id: str):
        return {"plan": plan_call(plans.get, plan_id).to_dict()}

    @app.get("/tasks/{plan_id}/audit")
    def task_audit(plan_id: str, limit: int = 200):
        plan_call(plans.get, plan_id)
        try:
            return {"events": storage.list_audit(plan_id, max(1, min(limit, 1000)))}
        except PersistenceError as e:
            raise HTTPException(503, str(e))

    # ---- plans ----------------------------------------------------------
    @app.get("/plans/{plan_id}")
    def get_plan(plan_id: str):
        return {"plan": plan_call(plans.get, plan_id).to_dict()}

    @app.post("/plans/{plan_id}/approve")
    def approve(plan_id: str, request: Request):
        return idempotent(request, lambda: plan_call(plans.approve, plan_id))

    @app.post("/plans/{plan_id}/cancel")
    def cancel(plan_id: str, request: Request):
        return idempotent(request, lambda: plan_call(plans.cancel, plan_id))

    @app.post("/plans/{plan_id}/next")
    def next_step(plan_id: str, request: Request):
        return idempotent(request, lambda: plan_call(plans.advance, plan_id))

    @app.post("/plans/{plan_id}/steps/{step_id}/confirm")
    def confirm(plan_id: str, step_id: int, request: Request):
        return idempotent(request, lambda: plan_call(plans.confirm, plan_id, step_id))

    @app.post("/plans/{plan_id}/steps/{step_id}/result")
    def result(plan_id: str, step_id: int, body: ResultIn, request: Request):
        return idempotent(request, lambda: plan_call(
            plans.report, plan_id, step_id, body.dispatch_id, body.outcome, body.error_class, body.verification,
            body.message))

    @app.post("/plans/{plan_id}/steps/{step_id}/verify")
    def verify(plan_id: str, step_id: int, body: VerifyIn, request: Request):
        return idempotent(request, lambda: plan_call(plans.verify, plan_id, step_id, body.happened))

    @app.post("/plans/{plan_id}/steps/{step_id}/retry")
    def retry(plan_id: str, step_id: int, request: Request):
        return idempotent(request, lambda: plan_call(plans.retry, plan_id, step_id))

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("JARVIS_PORT", "8000")))
