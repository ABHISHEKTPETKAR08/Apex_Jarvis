"""In-memory fake of Microsoft identity + Graph endpoints for tests (httpx.MockTransport)."""
import json

import httpx


class FakeMicrosoft:
    def __init__(self):
        self.pending_polls = 1
        self.token_n = 0
        self.refresh_revoked = False
        self.messages: dict[str, dict] = {}
        self.inbox = [
            {"subject": "Project review", "from": {"emailAddress": {"name": "Priya"}}, "isRead": False,
             "receivedDateTime": "2026-10-15T09:00:00Z", "bodyPreview": "Can we move the review to Friday?"},
            {"subject": "Invoice", "from": {"emailAddress": {"name": "Acme"}}, "isRead": True,
             "receivedDateTime": "2026-10-14T09:00:00Z", "bodyPreview": "Attached is your invoice"},
        ]
        self.sends = 0
        self.send_mode = "ok"  # ok | 429 | timeout_after_apply | stay_draft
        self.events: dict[str, dict] = {}
        self.event_creates = 0
        self.calls: list[str] = []
        self.wa_sent: list[dict] = []
        self.wa_mode = "ok"

    @property
    def token(self) -> str:
        return f"at{self.token_n}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    # ------------------------------------------------------------------
    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(f"{req.method} {req.url.host}{req.url.path}")
        host, path = req.url.host, req.url.path
        if host == "login.microsoftonline.com":
            return self._login(req, path)
        if host == "graph.microsoft.com":
            if req.headers.get("Authorization") != f"Bearer {self.token}":
                return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken"}})
            return self._graph(req, path.removeprefix("/v1.0"))
        if host == "graph.facebook.com":
            return self._whatsapp(req, path)
        if host.endswith(".speech.microsoft.com"):
            return self._speech(req, host)
        return httpx.Response(404)

    def _login(self, req, path):
        form = dict(httpx.QueryParams(req.content.decode()))
        if path.endswith("/devicecode"):
            assert "Mail.Send" in form["scope"] and "Calendars.ReadWrite" in form["scope"]
            return httpx.Response(200, json={"device_code": "dev-code", "user_code": "ABCD-1234",
                                             "verification_uri": "https://microsoft.com/devicelogin",
                                             "interval": 1, "expires_in": 900, "message": "Go sign in"})
        if form["grant_type"].endswith("device_code"):
            if self.pending_polls > 0:
                self.pending_polls -= 1
                return httpx.Response(400, json={"error": "authorization_pending"})
            return self._tokens()
        if form["grant_type"] == "refresh_token":
            if self.refresh_revoked:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return self._tokens()
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    def _tokens(self):
        self.token_n += 1
        return httpx.Response(200, json={"access_token": self.token, "refresh_token": f"rt{self.token_n}",
                                         "expires_in": 3600})

    def _graph(self, req, path):
        if path == "/me":
            return httpx.Response(200, json={"mail": "abhishek@example.com"})
        if path == "/me/mailFolders/inbox/messages":
            items = self.inbox
            if req.url.params.get("$filter") == "isRead eq false":
                items = [m for m in items if not m["isRead"]]
            return httpx.Response(200, json={"value": items[: int(req.url.params.get("$top", 10))]})
        if path == "/me/messages" and req.method == "POST":
            mid = f"draft{len(self.messages) + 1}"
            body = json.loads(req.content)
            self.messages[mid] = {"id": mid, "isDraft": True, "subject": body["subject"]}
            return httpx.Response(201, json=self.messages[mid])
        if path.startswith("/me/messages/"):
            parts = path.split("/")
            mid = parts[3]
            if mid not in self.messages:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            if len(parts) == 5 and parts[4] == "send":
                if self.send_mode == "429":
                    return httpx.Response(429, json={"error": {"code": "TooManyRequests"}})
                self.sends += 1
                if self.send_mode != "stay_draft":
                    self.messages[mid]["isDraft"] = False
                if self.send_mode == "timeout_after_apply":
                    raise httpx.ReadTimeout("timed out", request=req)
                return httpx.Response(202)
            return httpx.Response(200, json=self.messages[mid])
        if path == "/me/calendarView":
            return httpx.Response(200, json={"value": sorted(self.events.values(), key=lambda e: e["start"]["dateTime"])})
        if path == "/me/events" and req.method == "POST":
            body = json.loads(req.content)
            for ev in self.events.values():  # Graph de-duplicates on transactionId
                if ev.get("transactionId") == body.get("transactionId"):
                    return httpx.Response(201, json=ev)
            self.event_creates += 1
            eid = f"event{len(self.events) + 1}"
            self.events[eid] = {"id": eid, **body}
            return httpx.Response(201, json=self.events[eid])
        if path.startswith("/me/events/"):
            eid = path.split("/")[3]
            if eid not in self.events:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}})
            if req.method == "PATCH":
                self.events[eid].update(json.loads(req.content))
            return httpx.Response(200, json=self.events[eid])
        return httpx.Response(404, json={"error": {"code": "NotFound"}})

    def _whatsapp(self, req, path):
        if req.headers.get("Authorization") != "Bearer wa-token":
            return httpx.Response(401, json={"error": {"code": 190, "message": "Invalid OAuth access token"}})
        body = json.loads(req.content)
        if self.wa_mode == "outside_window":
            return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
        if self.wa_mode == "timeout":
            self.wa_sent.append(body)
            raise httpx.ReadTimeout("timed out", request=req)
        self.wa_sent.append(body)
        return httpx.Response(200, json={"messaging_product": "whatsapp",
                                         "messages": [{"id": f"wamid.{len(self.wa_sent)}"}]})

    def _speech(self, req, host):
        if req.headers.get("Ocp-Apim-Subscription-Key") != "speech-key":
            return httpx.Response(401)
        if host.endswith(".tts.speech.microsoft.com"):
            self.last_ssml = req.content.decode()
            return httpx.Response(200, content=b"ID3fake-mp3", headers={"content-type": "audio/mpeg"})
        self.last_stt_language = req.url.params.get("language")
        return httpx.Response(200, json={"RecognitionStatus": "Success", "DisplayText": "Open YouTube."})
