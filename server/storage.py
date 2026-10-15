"""Durable storage for tasks (plans + steps), grants, approvals, idempotency records, integration
secrets (OAuth tokens) and audit events.

- MongoDB Atlas when MONGODB_URI is set (database MONGODB_DB_NAME, default "jarvis").
- Local JSON files in server/data/ otherwise (single process, single machine; see LOCAL_LIMITATIONS).
- If MONGODB_URI is set but Atlas can't be reached, storage is *unavailable*: writes fail and the server
  refuses actions. It never silently switches to local files.
"""
import json
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlparse

KINDS = ("tasks", "grants", "approvals", "idempotency", "secrets")
MAX_LOCAL_AUDIT = 5000

LOCAL_LIMITATIONS = [
    "Single server process only: concurrent server instances would overwrite each other's files.",
    "Data lives only in server/data/ on this machine: no replication or backup; deleting the folder loses history and grants.",
    "Audit log keeps the most recent 5000 events.",
    "Other devices or a second laptop can't share tasks or grants.",
]


class PersistenceError(RuntimeError):
    """A write or read could not be completed. Callers must fail closed (no action)."""


class Storage:
    backend = "base"

    def load_all(self, kind: str) -> list[dict]:
        raise NotImplementedError

    def save(self, kind: str, doc_id: str, doc: dict) -> None:
        raise NotImplementedError

    def delete(self, kind: str, doc_id: str) -> None:
        raise NotImplementedError

    def append_audit(self, event: dict) -> None:
        raise NotImplementedError

    def list_audit(self, plan_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        raise NotImplementedError

    def status(self) -> dict:
        return {"backend": self.backend, "ok": True}


class LocalJsonStore(Storage):
    backend = "local_json"

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, dict]] = {}
        self._migrate_legacy_permissions()

    def _file(self, kind: str) -> Path:
        return self.dir / f"{kind}.json"

    def _read(self, kind: str) -> dict[str, dict]:
        if kind not in self._cache:
            f = self._file(kind)
            try:
                self._cache[kind] = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            except (OSError, ValueError) as e:
                raise PersistenceError(f"Can't read {f.name}: {type(e).__name__}") from e
        return self._cache[kind]

    def _write(self, kind: str, data: dict[str, dict]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self._file(kind).with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            tmp.replace(self._file(kind))
        except OSError as e:
            raise PersistenceError(f"Can't write {kind}: {type(e).__name__}") from e

    def load_all(self, kind: str) -> list[dict]:
        with self._lock:
            return [dict(v) for v in self._read(kind).values()]

    def save(self, kind: str, doc_id: str, doc: dict) -> None:
        with self._lock:
            data = dict(self._read(kind))
            data[doc_id] = json.loads(json.dumps(doc))  # deep copy, JSON-safe
            self._write(kind, data)  # cache updated only after a successful write
            self._cache[kind] = data

    def delete(self, kind: str, doc_id: str) -> None:
        with self._lock:
            data = dict(self._read(kind))
            if data.pop(doc_id, None) is not None:
                self._write(kind, data)
                self._cache[kind] = data

    def append_audit(self, event: dict) -> None:
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with (self.dir / "audit.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event) + "\n")
            except OSError as e:
                raise PersistenceError(f"Can't write audit log: {type(e).__name__}") from e
            self._trim_audit()

    def _trim_audit(self):
        path = self.dir / "audit.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > MAX_LOCAL_AUDIT * 1.2:
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines[-MAX_LOCAL_AUDIT:]) + "\n", encoding="utf-8")
            tmp.replace(path)

    def list_audit(self, plan_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        path = self.dir / "audit.jsonl"
        if not path.exists():
            return []
        with self._lock:
            events = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if plan_id:
            events = [e for e in events if e.get("plan_id") == plan_id]
        return events[-limit:]

    def _migrate_legacy_permissions(self):
        """Module-2 stored grants as a list in permissions.json; carry them over once."""
        legacy, target = self.dir / "permissions.json", self._file("grants")
        if legacy.exists() and not target.exists():
            try:
                grants = json.loads(legacy.read_text(encoding="utf-8"))
                self._write("grants", {g["id"]: g for g in grants if isinstance(g, dict) and "id" in g})
            except (OSError, ValueError, KeyError):
                pass  # unreadable legacy file: start empty (default deny)

    def status(self) -> dict:
        return {"backend": self.backend, "ok": True, "location": str(self.dir), "limitations": LOCAL_LIMITATIONS}


class MongoStore(Storage):
    """MongoDB Atlas storage. Transient network errors are retried with bounded exponential backoff;
    every write is an idempotent upsert (or insert with a fixed _id), so a retry can't duplicate records."""

    backend = "mongodb_atlas"
    MAX_ATTEMPTS = 3
    BASE_DELAY = 0.2

    def __init__(self, uri: str, db_name: str, client=None, sleep: Callable[[float], None] = time.sleep):
        from pymongo import errors

        self._errors = errors
        self._sleep = sleep
        self.db_name = db_name
        self.host = _mongo_host(uri)
        if client is None:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, retryWrites=True,
                                 appname="jarvis")
        self.client = client
        self.db = client[db_name]
        self._call(lambda: self.client.admin.command("ping"))
        self._call(lambda: self.db["audit"].create_index([("plan_id", 1), ("t", 1)]))

    def _call(self, fn):
        transient = (self._errors.AutoReconnect, self._errors.NetworkTimeout, self._errors.ConnectionFailure)
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return fn()
            except transient as e:
                if attempt == self.MAX_ATTEMPTS:
                    raise PersistenceError(f"MongoDB unreachable after {attempt} attempts: {type(e).__name__}") from e
                self._sleep(self.BASE_DELAY * 2 ** (attempt - 1))
            except self._errors.PyMongoError as e:
                raise PersistenceError(f"MongoDB error: {type(e).__name__}") from e

    def load_all(self, kind: str) -> list[dict]:
        docs = self._call(lambda: list(self.db[kind].find({})))
        return [{k: v for k, v in d.items() if k != "_id"} for d in docs]

    def save(self, kind: str, doc_id: str, doc: dict) -> None:
        self._call(lambda: self.db[kind].replace_one({"_id": doc_id}, {**doc, "_id": doc_id}, upsert=True))

    def delete(self, kind: str, doc_id: str) -> None:
        self._call(lambda: self.db[kind].delete_one({"_id": doc_id}))

    def append_audit(self, event: dict) -> None:
        def insert():
            try:
                self.db["audit"].insert_one({**event, "_id": event["id"]})
            except self._errors.DuplicateKeyError:
                pass  # an earlier attempt already landed; don't duplicate

        self._call(insert)

    def list_audit(self, plan_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        q = {"plan_id": plan_id} if plan_id else {}
        docs = self._call(lambda: list(self.db["audit"].find(q).sort("t", -1).limit(limit)))
        return [{k: v for k, v in d.items() if k != "_id"} for d in reversed(docs)]

    def status(self) -> dict:
        return {"backend": self.backend, "ok": True, "host": self.host, "database": self.db_name}


class UnavailableStore(Storage):
    """Configured storage that couldn't be reached. Every operation fails closed."""

    def __init__(self, backend: str, error: str, detail: Optional[dict] = None):
        self.backend = backend
        self.error = error
        self.detail = detail or {}

    def _fail(self, *a, **k):
        raise PersistenceError(f"{self.backend} unavailable: {self.error}")

    load_all = save = delete = append_audit = list_audit = _fail

    def status(self) -> dict:
        return {"backend": self.backend, "ok": False, "error": self.error, **self.detail}


def _mongo_host(uri: str) -> str:
    """Host part of a Mongo URI without credentials."""
    netloc = urlparse(uri).netloc
    return netloc.rsplit("@", 1)[-1] or "?"


def build_storage(env: Mapping[str, str], data_dir: Path, mongo_client=None) -> Storage:
    uri = (env.get("MONGODB_URI") or "").strip()
    if uri.startswith("<") or not uri:
        return LocalJsonStore(data_dir)
    db_name = (env.get("MONGODB_DB_NAME") or "").strip() or "jarvis"
    if not uri.startswith(("mongodb+srv://", "mongodb://")):
        return UnavailableStore("mongodb_atlas", "MONGODB_URI must start with mongodb+srv:// or mongodb://")
    try:
        return MongoStore(uri, db_name, client=mongo_client)
    except PersistenceError as e:
        return UnavailableStore("mongodb_atlas", str(e), {"host": _mongo_host(uri), "database": db_name})
    except Exception as e:  # bad URI options, DNS SRV lookup failure, ...
        return UnavailableStore("mongodb_atlas", f"{type(e).__name__}", {"host": _mongo_host(uri), "database": db_name})
