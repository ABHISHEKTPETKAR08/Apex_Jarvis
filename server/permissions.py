"""Capability grants for JARVIS. Everything is denied unless an active, in-scope grant exists."""
import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from storage import PersistenceError, Storage


class Capability(str, Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    CAMERA = "camera"
    MICROPHONE = "microphone"
    SPEAKER = "speaker"
    CALENDAR = "calendar"


# Operations each capability can be granted for.
SCOPES: dict[Capability, tuple[str, ...]] = {
    Capability.EMAIL: ("read", "draft", "send"),
    Capability.WHATSAPP: ("send",),
    Capability.CAMERA: ("capture",),
    Capability.MICROPHONE: ("listen",),
    Capability.SPEAKER: ("speak",),
    Capability.CALENDAR: ("read", "write"),
}

MAX_TTL_SECONDS = 30 * 24 * 3600


class PermissionError_(ValueError):
    """Invalid grant/revoke request (bad capability, scope, ttl or id)."""


def normalize_target(value: str) -> str:
    """Phone numbers compare by digits (+91 99... == 9199...), everything else case-insensitively."""
    v = str(value).strip().lower()
    if re.fullmatch(r"\+?[\d\s().-]{6,}", v):
        return re.sub(r"\D", "", v)
    return v


@dataclass
class Grant:
    id: str
    capability: str
    scopes: list[str]
    granted_at: float
    expires_at: Optional[float] = None  # None = until revoked
    revoked_at: Optional[float] = None
    targets: Optional[list[str]] = None  # e.g. allowed recipients; None = any

    def is_active(self, now: float) -> bool:
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)

    def covers(self, scope: str, target: Optional[str]) -> bool:
        if scope not in self.scopes:
            return False
        if self.targets is None:
            return True
        return target is not None and normalize_target(target) in {normalize_target(t) for t in self.targets}


@dataclass
class Decision:
    allowed: bool
    reason: str
    grant_id: Optional[str] = None


@dataclass
class PermissionStore:
    path: Optional[Path] = None  # legacy single-file storage (Module 2); prefer `storage`
    clock: Callable[[], float] = time.time
    storage: Optional[Storage] = None
    _grants: list[Grant] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        if self.storage is not None:
            # Load failure propagates: the server must not start with an unknown permission state.
            self._grants = [Grant(**{k: v for k, v in g.items() if k in Grant.__dataclass_fields__})
                            for g in self.storage.load_all("grants")]
            self._grants.sort(key=lambda g: g.granted_at)
        elif self.path and self.path.exists():
            try:
                self._grants = [Grant(**g) for g in json.loads(self.path.read_text(encoding="utf-8"))]
            except (ValueError, TypeError):
                # Corrupt file: start empty, which is default-deny.
                self._grants = []

    @staticmethod
    def parse_capability(value: str) -> Capability:
        try:
            return Capability(str(value).strip().lower())
        except ValueError:
            raise PermissionError_(f"Unknown capability '{value}'. Valid: {[c.value for c in Capability]}")

    def grant(
        self,
        capability: str,
        scopes: Optional[list[str]] = None,
        ttl_seconds: Optional[float] = None,
        targets: Optional[list[str]] = None,
    ) -> Grant:
        cap = self.parse_capability(capability)
        allowed = SCOPES[cap]
        scopes = list(dict.fromkeys(scopes or allowed))
        bad = [s for s in scopes if s not in allowed]
        if bad:
            raise PermissionError_(f"Invalid scope(s) {bad} for {cap.value}. Valid: {list(allowed)}")
        if ttl_seconds is not None and not (0 < ttl_seconds <= MAX_TTL_SECONDS):
            raise PermissionError_(f"ttl_seconds must be between 1 and {MAX_TTL_SECONDS}, or omitted")
        clean_targets = [t.strip() for t in targets if t and t.strip()] if targets else None
        now = self.clock()
        g = Grant(
            id=uuid.uuid4().hex[:12],
            capability=cap.value,
            scopes=scopes,
            granted_at=now,
            expires_at=now + ttl_seconds if ttl_seconds else None,
            targets=clean_targets or None,
        )
        with self._lock:
            self._persist(g)  # not active unless stored
            self._grants.append(g)
            if self.storage is None:
                self._save()
        return g

    def revoke(self, grant_id: str) -> Grant:
        with self._lock:
            for g in self._grants:
                if g.id == grant_id:
                    if g.revoked_at is None:
                        # Revoke in memory first so it takes effect even if the write fails.
                        g.revoked_at = self.clock()
                        self._save()
                        self._persist(g)
                    return g
        raise PermissionError_(f"No grant with id '{grant_id}'")

    def revoke_capability(self, capability: str) -> int:
        cap = self.parse_capability(capability)
        now = self.clock()
        changed = []
        with self._lock:
            for g in self._grants:
                if g.capability == cap.value and g.is_active(now):
                    g.revoked_at = now
                    changed.append(g)
            if changed:
                self._save()
                for g in changed:
                    self._persist(g)
        return len(changed)

    def authorize(self, capability: str, scope: str, target: Optional[str] = None) -> Decision:
        try:
            cap = self.parse_capability(capability)
        except PermissionError_ as e:
            return Decision(False, str(e))
        if scope not in SCOPES[cap]:
            return Decision(False, f"'{scope}' is not a valid scope for {cap.value}")
        now = self.clock()
        with self._lock:
            for g in self._grants:
                if g.capability == cap.value and g.is_active(now) and g.covers(scope, target):
                    return Decision(True, "granted", g.id)
        suffix = f" for '{target}'" if target else ""
        return Decision(False, f"No active {cap.value}:{scope} grant{suffix}. Grant it in App Access.")

    def list(self, include_inactive: bool = False) -> list[dict]:
        now = self.clock()
        with self._lock:
            out = []
            for g in self._grants:
                active = g.is_active(now)
                if active or include_inactive:
                    out.append({**asdict(g), "active": active})
            return out

    def _persist(self, g: Grant):
        if self.storage is not None:
            try:
                self.storage.save("grants", g.id, asdict(g))
            except PersistenceError as e:
                raise PersistenceError(f"Grant change for {g.capability} was not saved: {e}") from e

    def _save(self):
        if self.storage is not None or not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(g) for g in self._grants], indent=1), encoding="utf-8")
        tmp.replace(self.path)
