"""Sanitized audit log. Events never contain secrets or message content; recipients are masked."""
import re
import uuid
from typing import Callable, Optional

from storage import Storage

CONTENT_ARGS = {"body", "message", "text", "subject", "title"}
RECIPIENT_ARGS = {"to", "who"}

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"
    r"|mongodb(?:\+srv)?://\S+"
    r"|(?i:api[_-]?key|token|secret|password|pwd)\s*[=:]\s*\S+"
    r"|[A-Za-z0-9+/_\-]{32,})"
)


def scrub(text, limit: int = 300) -> str:
    """Remove anything that looks like a credential and cap the length."""
    return _SECRET_RE.sub("[redacted]", str(text or ""))[:limit]


def mask_recipient(value: str) -> str:
    v = str(value).strip()
    if "@" in v:
        name, _, domain = v.partition("@")
        return f"{name[:1]}***@{domain}"
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 6:
        return "*" * (len(digits) - 2) + digits[-2:]
    return v[:1] + "***" if v else ""


def sanitize_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if k in CONTENT_ARGS:
            out[k] = f"[redacted {len(str(v))} chars]"
        elif k in RECIPIENT_ARGS:
            out[k] = mask_recipient(v)
        else:
            out[k] = scrub(v, 100)
    return out


class Auditor:
    def __init__(self, storage: Storage, clock: Callable[[], float]):
        self.storage = storage
        self.clock = clock

    def record(self, event: str, plan_id: Optional[str] = None, step_id: Optional[int] = None,
               args: Optional[dict] = None, **details) -> dict:
        """Append an event. Raises PersistenceError if it can't be stored (callers fail closed)."""
        doc = {
            "id": uuid.uuid4().hex,
            "t": self.clock(),
            "event": event,
            "plan_id": plan_id,
            "step_id": step_id,
        }
        if args is not None:
            doc["args"] = sanitize_args(args)
        for k, v in details.items():
            doc[k] = v if isinstance(v, (int, float, bool)) or v is None else scrub(v)
        self.storage.append_audit(doc)
        return doc
