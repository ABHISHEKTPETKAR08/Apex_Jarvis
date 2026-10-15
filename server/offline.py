"""Rule-based intents used when AI_PROVIDER=offline. Produces the same {reply, steps} shape as the AI providers."""
import re
from datetime import datetime, timedelta


def _step(tool: str, **args) -> dict:
    return {"tool": tool, "args": [{"name": k, "value": str(v)} for k, v in args.items()]}


def _one(reply: str, tool: str, **args) -> dict:
    return {"reply": reply, "steps": [_step(tool, **args)]}


def offline_plan(text: str, is_available=lambda tool: True) -> dict:
    from tools import REGISTRY

    def has(name: str) -> bool:
        return name in REGISTRY and is_available(REGISTRY[name])

    t = text.lower().strip()
    raw = text.strip()

    # Outlook (Microsoft Graph) when connected.
    if re.search(r"\b(summari[sz]e|check|read)\b.*\b(e-?mails?|inbox|mail)\b", t):
        if has("email_summarize"):
            unread = "true" if "unread" in t else "false"
            return _one("Checking your inbox, sir.", "email_summarize", count=10, unread_only=unread)
        return {"reply": "Email isn't connected yet, sir. Connect Microsoft 365 in App Access.", "steps": []}
    if m := re.search(r"draft\s+(?:an\s+)?e-?mail\s+(?:to\s+)?(\S+@\S+)\s+(?:about|subject)\s+(.+?)(?:\s+saying\s+(.+))?$", raw, re.I):
        if has("email_draft"):
            return _one("I'll save that as a draft once you approve.", "email_draft", to=m.group(1).strip(" ."),
                        subject=m.group(2).strip(), body=(m.group(3) or m.group(2)).strip())

    if has("calendar_agenda") and re.search(r"\b(agenda|schedule|what'?s on my calendar|calendar (today|tomorrow))\b", t) \
            and not re.search(r"\b(add|create)\b", t):
        day = datetime.now() + timedelta(days=1) if "tomorrow" in t else datetime.now()
        return _one("Checking your calendar, sir.", "calendar_agenda", date=day.strftime("%Y-%m-%d"), days=1)
    if has("calendar_add_event") and (m := re.search(
            r"(?:add|create|schedule)\s+(?:an?\s+)?(?:event|meeting|appointment)\s+(.+?)\s+(?:on|at)\s+(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})", raw, re.I)):
        return _one("I'll add that to Outlook once you confirm.", "calendar_add_event",
                    title=m.group(1).strip(), start=re.sub(r"\s+", " ", m.group(2)))
    if has("calendar_update_event") and (m := re.search(r"\bmove\s+(E\d+)\s+to\s+(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})", raw, re.I)):
        return _one(f"I'll move {m.group(1).upper()} once you confirm.", "calendar_update_event",
                    event_ref=m.group(1).upper(), start=re.sub(r"\s+", " ", m.group(2)))
    if has("calendar_update_event") and (m := re.search(r"\brename\s+(E\d+)\s+to\s+(.+)", raw, re.I)):
        return _one(f"I'll rename {m.group(1).upper()} once you confirm.", "calendar_update_event",
                    event_ref=m.group(1).upper(), title=m.group(2).strip())

    # Capability tools (checked before the generic "open"/"call" patterns).
    if m := re.search(r"whatsapp\s+(?:message\s+)?(?:to\s+)?(.+?)\s+(?:saying|that says|:)\s+(.+)", raw, re.I):
        to, msg = m.group(1).strip(" ."), m.group(2).strip()
        if has("whatsapp_business_send") and re.fullmatch(r"\+?[1-9]\d{7,14}", to.replace(" ", "")):
            return _one(f"I'll send that from the WhatsApp Business number to {to} after you confirm.",
                        "whatsapp_business_send", to=to.replace(" ", ""), message=msg)
        return _one(f"I'll prepare a WhatsApp to {to}. Please confirm before I send it.", "send_whatsapp", to=to, message=msg)
    if m := re.search(r"e-?mail\s+(?:to\s+)?(\S+@\S+)\s+(?:about|subject)\s+(.+?)(?:\s+saying\s+(.+))?$", raw, re.I):
        to, subject, body = m.group(1).strip(" ."), m.group(2).strip(), (m.group(3) or m.group(2)).strip()
        if has("email_send"):
            return _one(f"I'll send that from Outlook to {to} after you confirm.", "email_send", to=to, subject=subject, body=body)
        return _one(f"I'll draft an email to {to}. Please confirm before I send it.", "send_email", to=to, subject=subject, body=body)
    if m := re.search(r"(?:add|create|schedule)\s+(?:an?\s+)?(?:event|meeting|appointment)\s+(.+?)\s+(?:on|at)\s+(\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2})", raw, re.I):
        return _one("I'll add that to your calendar once you confirm.", "create_calendar_event",
                    title=m.group(1).strip(), start=re.sub(r"\s+", " ", m.group(2)))
    if re.search(r"\b(take a (photo|picture|selfie)|open (the )?camera)\b", t):
        return _one("Opening the camera, sir.", "take_photo")
    if re.search(r"\b(open (my |the )?calendar|what'?s on my calendar)\b", t):
        return _one("Here's your calendar.", "open_calendar")
    if m := re.match(r"^share\s+(.+)", raw, re.I):
        return _one("Pick an app to share it with.", "share_text", text=m.group(1).strip())
    if m := re.match(r"^(?:say|announce)\s+(.+)", raw, re.I):
        return _one("", "speak", text=m.group(1).strip())

    # Existing phone actions.
    if m := re.search(r"\b(?:open|launch|start)\s+(.+)", t):
        target = m.group(1).strip(" .")
        if "." in target and " " not in target:
            return _one(f"Opening {target}.", "open_url", url=f"https://{target}")
        return _one(f"Opening {target}, sir.", "open_app", app=target)
    if m := re.search(r"\b(?:search(?: for)?|google|look up)\s+(.+)", t):
        q = m.group(1).strip(" .")
        return _one(f"Searching for {q}.", "web_search", query=q)
    if m := re.search(r"\bcall\s+(.+)", t):
        who = m.group(1).strip(" .")
        return _one(f"Calling {who}.", "dial", who=who)
    if m := re.search(r"timer (?:for )?(\d+)\s*(second|minute|hour)", t):
        n, unit = int(m.group(1)), m.group(2)
        secs = n * {"second": 1, "minute": 60, "hour": 3600}[unit]
        return _one(f"Timer set for {n} {unit}{'s' if n != 1 else ''}.", "set_timer", seconds=secs)
    if m := re.search(r"alarm (?:for |at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", t):
        h, mnt, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return _one(f"Alarm set for {h:02d}:{mnt:02d}.", "set_alarm", time=f"{h:02d}:{mnt:02d}")

    if "time" in t:
        return {"reply": f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}, sir.", "steps": []}
    if "date" in t or "day" in t:
        return {"reply": f"Today is {datetime.now().strftime('%A, %B %d')}.", "steps": []}
    if re.search(r"\b(hi|hello|hey)\b", t):
        return {"reply": "At your service, sir.", "steps": []}
    return {"reply": "I'm running in offline mode, sir. Set AI_PROVIDER in server/.env for full conversation.", "steps": []}
