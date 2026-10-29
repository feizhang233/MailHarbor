#!/usr/bin/env python3
"""Create a private MailHarbor environment without displaying credentials."""
import argparse
import base64
import getpass
import hashlib
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailhub.config import normalize_origin


def validate_url(value: str) -> tuple[str, str]:
    canonical = normalize_origin(value)
    return canonical, urlsplit(canonical).netloc


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 1024:
        raise ValueError("密碼需要 12–1024 字元，未寫入任何設定。")


def make_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return salt.hex() + ":" + derived.hex()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Public HTTPS origin, e.g. https://mail.example.com")
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("設定檔已存在，未覆寫。請先安全備份，再手動調整設定。")
    try:
        public_url, domain = validate_url(args.url or input("服務網址（https://mail.example.com）："))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    password = getpass.getpass("建立管理密碼（至少 12 字元，不會顯示）：")
    try:
        validate_password(password)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if password != getpass.getpass("再次輸入管理密碼："):
        raise SystemExit("兩次密碼不一致，未寫入任何設定。")
    data = {
        "MAILHUB_PUBLIC_URL": public_url,
        "MAILHUB_DOMAIN": domain,
        "MAILHUB_ADMIN_HASH": make_hash(password),
        "MAILHUB_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
        "MAILHUB_DATA_DIR": "./data",
        "MAILHUB_SYNC_SECONDS": "300",
        "MAILHUB_SYNC_DAYS": "30",
        "MAILHUB_SYNC_LIMIT": "200",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "MICROSOFT_CLIENT_ID": "",
        "MICROSOFT_CLIENT_SECRET": "",
    }
    # O_EXCL prevents racing another setup process and prevents symlink overwrite.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("# Private MailHarbor settings. Keep this file out of source control.\n")
        stream.write("\n".join(f"{key}={value}" for key, value in data.items()) + "\n")
    print("私人設定已建立。請保管管理密碼並備份加密金鑰；服務啟動後在管理頁填寫郵箱授權設定。")


if __name__ == "__main__":
    main()
