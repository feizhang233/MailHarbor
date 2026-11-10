"""Submit a reply from a connected mailbox and record the provider's outcome."""
import threading
from weakref import WeakValueDictionary

from . import providers


class ReplyError(ValueError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


_UNAVAILABLE = {
    "reauth_required": "請重新連接此郵箱並授予寄信權限。",
    "smtp_not_configured": "請為此郵箱設定 SMTP 主機與連接埠。",
    "sync_required": "請先同步此郵箱，以取得原郵件的回覆資料。",
    "invalid_recipient": "原郵件沒有可用的回覆地址。",
    "thread_unavailable": "原郵件缺少識別碼，無法在這個郵件會話中回覆。",
    "unsupported": "此郵箱服務不支援回覆。",
}


class MailboxReplies:
    def __init__(self, store):
        self.store = store
        self._locks = WeakValueDictionary()
        self._guard = threading.Lock()

    def account_lock(self, account_id):
        # Sync and replies both refresh credentials. Keep the lock until the
        # blocking provider call finishes, including when its HTTP client disconnects.
        with self._guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def availability(self, message, account):
        recipients = message.get("reply_recipients", [])
        reason = providers.reply_unavailable_reason(account["provider"], account["credentials"])
        if not reason and "reply_recipients" not in message:
            reason = "sync_required"
        if not reason and account["provider"] == "gmail" and not (message.get("reply_thread_id") and message.get("reply_message_id")):
            reason = "thread_unavailable"
        if not reason and not recipients:
            reason = "invalid_recipient"
        return {"available": reason is None, "recipients": recipients, "reason": reason}

    def message_detail(self, message_id, user_id):
        message = self.store.message(message_id, user_id=user_id)
        if message:
            account = self.store.account(message["account_id"], user_id=user_id)
            if account:
                message["reply"] = self.availability(message, account)
                return message
        return None

    def submit(self, settings, user_id, message_id, request_id, body):
        if not body.strip() or len(body) > 12000 or "\x00" in body:
            raise ReplyError(422, "invalid_body", "請填寫 1 至 12000 字的回覆內容。")
        try:
            body.encode("utf-8")
        except UnicodeError:
            raise ReplyError(422, "invalid_body", "回覆內容包含無效字元。") from None

        state, account_id = self.store.begin_reply(message_id, request_id, body, user_id=user_id)
        if state == "sent":
            return {"accepted": True}
        if state == "missing":
            raise ReplyError(404, "not_found", "郵件已不在目前快取，請重新搜尋。")
        if state == "conflict":
            raise ReplyError(409, "request_conflict", "這個請求編號已用於另一封回覆。")
        if state == "pending":
            raise ReplyError(409, "reply_pending", "回覆正在處理或尚未確認，請先查看寄件備份。")
        if state == "unknown":
            raise ReplyError(409, "delivery_unknown", "無法確認郵箱是否已接受回覆，請先核對寄件記錄。")
        if state != "new":
            raise RuntimeError("Unexpected reply state")

        outcome = "failed"
        try:
            with self.account_lock(account_id):
                message = self.store.message(message_id, user_id=user_id)
                if not message:
                    raise ReplyError(404, "not_found", "郵件已不在目前快取，請重新搜尋。")
                account = self.store.account(account_id, user_id=user_id)
                if not account:
                    raise ReplyError(404, "not_found", "找不到郵箱。")
                reason = self.availability(message, account)["reason"]
                if reason:
                    raise ReplyError(400, reason, _UNAVAILABLE[reason])
                outcome = "unknown"
                try:
                    providers.send_reply(account["provider"], account["credentials"], settings, message, body,
                        on_credentials_updated=lambda value: self.store.update_credentials(
                            account_id, value, account["generation"], user_id=user_id))
                except providers.ProviderError as exc:
                    if exc.code == "delivery_unknown":
                        raise ReplyError(409, exc.code, str(exc)) from None
                    outcome = "failed"
                    raise ReplyError(400, exc.code, str(exc)) from None
                except Exception:
                    raise ReplyError(409, "delivery_unknown", "無法確認郵箱是否已接受回覆，請先核對寄件記錄。") from None
                outcome = "sent"
        finally:
            self.store.finish_reply(message_id, request_id, outcome, user_id=user_id)
        return {"accepted": True}
