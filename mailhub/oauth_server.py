"""Small single-owner OAuth authorization server for the read-only MCP resource.

Public clients use S256 PKCE and dynamic registration. This is not an identity
provider: no OIDC/id_tokens, client secrets, token passthrough or arbitrary URL
fetching. Owner consent is checked by the host application's session validator.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


SCOPE = "mail.read"
REQUEST_TTL = 600
CODE_TTL = 60
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CLIENT_TTL = 90 * 86400
UNAUTHORIZED_CLIENT_TTL = 4 * 3600
MAX_CLIENTS = 300
MAX_REQUESTS = 100
MAX_GRANTS = 100
_PKCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class OAuthError(Exception):
    def __init__(self, error, description, status=400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def valid_redirect_uri(value):
    """Accept exact HTTPS or native-loopback HTTP redirects, never URL patterns."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value or "*" in value:
        return False
    try:
        value.encode("utf-8")
        u = urlsplit(value)
        if not u.hostname or u.username or u.password or u.fragment:
            return False
        _ = u.port
        if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}):
            return False
        # A callback's static query must not collide with authorization response fields.
        if set(parse_qs(u.query, keep_blank_values=True)) & {"code", "error", "state", "iss"}:
            return False
        return True
    except (ValueError, UnicodeError):
        return False


def valid_scope(value):
    return isinstance(value, str) and value.split() == [SCOPE]


def pkce_challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def redirect_with_params(uri, parameters):
    u = urlsplit(uri)
    query = u.query + ("&" if u.query else "") + urlencode(parameters)
    return urlunsplit((u.scheme, u.netloc, u.path, query, ""))


class OAuthServer:
    def __init__(self, settings_or_getter, store, owner_session=None):
        self._settings = settings_or_getter
        self.store = store
        self.owner_session = owner_session
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, redirects BLOB NOT NULL,
                    created_at REAL NOT NULL, last_used_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_requests (
                    id_hash TEXT PRIMARY KEY, payload BLOB NOT NULL,
                    owner_binding TEXT, expires_at REAL NOT NULL,
                    client_id TEXT REFERENCES oauth_clients(id)
                );
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    code_hash TEXT PRIMARY KEY, payload BLOB NOT NULL,
                    client_id TEXT NOT NULL REFERENCES oauth_clients(id),
                    grant_id TEXT NOT NULL, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_grants (
                    id TEXT PRIMARY KEY, client_id TEXT NOT NULL REFERENCES oauth_clients(id),
                    resource TEXT NOT NULL, scope TEXT NOT NULL,
                    created_at REAL NOT NULL, last_used_at REAL,
                    expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth_tokens (
                    token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL, expires_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens(grant_id);
                CREATE TABLE IF NOT EXISTS oauth_limits (
                    bucket TEXT PRIMARY KEY, window_start REAL NOT NULL, count INTEGER NOT NULL
                );
            """)
            if "client_id" not in {row[1] for row in db.execute("PRAGMA table_info(oauth_requests)")}:
                db.execute("ALTER TABLE oauth_requests ADD COLUMN client_id TEXT REFERENCES oauth_clients(id)")
        self.router = self._build_router()

    @property
    def settings(self):
        return self._settings() if callable(self._settings) else self._settings

    @property
    def issuer(self):
        return self.settings.public_url.rstrip("/")

    @property
    def resource(self):
        return self.issuer + "/mcp"

    @property
    def metadata_url(self):
        return self.issuer + "/.well-known/oauth-protected-resource/mcp"

    def _prune(self, db, now):
        db.execute("DELETE FROM oauth_requests WHERE expires_at<=?", (now,))
        # Retain consumed code hashes briefly so a repeated valid code can revoke its grant.
        db.execute("DELETE FROM oauth_codes WHERE expires_at<?", (now - 600,))
        db.execute("DELETE FROM oauth_codes WHERE grant_id IN (SELECT id FROM oauth_grants WHERE revoked=1 OR expires_at<=?)", (now,))
        db.execute("DELETE FROM oauth_grants WHERE revoked=1 OR expires_at<=?", (now,))
        db.execute("DELETE FROM oauth_tokens WHERE kind='access' AND expires_at<=?", (now,))
        db.execute("DELETE FROM oauth_codes WHERE client_id IN (SELECT id FROM oauth_clients WHERE last_used_at<?) AND expires_at<=?", (now - CLIENT_TTL, now))
        db.execute("""DELETE FROM oauth_clients WHERE (last_used_at<? OR created_at<?)
                   AND id NOT IN (SELECT client_id FROM oauth_grants)
                   AND id NOT IN (SELECT client_id FROM oauth_codes)
                   AND id NOT IN (SELECT client_id FROM oauth_requests WHERE client_id IS NOT NULL)""",
                   (now - CLIENT_TTL, now - UNAUTHORIZED_CLIENT_TTL))
        db.execute("DELETE FROM oauth_limits WHERE window_start<?", (now - 86400,))

    def _rate_limit(self, db, bucket, maximum, window=3600):
        now = time.time()
        row = db.execute("SELECT * FROM oauth_limits WHERE bucket=?", (bucket,)).fetchone()
        if not row or row["window_start"] <= now - window:
            db.execute("INSERT OR REPLACE INTO oauth_limits VALUES(?,?,1)", (bucket, now))
        elif row["count"] >= maximum:
            raise OAuthError("temporarily_unavailable", "Too many OAuth requests; retry later.", 429)
        else:
            db.execute("UPDATE oauth_limits SET count=count+1 WHERE bucket=?", (bucket,))

    def _consume_limits(self, limits):
        # Commit counters before the request transaction. Invalid codes/verifiers must
        # still consume limits even when the subsequent OAuth operation rolls back.
        with self.store.db() as db:
            db.execute("DELETE FROM oauth_limits WHERE window_start<?", (time.time() - 86400,))
            for bucket, maximum, window in limits:
                self._rate_limit(db, bucket, maximum, window)

    def register(self, data, peer="unknown"):
        if not isinstance(data, dict):
            raise OAuthError("invalid_client_metadata", "Client metadata must be a JSON object.")
        redirects = data.get("redirect_uris")
        if not isinstance(redirects, list) or not 1 <= len(redirects) <= 10 or not all(valid_redirect_uri(u) for u in redirects):
            raise OAuthError("invalid_redirect_uri", "Provide one to ten exact HTTPS or loopback redirect URIs.")
        if len(set(redirects)) != len(redirects):
            raise OAuthError("invalid_redirect_uri", "Redirect URIs must be unique.")
        if data.get("token_endpoint_auth_method", "none") != "none":
            raise OAuthError("invalid_client_metadata", "Only public clients using S256 PKCE are supported.")
        grants = data.get("grant_types", ["authorization_code", "refresh_token"])
        responses = data.get("response_types", ["code"])
        if (not isinstance(grants, list) or not all(isinstance(item, str) for item in grants)
                or "authorization_code" not in grants or set(grants) - {"authorization_code", "refresh_token"}
                or responses != ["code"]):
            raise OAuthError("invalid_client_metadata", "Only authorization_code and refresh_token grants are supported.")
        if data.get("scope") is not None and not valid_scope(data["scope"]):
            raise OAuthError("invalid_client_metadata", "The only available scope is mail.read.")
        name = data.get("client_name") or "未命名 AI 客戶端"
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or any(ord(c) < 32 for c in name):
            raise OAuthError("invalid_client_metadata", "Invalid client name.")
        try:
            name.encode("utf-8")
        except UnicodeError:
            raise OAuthError("invalid_client_metadata", "Invalid client name encoding.") from None
        self._consume_limits([("register:" + digest(peer), 20, 3600), ("register:global", 60, 3600)])
        now, client_id = time.time(), "mhc_" + secrets.token_urlsafe(24)
        with self.store.db() as db:
            self._prune(db, now)
            if db.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= MAX_CLIENTS:
                raise OAuthError("temporarily_unavailable", "Client registration limit reached.", 429)
            db.execute("INSERT INTO oauth_clients VALUES(?,?,?,?,?)",
                       (client_id, name.strip(), self.store.encrypt(redirects), now, now))
        return {"client_id": client_id, "client_id_issued_at": int(now), "client_name": name.strip(),
                "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], "scope": SCOPE}

    def begin_authorization(self, parameters, peer="unknown"):
        required = ("client_id", "redirect_uri", "response_type", "resource", "code_challenge", "code_challenge_method")
        if any(not isinstance(parameters.get(k), str) or not parameters[k] for k in required):
            raise OAuthError("invalid_request", "Missing authorization parameters.")
        if parameters["response_type"] != "code":
            raise OAuthError("unsupported_response_type", "Only authorization code is supported.")
        if parameters["resource"] != self.resource:
            raise OAuthError("invalid_target", "The resource must match the MCP endpoint exactly.")
        scope = parameters.get("scope", SCOPE)
        if not valid_scope(scope):
            raise OAuthError("invalid_scope", "The only available scope is mail.read.")
        if parameters["code_challenge_method"] != "S256" or not _PKCE.fullmatch(parameters["code_challenge"]):
            raise OAuthError("invalid_request", "A valid S256 PKCE challenge is required.")
        state = parameters.get("state", "")
        if not isinstance(state, str) or len(state) > 2048 or any(ord(c) < 32 for c in state):
            raise OAuthError("invalid_request", "Invalid state.")
        try:
            state.encode("utf-8")
        except UnicodeError:
            raise OAuthError("invalid_request", "Invalid state encoding.") from None
        self._consume_limits([("authorize:" + digest(peer), 60, 3600), ("authorize:global", 200, 3600)])
        now, request_id = time.time(), secrets.token_urlsafe(32)
        with self.store.db() as db:
            self._prune(db, now)
            client = db.execute("SELECT * FROM oauth_clients WHERE id=?", (parameters["client_id"],)).fetchone()
            if not client or parameters["redirect_uri"] not in self.store.decrypt(client["redirects"]):
                # Never redirect to an unvalidated caller-provided URI, even on errors.
                raise OAuthError("invalid_request", "Unknown client or unregistered redirect URI.")
            if db.execute("SELECT COUNT(*) FROM oauth_requests").fetchone()[0] >= MAX_REQUESTS:
                raise OAuthError("temporarily_unavailable", "Too many pending authorization requests.", 429)
            payload = {k: parameters[k] for k in required}
            payload.update(state=state, scope=SCOPE, client_name=client["name"], issuer=self.issuer)
            db.execute("INSERT INTO oauth_requests VALUES(?,?,NULL,?,?)", (digest(request_id), self.store.encrypt(payload), now + REQUEST_TTL, client["id"]))
            db.execute("UPDATE oauth_clients SET last_used_at=? WHERE id=?", (now, client["id"]))
        return request_id

    def consent_details(self, request_id, owner_binding):
        if not isinstance(request_id, str) or len(request_id) > 200:
            raise OAuthError("invalid_request", "Authorization request is invalid.", 404)
        with self.store.db() as db:
            row = db.execute("SELECT * FROM oauth_requests WHERE id_hash=?", (digest(request_id),)).fetchone()
            if not row or row["expires_at"] <= time.time():
                raise OAuthError("invalid_request", "Authorization request expired; restart from your AI client.", 404)
            if row["owner_binding"] and not hmac.compare_digest(row["owner_binding"], owner_binding):
                raise OAuthError("access_denied", "Authorization request belongs to another session.", 403)
            payload = self.store.decrypt(row["payload"])
            if payload["issuer"] != self.issuer or payload["resource"] != self.resource:
                raise OAuthError("invalid_target", "Service origin changed; restart authorization.")
            db.execute("UPDATE oauth_requests SET owner_binding=? WHERE id_hash=?", (owner_binding, digest(request_id)))
            return {"request_id": request_id, "client_name": payload["client_name"],
                    "redirect_uri": payload["redirect_uri"], "scope": SCOPE,
                    "resource": payload["resource"], "expires_at": timestamp(row["expires_at"])}

    def decide_consent(self, request_id, approve, owner_binding):
        if not isinstance(request_id, str) or len(request_id) > 200 or not isinstance(approve, bool):
            raise OAuthError("invalid_request", "Invalid consent decision.")
        with self.store.db() as db:
            row = db.execute("SELECT * FROM oauth_requests WHERE id_hash=?", (digest(request_id),)).fetchone()
            if not row or row["expires_at"] <= time.time():
                raise OAuthError("invalid_request", "Authorization request expired; restart from your AI client.", 404)
            # A prior GET is required to bind the displayed consent to this owner session.
            if not row["owner_binding"] or not hmac.compare_digest(row["owner_binding"], owner_binding):
                raise OAuthError("access_denied", "Read the consent request in this session before deciding.", 403)
            payload = self.store.decrypt(row["payload"])
            if payload["issuer"] != self.issuer or payload["resource"] != self.resource:
                raise OAuthError("invalid_target", "Service origin changed; restart authorization.")
            client = db.execute("SELECT * FROM oauth_clients WHERE id=?", (payload["client_id"],)).fetchone()
            if not client or payload["redirect_uri"] not in self.store.decrypt(client["redirects"]):
                raise OAuthError("invalid_request", "Client registration no longer exists.")
            response = {"iss": self.issuer}
            if payload["state"]:
                response["state"] = payload["state"]
            if approve:
                if db.execute("SELECT COUNT(*) FROM oauth_grants WHERE revoked=0 AND expires_at>?", (time.time(),)).fetchone()[0] >= MAX_GRANTS:
                    raise OAuthError("temporarily_unavailable", "Revoke an old application before adding another.", 429)
                now, grant_id, code = time.time(), secrets.token_hex(16), "mhcode_" + secrets.token_urlsafe(32)
                db.execute("INSERT INTO oauth_grants VALUES(?,?,?,?,?,NULL,?,0)",
                           (grant_id, payload["client_id"], self.resource, SCOPE, now, now + REFRESH_TTL))
                db.execute("INSERT INTO oauth_codes VALUES(?,?,?,?,?,0)",
                           (digest(code), self.store.encrypt(payload), payload["client_id"], grant_id, now + CODE_TTL))
                response["code"] = code
            else:
                response["error"] = "access_denied"
            db.execute("DELETE FROM oauth_requests WHERE id_hash=?", (digest(request_id),))
            return {"redirect_url": redirect_with_params(payload["redirect_uri"], response)}

    def _issue_tokens(self, db, grant):
        now = time.time()
        access, refresh = "mha_" + secrets.token_urlsafe(32), "mhr_" + secrets.token_urlsafe(32)
        expires_in = min(ACCESS_TTL, max(0, int(grant["expires_at"] - now)))
        if expires_in <= 0:
            raise OAuthError("invalid_grant", "Authorization expired; authorize again.")
        db.execute("INSERT INTO oauth_tokens VALUES(?,?,?,?,0)", (digest(access), grant["id"], "access", now + expires_in))
        db.execute("INSERT INTO oauth_tokens VALUES(?,?,?,?,0)", (digest(refresh), grant["id"], "refresh", grant["expires_at"]))
        return {"access_token": access, "token_type": "Bearer", "expires_in": expires_in,
                "refresh_token": refresh, "scope": SCOPE}

    def exchange(self, data, peer="unknown"):
        client_id = data.get("client_id", "")
        if not isinstance(client_id, str) or len(client_id) > 200:
            raise OAuthError("invalid_client", "A registered public client_id is required.", 401)
        if data.get("resource") != self.resource:
            raise OAuthError("invalid_target", "The resource must match the MCP endpoint exactly.")
        if data.get("client_secret") or data.get("client_assertion"):
            raise OAuthError("invalid_client", "This server accepts public clients using PKCE only.", 401)
        if "scope" in data and not valid_scope(data["scope"]):
            raise OAuthError("invalid_scope", "The only available scope is mail.read.")
        self._consume_limits([("token:global", 1000, 600), ("token-peer:" + digest(peer), 200, 600)])
        with self.store.db() as db:
            if not db.execute("SELECT 1 FROM oauth_clients WHERE id=?", (client_id,)).fetchone():
                raise OAuthError("invalid_client", "Unknown client.", 401)
        self._consume_limits([("token-client:" + digest(client_id), 120, 3600)])
        error = None
        response = None
        now = time.time()
        with self.store.db() as db:
            self._prune(db, now)
            client = db.execute("SELECT * FROM oauth_clients WHERE id=?", (client_id,)).fetchone()
            if not client:
                raise OAuthError("invalid_client", "Unknown client.", 401)
            grant_type = data.get("grant_type")
            if grant_type == "authorization_code":
                code, verifier = data.get("code", ""), data.get("code_verifier", "")
                if not isinstance(code, str) or len(code) > 200 or not isinstance(verifier, str) or not _VERIFIER.fullmatch(verifier):
                    raise OAuthError("invalid_grant", "Invalid authorization code or verifier.")
                row = db.execute("SELECT * FROM oauth_codes WHERE code_hash=?", (digest(code),)).fetchone()
                if not row or row["client_id"] != client_id or row["expires_at"] <= now:
                    raise OAuthError("invalid_grant", "Invalid or expired authorization code.")
                payload = self.store.decrypt(row["payload"])
                if (data.get("redirect_uri") != payload["redirect_uri"] or data["resource"] != payload["resource"]
                        or payload["issuer"] != self.issuer or not hmac.compare_digest(pkce_challenge(verifier), payload["code_challenge"])):
                    raise OAuthError("invalid_grant", "Authorization code binding check failed.")
                if row["used"]:
                    # A valid replay proves knowledge of the verifier: revoke the token family.
                    db.execute("UPDATE oauth_grants SET revoked=1 WHERE id=?", (row["grant_id"],))
                    error = OAuthError("invalid_grant", "Authorization code already used; authorize again.")
                else:
                    grant = db.execute("SELECT * FROM oauth_grants WHERE id=? AND revoked=0 AND expires_at>?", (row["grant_id"], now)).fetchone()
                    if not grant or grant["resource"] != self.resource or grant["scope"] != SCOPE:
                        raise OAuthError("invalid_grant", "Authorization is no longer valid.")
                    db.execute("UPDATE oauth_codes SET used=1 WHERE code_hash=?", (digest(code),))
                    response = self._issue_tokens(db, grant)
            elif grant_type == "refresh_token":
                token = data.get("refresh_token", "")
                if not isinstance(token, str) or len(token) > 200:
                    raise OAuthError("invalid_grant", "Invalid refresh token.")
                row = db.execute("""SELECT t.*,g.client_id,g.resource,g.scope,g.revoked,g.expires_at AS grant_expires
                                  FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id
                                  WHERE t.token_hash=? AND t.kind='refresh'""", (digest(token),)).fetchone()
                if (not row or row["client_id"] != client_id or row["resource"] != self.resource
                        or row["scope"] != SCOPE or row["revoked"] or row["expires_at"] <= now or row["grant_expires"] <= now):
                    raise OAuthError("invalid_grant", "Invalid or expired refresh token.")
                if row["used"]:
                    db.execute("UPDATE oauth_grants SET revoked=1 WHERE id=?", (row["grant_id"],))
                    error = OAuthError("invalid_grant", "Refresh token reuse detected; authorize again.")
                else:
                    grant = db.execute("SELECT * FROM oauth_grants WHERE id=?", (row["grant_id"],)).fetchone()
                    db.execute("UPDATE oauth_tokens SET used=1 WHERE token_hash=?", (digest(token),))
                    # Retire preceding access tokens when a family is refreshed.
                    db.execute("DELETE FROM oauth_tokens WHERE grant_id=? AND kind='access'", (grant["id"],))
                    response = self._issue_tokens(db, grant)
            else:
                raise OAuthError("unsupported_grant_type", "Only authorization_code and refresh_token are supported.")
            db.execute("UPDATE oauth_clients SET last_used_at=? WHERE id=?", (now, client_id))
        # Raise after transaction commits so replay-triggered revocation is not rolled back.
        if error:
            raise error
        return response

    def validate_access_token(self, token, resource=None):
        if not isinstance(token, str) or not token.startswith("mha_") or len(token) > 200:
            return False
        expected = resource or self.resource
        now = time.time()
        with self.store.db() as db:
            row = db.execute("""SELECT t.grant_id,t.expires_at,g.resource,g.scope,g.revoked,
                               g.expires_at AS grant_expires FROM oauth_tokens t
                               JOIN oauth_grants g ON g.id=t.grant_id
                               WHERE t.token_hash=? AND t.kind='access'""", (digest(token),)).fetchone()
            if (not row or row["expires_at"] <= now or row["grant_expires"] <= now or row["revoked"]
                    or row["resource"] != expected or row["resource"] != self.resource or row["scope"] != SCOPE):
                return False
            db.execute("UPDATE oauth_grants SET last_used_at=? WHERE id=?", (now, row["grant_id"]))
            return True

    def revoke(self, token, client_id):
        if not isinstance(token, str) or not isinstance(client_id, str) or len(token) > 200 or len(client_id) > 200:
            return
        with self.store.db() as db:
            row = db.execute("""SELECT g.id FROM oauth_tokens t JOIN oauth_grants g ON g.id=t.grant_id
                               WHERE t.token_hash=? AND g.client_id=?""", (digest(token), client_id)).fetchone()
            if row:
                db.execute("UPDATE oauth_grants SET revoked=1 WHERE id=?", (row["id"],))

    def grants(self):
        with self.store.db() as db:
            rows = db.execute("""SELECT g.id,c.name AS client_name,g.created_at,g.last_used_at
                               FROM oauth_grants g JOIN oauth_clients c ON c.id=g.client_id
                               WHERE g.revoked=0 AND g.expires_at>? ORDER BY g.created_at DESC""", (time.time(),))
            return [{"id": row["id"], "client_name": row["client_name"], "created_at": timestamp(row["created_at"]),
                     "last_used_at": timestamp(row["last_used_at"]) if row["last_used_at"] else None} for row in rows]

    def revoke_grant(self, grant_id):
        with self.store.db() as db:
            return bool(db.execute("UPDATE oauth_grants SET revoked=1 WHERE id=? AND revoked=0", (grant_id,)).rowcount)

    def _build_router(self):
        from fastapi import APIRouter, Request
        from fastapi.responses import JSONResponse, RedirectResponse

        router = APIRouter()
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}

        def output(value, status=200):
            return JSONResponse(value, status_code=status, headers=headers)

        def failure(exc, owner=False):
            return output({"detail": exc.description} if owner else {"error": exc.error, "error_description": exc.description}, exc.status)

        def binding(request):
            # Fail closed even if a future caller mounts this router outside the host guard.
            session = self.owner_session(request) if callable(self.owner_session) else None
            sid = request.cookies.get("mailharbor_session", "")
            if not session or not sid:
                raise OAuthError("access_denied", "請先登入管理頁。", 401)
            if request.method not in {"GET", "HEAD"}:
                csrf = request.headers.get("x-csrf-token", "")
                if (request.headers.get("origin") != self.issuer or not session.get("csrf")
                        or not csrf.isascii() or not hmac.compare_digest(csrf, session["csrf"])):
                    raise OAuthError("access_denied", "頁面驗證已過期，請重新整理。", 403)
            return digest(sid)

        async def json_body(request):
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise OAuthError("invalid_request", "Use application/json.")
            raw = await request.body()
            if len(raw) > 16384:
                raise OAuthError("invalid_request", "Request body too large.", 413)
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (ValueError, UnicodeError, RecursionError):
                raise OAuthError("invalid_request", "Malformed JSON request.") from None

        async def form_body(request):
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
                raise OAuthError("invalid_request", "Use application/x-www-form-urlencoded.")
            raw = await request.body()
            if len(raw) > 16384:
                raise OAuthError("invalid_request", "Request body too large.", 413)
            try:
                values = parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=25)
                if any(len(v) != 1 for v in values.values()):
                    raise ValueError()
                return {k: v[0] for k, v in values.items()}
            except (ValueError, UnicodeError):
                raise OAuthError("invalid_request", "Malformed or repeated form parameters.") from None

        @router.get("/.well-known/oauth-protected-resource")
        @router.get("/.well-known/oauth-protected-resource/mcp")
        def protected_resource():
            return output({"resource": self.resource, "authorization_servers": [self.issuer],
                           "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"],
                           "resource_name": "MailHarbor read-only email"})

        @router.get("/.well-known/oauth-authorization-server")
        def authorization_metadata():
            return output({"issuer": self.issuer, "authorization_endpoint": self.issuer + "/oauth/authorize",
                           "token_endpoint": self.issuer + "/oauth/token", "registration_endpoint": self.issuer + "/oauth/register",
                           "revocation_endpoint": self.issuer + "/oauth/revoke", "scopes_supported": [SCOPE],
                           "response_types_supported": ["code"], "response_modes_supported": ["query"],
                           "grant_types_supported": ["authorization_code", "refresh_token"],
                           "token_endpoint_auth_methods_supported": ["none"], "revocation_endpoint_auth_methods_supported": ["none"],
                           "code_challenge_methods_supported": ["S256"], "authorization_response_iss_parameter_supported": True})

        @router.post("/oauth/register")
        async def registration(request: Request):
            try:
                result = self.register(await json_body(request), request.client.host if request.client else "unknown")
                return output(result, 201)
            except OAuthError as exc:
                return failure(exc)

        @router.get("/oauth/authorize")
        def authorize(request: Request):
            try:
                if len(request.url.query) > 8192:
                    raise OAuthError("invalid_request", "Authorization query is too long.")
                values = {}
                for key, value in request.query_params.multi_items():
                    if key in values:
                        raise OAuthError("invalid_request", "Repeated authorization parameters are not supported.")
                    values[key] = value
                request_id = self.begin_authorization(values, request.client.host if request.client else "unknown")
                return RedirectResponse("/?" + urlencode({"oauth_request": request_id}) + "#connect", status_code=303, headers=headers)
            except OAuthError as exc:
                return failure(exc)

        @router.get("/api/oauth/consent")
        def get_consent(request: Request, request_id: str = ""):
            try:
                return output(self.consent_details(request_id, binding(request)))
            except OAuthError as exc:
                return failure(exc, owner=True)

        @router.post("/api/oauth/consent")
        async def consent(request: Request):
            try:
                owner = binding(request)
                data = await json_body(request)
                return output(self.decide_consent(data.get("request_id"), data.get("approve"), owner))
            except OAuthError as exc:
                return failure(exc, owner=True)

        @router.post("/oauth/token")
        async def token(request: Request):
            try:
                if request.headers.get("authorization"):
                    raise OAuthError("invalid_client", "Use public client_id in the request body.", 401)
                return output(self.exchange(await form_body(request), request.client.host if request.client else "unknown"))
            except OAuthError as exc:
                return failure(exc)

        @router.post("/oauth/revoke")
        async def revoke_token(request: Request):
            try:
                data = await form_body(request)
                self.revoke(data.get("token", ""), data.get("client_id", ""))
                return output({})
            except OAuthError as exc:
                return failure(exc)

        @router.get("/api/oauth/grants")
        def grant_list(request: Request):
            try:
                binding(request)
                return output({"grants": self.grants()})
            except OAuthError as exc:
                return failure(exc, owner=True)

        @router.delete("/api/oauth/grants/{grant_id}")
        def remove_grant(grant_id: str, request: Request):
            try:
                binding(request)
                if not self.revoke_grant(grant_id):
                    raise OAuthError("invalid_request", "找不到授權應用程式。", 404)
                return output({"ok": True})
            except OAuthError as exc:
                return failure(exc, owner=True)

        return router
