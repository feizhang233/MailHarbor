"""Malformed browser credentials fail as forbidden requests, without mutations."""
import hashlib
import importlib.util
import tempfile
import unittest

from mailhub.config import Settings


HAS_APP = all(importlib.util.find_spec(name) is not None
              for name in ("cryptography", "fastapi", "httpx", "mcp"))


@unittest.skipUnless(HAS_APP, "Application runtime dependencies are not installed")
class CsrfHeaderTests(unittest.TestCase):
    def test_invalid_csrf_headers_are_forbidden_and_do_not_create_tokens(self):
        from cryptography.fernet import Fernet
        from fastapi.testclient import TestClient
        from mailhub.app import create_app

        password, salt = "csrf-test-password", b"csrf-regression-salt"
        password_hash = salt.hex() + ":" + hashlib.scrypt(
            password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=64).hex()
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(Settings(key=Fernet.generate_key().decode(), admin_hash=password_hash,
                                      data_dir=directory))
            with TestClient(app, base_url="http://localhost:8080", raise_server_exceptions=False) as client:
                login = client.post("/api/login", json={"password": password},
                                    headers={"Origin": "http://localhost:8080"})
                self.assertEqual(login.status_code, 200)
                for csrf in (b"", b"wrong-token", b"\xe9", b"\xff"):
                    with self.subTest(csrf=csrf):
                        response = client.post("/api/tokens", json={"name": "rejected"}, headers=[
                            (b"Origin", b"http://localhost:8080"), (b"X-CSRF-Token", csrf)])
                        self.assertEqual(response.status_code, 403, response.text)
                        self.assertEqual(app.state.store.tokens(), [])
                accepted = client.post("/api/tokens", json={"name": "accepted"}, headers={
                    "Origin": "http://localhost:8080", "X-CSRF-Token": login.json()["csrf_token"]})
                self.assertEqual(accepted.status_code, 200, accepted.text)
                self.assertEqual(len(app.state.store.tokens()), 1)


if __name__ == "__main__":
    unittest.main()
