"""OAuth tokens and similar secrets, kept in the server's storage (local files or Atlas) and never returned
by any endpoint. Encrypted at rest with Fernet when JARVIS_SECRET_KEY is set."""
import base64
import hashlib
import json
from typing import Mapping, Optional

from storage import PersistenceError, Storage


class SecretStore:
    KIND = "secrets"

    def __init__(self, storage: Storage, env: Mapping[str, str]):
        self.storage = storage
        passphrase = (env.get("JARVIS_SECRET_KEY") or "").strip()
        self._fernet = None
        if passphrase and not passphrase.startswith("<"):
            from cryptography.fernet import Fernet

            key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
            self._fernet = Fernet(key)

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    def get(self, name: str) -> Optional[dict]:
        for doc in self.storage.load_all(self.KIND):
            if doc.get("name") == name:
                return self._decode(doc)
        return None

    def put(self, name: str, value: dict) -> None:
        payload = json.dumps(value)
        if self._fernet:
            doc = {"name": name, "enc": self._fernet.encrypt(payload.encode()).decode()}
        else:
            doc = {"name": name, "plain": value}
        self.storage.save(self.KIND, name, doc)

    def delete(self, name: str) -> None:
        self.storage.delete(self.KIND, name)

    def _decode(self, doc: dict) -> Optional[dict]:
        if "enc" in doc:
            if not self._fernet:
                raise PersistenceError("Stored secret is encrypted but JARVIS_SECRET_KEY is not set")
            from cryptography.fernet import InvalidToken

            try:
                return json.loads(self._fernet.decrypt(doc["enc"].encode()))
            except InvalidToken:
                raise PersistenceError("Stored secret can't be decrypted with the current JARVIS_SECRET_KEY")
        return doc.get("plain")
