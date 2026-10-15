"""WhatsApp Business Platform (Cloud API) — only with an authorized business configuration.

This sends from a WhatsApp *Business* phone number registered in Meta's developer platform. It cannot read
or send from the user's personal WhatsApp chats. Free-form text is only accepted by Meta for recipients who
messaged this business number in the last 24 hours (otherwise an approved template is required, which is
not implemented here). A returned message id means Meta accepted the message; delivery/read receipts arrive
only through a webhook, which is not set up, so delivery is never claimed.
"""
from typing import Mapping, Optional

import httpx

from graph import IntegrationError, http_call

GRAPH_FB = "https://graph.facebook.com"


def _clean(env: Mapping[str, str], key: str) -> str:
    v = (env.get(key) or "").strip()
    return "" if not v or v.startswith("<") else v


class WhatsAppBusiness:
    def __init__(self, env: Mapping[str, str], http: Optional[httpx.Client] = None):
        self.token = _clean(env, "WHATSAPP_ACCESS_TOKEN")
        self.phone_number_id = _clean(env, "WHATSAPP_PHONE_NUMBER_ID")
        self.version = _clean(env, "WHATSAPP_API_VERSION") or "v21.0"
        self.http = http or httpx.Client(timeout=20.0)

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_number_id)

    def status(self) -> dict:
        if not self.configured:
            missing = [k for k, v in (("WHATSAPP_ACCESS_TOKEN", self.token),
                                      ("WHATSAPP_PHONE_NUMBER_ID", self.phone_number_id)) if not v]
            return {"state": "NOT_CONFIGURED", "detail": f"Set {', '.join(missing)}; until then WhatsApp uses the "
                                                         f"phone's share/hand-off flow"}
        return {"state": "CONFIGURED", "api_version": self.version,
                "detail": "Business number only; free-form text to people who messaged it in the last 24h"}

    def send_text(self, to: str, text: str) -> str:
        """Returns Meta's message id (accepted, not necessarily delivered)."""
        if not self.configured:
            raise IntegrationError("NOT_CONFIGURED", "WhatsApp Business is not configured")
        digits = "".join(ch for ch in to if ch.isdigit())
        resp = http_call(self.http, "POST", f"{GRAPH_FB}/{self.version}/{self.phone_number_id}/messages",
                         headers={"Authorization": f"Bearer {self.token}"},
                         json={"messaging_product": "whatsapp", "recipient_type": "individual", "to": digits,
                               "type": "text", "text": {"preview_url": False, "body": text}})
        if resp.status_code < 400:
            try:
                return resp.json()["messages"][0]["id"]
            except (ValueError, KeyError, IndexError):
                raise IntegrationError("PROVIDER_ERROR", "Unexpected WhatsApp response", maybe_applied=True)
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code")
        if resp.status_code == 401 or code == 190:
            raise IntegrationError("AUTH_FAILED", "WhatsApp access token is invalid or expired")
        if code == 131047:
            raise IntegrationError("OUTSIDE_WINDOW", "This person hasn't messaged the business number in the last "
                                                     "24 hours, so WhatsApp only allows an approved template")
        if code in (131026, 131021):
            raise IntegrationError("UNDELIVERABLE", "WhatsApp can't deliver to this number")
        if resp.status_code == 429 or code in (4, 80007, 130429, 131048, 131056):
            raise IntegrationError("RATE_LIMITED", "WhatsApp rate limit; try again shortly", transient=True)
        if resp.status_code >= 500:
            raise IntegrationError("PROVIDER_ERROR", f"WhatsApp error {resp.status_code}", transient=True,
                                   maybe_applied=True)
        raise IntegrationError("PROVIDER_ERROR", f"WhatsApp rejected the message ({code or resp.status_code})")
