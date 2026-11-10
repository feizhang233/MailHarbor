from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit


READ_SCOPE = "mail.read"
REPLY_SCOPE = "mail.reply"
READ_REPLY_SCOPE = f"{READ_SCOPE} {REPLY_SCOPE}"


def normalize_origin(value: str, *, allow_loopback_http: bool = True) -> str:
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value) or any(c in value for c in "\\\"'`$;{}"):
        raise ValueError("Public URL contains invalid characters")
    u = urlsplit(value)
    if not u.hostname or u.username is not None or u.path not in ("", "/") or "?" in value or "#" in value:
        raise ValueError("Public URL must be an origin without path or credentials")
    scheme = u.scheme.lower()
    host = u.hostname.encode("idna").decode("ascii").lower()
    if "*" in host:
        raise ValueError("Public URL requires a specific host")
    if scheme != "https" and not (allow_loopback_http and scheme == "http" and host in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("The service origin must use HTTPS")
    port = u.port
    if port == 0:
        raise ValueError("Invalid public URL port")
    authority = f"[{host}]" if ":" in host else host
    if port and port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return f"{scheme}://{authority}"


def client_bearer_token(environment) -> str:
    token = environment.get("MAILHUB_MCP_TOKEN", "")
    if not (0 < len(token) <= 4096 and token.isascii() and token.isprintable() and " " not in token):
        raise ValueError("Set a valid MAILHUB_MCP_TOKEN in the process environment.")
    return token


def make_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return salt.hex() + ":" + derived.hex()


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
        # TrustedHostMiddleware compares Host literals, including IPv6 brackets.
        origin = urlsplit(self.public_url)
        return origin.netloc if origin.port is None else origin.netloc.rsplit(":", 1)[0]

    @classmethod
    def from_env(cls):
        from dotenv import load_dotenv
        load_dotenv()
        return cls(
            key=os.getenv("MAILHUB_KEY", ""), admin_hash=os.getenv("MAILHUB_ADMIN_HASH", ""),
            public_url=os.getenv("MAILHUB_PUBLIC_URL", "http://localhost:8080"),
            data_dir=os.getenv("MAILHUB_DATA_DIR", "./data"),
            sync_interval_seconds=int(os.getenv("MAILHUB_SYNC_SECONDS", "300")),
            sync_days=int(os.getenv("MAILHUB_SYNC_DAYS", "30")),
            sync_limit=int(os.getenv("MAILHUB_SYNC_LIMIT", "200")),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            microsoft_client_id=os.getenv("MICROSOFT_CLIENT_ID", ""),
            microsoft_client_secret=os.getenv("MICROSOFT_CLIENT_SECRET", ""),
        )
