"""AI providers. Selected explicitly with AI_PROVIDER; a misconfigured provider reports an error
instead of silently falling back to another one.

Every provider returns the same shape: {"reply": str, "steps": [{"tool": str, "args": [{"name", "value"}]}]}.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from tools import REGISTRY, describe_tools

PROVIDERS = ("anthropic", "azure_openai", "offline")
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
MAX_TURNS = 20

def build_system(is_available: Callable = lambda tool: True) -> str:
    return _SYSTEM_TEMPLATE.replace("{TOOLS}", describe_tools(is_available))


_SYSTEM_TEMPLATE = """You are JARVIS, a concise, witty voice assistant running on the user's Android phone.
Your reply is spoken aloud by text-to-speech: keep it to 1-3 short sentences, no markdown, no lists, no emojis.
Address the user as "sir" occasionally, in the style of Tony Stark's JARVIS.

To act on the phone, add steps. You may ONLY use these tools, with string arguments:
{TOOLS}

Rules:
- Use no steps for plain conversation.
- Use several steps only when the request genuinely needs them, in execution order.
- Never invent tools, and never put code or shell commands in arguments.
- To message someone ("WhatsApp X saying Y", "send a message to X on WhatsApp") use send_whatsapp (or
  whatsapp_business_send when listed) with "to" and "message" — never just open_app. If the message text is missing,
  ask for it in your reply and use no steps. Example: "WhatsApp Mom saying on my way" ->
  send_whatsapp with to "Mom" and message "on my way".
- For "open/launch/start <app>" always use open_app with the app's name, even if you're unsure it's installed:
  the phone checks its installed apps and reports truthfully. Never substitute a web search or download page.
- Put each argument in the field with its exact parameter name (e.g. open_app -> "app": "YouTube") and set every
  field the tool doesn't use to null.
- For anything marked "user confirms", say in your reply what you will do; the user approves it on the phone.
- Do not claim a message was sent or an event created; results are reported after verification.
- Prefer email_send / calendar tools / whatsapp_business_send when listed; otherwise use the phone hand-off tools."""

SYSTEM = build_system()

ALL_PARAMS = sorted({p.name for t in REGISTRY.values() for p in t.params})


def _step_schema(tool) -> dict:
    """Strict per-tool step: optional parameters are nullable, but every field name is the tool's own."""
    props = {p.name: ({"type": "string", "description": p.description} if p.required else
                      {"anyOf": [{"type": "string"}, {"type": "null"}], "description": p.description})
             for p in tool.params}
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": [tool.name]},
            "args": {"type": "object", "properties": props, "required": [p.name for p in tool.params],
                     "additionalProperties": False},
        },
        "required": ["tool", "args"],
        "additionalProperties": False,
    }

SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "steps": {
            "type": "array",
            # One schema per tool: a step can only carry that tool's own parameter names.
            "items": {"anyOf": [_step_schema(t) for t in sorted(REGISTRY.values(), key=lambda t: t.name)]},
        },
    },
    "required": ["reply", "steps"],
    "additionalProperties": False,
}


class ConfigError(ValueError):
    pass


class ProviderError(RuntimeError):
    """Provider call failed. Message is safe to show (never contains secrets).

    transient=True means retrying later may work (rate limit, timeout, 5xx, network). The SDKs have already
    retried those with bounded exponential backoff (max_retries=2) before this is raised."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _is_transient(e: Exception, sdk) -> bool:
    if isinstance(e, (sdk.RateLimitError, sdk.APIConnectionError, sdk.InternalServerError)):
        return True  # APITimeoutError subclasses APIConnectionError
    return getattr(e, "status_code", None) in (408, 409, 429) or (getattr(e, "status_code", 0) or 0) >= 500


def _clean(env: Mapping[str, str], key: str) -> str:
    """Env value with placeholders treated as unset."""
    v = (env.get(key) or "").strip().strip('"').strip("'")
    if not v or (v.startswith("<") and v.endswith(">")) or v.lower().startswith(("your-", "your_", "changeme")):
        return ""
    return v


def _parse_json_reply(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"reply": str(raw or ""), "steps": []}
    if not isinstance(data, dict):
        return {"reply": str(raw), "steps": []}
    steps = data.get("steps")
    return {"reply": str(data.get("reply", "")), "steps": steps if isinstance(steps, list) else []}


def _stamp(text: str) -> str:
    return f"[local time: {datetime.now().strftime('%A %d %B %Y, %H:%M')}]\n{text}"


# --------------------------------------------------------------------------- Azure

@dataclass(frozen=True)
class AzureConfig:
    protocol: str  # "azure_deployments" (AzureOpenAI client) or "openai_v1" (OpenAI client, base_url)
    base_url: str
    api_key: str
    deployment: str
    api_version: str  # empty for openai_v1

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc


def classify_azure_endpoint(endpoint: str) -> dict:
    """Work out which API an Azure/Foundry endpoint speaks.

    - https://<res>.openai.azure.com/ (or .cognitiveservices.azure.com / .services.ai.azure.com root)
        -> Azure OpenAI deployments API: AzureOpenAI client, needs api-version.
    - .../openai/v1/  -> OpenAI-compatible v1 API: OpenAI client with base_url, no api-version.
    - A deployment Target URI (.../openai/deployments/<name>/chat/completions?api-version=...)
        -> deployments API; deployment and api-version are read from it.
    - .../models (Azure AI model inference) and .../api/projects/<p> (Foundry project) are rejected:
        they need different SDKs that this server does not use.
    """
    u = urlparse(endpoint.strip())
    if u.scheme != "https" or not u.netloc:
        raise ConfigError("AZURE_OPENAI_ENDPOINT must be an https:// URL")
    host = u.netloc.lower()
    path = re.sub(r"/+", "/", u.path or "/")
    root = f"https://{u.netloc}"
    if "/api/projects/" in path:
        raise ConfigError(
            "AZURE_OPENAI_ENDPOINT is a Foundry *project* endpoint (/api/projects/...). Use the resource's "
            "Azure OpenAI endpoint instead: https://<resource>.openai.azure.com/ (Foundry: project Overview > "
            "'Azure OpenAI' endpoint, or the deployment's Target URI)."
        )
    if path.rstrip("/").endswith("/models") or path.startswith("/models"):
        raise ConfigError(
            "AZURE_OPENAI_ENDPOINT is an Azure AI model-inference endpoint (/models), which needs the "
            "azure-ai-inference SDK. Use the OpenAI-compatible endpoint https://<resource>.services.ai.azure.com/openai/v1/ "
            "or https://<resource>.openai.azure.com/ instead."
        )
    m = re.match(r"^/openai/deployments/([^/]+)", path)
    if m:
        version = parse_qs(u.query).get("api-version", [""])[0]
        return {"protocol": "azure_deployments", "base_url": root + "/", "deployment": m.group(1), "api_version": version}
    if re.match(r"^/openai/v1/?", path):
        return {"protocol": "openai_v1", "base_url": root + "/openai/v1/", "deployment": "", "api_version": ""}
    if path in ("", "/"):
        if not host.endswith((".openai.azure.com", ".cognitiveservices.azure.com", ".services.ai.azure.com")):
            raise ConfigError(
                "AZURE_OPENAI_ENDPOINT host is not an Azure OpenAI/Foundry host. For a custom domain or gateway, "
                "use its full OpenAI-compatible path ending in /openai/v1/."
            )
        return {"protocol": "azure_deployments", "base_url": root + "/", "deployment": "", "api_version": ""}
    raise ConfigError(f"Unrecognised AZURE_OPENAI_ENDPOINT path '{path}'. Expected '/', '/openai/v1/' or a deployment Target URI.")


def load_azure_config(env: Mapping[str, str]) -> AzureConfig:
    endpoint = _clean(env, "AZURE_OPENAI_ENDPOINT")
    key = _clean(env, "AZURE_OPENAI_API_KEY")
    deployment = _clean(env, "AZURE_OPENAI_DEPLOYMENT_NAME")
    version = _clean(env, "AZURE_OPENAI_API_VERSION")
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", endpoint), ("AZURE_OPENAI_API_KEY", key)) if not v]
    if missing:
        raise ConfigError(f"Missing {', '.join(missing)} for AI_PROVIDER=azure_openai")
    info = classify_azure_endpoint(endpoint)
    if info["deployment"]:
        if deployment and deployment != info["deployment"]:
            raise ConfigError(
                f"AZURE_OPENAI_DEPLOYMENT_NAME '{deployment}' does not match the deployment in the endpoint URL "
                f"('{info['deployment']}')"
            )
        deployment = info["deployment"]
    if info["api_version"]:
        if version and version != info["api_version"]:
            raise ConfigError(
                f"AZURE_OPENAI_API_VERSION '{version}' does not match api-version in the endpoint URL ('{info['api_version']}')"
            )
        version = info["api_version"]
    if not deployment:
        raise ConfigError("Missing AZURE_OPENAI_DEPLOYMENT_NAME (the deployment name, not the model name)")
    if info["protocol"] == "azure_deployments" and not version:
        raise ConfigError(
            "Missing AZURE_OPENAI_API_VERSION (required for https://<resource>.openai.azure.com/ endpoints; "
            "e.g. 2024-10-21). Not needed if you use the /openai/v1/ endpoint."
        )
    if version and not re.match(r"^\d{4}-\d{2}-\d{2}(-preview)?$", version):
        raise ConfigError(f"AZURE_OPENAI_API_VERSION '{version}' should look like 2024-10-21 or 2025-04-01-preview")
    return AzureConfig(info["protocol"], info["base_url"], key, deployment, version if info["protocol"] == "azure_deployments" else "")


# --------------------------------------------------------------------------- providers

class Provider:
    name = "base"
    configured = True
    tool_filter: Callable = staticmethod(lambda tool: True)  # set by the server: tools whose integration works

    def system(self) -> str:
        return build_system(self.tool_filter)

    def summarize(self, instruction: str, text: str) -> Optional[str]:
        """Short AI summary, or None when this provider has no model (callers then use a rule-based summary)."""
        return None

    def respond(self, session_id: str, text: str) -> dict:
        raise NotImplementedError

    def reset(self, session_id: str) -> None:
        pass

    def status(self) -> dict:
        return {"provider": self.name, "configured": self.configured}

    def check(self) -> dict:
        """Live connectivity check. Never returns secrets."""
        return {"provider": self.name, "ok": True, "detail": "No network needed."}


class OfflineProvider(Provider):
    name = "offline"

    def respond(self, session_id: str, text: str) -> dict:
        from offline import offline_plan

        return offline_plan(text, self.tool_filter)

    def status(self) -> dict:
        return {**super().status(), "detail": "Rule-based intents only; no AI model."}


class MisconfiguredProvider(Provider):
    """Stands in for the requested provider when its config is invalid. Never falls back."""

    configured = False

    def __init__(self, name: str, error: str):
        self.name = name
        self.error = error

    def respond(self, session_id: str, text: str) -> dict:
        raise ProviderError(f"{self.name} is not configured: {self.error}")

    def status(self) -> dict:
        return {"provider": self.name, "configured": False, "error": self.error}

    def check(self) -> dict:
        return {"provider": self.name, "ok": False, "detail": f"Not configured: {self.error}"}


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, env: Mapping[str, str]):
        import anthropic

        self._anthropic = anthropic
        self.model = _clean(env, "JARVIS_MODEL") or DEFAULT_ANTHROPIC_MODEL
        key = _clean(env, "ANTHROPIC_API_KEY")
        # Explicit key from .env; otherwise the SDK's own credential chain (ANTHROPIC_AUTH_TOKEN, `ant auth login`).
        try:
            self.client = anthropic.Anthropic(api_key=key, timeout=60.0) if key else anthropic.Anthropic(timeout=60.0)
            has_credential = bool(key or getattr(self.client, "api_key", None) or getattr(self.client, "auth_token", None)
                                  or getattr(self.client, "credentials", None))
        except Exception as e:  # SDK raises (e.g. CredentialsError) when no credential source resolves
            raise ConfigError(f"No Anthropic credential ({type(e).__name__}): set ANTHROPIC_API_KEY for AI_PROVIDER=anthropic")
        if not has_credential:
            raise ConfigError("No Anthropic credential: set ANTHROPIC_API_KEY for AI_PROVIDER=anthropic")
        self.sessions: dict[str, list] = {}

    def respond(self, session_id: str, text: str) -> dict:
        history = self.sessions.setdefault(session_id, [])
        history.append({"role": "user", "content": _stamp(text)})
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=self.system(),
                messages=history,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except self._anthropic.APIError as e:
            history.pop()
            raise ProviderError(f"Anthropic request failed: {type(e).__name__}", _is_transient(e, self._anthropic)) from e
        if resp.stop_reason == "refusal":
            history.pop()
            return {"reply": "I'm afraid I can't help with that one, sir.", "steps": []}
        # Append full content (thinking/fallback blocks included) so history stays append-only.
        history.append({"role": "assistant", "content": resp.content})
        if len(history) >= MAX_TURNS * 2:
            self.sessions.pop(session_id, None)  # fresh session rather than editing old turns
        return _parse_json_reply(next((b.text for b in resp.content if b.type == "text"), "{}"))

    def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def summarize(self, instruction: str, text: str) -> Optional[str]:
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=1000, system=instruction,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": text}],
            )
        except self._anthropic.APIError:
            return None
        if resp.stop_reason == "refusal":
            return None
        return next((b.text for b in resp.content if b.type == "text"), None)

    def status(self) -> dict:
        return {**super().status(), "model": self.model, "credential": "set"}

    def check(self) -> dict:
        # Model lookup verifies credentials and model access without generating tokens.
        try:
            m = self.client.models.retrieve(self.model)
            return {"provider": self.name, "ok": True, "detail": f"Authenticated; model {m.id} available."}
        except self._anthropic.AuthenticationError:
            return {"provider": self.name, "ok": False, "detail": "Authentication failed (check ANTHROPIC_API_KEY)."}
        except self._anthropic.NotFoundError:
            return {"provider": self.name, "ok": False, "detail": f"Model {self.model} not found for this key."}
        except self._anthropic.APIConnectionError:
            return {"provider": self.name, "ok": False, "detail": "Cannot reach api.anthropic.com."}
        except self._anthropic.APIError as e:
            return {"provider": self.name, "ok": False, "detail": f"API error: {type(e).__name__}"}


class AzureOpenAIProvider(Provider):
    name = "azure_openai"

    def __init__(self, cfg: AzureConfig):
        import openai

        self._openai = openai
        self.cfg = cfg
        if cfg.protocol == "openai_v1":
            self.client = openai.OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=60.0)
        else:
            self.client = openai.AzureOpenAI(
                azure_endpoint=cfg.base_url, api_key=cfg.api_key, api_version=cfg.api_version, timeout=60.0
            )
        self.sessions: dict[str, list] = {}

    def _complete(self, messages: list[dict], structured: bool):
        kwargs = {}
        if structured:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "jarvis_reply", "schema": SCHEMA, "strict": True},
            }
        return self.client.chat.completions.create(model=self.cfg.deployment, messages=messages, **kwargs)

    def _explain(self, e: Exception) -> str:
        o = self._openai
        if isinstance(e, o.AuthenticationError):
            return "Authentication failed (check AZURE_OPENAI_API_KEY matches this endpoint's resource)."
        if isinstance(e, o.NotFoundError):
            return (f"Deployment '{self.cfg.deployment}' not found at {self.cfg.host}"
                    + (f" with api-version {self.cfg.api_version}" if self.cfg.api_version else "") + ".")
        if isinstance(e, o.PermissionDeniedError):
            return "Access denied (key/network rules on the Azure resource)."
        if isinstance(e, o.RateLimitError):
            return "Rate limited or quota exceeded on the deployment."
        if isinstance(e, o.BadRequestError):
            return ("Bad request — the deployment's model or this api-version may not support JSON-schema "
                    "structured outputs (use api-version 2024-08-01-preview or later and a gpt-4o-2024-08-06+ model).")
        if isinstance(e, o.APIConnectionError):
            return f"Cannot reach {self.cfg.host}."
        return f"API error: {type(e).__name__}"

    def respond(self, session_id: str, text: str) -> dict:
        history = self.sessions.setdefault(session_id, [])
        history.append({"role": "user", "content": _stamp(text)})
        try:
            resp = self._complete([{"role": "system", "content": self.system()}, *history], structured=True)
        except self._openai.APIError as e:
            history.pop()
            raise ProviderError(f"Azure OpenAI request failed: {self._explain(e)}", _is_transient(e, self._openai)) from e
        choice = resp.choices[0]
        content = choice.message.content or ""
        if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
            history.pop()
            return {"reply": "I'm afraid I can't help with that one, sir.", "steps": []}
        history.append({"role": "assistant", "content": content})
        if len(history) >= MAX_TURNS * 2:
            self.sessions.pop(session_id, None)
        return _parse_json_reply(content)

    def reset(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def summarize(self, instruction: str, text: str) -> Optional[str]:
        try:
            r = self._complete([{"role": "system", "content": instruction}, {"role": "user", "content": text}],
                               structured=False)
        except self._openai.APIError:
            return None
        return r.choices[0].message.content or None

    def status(self) -> dict:
        return {
            **super().status(),
            "protocol": self.cfg.protocol,
            "endpoint_host": self.cfg.host,
            "deployment": self.cfg.deployment,
            "api_version": self.cfg.api_version or None,
            "credential": "set",
        }

    def check(self) -> dict:
        # Minimal chat call: the only way to confirm the *deployment* (not just the key) works.
        try:
            r = self._complete([{"role": "user", "content": "Reply with the single word OK."}], structured=False)
            return {"provider": self.name, "ok": True,
                    "detail": f"Deployment '{self.cfg.deployment}' responded (model {getattr(r, 'model', '?')})."}
        except self._openai.APIError as e:
            return {"provider": self.name, "ok": False, "detail": self._explain(e)}


def build_provider(env: Mapping[str, str]) -> tuple[Provider, Optional[str]]:
    """Returns (provider, warning). Never switches providers on its own."""
    requested = _clean(env, "AI_PROVIDER").lower()
    warning = None
    if not requested:
        requested = "offline"
        if _clean(env, "ANTHROPIC_API_KEY") or _clean(env, "AZURE_OPENAI_API_KEY"):
            warning = "AI_PROVIDER is not set, so JARVIS runs offline even though an API key is present."
    if requested not in PROVIDERS:
        return MisconfiguredProvider(requested, f"Unknown AI_PROVIDER '{requested}'. Use one of {list(PROVIDERS)}."), warning
    try:
        if requested == "offline":
            return OfflineProvider(), warning
        if requested == "anthropic":
            return AnthropicProvider(env), warning
        return AzureOpenAIProvider(load_azure_config(env)), warning
    except ConfigError as e:
        return MisconfiguredProvider(requested, str(e)), warning
