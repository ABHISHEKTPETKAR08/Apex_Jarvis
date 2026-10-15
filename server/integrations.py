"""Server-side executors for provider-backed tools (Microsoft Graph mail/calendar, WhatsApp Business).

Each executor returns a task-engine outcome: {"outcome", "error_class"?, "verification"?, "message"}.
Missing credentials surface as NOT_CONFIGURED; nothing is ever faked. Results are verified with the provider
(read-back) before "succeeded" is reported.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Mapping, Optional

import httpx

from audit import mask_recipient
from graph import GraphClient, IntegrationError
from secrets_store import SecretStore
from storage import Storage
from speech import AzureSpeech
from whatsapp import WhatsAppBusiness


@dataclass
class ExecContext:
    plan_id: str
    step_id: int
    dispatch_id: str
    session_id: str
    ref: dict  # provider references saved by earlier attempts of this step (e.g. draft id)
    save_ref: Callable[[dict], None]  # persists refs *before* the next side effect


def succeeded(verification: str, message: str) -> dict:
    return {"outcome": "succeeded", "verification": verification, "message": message}


def from_error(e: IntegrationError, mutation: bool) -> dict:
    """Map a provider error to an outcome. A mutation that may have been applied is never 'failed'."""
    if e.code == "NOT_CONFIGURED":
        return {"outcome": "failed", "error_class": "permanent", "message": f"NOT_CONFIGURED: {e}"}
    if mutation and e.maybe_applied:
        return {"outcome": "uncertain", "message": f"{e} — it may have gone through; check before retrying"}
    if e.transient:
        return {"outcome": "failed", "error_class": "transient", "message": str(e)}
    return {"outcome": "failed", "error_class": "permanent", "message": f"{e.code}: {e}"}


class Integrations:
    def __init__(self, env: Mapping[str, str], storage: Storage, http: Optional[httpx.Client] = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 start_threads: bool = True):
        self.secrets = SecretStore(storage, env)
        self.graph = GraphClient(env, self.secrets, http, clock, start_thread=start_threads)
        self.whatsapp = WhatsAppBusiness(env, http)
        self.speech = AzureSpeech(env, http)
        self.clock = clock
        self.sleep = sleep
        # Set by the server: (instruction, text) -> summary or None when no AI provider is configured.
        self.summarizer: Callable[[str, str], Optional[str]] = lambda instruction, text: None
        self._event_refs: dict[str, dict[str, str]] = {}  # session -> {"E1": graph event id}

    # ---- availability ----------------------------------------------------
    def available(self, requires: Optional[str]) -> bool:
        if requires is None:
            return True
        if requires == "graph":
            return self.graph.connected
        if requires == "whatsapp_business":
            return self.whatsapp.configured
        return False

    def unavailable_reason(self, requires: str) -> str:
        if requires == "graph":
            return self.graph.status().get("detail", "Microsoft account not connected")
        if requires == "whatsapp_business":
            return self.whatsapp.status()["detail"]
        return f"{requires} is not configured"

    def status(self) -> dict:
        return {"microsoft_graph": self.graph.status(), "whatsapp_business": self.whatsapp.status(),
                "azure_speech": self.speech.status()}

    # ---- execution -------------------------------------------------------
    def run(self, tool: str, args: dict, ctx: ExecContext) -> dict:
        """Plan step execution. Returns a task-engine outcome."""
        handler = getattr(self, f"_run_{tool}", None)
        if handler is None:
            return {"outcome": "failed", "error_class": "permanent", "message": f"No server executor for {tool}"}
        return handler(args, ctx)

    def read(self, tool: str, args: dict, session_id: str) -> dict:
        """Direct (non-plan) read. Returns {"text", "data"}; raises IntegrationError."""
        handler = getattr(self, f"_read_{tool}", None)
        if handler is None:
            raise IntegrationError("PROVIDER_ERROR", f"{tool} is not a read operation")
        return handler(args, session_id)

    # ---- email -----------------------------------------------------------
    def _read_email_summarize(self, args: dict, session_id: str) -> dict:
        count = int(args.get("count") or 10)
        unread_only = (args.get("unread_only") or "").lower() == "true"
        msgs = self.graph.list_messages(top=count, unread_only=unread_only)
        items = [{
            "from": (m.get("from") or {}).get("emailAddress", {}).get("name") or "Unknown",
            "subject": m.get("subject") or "(no subject)",
            "received": m.get("receivedDateTime"),
            "unread": not m.get("isRead", True),
        } for m in msgs]
        if not items:
            return {"text": "No unread email." if unread_only else "Your inbox is empty.", "data": {"messages": []},
                    "source": "provider"}
        listing = "\n".join(f"- {i['from']}: {i['subject']} — {(m.get('bodyPreview') or '')[:200]}"
                            for i, m in zip(items, msgs))
        summary = self.summarizer(
            "Summarize these emails for a voice assistant in at most 3 short spoken sentences. "
            "Mention who needs a reply. Don't invent anything.", listing)
        source = "ai"
        if not summary:
            source = "rule_based"
            unread = sum(i["unread"] for i in items)
            top = "; ".join(f"{i['from']} about {i['subject']}" for i in items[:3])
            summary = f"{len(items)} messages, {unread} unread. Latest: {top}."
        return {"text": summary, "data": {"messages": items}, "source": source}

    def _run_email_summarize(self, args: dict, ctx: ExecContext) -> dict:
        try:
            out = self._read_email_summarize(args, ctx.session_id)
        except IntegrationError as e:
            return from_error(e, mutation=False)
        return succeeded("graph_read", f"Summarized {len(out['data']['messages'])} messages ({out['source']})")

    def _run_email_draft(self, args: dict, ctx: ExecContext) -> dict:
        try:
            draft_id = ctx.ref.get("draft_id")
            if not draft_id:
                draft_id = self.graph.create_draft(args["to"], args["subject"], args["body"])
                ctx.save_ref({"draft_id": draft_id})
            msg = self.graph.get_message(draft_id)
        except IntegrationError as e:
            return from_error(e, mutation=True)
        if not msg.get("isDraft"):
            return {"outcome": "uncertain", "message": "That message is no longer a draft."}
        return succeeded("graph_draft_readback", f"Draft to {mask_recipient(args['to'])} saved in Outlook Drafts")

    def _run_email_send(self, args: dict, ctx: ExecContext) -> dict:
        """Draft-then-send with an immutable id: a retry first checks whether the draft was already sent."""
        draft_id = ctx.ref.get("draft_id")
        try:
            if not draft_id:
                draft_id = self.graph.create_draft(args["to"], args["subject"], args["body"])
                ctx.save_ref({"draft_id": draft_id})
            elif not self.graph.get_message(draft_id).get("isDraft"):
                return succeeded("graph_sent_items", "Already sent (confirmed in Sent Items)")
        except IntegrationError as e:
            if e.code == "NOT_FOUND":
                return {"outcome": "uncertain", "message": "The draft is gone; check Sent Items before retrying"}
            return from_error(e, mutation=False)  # nothing has been sent yet

        try:
            self.graph.send_draft(draft_id)
        except IntegrationError as e:
            return from_error(e, mutation=True)

        for delay in (0.0, 1.0, 2.0):
            self.sleep(delay)
            try:
                if not self.graph.get_message(draft_id).get("isDraft"):
                    return succeeded("graph_sent_items", f"Sent to {mask_recipient(args['to'])}; confirmed in Sent Items")
            except IntegrationError:
                continue
        return {"outcome": "uncertain", "message": "Outlook accepted the send but it hasn't left Drafts yet"}

    # ---- WhatsApp Business ------------------------------------------------
    def _run_whatsapp_business_send(self, args: dict, ctx: ExecContext) -> dict:
        if ctx.ref.get("wamid"):
            return succeeded("whatsapp_api_accepted", "Already accepted by WhatsApp earlier")
        try:
            wamid = self.whatsapp.send_text(args["to"], args["message"])
        except IntegrationError as e:
            return from_error(e, mutation=True)
        try:
            ctx.save_ref({"wamid": wamid})
        except Exception:
            pass  # the send happened; losing the id only affects later lookups
        return succeeded("whatsapp_api_accepted",
                         f"Accepted by WhatsApp for {mask_recipient(args['to'])} (delivery not confirmed)")

    # ---- calendar --------------------------------------------------------
    @staticmethod
    def _iso(day_time: datetime) -> str:
        return day_time.astimezone().isoformat(timespec="seconds")  # server-local offset

    @staticmethod
    def _graph_dt(start: str, minutes: int = 0) -> str:
        return (datetime.strptime(start, "%Y-%m-%d %H:%M") + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:00")

    def _read_calendar_agenda(self, args: dict, session_id: str) -> dict:
        first = datetime.strptime(args["date"], "%Y-%m-%d") if args.get("date") else \
            datetime.fromtimestamp(self.clock()).replace(hour=0, minute=0, second=0, microsecond=0)
        days = int(args.get("days") or 1)
        events = self.graph.calendar_view(self._iso(first), self._iso(first + timedelta(days=days)))
        refs = {}
        items = []
        for i, ev in enumerate(events[:50], start=1):
            ref = f"E{i}"
            refs[ref] = ev["id"]
            items.append({"ref": ref, "title": ev.get("subject") or "(no title)",
                          "start": (ev.get("start") or {}).get("dateTime", "")[:16].replace("T", " "),
                          "end": (ev.get("end") or {}).get("dateTime", "")[:16].replace("T", " "),
                          "all_day": bool(ev.get("isAllDay"))})
        self._event_refs[session_id] = refs
        if not items:
            return {"text": "Nothing on your calendar for that period.", "data": {"events": []}, "source": "provider"}
        text = "; ".join(f"{i['ref']}: {i['title']} at {i['start'][11:] or i['start']}" for i in items[:6])
        return {"text": f"{len(items)} event(s). {text}.", "data": {"events": items}, "source": "provider"}

    def _run_calendar_agenda(self, args: dict, ctx: ExecContext) -> dict:
        try:
            out = self._read_calendar_agenda(args, ctx.session_id)
        except IntegrationError as e:
            return from_error(e, mutation=False)
        return succeeded("graph_read", f"Read {len(out['data']['events'])} events")

    @staticmethod
    def _matches(ev: dict, title: Optional[str], start: Optional[str]) -> bool:
        if title is not None and ev.get("subject") != title:
            return False
        if start is not None and not (ev.get("start") or {}).get("dateTime", "").startswith(start[:16]):
            return False
        return True

    def _run_calendar_add_event(self, args: dict, ctx: ExecContext) -> dict:
        minutes = int(args.get("duration_minutes") or 60)
        start = self._graph_dt(args["start"])
        try:
            # transactionId makes Graph return the same event if this attempt is retried.
            ev = self.graph.create_event(args["title"], start, self._graph_dt(args["start"], minutes),
                                         transaction_id=f"jarvis-{ctx.plan_id}-{ctx.step_id}")
            ctx.save_ref({"event_id": ev["id"]})
            check = self.graph.get_event(ev["id"])
        except IntegrationError as e:
            # Safe to retry even if it was applied: the transactionId de-duplicates on Microsoft's side.
            return from_error(e, mutation=False)
        if not self._matches(check, args["title"], start):
            return {"outcome": "uncertain", "message": "The event exists but doesn't match what was requested"}
        return succeeded("graph_event_readback", f"Added \"{args['title']}\" at {args['start']} (verified in Outlook)")

    def _run_calendar_update_event(self, args: dict, ctx: ExecContext) -> dict:
        event_id = ctx.ref.get("event_id") or self._event_refs.get(ctx.session_id, {}).get(args["event_ref"])
        if not event_id:
            return {"outcome": "failed", "error_class": "permanent",
                    "message": f"I don't know which event {args['event_ref']} is any more; ask for your agenda again"}
        try:
            ctx.save_ref({"event_id": event_id})  # resolve the ref once; retries reuse the real id
            fields: dict = {}
            if "title" in args:
                fields["subject"] = args["title"]
            if "start" in args or "duration_minutes" in args:
                current = self.graph.get_event(event_id)
                start = self._graph_dt(args["start"]) if "start" in args else \
                    current["start"]["dateTime"][:16].replace("T", " ")
                start_txt = start[:16].replace("T", " ")
                if "duration_minutes" in args:
                    minutes = int(args["duration_minutes"])
                else:
                    fmt = "%Y-%m-%dT%H:%M"
                    minutes = int((datetime.strptime(current["end"]["dateTime"][:16], fmt) -
                                   datetime.strptime(current["start"]["dateTime"][:16], fmt)).total_seconds() // 60)
                fields["start"] = {"dateTime": self._graph_dt(start_txt), "timeZone": self.graph.timezone}
                fields["end"] = {"dateTime": self._graph_dt(start_txt, minutes), "timeZone": self.graph.timezone}
            self.graph.update_event(event_id, fields)  # PATCH: repeating it gives the same result
            check = self.graph.get_event(event_id)
        except IntegrationError as e:
            return from_error(e, mutation=False)
        want_start = fields.get("start", {}).get("dateTime")
        if not self._matches(check, fields.get("subject"), want_start):
            return {"outcome": "uncertain", "message": "Outlook didn't reflect the change yet"}
        return succeeded("graph_event_readback", f"Updated {args['event_ref']} (verified in Outlook)")
