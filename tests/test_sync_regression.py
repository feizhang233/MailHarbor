"""Synchronization regressions using a synthetic mailbox and mocked provider HTTP."""
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from mailhub.app import create_app
from mailhub.config import Settings
from mailhub.store import Store


class SyncRegressionTests(unittest.TestCase):
    def test_graph_pagination_failure_preserves_previous_snapshot_and_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(key=Fernet.generate_key().decode(), admin_hash="synthetic-test-hash",
                                data_dir=directory, microsoft_client_id="synthetic-client",
                                microsoft_client_secret="synthetic-secret")
            store = Store(directory, settings.key)
            credentials = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
                           "expires_at": time.time() + 3600}
            account = store.save_account("microsoft", {"email": "synthetic@example.com", "provider_uid": "synthetic"},
                                         credentials)
            store.replace_snapshot(account["id"], [{"remote_id": "previous", "received_at": "2026-10-01T00:00:00+00:00",
                                                     "subject": "Previous successful snapshot", "body": "Synthetic cached text"}],
                                   credentials, sync_days=7, sync_limit=50)
            previous = store.account(account["id"])
            next_url = "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$skiptoken=repeat"
            response = {"value": [], "@odata.nextLink": next_url}
            failed = threading.Event()
            set_status = store.set_status

            def record_status(account_id, status, error=None, generation=None):
                set_status(account_id, status, error, generation)
                if status == "error":
                    failed.set()

            app = create_app(settings, store)
            with patch("mailhub.providers._request_json", return_value=response) as request, \
                 patch.object(store, "set_status", side_effect=record_status), \
                 TestClient(app, base_url="http://localhost:8080"):
                self.assertTrue(failed.wait(3), "An incomplete provider response must mark sync as failed")
            current = store.account(account["id"])
            self.assertEqual(current["status"], "error")
            self.assertEqual(current["last_sync"], previous["last_sync"])
            self.assertEqual((current["snapshot_days"], current["snapshot_limit"]), (7, 50))
            self.assertEqual([m["remote_id"] for m in store.search()["messages"]], ["previous"])
            self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
