"""Schedule and synchronize mailbox snapshots using the current service settings."""
import asyncio
from contextlib import asynccontextmanager

from . import providers


class MailboxSync:
    def __init__(self, store, get_settings, account_lock):
        self.store = store
        self.get_settings = get_settings
        self.account_lock = account_lock
        self._tasks = {}
        self._repeat = set()
        self._slots = asyncio.Semaphore(3)
        self._wakeup = asyncio.Event()

    def _fetch_snapshot(self, user_id, account_id):
        generation = None
        try:
            # A reply can refresh these credentials too. Hold the shared mailbox
            # lock for the blocking call, even if the scheduling task is cancelled.
            with self.account_lock(account_id):
                account = self.store.account(account_id, user_id=user_id)
                if not account:
                    return
                generation = account["generation"]
                settings = self.get_settings()
                self.store.set_status(account_id, "syncing", None, generation, user_id=user_id)
                messages, credentials = providers.fetch_messages(
                    account["provider"], account["credentials"], settings,
                    limit=settings.sync_limit, days=settings.sync_days,
                    on_credentials_updated=lambda value: self.store.update_credentials(
                        account_id, value, generation, user_id=user_id))
                self.store.replace_snapshot(account_id, messages, credentials, generation,
                                            settings.sync_days, settings.sync_limit, user_id=user_id)
        except providers.ProviderError as exc:
            self.store.set_status(account_id, "error", str(exc)[:500], generation, user_id=user_id)
        except Exception:
            self.store.set_status(account_id, "error", "同步失敗，請稍後重試或重新連接郵箱。", generation, user_id=user_id)

    def schedule(self, user_id, account_id, *, repeat_if_running=False):
        if account_id in self._tasks:
            if repeat_if_running:
                self._repeat.add(account_id)
            return
        self.store.set_status(account_id, "syncing", user_id=user_id)
        self._tasks[account_id] = asyncio.create_task(self._run_account(user_id, account_id))

    async def _run_account(self, user_id, account_id):
        try:
            while True:
                async with self._slots:
                    await asyncio.to_thread(self._fetch_snapshot, user_id, account_id)
                if account_id not in self._repeat:
                    return
                self._repeat.remove(account_id)
                self.store.set_status(account_id, "syncing", user_id=user_id)
        finally:
            self._tasks.pop(account_id, None)
            self._repeat.discard(account_id)

    def settings_changed(self):
        self._wakeup.set()

    async def _poll(self):
        settings_changed = False
        while True:
            for user_id, account_id in await asyncio.to_thread(self.store.all_account_ids):
                self.schedule(user_id, account_id, repeat_if_running=settings_changed)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.get_settings().sync_interval_seconds)
                self._wakeup.clear()
                settings_changed = True
            except asyncio.TimeoutError:
                settings_changed = False

    @asynccontextmanager
    async def running(self):
        poll = asyncio.create_task(self._poll())
        try:
            yield
        finally:
            tasks = (poll, *self._tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            self._repeat.clear()
