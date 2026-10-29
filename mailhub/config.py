from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def normalize_origin(value: str) -> str:
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in "\\\"'`$;{}"):
        raise ValueError("Public URL contains invalid characters")
    u = urlsplit(value)
    if not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
        raise ValueError("Public URL must be an origin without path or credentials")
    scheme = u.scheme.lower()
    host = u.hostname.encode("idna").decode("ascii").lower()
    if scheme != "https" and not (scheme == "http" and host in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("Remote origins must use HTTPS")
    port = u.port
    if port == 0:
        raise ValueError("Invalid public URL port")
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return f"{scheme}://{authority}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = encoded.split(":")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=64)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class Settings:
    key: str
    admin_hash: str
    public_url: str = "http://localhost:8080"
    data_dir: str = "./data"
    sync_interval_seconds: int = 300
    sync_days: int = 30
    sync_limit: int = 200
    google_client_id: str = ""
    google_client_secret: str = ""
    microsoft_client_id: str = ""
    microsoft_client_secret: str = ""

    def __post_init__(self):
        object.__setattr__(self, "public_url", normalize_origin(self.public_url))
        if not self.key or not self.admin_hash:
            raise ValueError("Run python scripts/setup.py first; MAILHUB_KEY and MAILHUB_ADMIN_HASH are required")
        if not (30 <= self.sync_interval_seconds <= 86400 and 1 <= self.sync_days <= 365 and 1 <= self.sync_limit <= 1000):
            raise ValueError("Sync settings out of range: seconds 30..86400, days 1..365, limit 1..1000")

    @property
    def secure(self) -> bool:
        return urlsplit(self.public_url).scheme == "https"

    @property
    def hostname(self) -> str:
        return urlsplit(self.public_url).hostname or "localhost"

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv
        load_dotenv()
        return cls(
            key=os.getenv("MAILHUB_KEY", ""), admin_hash=os.getenv("MAILHUB_ADMIN_HASH", ""),
            public_url=os.getenv("MAILHUB_PUBLIC_URL", "http://localhost:8080").rstrip("/"),
            data_dir=os.getenv("MAILHUB_DATA_DIR", "./data"),
            sync_interval_seconds=int(os.getenv("MAILHUB_SYNC_SECONDS", "300")),
            sync_days=int(os.getenv("MAILHUB_SYNC_DAYS", "30")),
            sync_limit=int(os.getenv("MAILHUB_SYNC_LIMIT", "200")),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            microsoft_client_id=os.getenv("MICROSOFT_CLIENT_ID", ""),
            microsoft_client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", ""),
        )
