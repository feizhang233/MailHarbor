"""Bounded inbox snapshots and plain-text replies to a specific cached message."""
from __future__ import annotations

import base64
import binascii
import hashlib
import imaplib
import ipaddress
import json
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.errors import HeaderParseError, NonASCIILocalPartDefect
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlencode, urlsplit

MAX_BODY = 131072
MAX_WIRE_BODY = 262144
MAX_HEADER = 65536
MAX_JSON = 4 * 1024 * 1024
TIMEOUT = 20


class ProviderError(Exception):
    """A deliberately sanitized, user-facing connection/synchronization error."""

    def __init__(self, message, *, code="provider_error", retryable=False, reauth_required=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.reauth_required = reauth_required


_NETEASE_FREE_GUIDE = "https://help.mail.126.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2a5feb28b66796d3b"
_NETEASE_VIP_GUIDE = "https://help.vip.188.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac205fa9c38aad06acb"
_NETEASE = {
    "163": ("網易 163", "163.com", _NETEASE_FREE_GUIDE),
    "126": ("網易 126", "126.com", _NETEASE_FREE_GUIDE),
    "yeah": ("網易 Yeah", "yeah.net", _NETEASE_FREE_GUIDE),
    "vip163": ("網易 VIP 163", "vip.163.com", _NETEASE_VIP_GUIDE),
    "vip126": ("網易 VIP 126", "vip.126.com", _NETEASE_VIP_GUIDE),
    "188": ("網易 188", "188.com", _NETEASE_VIP_GUIDE),
}
_NETEASE_HOSTS = {f"imap.{domain}" for _, domain, _ in _NETEASE.values()}


@dataclass(frozen=True)
class _ImapPreset:
    name: str
    host: str
    smtp_host: str
    note: str
    guide_url: str
    smtp_port: int = 465
    email_domain: str = ""


_PRESETS = {
    "icloud": _ImapPreset("iCloud Mail", "imap.mail.me.com", "smtp.mail.me.com", "使用 Apple App 專用密碼，需啟用雙重認證。", "https://support.apple.com/102525", smtp_port=587),
    "qq": _ImapPreset("QQ 郵箱", "imap.qq.com", "smtp.qq.com", "先開啟 IMAP，使用郵箱授權碼。", "https://help.mail.qq.com/detail/106/985"),
    "qq_enterprise": _ImapPreset("騰訊企業郵箱", "imap.exmail.qq.com", "smtp.exmail.qq.com", "需管理員允許 IMAP；依帳號安全設定使用客戶端授權碼。", "https://exmail.qq.com/"),
    **{key: _ImapPreset(name, f"imap.{domain}", f"smtp.{domain}", "先開啟 IMAP，使用客戶端授權碼；以帳號內設定為準。", guide, email_domain=domain)
       for key, (name, domain, guide) in _NETEASE.items()},
    "yahoo": _ImapPreset("Yahoo Mail", "imap.mail.yahoo.com", "smtp.mail.yahoo.com", "使用 Yahoo App password。", "https://help.yahoo.com/kb/SLN4075.html"),
    "aol": _ImapPreset("AOL Mail", "imap.aol.com", "smtp.aol.com", "使用 AOL App password。", "https://help.aol.com/articles/how-do-i-use-other-email-applications-to-send-and-receive-my-aol-mail"),
    "fastmail": _ImapPreset("Fastmail", "imap.fastmail.com", "smtp.fastmail.com", "使用 App password；Basic 方案不含 IMAP。", "https://www.fastmail.help/hc/en-us/articles/1500000278342-Server-names-and-ports"),
    "zoho": _ImapPreset("Zoho Mail", "imap.zoho.com", "smtp.zoho.com", "需開啟 IMAP；付費企業版或不同資料中心可能需用自訂主機。新免費方案可能不支援。", "https://www.zoho.com/mail/help/imap-access.html"),
    "gmx": _ImapPreset("GMX", "imap.gmx.com", "mail.gmx.com", "先開啟 IMAP；地區帳號請核對官方主機設定。", "https://support.gmx.com/pop-imap/imap/server.html"),
    "custom": _ImapPreset("其他 IMAP 郵箱", "", "", "輸入公開 IMAP 主機；僅支援 993 TLS。內網與 Proton Bridge 暫不支援。", ""),
}


def provider_catalog(settings) -> list[dict]:
    result = [
        {"id": "gmail", "name": "Gmail / Google Workspace", "kind": "oauth", "configured": bool(settings.google_client_id and settings.google_client_secret),
         "note": "每個帳號各自授權讀取郵件，可選擇開啟寄信權限。Testing 模式授權通常 7 天失效。", "guide_url": "https://developers.google.com/workspace/gmail/api/auth/scopes"},
        {"id": "microsoft", "name": "Outlook / Microsoft 365", "kind": "oauth", "configured": bool(settings.microsoft_client_id and settings.microsoft_client_secret),
         "note": "每個帳號各自授權 Mail.Read，可選擇開啟 Mail.Send；組織帳號可能需要管理員允許。", "guide_url": "https://learn.microsoft.com/graph/permissions-reference"},
    ]
    for key, preset in _PRESETS.items():
        entry = {"id": key, "name": preset.name, "kind": "imap", "configured": True,
                 "host": preset.host, "port": 993, "smtp_host": preset.smtp_host, "smtp_port": preset.smtp_port,
                 "note": preset.note, "guide_url": preset.guide_url}
        if preset.email_domain:
            entry["email_domain"] = preset.email_domain
        result.append(entry)
    result.extend([
        {"id": "proton", "name": "Proton Mail", "kind": "unsupported", "configured": False, "note": "需要本機 Proton Mail Bridge（付費方案）；本版不開放內網 Bridge 連線。", "guide_url": "https://proton.me/support/imap-smtp-and-pop3-setup"},
        {"id": "tuta", "name": "Tuta", "kind": "unsupported", "configured": False, "note": "不提供一般 IMAP 存取；本版未支援。", "guide_url": "https://tuta.com/support#imap"},
    ])
    return result


def _oauth_config(provider, settings, *, allow_reply=False):
    if provider == "gmail":
        return (settings.google_client_id, settings.google_client_secret,
                "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token",
                "openid email https://www.googleapis.com/auth/gmail.readonly" +
                (" https://www.googleapis.com/auth/gmail.send" if allow_reply else ""))
    if provider == "microsoft":
        return (settings.microsoft_client_id, settings.microsoft_client_secret,
                "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                "openid offline_access User.Read Mail.Read" + (" Mail.Send" if allow_reply else ""))
    raise ProviderError("此郵箱不支援 OAuth 連線。")


def oauth_authorize_url(provider, settings, state, challenge, *, allow_reply=False) -> str:
    client_id, secret, endpoint, _, scope = _oauth_config(provider, settings, allow_reply=allow_reply)
    if not client_id or not secret:
        raise ProviderError("尚未設定此服務的 OAuth 應用程式。")
    params = {"client_id": client_id, "redirect_uri": f"{settings.public_url.rstrip('/')}/api/oauth/{provider}/callback",
              "response_type": "code", "scope": scope, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256",
              "prompt": "consent select_account" if provider == "gmail" else "select_account"}
    if provider == "gmail":
        params["access_type"] = "offline"
    return endpoint + "?" + urlencode(params)


def _http_error(status, payload=None, *, oauth=False):
    """Classify allowlisted error codes; never echo provider error descriptions."""
    payload = payload if isinstance(payload, dict) else {}
    error = payload.get("error")
    if oauth and isinstance(error, str):
        if error in {"invalid_grant", "interaction_required", "login_required", "consent_required"}:
            return ProviderError("郵箱授權已失效，請重新連接並授權。", code="reauth_required", reauth_required=True)
        if error in {"invalid_client", "unauthorized_client", "deleted_client", "invalid_scope", "redirect_uri_mismatch"}:
            return ProviderError("OAuth 應用設定無效，請檢查 Client ID、Client Secret、回呼網址及權限設定。", code="oauth_configuration")
        if error in {"temporarily_unavailable", "server_error"}:
            return ProviderError("郵箱授權服務暫時無法使用，稍後會重試。", code="service_unavailable", retryable=True)
    reasons = []
    if isinstance(error, dict) and isinstance(error.get("errors"), list):
        reasons = [item.get("reason") for item in error["errors"] if isinstance(item, dict)]
    if status == 429 or any(reason in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"} for reason in reasons):
        return ProviderError("郵箱服務暫時限制請求頻率，稍後會重試。", code="rate_limited", retryable=True)
    if status == 401:
        return ProviderError("郵箱授權已失效，請重新連接並授權。", code="reauth_required", reauth_required=True)
    if status == 403:
        return ProviderError("郵箱權限不足或受到組織政策限制，請檢查授權與管理員設定。", code="permission_denied")
    if status == 404:
        return ProviderError("指定郵件或郵箱資源已不存在。", code="not_found")
    if status >= 500 or status in {408, 425}:
        return ProviderError("郵箱服務暫時無法使用，稍後會重試。", code="service_unavailable", retryable=True)
    return ProviderError("郵箱服務未能完成請求，請核對連線設定或稍後重試。")


def _request_json(client, method, url, *, missing_ok=False, oauth=False, **kwargs):
    """Bound response bytes, disable redirects, and never include remote errors."""
    import httpx

    try:
        with client.stream(method, url, **kwargs) as response:
            if response.status_code == 404 and missing_ok:
                return None
            failed = response.status_code >= 300
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if failed and len(data) > MAX_HEADER:
                    raise _http_error(response.status_code, oauth=oauth)
                if not failed and len(data) > MAX_JSON:
                    raise ProviderError("郵箱回應超出本版的大小限制。")
            try:
                result = json.loads(data)
            except (ValueError, UnicodeError):
                if failed:
                    raise _http_error(response.status_code, oauth=oauth) from None
                raise ProviderError("郵箱服務回傳格式不正確。") from None
            if failed:
                raise _http_error(response.status_code, result, oauth=oauth)
            if not isinstance(result, dict):
                raise ProviderError("郵箱服務回傳格式不正確。")
            return result
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, OSError):
        raise ProviderError("無法連線至郵箱服務，請檢查網路；稍後會重試。", code="network_error", retryable=True) from None


def _client():
    import httpx

    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False)


def _token_credentials(provider, result, previous=None, *, require_refresh=True):
    updated = dict(previous or {})
    if not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise ProviderError("服務未提供有效授權，請重新連接。")
    granted = result.get("scope")
    if isinstance(granted, str):
        required = {"https://www.googleapis.com/auth/gmail.readonly"} if provider == "gmail" else {"mail.read", "https://graph.microsoft.com/mail.read"}
        if not required.intersection(granted.lower().split()):
            raise ProviderError("未取得讀取郵件所需的權限，請重新授權。")
    updated["access_token"] = result["access_token"]
    if result.get("refresh_token"):
        updated["refresh_token"] = result["refresh_token"]
    if require_refresh and not updated.get("refresh_token"):
        raise ProviderError("未取得背景同步授權，請撤銷舊授權後重新連接。")
    try:
        updated["expires_at"] = time.time() + max(0, min(int(result.get("expires_in", 3600)), 86400))
    except (ValueError, TypeError, OverflowError):
        raise ProviderError("服務回傳的授權期限無效，請重新連接。") from None
    if isinstance(granted, str):
        updated["scope"] = granted
    return updated


def oauth_exchange(provider, settings, code, verifier, *, allow_reply=False) -> dict:
    client_id, secret, _, endpoint, scope = _oauth_config(provider, settings, allow_reply=allow_reply)
    if not client_id or not secret:
        raise ProviderError("尚未設定此服務的 OAuth 應用程式。")
    fields = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier,
              "client_id": client_id, "client_secret": secret,
              "redirect_uri": f"{settings.public_url.rstrip('/')}/api/oauth/{provider}/callback"}
    if provider == "microsoft":
        fields["scope"] = scope
    with _client() as client:
        response = _request_json(client, "POST", endpoint, data=fields, oauth=True)
    # A repeat grant may omit the refresh token. Only the verified mailbox can
    # supply its existing token, after identify_oauth resolves the provider UID.
    # OAuth permits omitting scope when all requested permissions were granted.
    response.setdefault("scope", scope)
    return {**_token_credentials(provider, response, require_refresh=False), "client_id": client_id,
            "allow_reply": allow_reply}


def _refresh(provider, credentials, settings):
    try:
        if credentials.get("access_token") and float(credentials.get("expires_at", 0)) > time.time() + 90:
            return dict(credentials)
    except (ValueError, TypeError):
        pass
    client_id, secret, _, endpoint, _ = _oauth_config(provider, settings)
    if not credentials.get("refresh_token") or not client_id or not secret:
        raise ProviderError("缺少背景同步授權或應用設定，請重新連接。")
    fields = {"grant_type": "refresh_token", "refresh_token": credentials["refresh_token"], "client_id": client_id, "client_secret": secret}
    # Omitting scope refreshes the original grant, including legacy read-only accounts.
    with _client() as client:
        result = _request_json(client, "POST", endpoint, data=fields, oauth=True)
    return {**_token_credentials(provider, result, credentials), "client_id": client_id}


def identify_oauth(provider, credentials) -> dict:
    headers = {"Authorization": "Bearer " + credentials.get("access_token", "")}
    with _client() as client:
        if provider == "gmail":
            profile = _request_json(client, "GET", "https://openidconnect.googleapis.com/v1/userinfo", headers=headers)
            email, uid = profile.get("email"), profile.get("sub")
        elif provider == "microsoft":
            profile = _request_json(client, "GET", "https://graph.microsoft.com/v1.0/me", headers=headers, params={"$select": "id,mail,userPrincipalName"})
            email, uid = profile.get("mail") or profile.get("userPrincipalName"), profile.get("id")
        else:
            raise ProviderError("此郵箱不支援 OAuth 連線。")
    if not isinstance(email, str) or "@" not in email or not isinstance(uid, str) or not uid:
        raise ProviderError("無法確認授權帳號的郵箱身分。")
    return {"email": email[:320], "provider_uid": uid[:512]}


def _clean(value, limit=2000):
    if value is None:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))[:limit]


class _TextHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "template"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "template"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain_html(value, status=None):
    parser = _TextHTML()
    try:
        value = str(value)
        parser.feed(value[:MAX_WIRE_BODY])
        text = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(parser.parts)).strip()
        if status is not None and (len(value) > MAX_WIRE_BODY or len(text) > MAX_BODY):
            status["truncated"] = True
        return _clean(text, MAX_BODY)
    except (ValueError, RecursionError):
        if status is not None:
            status["truncated"] = True
        return "[此 HTML 郵件無法轉換成純文字]"


def _decode_header(value):
    try:
        return _clean(str(make_header(decode_header(str(value or "")))))
    except (LookupError, UnicodeError, ValueError):
        return _clean(value)


def _mailboxes(value):
    """Return the complete address list or none. Never send to a partially parsed list."""
    if not isinstance(value, str) or len(value) > 16384:
        return []
    unfolded = re.sub(r"\r?\n[ \t]+", " ", value)
    if re.search(r"[\x00-\x08\x0a-\x1f\x7f]", unfolded):
        return []
    try:
        header = policy.default.header_factory("To", unfolded)
        if any(not isinstance(error, NonASCIILocalPartDefect) for error in header.defects) or len(header.addresses) > 100:
            return []
        addresses = []
        for address in header.addresses:
            if not address.username or not address.domain or len(address.addr_spec) > 254 or re.search(r"[\x00-\x1f\x7f]", address.addr_spec):
                return []
            addresses.append(address.addr_spec)
        return list(dict.fromkeys(addresses))
    except (ValueError, IndexError, AttributeError, HeaderParseError, RecursionError):
        return []


_MESSAGE_ID = re.compile(r"<[^\s<>@\x00-\x1f\x7f]+@[^\s<>@\x00-\x1f\x7f]+>")


def _reply_headers(items):
    fields = {}
    for name, value in items:
        fields.setdefault(str(name).lower(), []).append(value)
    destination = fields.get("reply-to", fields.get("from", []))
    recipients = _mailboxes(destination[0]) if len(destination) == 1 else []
    ids = fields.get("message-id", [])
    parent = str(ids[0]).strip() if len(ids) == 1 else ""
    if len(parent) > 998 or not parent.isascii() or not _MESSAGE_ID.fullmatch(parent):
        parent = ""
    chain = fields.get("references", fields.get("in-reply-to", []))
    refs = [item for item in _MESSAGE_ID.findall(str(chain[0])[:MAX_HEADER]) if item.isascii() and len(item) <= 998] if len(chain) == 1 else []
    # Retain the root and most recent ancestors within a bounded header.
    return {"reply_recipients": recipients, "reply_message_id": parent, "reply_references": refs[:1] + refs[1:][-15:]}


def _graph_reply_recipients(raw):
    targets = raw.get("replyTo")
    if targets is None or targets == []:
        targets = [raw.get("from")]
    if not isinstance(targets, list) or len(targets) > 100:
        return []
    result = []
    for target in targets:
        email = target.get("emailAddress") if isinstance(target, dict) else None
        address = email.get("address") if isinstance(email, dict) else None
        if _mailboxes(address) != [address]:
            return []
        result.append(address)
    return list(dict.fromkeys(result))


def _date(value, fallback=None):
    try:
        if isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif value:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                parsed = parsedate_to_datetime(str(value))
        else:
            return fallback or "1970-01-01T00:00:00+00:00"
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return fallback or "1970-01-01T00:00:00+00:00"


def _gmail_body(payload, depth=0, status=None):
    if status is None:
        status = {"truncated": False}

    def truncated(kind=None):
        status["truncated"] = True
        status[(kind + "_truncated") if kind else "structure_truncated"] = True

    if depth > 20 or not isinstance(payload, dict):
        truncated()
        return [], [], []
    texts, htmls, attachments = [], [], []
    filename = payload.get("filename", "")
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    disposition = next((header.get("value") for header in payload.get("headers", []) if isinstance(header, dict)
                        and str(header.get("name", "")).lower() == "content-disposition"), "")
    is_attachment = bool(filename) or str(disposition).partition(";")[0].strip().lower() == "attachment"
    if is_attachment or (mime not in ("text/plain", "text/html") and not mime.startswith("multipart/")):
        if is_attachment or body.get("attachmentId"):
            attachments.append({"filename": _decode_header(filename) or "attachment", "content_type": _clean(mime, 120), "size": _safe_size(body.get("size"))})
        return texts, htmls, attachments
    encoded = body.get("data")
    kind = "plain" if mime == "text/plain" else "html"
    if mime in ("text/plain", "text/html") and body.get("attachmentId") and not encoded:
        # Large body parts may be stored behind the attachment API; this MVP does not download them.
        truncated(kind)
    if isinstance(encoded, str) and mime in ("text/plain", "text/html"):
        try:
            if len(encoded) > MAX_WIRE_BODY * 2 or _safe_size(body.get("size")) > MAX_WIRE_BODY:
                truncated(kind)
            encoded = encoded[:MAX_WIRE_BODY * 2]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))[:MAX_WIRE_BODY]
            charset = "utf-8"
            for header in payload.get("headers", []):
                if str(header.get("name", "")).lower() == "content-type":
                    match = re.search(r"charset\s*=\s*[\"']?([^\s;\"']+)", header.get("value", ""), re.I)
                    if match:
                        charset = match.group(1)
            try:
                decoded = raw.decode(charset, errors="replace")
            except LookupError:
                decoded = raw.decode("utf-8", errors="replace")
            if mime == "text/plain":
                if len(decoded) > MAX_BODY:
                    truncated(kind)
                texts.append(_clean(decoded, MAX_BODY))
            else:
                html_status = {"truncated": False}
                htmls.append(_plain_html(decoded, html_status))
                if html_status["truncated"]:
                    truncated(kind)
        except (ValueError, binascii.Error):
            truncated(kind)
    if len(payload.get("parts", [])) > 100:
        truncated()
    for child in payload.get("parts", [])[:100]:
        a, b, c = _gmail_body(child, depth + 1, status)
        texts.extend(a); htmls.extend(b); attachments.extend(c)
    return texts, htmls, attachments


def _safe_size(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cutoff(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _within_cutoff(received_at, cutoff):
    try:
        return datetime.fromisoformat(received_at) >= cutoff
    except (TypeError, ValueError):
        return False


def _page_items(items):
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ProviderError("郵箱服務回傳格式不正確。")
    return items


def _next_page(page, field):
    value = page.get(field)
    if value is not None and not isinstance(value, str):
        raise ProviderError("郵箱分頁回應不符合預期。")
    return value


def _gmail_message_ids(client, base, headers, cutoff, limit):
    page_token, seen_pages, seen_ids = None, set(), set()
    while True:
        if page_token in seen_pages or len(seen_pages) >= max(100, limit):
            raise ProviderError("郵箱分頁回應重複或超出限制，請稍後重試。")
        seen_pages.add(page_token)
        # Epoch seconds avoid Gmail's PST interpretation of date-only query values.
        params = {"q": f"in:inbox after:{int(cutoff.timestamp())}", "maxResults": min(100, limit)}
        if page_token:
            params["pageToken"] = page_token
        response = _request_json(client, "GET", base, headers=headers, params=params)
        # Gmail may omit messages on an empty page.
        for item in _page_items(response.get("messages", [])):
            remote_id = item.get("id")
            if not isinstance(remote_id, str) or not remote_id:
                raise ProviderError("郵箱服務回傳無效的郵件識別碼。")
            if remote_id not in seen_ids:
                seen_ids.add(remote_id)
                yield remote_id
        page_token = _next_page(response, "nextPageToken")
        if not page_token:
            return


def _fetch_gmail(client, credentials, limit, days):
    headers = {"Authorization": "Bearer " + credentials["access_token"]}
    base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    cutoff = _cutoff(days)
    messages = []
    for remote_id in _gmail_message_ids(client, base, headers, cutoff, limit):
        raw = _request_json(client, "GET", base + "/" + quote(str(remote_id), safe=""), headers=headers, params={"format": "full"}, missing_ok=True)
        if raw is None or "INBOX" not in raw.get("labelIds", []):
            continue  # It can be deleted or moved after the list request.
        payload = raw.get("payload", {})
        header_pairs = [(str(x.get("name", "")).lower(), x.get("value", "")) for x in payload.get("headers", []) if isinstance(x, dict)]
        mail_headers = dict(header_pairs)
        status = {"truncated": False}
        plain, htmls, attachments = _gmail_body(payload, status=status)
        plain = [text for text in plain if text.strip()]
        htmls = [text for text in htmls if text.strip()]
        combined = "\n\n".join(plain or htmls)
        body = _clean(combined, MAX_BODY)
        selected_truncated = status.get("plain_truncated") if plain else (status.get("html_truncated") if htmls else status.get("truncated"))
        status["truncated"] = bool(status.get("structure_truncated") or selected_truncated or len(combined) > MAX_BODY)
        if not body and (not attachments or status["truncated"]):
            body = _clean(raw.get("snippet", ""), MAX_BODY)
            if body:
                status["truncated"] = True
        try:
            received = _date(int(raw["internalDate"]) / 1000)
        except (KeyError, TypeError, ValueError):
            received = _date(mail_headers.get("date"))
        if not _within_cutoff(received, cutoff):
            continue
        messages.append({"remote_id": str(remote_id), "subject": _decode_header(mail_headers.get("subject")),
                         "reply_thread_id": _clean(raw.get("threadId"), 512), **_reply_headers(header_pairs),
                         "sender": _decode_header(mail_headers.get("from")), "recipients": _decode_header(mail_headers.get("to")),
                         "received_at": received, "body": body, "snippet": _clean(re.sub(r"\s+", " ", body), 300),
                         "unread": "UNREAD" in raw.get("labelIds", []), "attachments": attachments[:100], "body_truncated": status["truncated"]})
        if len(messages) == limit:
            break
    return messages


def _graph_page(url, *, expected_path=None):
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.hostname == "graph.microsoft.com" and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment and parsed.path.startswith("/v1.0/me/")
        if expected_path is not None:
            valid = valid and parsed.path == expected_path
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ProviderError("郵箱分頁回應不符合預期。")
    return url


def _graph_attachments(client, headers, remote_id):
    """Metadata only: $select excludes contentBytes/item; follow bounded pages."""
    url = "https://graph.microsoft.com/v1.0/me/messages/" + quote(remote_id, safe="") + "/attachments"
    expected_path = urlsplit(url).path
    fields = "name,contentType,size"
    params = {"$select": fields}
    attachments, seen = [], set()
    while url and len(attachments) < 100:
        if url in seen or len(seen) >= 100:
            raise ProviderError("附件分頁回應重複，請稍後重試。")
        seen.add(url)
        info = _request_json(client, "GET", _graph_page(url, expected_path=expected_path), headers=headers, params=params, missing_ok=True)
        if info is None:
            return None
        params = None
        for item in _page_items(info.get("value"))[:100 - len(attachments)]:
            attachments.append({"filename": _clean(item.get("name"), 1000), "content_type": _clean(item.get("contentType"), 120), "size": _safe_size(item.get("size"))})
        url = _next_page(info, "@odata.nextLink")
        if url:
            _graph_page(url, expected_path=expected_path)
            # Never follow a server page that would drop the metadata-only projection.
            selected = parse_qs(urlsplit(url).query).get("$select", [])
            if len(selected) != 1 or set(selected[0].split(",")) != set(fields.split(",")):
                raise ProviderError("附件分頁未保留安全的欄位範圍。")
    return attachments


def _fetch_microsoft(client, credentials, limit, days):
    headers = {"Authorization": "Bearer " + credentials["access_token"], "Prefer": 'outlook.body-content-type="text", IdType="ImmutableId"'}
    cutoff = _cutoff(days)
    since = cutoff.isoformat().replace("+00:00", "Z")
    params = {"$select": "id,subject,from,replyTo,toRecipients,receivedDateTime,body,bodyPreview,isRead,hasAttachments,webLink",
              "$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime desc", "$top": min(limit, 100)}
    url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
    messages, seen, seen_ids = [], set(), set()
    while url and len(messages) < limit:
        if url in seen or len(seen) >= max(100, limit):
            # Never publish a partial cache as a successful sync when pagination
            # loops or keeps returning pages without enough usable messages.
            raise ProviderError("郵箱分頁回應重複或超出限制，請稍後重試。")
        seen.add(url)
        response = _request_json(client, "GET", _graph_page(url), headers=headers, params=params)
        params = None
        for raw in _page_items(response.get("value")):
            if len(messages) >= limit:
                break
            remote_id = raw.get("id")
            if not isinstance(remote_id, str) or not remote_id:
                raise ProviderError("郵箱服務回傳無效的郵件識別碼。")
            if remote_id in seen_ids:
                continue
            seen_ids.add(remote_id)
            raw_body = raw.get("body") or {}
            body_status = {"truncated": False}
            if str(raw_body.get("contentType", "")).lower() == "html":
                body = _plain_html(raw_body.get("content", ""), body_status)
            else:
                content = raw_body.get("content") or ""
                body = _clean(content, MAX_BODY)
                body_status["truncated"] = len(content) > MAX_BODY
            body_truncated = body_status["truncated"]
            if not body and raw.get("bodyPreview"):
                body = _clean(raw["bodyPreview"], MAX_BODY)
                body_truncated = True
            received_at = _date(raw.get("receivedDateTime"))
            if not _within_cutoff(received_at, cutoff):
                continue
            attachments = []
            if raw.get("hasAttachments"):
                attachments = _graph_attachments(client, headers, remote_id)
                if attachments is None:
                    continue  # Message deleted between listing and attachment metadata.
            sender = (raw.get("from") or {}).get("emailAddress") or {}
            sender_value = f"{sender.get('name', '')} <{sender.get('address', '')}>"
            recipients = ", ".join(str((x.get("emailAddress") or {}).get("address", "")) for x in (raw.get("toRecipients") or [])[:100])
            message = {"remote_id": remote_id, "subject": _clean(raw.get("subject")), "sender": _clean(sender_value), "recipients": _clean(recipients),
                       "reply_recipients": _graph_reply_recipients(raw),
                       "received_at": received_at, "body": body, "snippet": _clean(raw.get("bodyPreview") or re.sub(r"\s+", " ", body), 300),
                       "unread": not bool(raw.get("isRead")), "attachments": attachments, "body_truncated": body_truncated}
            # Only provider-owned webmail links are allowed, never arbitrary URLs from data.
            link = raw.get("webLink", "")
            try:
                parsed = urlsplit(link)
                if parsed.scheme == "https" and parsed.hostname in {"outlook.office365.com", "outlook.office.com", "outlook.live.com"} and not parsed.username and not parsed.password:
                    message["source_url"] = link[:4000]
            except ValueError:
                pass
            messages.append(message)
        url = _next_page(response, "@odata.nextLink") if len(messages) < limit else None
    return messages


def _public_addresses(host, port, *, smtp=False):
    protocol = "SMTP" if smtp else "IMAP"
    if not isinstance(host, str) or not host or len(host) > 253 or any(c in host for c in "/\\@\x00\r\n\t "):
        raise ProviderError(f"{protocol} 主機名稱無效。")
    if smtp and port not in (465, 587):
        raise ProviderError("SMTP 僅支援 465 TLS 或 587 STARTTLS。")
    if not smtp and port != 993:
        raise ProviderError("本版僅支援使用 TLS 的 IMAP 993 連接埠。")
    try:
        host = host.rstrip(".").encode("idna").decode("ascii")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError()
        for _, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise ProviderError(f"僅允許公開網路的 {protocol} 主機；不支援內網、回環或保留位址。")
        return host, addresses
    except ProviderError:
        raise
    except (ValueError, UnicodeError, OSError):
        raise ProviderError(f"無法解析公開 {protocol} 主機，請核對伺服器設定。") from None


def _pinned_socket(addresses, timeout, host, tls_context=None):
    for family, kind, proto, _, address in addresses:
        try:
            sock = socket.socket(family, kind, proto)
        except OSError:
            continue
        try:
            sock.settimeout(timeout)
            sock.connect(address)
            return tls_context.wrap_socket(sock, server_hostname=host) if tls_context else sock
        except OSError:
            sock.close()
    raise ProviderError("無法連接郵箱伺服器，請核對主機與 TLS 設定。")


class _PinnedIMAPSSL(imaplib.IMAP4_SSL):
    """Connect only to the IPs validated before TLS; no second DNS resolution."""
    def __init__(self, host, port=993):
        host, self._addresses = _public_addresses(host, port)
        super().__init__(host=host, port=port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT)

    def _create_socket(self, timeout):
        return _pinned_socket(self._addresses, timeout, self.host, self.ssl_context)

    def read(self, size):
        if size > MAX_WIRE_BODY + MAX_HEADER:
            raise ProviderError("IMAP 回應超出安全大小限制。")
        return super().read(size)


def _imap_mailbox_name(value):
    """RFC 3501 modified UTF-7, including literal ampersand handling."""
    parts, non_ascii = [], []

    def flush():
        if non_ascii:
            encoded = base64.b64encode("".join(non_ascii).encode("utf-16-be")).decode("ascii")
            parts.append("&" + encoded.rstrip("=").replace("/", ",") + "-")
            non_ascii.clear()

    for character in value:
        if 0x20 <= ord(character) <= 0x7E:
            flush()
            parts.append("&-" if character == "&" else character)
        else:
            non_ascii.append(character)
    flush()
    return "".join(parts)


def _logout_imap(client):
    if client is not None:
        try:
            client.logout()
        except Exception:
            pass  # Cleanup must not replace the result of the mailbox operation.


def _imap_open(credentials):
    username, password = credentials.get("username") or credentials.get("email"), credentials.get("password")
    folder = credentials.get("folder") or "INBOX"
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password or not isinstance(folder, str) or any(c in username + password + folder for c in "\r\n\x00") or len(folder) > 200:
        raise ProviderError("郵箱帳號、授權碼或資料夾設定無效。")
    client = None
    try:
        client = _PinnedIMAPSSL(credentials.get("host", ""), int(credentials.get("port", 993)))
        # imaplib quotes passwords, but passes usernames through verbatim.
        quoted_username = '"' + username.replace("\\", "\\\\").replace('"', '\\"') + '"'
        client.login(quoted_username, password)
        # NetEase requires an RFC 2971 client ID on some mailboxes. No personal data.
        if credentials.get("host", "").lower() in _NETEASE_HOSTS and any(cap in client.capabilities for cap in ("ID", b"ID")):
            if "ID" not in imaplib.Commands:
                imaplib.Commands["ID"] = ("AUTH", "SELECTED")
            client._simple_command("ID", '("name" "MailHarbor" "version" "1.0")')
        encoded_folder = _imap_mailbox_name(folder)
        typ, _ = client.select('"' + encoded_folder.replace("\\", "\\\\").replace('"', '\\"') + '"', readonly=True)
        if typ != "OK":
            raise ProviderError("無法唯讀開啟指定資料夾，請核對名稱與 IMAP 權限。")
        return client
    except ProviderError:
        _logout_imap(client)
        raise
    except (imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError):
        _logout_imap(client)
        raise ProviderError("IMAP 連線失敗，請檢查帳號、授權碼、服務開關及資料夾設定。") from None


def probe_imap(credentials) -> dict:
    client = _imap_open(credentials)
    try:
        email = credentials.get("email", "")
        if not isinstance(email, str) or "@" not in email:
            raise ProviderError("請提供有效的郵箱地址。")
        username = credentials.get("username") or email
        identity = f"{credentials['host'].lower()}\0{username.lower()}\0{credentials.get('folder') or 'INBOX'}"
        return {"email": email[:320], "provider_uid": hashlib.sha256(identity.encode()).hexdigest()}
    finally:
        _logout_imap(client)


def _imap_tokens(data):
    """Parse the limited IMAP S-expression grammar used by BODYSTRUCTURE."""
    position, count = 0, 0

    def parse(depth=0):
        nonlocal position, count
        count += 1
        if depth > 30 or count > 10000:
            raise ProviderError("郵件結構過於複雜，超出本版限制。")
        while position < len(data) and data[position:position + 1].isspace():
            position += 1
        if position >= len(data):
            raise ValueError("incomplete structure")
        if data[position] == 40:
            position += 1
            values = []
            while True:
                while position < len(data) and data[position:position + 1].isspace():
                    position += 1
                if position >= len(data):
                    raise ValueError("unclosed structure")
                if data[position] == 41:
                    position += 1
                    return values
                values.append(parse(depth + 1))
        if data[position] == 34:
            position += 1
            value = bytearray()
            while position < len(data):
                char = data[position]
                position += 1
                if char == 34:
                    return value.decode("utf-8", errors="replace")
                if char == 92 and position < len(data):
                    char = data[position]; position += 1
                value.append(char)
            raise ValueError("unclosed quoted value")
        if data[position] == 123:
            match = re.match(rb"\{(\d+)\}\r?\n", data[position:])
            if not match:
                raise ValueError("invalid literal")
            position += match.end()
            length = int(match.group(1))
            if length > MAX_HEADER or position + length > len(data):
                raise ValueError("literal too long")
            value = data[position:position + length]; position += length
            return value.decode("utf-8", errors="replace")
        match = re.match(rb"[^\s()]+", data[position:])
        if not match:
            raise ValueError("invalid token")
        position += match.end()
        value = match.group().decode("ascii", errors="replace")
        return None if value.upper() == "NIL" else (int(value) if value.isdigit() else value)

    return parse()


def _parameters(value):
    if not isinstance(value, list):
        return {}
    return {str(value[i]).lower(): value[i + 1] for i in range(0, len(value) - 1, 2)}


def _attachment_name(params, disposition):
    if not isinstance(disposition, list) or not disposition:
        return _decode_header(params.get("name"))
    disposition_params = _parameters(disposition[1]) if len(disposition) > 1 else {}
    filename = _decode_header(disposition_params.get("filename") or params.get("name"))
    return filename or ("attachment" if str(disposition[0]).lower() == "attachment" else "")


def _mime_sections(node, prefix="", depth=0):
    bodies, attachments = [], []
    if depth > 20 or not isinstance(node, list) or not node:
        return bodies, attachments
    if isinstance(node[0], list):
        children = []
        for item in node:
            if not isinstance(item, list):
                break
            children.append(item)
        # A multipart attachment owns its whole subtree, including any text parts.
        fields = node[len(children):]
        if len(fields) >= 2:
            filename = _attachment_name(_parameters(fields[1]), fields[2] if len(fields) > 2 else None)
            if filename:
                return [], [{"filename": filename, "content_type": _clean("multipart/" + str(fields[0]).lower(), 120)}]
        for index, child in enumerate(children[:100], 1):
            child_prefix = f"{prefix}.{index}" if prefix else str(index)
            a, b = _mime_sections(child, child_prefix, depth + 1)
            bodies.extend(a); attachments.extend(b)
        return bodies, attachments
    if len(node) < 7:
        return bodies, attachments
    mime = f"{str(node[0]).lower()}/{str(node[1]).lower()}"
    params = _parameters(node[2])
    # RFC 3501: disposition follows MD5, whose index depends on text/message fields.
    disposition_index = 9 if str(node[0]).lower() == "text" else (11 if mime == "message/rfc822" else 8)
    disposition = node[disposition_index] if len(node) > disposition_index else None
    filename = _attachment_name(params, disposition)
    if filename or mime not in {"text/plain", "text/html"}:
        attachments.append({"filename": filename or "attachment", "content_type": mime[:120], "size": _safe_size(node[6])})
    else:
        bodies.append({"section": prefix or "1", "mime": mime, "charset": str(params.get("charset") or "utf-8"), "encoding": str(node[5] or "7bit"), "size": _safe_size(node[6])})
    return bodies, attachments


def _response_has_uid(data, uid):
    uid_field = rb"(?:\(|\s)UID\s+" + re.escape(uid) + rb"(?=[\s)]|$)"
    for part in data or []:
        metadata = part[0] if isinstance(part, tuple) else part
        if isinstance(metadata, bytes) and re.search(uid_field, metadata, re.I):
            return True
    return False


def _fetch_literal(client, uid, section, cap):
    typ, data = client.uid("fetch", uid, f"(BODY.PEEK[{section}]<0.{cap}>)")
    if typ != "OK":
        raise ProviderError("無法讀取郵件，請稍後重試。")
    field = rb"\bBODY\[" + re.escape(section.encode("ascii")) + rb"\](?:<0>)?\s+"
    for part in data or []:
        if isinstance(part, tuple) and isinstance(part[1], bytes) and re.search(field, part[0], re.I):
            return part[1][:cap]
        if isinstance(part, bytes):
            # A short IMAP nstring may be quoted instead of a literal.
            match = re.search(field + rb'(NIL|"((?:\\.|[^"\\])*)")', part, re.I)
            if match:
                return re.sub(rb'\\(["\\])', rb'\1', match.group(2) or b"")[:cap]
    if _response_has_uid(data, uid):
        raise ProviderError("郵箱未回傳要求的郵件內容，請稍後重試。")
    # A UID expunged since SEARCH returns OK with no FETCH data.
    return None


def _fetch_imap(credentials, limit, days):
    client = _imap_open(credentials)
    try:
        uidvalidity = client.response("UIDVALIDITY")[1]
        if not uidvalidity or not isinstance(uidvalidity[0], bytes) or not uidvalidity[0].isdigit():
            raise ProviderError("郵箱未提供有效的 UIDVALIDITY，無法安全識別郵件。")
        validity = uidvalidity[0].decode("ascii")
        cutoff = _cutoff(days)
        # IMAP SINCE compares date only, ignoring time and zone. Widen by one day,
        # then filter the full INTERNALDATE timestamp before downloading any body.
        since_date = cutoff - timedelta(days=1)
        months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
        since = f"{since_date.day:02d}-{months[since_date.month - 1]}-{since_date.year}"
        typ, data = client.uid("search", None, "SINCE", since)
        if typ != "OK":
            raise ProviderError("無法搜尋指定日期範圍的郵件。")
        if not data or not isinstance(data[0], bytes):
            raise ProviderError("郵箱未提供完整的搜尋結果，請稍後重試。")
        uids = data[0].split()
        if any(not uid.isdigit() or len(uid) > 10 for uid in uids):
            raise ProviderError("郵箱服務回傳無效的郵件識別碼。")
        messages = []
        for checked, uid in enumerate(sorted(set(uids), key=int, reverse=True)):
            # Bound retries when most search results disappear or fall outside the exact cutoff.
            if checked >= limit + max(100, limit):
                raise ProviderError("無法在同步限制內確認足夠的郵件，請稍後重試。")
            typ, structure_data = client.uid("fetch", uid, "(BODYSTRUCTURE FLAGS INTERNALDATE)")
            if typ != "OK":
                raise ProviderError("無法讀取郵件結構，請稍後重試。")
            structure_bytes = b" ".join((part[0] + b"\r\n" + part[1]) if isinstance(part, tuple) else (part or b"") for part in structure_data)
            if len(structure_bytes) > MAX_HEADER:
                raise ProviderError("郵件結構超出大小限制。")
            match = re.search(rb"BODYSTRUCTURE\s+(\()", structure_bytes, re.I)
            if not match:
                if _response_has_uid(structure_data, uid):
                    raise ProviderError("郵箱未回傳要求的郵件結構，請稍後重試。")
                continue
            try:
                structure = _imap_tokens(structure_bytes[match.start(1):])
            except (ValueError, IndexError, TypeError):
                raise ProviderError("無法解析郵件 MIME 結構。") from None
            sections, attachments = _mime_sections(structure)
            header_bytes = _fetch_literal(client, uid, "HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID REPLY-TO REFERENCES IN-REPLY-TO)", MAX_HEADER)
            if header_bytes is None:
                continue
            header = BytesParser(policy=policy.default).parsebytes(header_bytes, headersonly=True)
            internal = re.search(rb'INTERNALDATE\s+"([^"]+)"', structure_bytes, re.I)
            received = _date(internal.group(1).decode("ascii", errors="replace") if internal else None, _date(header.get("Date")))
            if not _within_cutoff(received, cutoff):
                continue
            # Prefer text/plain, never fetch attachment sections or entire RFC822 messages.
            selected = [p for p in sections if p["mime"] == "text/plain"] or [p for p in sections if p["mime"] == "text/html"]
            content = []
            remaining = MAX_WIRE_BODY
            body_truncated = len(selected) > 10
            disappeared = False
            for part in selected[:10]:
                if remaining <= 0:
                    body_truncated = True
                    break
                cap = min(remaining, MAX_WIRE_BODY)
                body_bytes = _fetch_literal(client, uid, part["section"], cap)
                if body_bytes is None:
                    disappeared = True
                    break
                if part["size"] > len(body_bytes):
                    body_truncated = True
                remaining -= len(body_bytes)
                mime_headers = f"Content-Type: {part['mime']}; charset=\"{part['charset']}\"\r\nContent-Transfer-Encoding: {part['encoding']}\r\n\r\n".encode("ascii", errors="replace")
                # Reject header injections from untrusted BODYSTRUCTURE fields.
                if any(c in part["charset"] + part["encoding"] for c in "\r\n\x00"):
                    raise ProviderError("郵件編碼資訊無效。")
                parsed_body = BytesParser(policy=policy.default).parsebytes(mime_headers + body_bytes)
                decoded_bytes = parsed_body.get_payload(decode=True) or b""
                try:
                    decoded = decoded_bytes.decode(part["charset"], errors="replace")
                except (LookupError, UnicodeError):
                    decoded = decoded_bytes.decode("utf-8", errors="replace")
                if part["mime"] == "text/html":
                    html_status = {"truncated": False}
                    content.append(_plain_html(decoded, html_status))
                    body_truncated = body_truncated or html_status["truncated"]
                else:
                    body_truncated = body_truncated or len(decoded) > MAX_BODY
                    content.append(_clean(decoded, MAX_BODY))
            if disappeared:
                continue
            combined = "\n\n".join(content)
            body_truncated = body_truncated or len(combined) > MAX_BODY
            body = _clean(combined, MAX_BODY)
            flags = re.search(rb"FLAGS\s+\(([^)]*)\)", structure_bytes, re.I)
            messages.append({"remote_id": f"{validity}:{uid.decode('ascii')}", "subject": _decode_header(header.get("Subject")),
                             **(_reply_headers(header.raw_items()) if len(header_bytes) < MAX_HEADER and not header.defects else {"reply_recipients": []}),
                             "sender": _decode_header(header.get("From")), "recipients": _decode_header(header.get("To")),
                             "received_at": received, "snippet": _clean(re.sub(r"\s+", " ", body), 300), "body": body,
                             "unread": not bool(flags and re.search(rb"(?:^|\s)\\Seen(?:$|\s)", flags.group(1), re.I)), "attachments": attachments[:100], "body_truncated": body_truncated})
            if len(messages) == limit:
                break
        return messages
    except ProviderError:
        raise
    except (imaplib.IMAP4.error, OSError, ValueError, TypeError, UnicodeError, IndexError, KeyError):
        raise ProviderError("IMAP 同步未完成，請稍後重試或核對郵箱設定。") from None
    finally:
        _logout_imap(client)


def fetch_messages(provider, credentials, settings, limit=200, days=30, *, on_credentials_updated=None) -> tuple[list[dict], dict]:
    try:
        limit = max(1, min(int(limit), 1000))
        days = max(1, min(int(days), 365))
    except (ValueError, TypeError, OverflowError):
        raise ProviderError("同步範圍設定無效。") from None
    try:
        if provider in {"gmail", "microsoft"}:
            updated = _refresh(provider, credentials, settings)
            if on_credentials_updated is not None:
                on_credentials_updated(updated)
            with _client() as client:
                messages = _fetch_gmail(client, updated, limit, days) if provider == "gmail" else _fetch_microsoft(client, updated, limit, days)
            return messages, updated
        if provider in _PRESETS:
            effective = dict(credentials)
            expected_host = _PRESETS[provider].host
            if expected_host:
                effective["host"] = expected_host
            return _fetch_imap(effective, limit, days), dict(credentials)
        raise ProviderError("本版尚未支援此郵箱服務。")
    except ProviderError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, IndexError, UnicodeError, RecursionError):
        raise ProviderError("郵件服務回應格式無法處理，請稍後重試。") from None


def reply_unavailable_reason(provider, credentials):
    if provider in ("gmail", "microsoft"):
        scopes = set(str(credentials.get("scope", "")).lower().split())
        send = {"https://www.googleapis.com/auth/gmail.send"} if provider == "gmail" else {"mail.send", "https://graph.microsoft.com/mail.send"}
        return None if credentials.get("allow_reply") is True and scopes & send else "reauth_required"
    if provider == "custom":
        return None if credentials.get("smtp_host") else "smtp_not_configured"
    return None if provider in _PRESETS and _PRESETS[provider].smtp_host else "unsupported"


def _require_reply_access(provider, credentials):
    reason = reply_unavailable_reason(provider, credentials)
    if reason == "reauth_required":
        raise ProviderError("請重新連接郵箱並授予寄信權限。", code=reason, reauth_required=True)
    if reason == "smtp_not_configured":
        raise ProviderError("請在自訂郵箱連線中設定 SMTP 主機與連接埠。", code=reason)
    if reason:
        raise ProviderError("此郵箱服務不支援回覆。", code=reason)


def _delivery_unknown():
    return ProviderError("無法確認郵箱服務是否已接受回覆，請先檢查寄件備份。", code="delivery_unknown")


def _submit_http_reply(client, url, headers, payload):
    import httpx

    try:
        with client.stream("POST", url, headers=headers, json=payload) as response:
            # Graph replies have an empty 202 body. Gmail's 200 is also acceptance.
            if 200 <= response.status_code < 300:
                return
            if response.status_code >= 500 or response.status_code == 408:
                raise _delivery_unknown()
            raise _http_error(response.status_code)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise ProviderError("無法連接寄信服務，請稍後重試。", code="network_error") from None
    except httpx.HTTPError:
        raise _delivery_unknown() from None


def _mime_reply(original, recipients, body, *, keep_subject=False):
    sender = original.get("account_email")
    if not sender or _mailboxes(sender) != [sender]:
        raise ProviderError("寄件郵箱地址無效。", code="invalid_sender")
    international = not sender.isascii() or any(not address.isascii() for address in recipients)
    msg = EmailMessage(policy=policy.SMTPUTF8 if international else policy.SMTP)
    msg["From"], msg["To"] = sender, ", ".join(recipients)
    subject = re.sub(r"[\x00-\x1f\x7f]+", " ", str(original.get("subject") or "")).strip()
    msg["Subject"] = subject if keep_subject or subject.lower().startswith("re:") else "Re: " + subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    parent = original.get("reply_message_id")
    if parent and _MESSAGE_ID.fullmatch(parent):
        msg["In-Reply-To"] = parent
        refs = [item for item in original.get("reply_references", []) if _MESSAGE_ID.fullmatch(item) and item != parent]
        msg["References"] = " ".join([*refs, parent])
    msg.set_content(body, cte="base64")
    return msg


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host, port):
        host, self._addresses = _public_addresses(host, port, smtp=True)
        self._tls = ssl.create_default_context()
        super().__init__(host=host, port=port, local_hostname="localhost", timeout=TIMEOUT)
        if port == 587:
            try:
                self.starttls(context=self._tls)
            except Exception:
                self.close()
                raise

    def _get_socket(self, host, port, timeout):
        return _pinned_socket(self._addresses, timeout, host, self._tls if port == 465 else None)


def _submit_smtp_reply(provider, credentials, msg, recipients):
    preset = _PRESETS[provider]
    if provider == "custom":
        host, port = credentials.get("smtp_host"), credentials.get("smtp_port", preset.smtp_port)
    else:
        host, port = preset.smtp_host, preset.smtp_port
    username = credentials.get("username") or credentials["email"]
    if provider == "icloud":
        username = credentials["email"]
    wire = msg.as_bytes()
    client = None
    try:
        client = _PinnedSMTP(host, port)
        client.login(username, credentials["password"])
        options = []
        if msg.policy.utf8:
            if not client.has_extn("smtputf8"):
                raise ProviderError("SMTP 服務不支援此國際化郵箱地址。")
            options = ["SMTPUTF8", "BODY=8BITMIME"]
        status, _ = client.mail(str(msg["From"]), options)
        if status != 250:
            raise ProviderError("SMTP 服務拒絕寄件地址。")
        for address in recipients:
            status, _ = client.rcpt(address)
            if status not in (250, 251, 252):
                raise ProviderError("SMTP 服務拒絕回覆收件人，郵件未寄出。")
        # Send DATA only after every recipient was accepted.
        try:
            status, _ = client.data(wire)
        except smtplib.SMTPResponseException:
            raise ProviderError("SMTP 服務拒絕此回覆。") from None
        except OSError:
            raise _delivery_unknown() from None
        if status != 250:
            raise ProviderError("SMTP 服務拒絕此回覆。")
    except smtplib.SMTPAuthenticationError:
        raise ProviderError("SMTP 驗證失敗，請檢查寄信權限與授權碼。") from None
    except (OSError, UnicodeError, ValueError):
        raise ProviderError("無法透過安全 SMTP 連線寄信，請檢查伺服器設定。") from None
    finally:
        if client:
            try:
                client.close()
            except OSError:
                pass  # A cleanup error cannot reverse a confirmed DATA result.


def send_reply(provider, credentials, settings, message, body, *, on_credentials_updated=None):
    """Submit one reply using only the server's original message and mailbox identity."""
    _require_reply_access(provider, credentials)
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY or "\x00" in body:
        raise ProviderError("回覆內容無效。", code="invalid_reply")
    if "reply_recipients" not in message:
        raise ProviderError("請先同步原郵件，再回覆。", code="sync_required")
    recipients = message["reply_recipients"]
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 100 or any(_mailboxes(addr) != [addr] for addr in recipients):
        raise ProviderError("原郵件沒有可驗證的回覆地址。", code="invalid_recipient")
    if provider not in ("gmail", "microsoft"):
        _submit_smtp_reply(provider, credentials, _mime_reply(message, recipients, body), recipients)
        return
    updated = _refresh(provider, credentials, settings)
    if on_credentials_updated and updated != credentials:
        on_credentials_updated(updated)
    _require_reply_access(provider, updated)
    headers = {"Authorization": "Bearer " + updated["access_token"]}
    if provider == "gmail":
        if not message.get("reply_thread_id") or not _MESSAGE_ID.fullmatch(message.get("reply_message_id") or ""):
            raise ProviderError("原郵件缺少有效的 Gmail 會話或郵件識別碼。", code="thread_unavailable")
        # Gmail requires matching subjects as well as threadId and RFC reply headers.
        mime = _mime_reply(message, recipients, body, keep_subject=True)
        url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
        payload = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii"), "threadId": message["reply_thread_id"]}
    else:
        remote = quote(message["remote_id"], safe="")
        if remote in ("", ".", ".."):
            raise ProviderError("原郵件識別碼無效。", code="invalid_reply")
        url = f"https://graph.microsoft.com/v1.0/me/messages/{remote}/reply"
        headers["Prefer"] = 'IdType="ImmutableId"'
        payload = {"message": {"toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
                               "body": {"contentType": "Text", "content": body}}}
    with _client() as client:
        _submit_http_reply(client, url, headers, payload)
