"""Registry of the only tools JARVIS may run. Each tool maps validated arguments to a phone action.

The model never supplies code: it names a registered tool and string arguments, which are
validated here. Anything else is rejected before execution.
"""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from permissions import Capability


class ToolValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    description: str
    required: bool = True
    pattern: Optional[str] = None
    max_len: int = 500


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: tuple[Param, ...]
    to_action: Callable[[dict], dict]
    summarize: Callable[[dict], str]
    capability: Optional[Capability] = None
    scope: Optional[str] = None
    consequential: bool = False  # requires explicit final confirmation
    target_param: Optional[str] = None  # argument checked against grant targets
    # Safe to dispatch again if the first attempt's outcome is unknown (e.g. after a restart).
    # False for sends and mutations: those are never re-sent blindly; the user must verify first.
    idempotent: bool = True
    # The phone can't observe the real outcome (another app finishes it), so the user must verify.
    user_verifies: bool = False
    runs_on: str = "phone"  # "server": executed by the backend (provider APIs), never sent to the phone
    mutates: bool = False  # changes data at a provider: only via an approved plan, never as a direct action
    requires: Optional[str] = None  # integration that must be configured (e.g. "graph")


def _simple(action_type: str, param: str) -> Callable[[dict], dict]:
    return lambda a: {"type": action_type, "value": a[param], "params": {}}


TIME_HHMM = r"^([01]?\d|2[0-3]):[0-5]\d$"
DATETIME = r"^\d{4}-\d{2}-\d{2} ([01]?\d|2[0-3]):[0-5]\d$"
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

_TOOLS = [
    # Existing phone actions (no capability needed, unchanged behaviour).
    # App names: letters/digits and ordinary punctuation only, so corrupted model output is rejected up front.
    Tool("open_app", "Open an installed app by name.",
         (Param("app", "App name, e.g. YouTube", max_len=60, pattern=r"^[\w][\w .&+'’:!-]{0,59}$"),),
         _simple("open_app", "app"), lambda a: f"Open {a['app']}"),
    Tool("open_url", "Open a website.", (Param("url", "Full https URL"),),
         _simple("open_url", "url"), lambda a: f"Open {a['url']}"),
    Tool("web_search", "Search the web.", (Param("query", "Search query"),),
         _simple("web_search", "query"), lambda a: f"Search for {a['query']}"),
    Tool("dial", "Open the dialer for a number or contact.", (Param("who", "Phone number or contact name"),),
         _simple("dial", "who"), lambda a: f"Dial {a['who']}"),
    Tool("set_alarm", "Set an alarm.", (Param("time", "24h time HH:MM", pattern=TIME_HHMM),),
         _simple("set_alarm", "time"), lambda a: f"Set alarm for {a['time']}", idempotent=False),
    Tool("set_timer", "Set a countdown timer.", (Param("seconds", "Duration in seconds", pattern=r"^[1-9]\d{0,5}$"),),
         _simple("set_timer", "seconds"), lambda a: f"Set a {a['seconds']}s timer", idempotent=False),
    # Capability-gated tools.
    Tool(
        "send_email", "Prepare an email in the phone's mail app.",
        (Param("to", "Recipient email address", pattern=EMAIL), Param("subject", "Subject", max_len=200),
         Param("body", "Message body", max_len=4000)),
        lambda a: {"type": "compose_email", "value": a["to"], "params": dict(a)},
        lambda a: f"Email {a['to']} — \"{a['subject']}\": {a['body']}",
        Capability.EMAIL, "send", consequential=True, target_param="to", idempotent=False, user_verifies=True,
    ),
    Tool(
        "send_whatsapp", "Open WhatsApp on the phone with a pre-filled message; the user taps send there.",
        (Param("to", "Phone number with country code, or contact name", max_len=100),
         Param("message", "Message text", max_len=2000)),
        lambda a: {"type": "whatsapp_message", "value": a["to"], "params": dict(a)},
        lambda a: f"WhatsApp {a['to']}: {a['message']}",
        Capability.WHATSAPP, "send", consequential=True, target_param="to", idempotent=False, user_verifies=True,
    ),
    Tool("take_photo", "Open the camera to take a photo.", (),
         lambda a: {"type": "camera_capture", "value": "", "params": {}}, lambda a: "Open camera to take a photo",
         Capability.CAMERA, "capture"),
    Tool("listen", "Listen for the user's next voice command.", (),
         lambda a: {"type": "listen", "value": "", "params": {}}, lambda a: "Listen with the microphone",
         Capability.MICROPHONE, "listen"),
    Tool("speak", "Say a message aloud on the phone speaker.", (Param("text", "Text to speak", max_len=1000),),
         _simple("speak", "text"), lambda a: f"Say: {a['text']}", Capability.SPEAKER, "speak"),
    Tool("open_calendar", "Show the calendar, optionally at a date.",
         (Param("date", "Date YYYY-MM-DD", required=False, pattern=r"^\d{4}-\d{2}-\d{2}$"),),
         lambda a: {"type": "calendar_view", "value": a.get("date", ""), "params": dict(a)},
         lambda a: f"Open calendar{' on ' + a['date'] if a.get('date') else ''}",
         Capability.CALENDAR, "read"),
    Tool(
        "create_calendar_event", "Add an event to the calendar.",
        (Param("title", "Event title", max_len=200), Param("start", "Start 'YYYY-MM-DD HH:MM'", pattern=DATETIME),
         Param("duration_minutes", "Length in minutes", required=False, pattern=r"^[1-9]\d{0,3}$")),
        lambda a: {"type": "calendar_insert", "value": a["title"], "params": dict(a)},
        lambda a: f"Add event \"{a['title']}\" at {a['start']} ({a.get('duration_minutes', '60')} min)",
        Capability.CALENDAR, "write", consequential=True, idempotent=False, user_verifies=True,
    ),
    # Microsoft Graph (Outlook) — server-executed, verified with the provider.
    Tool("email_summarize", "Summarize recent Outlook email.",
         (Param("count", "How many recent messages, 1-25", required=False, pattern=r"^([1-9]|1\d|2[0-5])$"),
          Param("unread_only", "true or false", required=False, pattern=r"^(true|false)$")),
         lambda a: {}, lambda a: f"Summarize {a.get('count', '10')} recent emails",
         Capability.EMAIL, "read", runs_on="server", requires="graph"),
    Tool("email_draft", "Save an Outlook email draft (not sent).",
         (Param("to", "Recipient email address", pattern=EMAIL), Param("subject", "Subject", max_len=200),
          Param("body", "Message body", max_len=4000)),
         lambda a: {}, lambda a: f"Save draft to {a['to']} — \"{a['subject']}\": {a['body']}",
         Capability.EMAIL, "draft", target_param="to", idempotent=False, runs_on="server", mutates=True,
         requires="graph"),
    Tool("email_send", "Send an email from the user's Outlook account.",
         (Param("to", "Recipient email address", pattern=EMAIL), Param("subject", "Subject", max_len=200),
          Param("body", "Message body", max_len=4000)),
         lambda a: {}, lambda a: f"Send email to {a['to']} — \"{a['subject']}\": {a['body']}",
         Capability.EMAIL, "send", consequential=True, target_param="to", runs_on="server", mutates=True,
         requires="graph"),  # idempotent: a retry checks the saved draft before sending
    Tool("calendar_agenda", "Read upcoming Outlook calendar events (gives refs E1, E2, ... for updates).",
         (Param("date", "First day YYYY-MM-DD (default today)", required=False, pattern=r"^\d{4}-\d{2}-\d{2}$"),
          Param("days", "Number of days, 1-14", required=False, pattern=r"^([1-9]|1[0-4])$")),
         lambda a: {}, lambda a: f"Read calendar for {a.get('days', '1')} day(s) from {a.get('date', 'today')}",
         Capability.CALENDAR, "read", runs_on="server", requires="graph"),
    Tool("calendar_add_event", "Create an Outlook calendar event.",
         (Param("title", "Event title", max_len=200), Param("start", "Start 'YYYY-MM-DD HH:MM'", pattern=DATETIME),
          Param("duration_minutes", "Length in minutes", required=False, pattern=r"^[1-9]\d{0,3}$")),
         lambda a: {}, lambda a: f"Add Outlook event \"{a['title']}\" at {a['start']} ({a.get('duration_minutes', '60')} min)",
         Capability.CALENDAR, "write", consequential=True, runs_on="server", mutates=True, requires="graph"),
    Tool("calendar_update_event", "Change an Outlook event listed by calendar_agenda (use its ref, e.g. E2).",
         (Param("event_ref", "Event ref from the agenda, e.g. E2", pattern=r"^E([1-9]|[1-4]\d|50)$"),
          Param("title", "New title", required=False, max_len=200),
          Param("start", "New start 'YYYY-MM-DD HH:MM'", required=False, pattern=DATETIME),
          Param("duration_minutes", "New length in minutes", required=False, pattern=r"^[1-9]\d{0,3}$")),
         lambda a: {}, lambda a: "Update event {}: {}".format(
             a["event_ref"], ", ".join(f"{k}={v}" for k, v in a.items() if k != "event_ref")),
         Capability.CALENDAR, "write", consequential=True, runs_on="server", mutates=True, requires="graph"),
    Tool("whatsapp_business_send",
         "Send a WhatsApp text from the configured WhatsApp Business number (not the user's personal account). "
         "Only works for people who messaged the business in the last 24 hours.",
         (Param("to", "Phone number with country code, digits only", pattern=r"^\+?[1-9]\d{7,14}$"),
          Param("message", "Message text", max_len=4096)),
         lambda a: {}, lambda a: f"WhatsApp Business message to {a['to']}: {a['message']}",
         Capability.WHATSAPP, "send", consequential=True, target_param="to", idempotent=False, runs_on="server",
         mutates=True, requires="whatsapp_business"),
    Tool("share_text", "Open the Android share sheet with text; the user picks the app and sends it.",
         (Param("text", "Text to share", max_len=4000),),
         _simple("share_text", "text"), lambda a: f"Share: {a['text']}"),
]

REGISTRY: dict[str, Tool] = {t.name: t for t in _TOOLS}


def get_tool(name: str) -> Tool:
    tool = REGISTRY.get(str(name))
    if tool is None:
        raise ToolValidationError(f"Unknown tool '{name}'. Only registered tools can run.")
    return tool


def validate_step(tool_name: str, args) -> tuple[Tool, dict]:
    """Validate a model-proposed step. Accepts args as a dict or a [{name, value}] list."""
    tool = get_tool(tool_name)
    if isinstance(args, list):
        pairs = {}
        for item in args:
            if not isinstance(item, dict) or "name" not in item or "value" not in item:
                raise ToolValidationError(f"{tool.name}: malformed argument {item!r}")
            pairs[str(item["name"])] = item["value"]
        args = pairs
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolValidationError(f"{tool.name}: arguments must be an object")
    # The structured-output schema sends every parameter field; unused ones arrive as null/empty.
    args = {k: v for k, v in args.items() if v is not None and not (isinstance(v, str) and not v.strip())}

    known = {p.name: p for p in tool.params}
    unexpected = set(args) - set(known)
    if unexpected:
        raise ToolValidationError(f"{tool.name}: unexpected argument(s) {sorted(unexpected)}")
    clean: dict[str, str] = {}
    for p in tool.params:
        raw = args.get(p.name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if p.required:
                raise ToolValidationError(f"{tool.name}: missing required argument '{p.name}'")
            continue
        if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            raise ToolValidationError(f"{tool.name}: '{p.name}' must be a string")
        value = str(raw).strip()
        if len(value) > p.max_len:
            raise ToolValidationError(f"{tool.name}: '{p.name}' is longer than {p.max_len} characters")
        if p.pattern and not re.match(p.pattern, value):
            raise ToolValidationError(f"{tool.name}: '{p.name}' has an invalid format ({p.description})")
        clean[p.name] = value
    if "start" in clean and tool.name in ("create_calendar_event", "calendar_add_event", "calendar_update_event"):
        try:
            datetime.strptime(clean["start"], "%Y-%m-%d %H:%M")
        except ValueError:
            raise ToolValidationError(f"{tool.name}: 'start' is not a real date/time")
    if tool.name == "calendar_update_event" and len(clean) < 2:
        raise ToolValidationError("calendar_update_event: give at least one of title, start, duration_minutes")
    return tool, clean


def describe_tools(is_available: Callable[[Tool], bool] = lambda t: True) -> str:
    """Tool list for the system prompt (only tools whose integration is configured)."""
    lines = []
    for t in _TOOLS:
        if not is_available(t):
            continue
        params = ", ".join(f"{p.name}{'' if p.required else '?'}: {p.description}" for p in t.params) or "no arguments"
        gate = f" [needs {t.capability.value}:{t.scope} permission]" if t.capability else ""
        conf = " [user confirms before it runs]" if t.consequential else ""
        lines.append(f"- {t.name}({params}): {t.description}{gate}{conf}")
    return "\n".join(lines)
