"""Microsoft Graph (Outlook mail + calendar) over HTTPS with delegated OAuth (device-code flow).

Scopes requested: offline_access User.Read Mail.ReadWrite Mail.Send Calendars.ReadWrite — the minimum for
read/draft/send mail and read/create/update events. Tokens live in SecretStore, never in responses or logs.
"""
import logging
import threading
import time
from typing import Callable, Mapping, Optional

import httpx

from secrets_store import SecretStore
from storage import PersistenceError

AUTHORITY = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Mail.ReadWrite Mail.Send Calendars.ReadWrite"
IMMUTABLE = {"Prefer": 'IdType="ImmutableId"'}  # ids survive moving Drafts -> Sent Items
SECRET_NAME = "ms_graph"
log = logging.getLogger("jarvis.graph")


class IntegrationError(Exception):
    """code: NOT_CONFIGURED | AUTH_FAILED | NOT_FOUND | RATE_LIMITED | PROVIDER_ERROR | NETWORK.
    transient: retrying later may work. maybe_applied: the provider may have performed the request."""

    def __init__(self, code: str, message: str, transient: bool = False, maybe_applied: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient
        self.maybe_applied = maybe_applied


def _clean(env: Mapping[str, str], key: str) -> str:
    v = (env.get(key) or "").strip()
    return "" if not v or v.startswith("<") else v


def mask_email(addr: str) -> str:
    name, _, domain = (addr or "").partition("@")
    return f"{name[:1]}***@{domain}" if domain else ""


def http_call(http: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    """Network errors classified by whether the request could have reached the provider."""
    try:
        return http.request(method, url, **kw)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise IntegrationError("NETWORK", f"Can't reach {httpx.URL(url).host}", transient=True) from e
    except httpx.TimeoutException as e:
        raise IntegrationError("NETWORK", "Request timed out after sending", transient=True, maybe_applied=True) from e
    except httpx.HTTPError as e:
        raise IntegrationError("NETWORK", f"Network error: {type(e).__name__}", transient=True,
                               maybe_applied=True) from e


def raise_for(resp: httpx.Response, what: str):
    if resp.status_code < 400:
        return
    code = ""
    try:
        code = resp.json().get("error", {}).get("code", "")
    except ValueError:
        pass
    s = resp.status_code
    if s in (401, 403):
        raise IntegrationError("AUTH_FAILED", f"{what}: not authorized ({code or s}); reconnect or check app permissions")
    if s == 404:
        raise IntegrationError("NOT_FOUND", f"{what}: not found")
    if s in (429, 503, 504):  # throttled/unavailable: request was not processed
        raise IntegrationError("RATE_LIMITED", f"{what}: provider busy ({s}); try again shortly", transient=True)
    if s >= 500:
        raise IntegrationError("PROVIDER_ERROR", f"{what}: provider error {s}", transient=True, maybe_applied=True)
    raise IntegrationError("PROVIDER_ERROR", f"{what}: rejected ({code or s})")


class GraphClient:
    def __init__(self, env: Mapping[str, str], secrets: SecretStore, http: Optional[httpx.Client] = None,
                 clock: Callable[[], float] = time.time, start_thread: bool = True):
        self.client_id = _clean(env, "MS_GRAPH_CLIENT_ID")
        self.tenant = _clean(env, "MS_GRAPH_TENANT") or "common"
        self.timezone = _clean(env, "JARVIS_TIMEZONE") or "India Standard Time"
        self.secrets = secrets
        self.http = http or httpx.Client(timeout=20.0)
        self.clock = clock
        self.start_thread = start_thread
        self._flow: Optional[dict] = None
        self._flow_error: Optional[str] = None
        self._flow_seq = 0  # each Connect gets its own poller; older pollers stop
        self._lock = threading.Lock()

    # ---- state --------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    def _tokens(self) -> Optional[dict]:
        try:
            return self.secrets.get(SECRET_NAME)
        except PersistenceError:
            return None

    @property
    def connected(self) -> bool:
        return self.configured and bool((self._tokens() or {}).get("refresh_token"))

    def status(self) -> dict:
        if not self.configured:
            return {"state": "NOT_CONFIGURED", "detail": "Set MS_GRAPH_CLIENT_ID in server/.env"}
        tokens = self._tokens()
        if tokens and tokens.get("refresh_token"):
            return {"state": "CONNECTED", "account": mask_email(tokens.get("account", "")), "scopes": SCOPES,
                    "tokens_encrypted": self.secrets.encrypted}
        if self._flow and self.clock() < self._flow["expires_at"]:
            return {"state": "CONNECTING", "user_code": self._flow["user_code"],
                    "verification_uri": self._flow["verification_uri"],
                    "expires_in": int(self._flow["expires_at"] - self.clock()),
                    "tenant": self.tenant,
                    "detail": "Waiting for you to finish signing in. Only accounts this app registration allows "
                              "can sign in (see README)."}
        out = {"state": "NOT_CONNECTED", "detail": "Microsoft account not connected"}
        if self._flow_error:
            out["last_error"] = self._flow_error
        return out

    # ---- device-code sign-in -------------------------------------------------
    def start_device_flow(self) -> dict:
        if not self.configured:
            raise IntegrationError("NOT_CONFIGURED", "MS_GRAPH_CLIENT_ID is not set")
        resp = http_call(self.http, "POST", f"{AUTHORITY}/{self.tenant}/oauth2/v2.0/devicecode",
                         data={"client_id": self.client_id, "scope": SCOPES})
        if resp.status_code >= 400:
            err = resp.json().get("error_description", "") if resp.headers.get("content-type", "").startswith(
                "application/json") else ""
            raise IntegrationError("AUTH_FAILED", f"Sign-in could not start: {err[:200] or resp.status_code}")
        d = resp.json()
        with self._lock:
            self._flow_seq += 1
            seq = self._flow_seq
            self._flow = {"device_code": d["device_code"], "user_code": d["user_code"],
                          "verification_uri": d["verification_uri"], "interval": int(d.get("interval", 5)),
                          "expires_at": self.clock() + int(d.get("expires_in", 900)), "seq": seq}
            self._flow_error = None
        log.info("Microsoft sign-in started (tenant=%s); waiting for the user", self.tenant)
        if self.start_thread:
            threading.Thread(target=self._poll_loop, args=(seq,), daemon=True).start()
        return {"user_code": d["user_code"], "verification_uri": d["verification_uri"],
                "expires_in": int(d.get("expires_in", 900)), "message": d.get("message", "")}

    def _poll_loop(self, seq: int):
        while True:
            flow = self._flow
            if not flow or flow.get("seq") != seq:
                return  # finished, cancelled, or replaced by a newer Connect
            time.sleep(flow["interval"])
            try:
                if self.poll_device_flow() != "pending":
                    return
            except Exception as e:  # never leave the UI stuck on CONNECTING
                self._end_flow(f"Sign-in polling failed ({type(e).__name__})")
                log.warning("Microsoft sign-in polling failed: %s", type(e).__name__)
                return

    def poll_device_flow(self) -> str:
        """One poll. Returns pending | connected | failed."""
        flow = self._flow
        if not flow:
            return "failed"
        if self.clock() >= flow["expires_at"]:
            self._end_flow("Sign-in code expired")
            return "failed"
        try:
            resp = http_call(self.http, "POST", f"{AUTHORITY}/{self.tenant}/oauth2/v2.0/token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id, "device_code": flow["device_code"]})
        except IntegrationError:
            return "pending"
        body = resp.json()
        if resp.status_code >= 400:
            err = body.get("error", "")
            if err in ("authorization_pending", "slow_down"):
                if err == "slow_down":
                    flow["interval"] += 5
                return "pending"
            # Microsoft's error code (AADSTSxxxx) is safe to show and is what diagnoses tenant/consent problems.
            desc = str(body.get("error_description", ""))
            aadsts = desc.split(":")[0] if desc.startswith("AADSTS") else ""
            reason = {"authorization_declined": "Sign-in was declined",
                      "expired_token": "Sign-in code expired before sign-in was completed"}.get(
                err, f"Sign-in failed ({err}{' ' + aadsts if aadsts else ''})")
            self._end_flow(reason)
            log.warning("Microsoft sign-in ended: %s", reason)
            return "failed"
        try:
            self._save_tokens(body)
        except PersistenceError as e:
            self._end_flow(f"Signed in, but the tokens couldn't be stored ({e}); nothing was saved")
            return "failed"
        try:
            me = self._get("/me", "Account lookup", params={"$select": "mail,userPrincipalName"})
            tokens = self._tokens() or {}
            tokens["account"] = me.get("mail") or me.get("userPrincipalName") or ""
            self.secrets.put(SECRET_NAME, tokens)
        except (IntegrationError, PersistenceError):
            pass
        with self._lock:
            self._flow = None
        log.info("Microsoft sign-in completed")
        return "connected"

    def _end_flow(self, error: str):
        with self._lock:
            self._flow = None
            self._flow_error = error

    def disconnect(self):
        with self._lock:
            self._flow = None
        self.secrets.delete(SECRET_NAME)

    def _save_tokens(self, body: dict):
        old = self._tokens() or {}
        self.secrets.put(SECRET_NAME, {
            "access_token": body["access_token"],
            "refresh_token": body.get("refresh_token") or old.get("refresh_token"),
            "expires_at": self.clock() + int(body.get("expires_in", 3600)),
            "account": old.get("account", ""),
        })

    def _access_token(self, force_refresh: bool = False) -> str:
        if not self.configured:
            raise IntegrationError("NOT_CONFIGURED", "MS_GRAPH_CLIENT_ID is not set")
        tokens = self._tokens()
        if not tokens or not tokens.get("refresh_token"):
            raise IntegrationError("NOT_CONFIGURED", "Microsoft account not connected (App Access > Connect)")
        if not force_refresh and tokens.get("access_token") and self.clock() < tokens.get("expires_at", 0) - 60:
            return tokens["access_token"]
        resp = http_call(self.http, "POST", f"{AUTHORITY}/{self.tenant}/oauth2/v2.0/token", data={
            "grant_type": "refresh_token", "client_id": self.client_id,
            "refresh_token": tokens["refresh_token"], "scope": SCOPES})
        if resp.status_code >= 400:
            if resp.status_code in (400, 401):  # invalid_grant: consent revoked or token expired
                self.secrets.delete(SECRET_NAME)
                raise IntegrationError("NOT_CONFIGURED", "Microsoft sign-in expired; connect again")
            raise_for(resp, "Token refresh")
        self._save_tokens(resp.json())
        return self._tokens()["access_token"]

    # ---- REST ---------------------------------------------------------------
    def _call(self, method: str, path: str, what: str, **kw) -> httpx.Response:
        headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {self._access_token()}"}
        resp = http_call(self.http, method, GRAPH + path, headers=headers, **kw)
        if resp.status_code == 401:  # access token rejected before processing: refresh once and retry
            headers["Authorization"] = f"Bearer {self._access_token(force_refresh=True)}"
            resp = http_call(self.http, method, GRAPH + path, headers=headers, **kw)
        raise_for(resp, what)
        return resp

    def _get(self, path: str, what: str, **kw) -> dict:
        return self._call("GET", path, what, **kw).json()

    # Mail
    def list_messages(self, top: int = 10, unread_only: bool = False) -> list[dict]:
        params = {"$top": str(top), "$select": "subject,from,receivedDateTime,bodyPreview,isRead"}
        if unread_only:
            params["$filter"] = "isRead eq false"
        else:
            params["$orderby"] = "receivedDateTime desc"
        return self._get("/me/mailFolders/inbox/messages", "Reading mail", params=params).get("value", [])

    def create_draft(self, to: str, subject: str, body: str) -> str:
        msg = {"subject": subject, "body": {"contentType": "Text", "content": body},
               "toRecipients": [{"emailAddress": {"address": to}}]}
        return self._call("POST", "/me/messages", "Creating draft", json=msg, headers=IMMUTABLE).json()["id"]

    def get_message(self, message_id: str) -> dict:
        return self._get(f"/me/messages/{message_id}", "Checking message",
                         params={"$select": "isDraft,subject"}, headers=IMMUTABLE)

    def send_draft(self, message_id: str) -> None:
        self._call("POST", f"/me/messages/{message_id}/send", "Sending", headers=IMMUTABLE)

    # Calendar
    def calendar_view(self, start_iso: str, end_iso: str, top: int = 20) -> list[dict]:
        return self._get("/me/calendarView", "Reading calendar", params={
            "startDateTime": start_iso, "endDateTime": end_iso, "$top": str(top), "$orderby": "start/dateTime",
            "$select": "subject,start,end,location,isAllDay"},
            headers={"Prefer": f'outlook.timezone="{self.timezone}"'}).get("value", [])

    def create_event(self, subject: str, start: str, end: str, transaction_id: str) -> dict:
        body = {"subject": subject, "start": {"dateTime": start, "timeZone": self.timezone},
                "end": {"dateTime": end, "timeZone": self.timezone}, "transactionId": transaction_id}
        return self._call("POST", "/me/events", "Creating event", json=body, headers={
            **IMMUTABLE, "Prefer": f'IdType="ImmutableId", outlook.timezone="{self.timezone}"'}).json()

    def get_event(self, event_id: str) -> dict:
        return self._get(f"/me/events/{event_id}", "Checking event", params={"$select": "subject,start,end"},
                         headers={"Prefer": f'IdType="ImmutableId", outlook.timezone="{self.timezone}"'})

    def update_event(self, event_id: str, fields: dict) -> dict:
        return self._call("PATCH", f"/me/events/{event_id}", "Updating event", json=fields, headers={
            "Prefer": f'IdType="ImmutableId", outlook.timezone="{self.timezone}"'}).json()
