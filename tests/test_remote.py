"""End-to-end test of hosted mode: Claude registers, the user signs in with
Google, Claude gets tokens and calls tools with them."""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from google_photos_mcp import server
from google_photos_mcp.client import GooglePhotosClient
from google_photos_mcp.remote import GOOGLE_SCOPES, RemoteConfig, RequestTokenSource, Sealer
from test_server import FakeGoogle

PUBLIC = "https://photos.example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


class FakeGoogleAccounts:
    def __init__(self, email="me@gmail.com", scopes=GOOGLE_SCOPES):
        self.email, self.scopes = email, scopes
        self.revoked: list[str] = []
        self.refreshes = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/token":
            form = parse_qs(req.content.decode())
            assert form["client_id"] == ["gid"] and form["client_secret"] == ["gsecret"]
            if form["grant_type"] == ["authorization_code"]:
                assert form["code"] == ["google-code"]
                assert form["redirect_uri"] == [PUBLIC + "/oauth/google/callback"]
                return httpx.Response(200, json={"access_token": "g-access-1", "refresh_token": "g-refresh",
                                                 "expires_in": 3599, "scope": " ".join(self.scopes)})
            assert form["refresh_token"] == ["g-refresh"]
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": f"g-access-{self.refreshes + 1}", "expires_in": 3599})
        if req.url.path == "/v1/userinfo":
            return httpx.Response(200, json={"sub": "12345", "email": self.email, "email_verified": True})
        if req.url.path == "/revoke":
            self.revoked.append(parse_qs(req.content.decode())["token"][0])
            return httpx.Response(200)
        return httpx.Response(404)


class PhotosRecorder(FakeGoogle):
    """Photos API fake that accepts any Google access token and records which one."""

    def __call__(self, req):
        self.tokens = getattr(self, "tokens", []) + [req.headers["authorization"]]
        req.headers["authorization"] = "Bearer test-token"
        return super().__call__(req)


@pytest.fixture
def hosted():
    accounts = FakeGoogleAccounts()
    photos = PhotosRecorder()
    cfg = RemoteConfig.build(PUBLIC, "gid", "gsecret", "x" * 40, {"me@gmail.com"},
                             http=httpx.AsyncClient(transport=httpx.MockTransport(accounts)))
    app_server = server.build_server(cfg)
    server.set_client(GooglePhotosClient(RequestTokenSource(),
                                         httpx.AsyncClient(transport=httpx.MockTransport(photos))))
    with TestClient(app_server.streamable_http_app(transport_security=cfg.transport_security),
                    base_url=PUBLIC) as client:
        yield client, accounts, photos, app_server
    server.set_client(None)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def connect(client, accounts=None):
    """Run the whole flow Claude runs. Returns (client_id, token_response)."""
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] == PUBLIC + "/register"

    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Claude",
                                         "grant_types": ["authorization_code", "refresh_token"],
                                         "response_types": ["code"], "token_endpoint_auth_method": "client_secret_post"})
    assert reg.status_code == 201, reg.text
    reg = reg.json()
    client_id, client_secret = reg["client_id"], reg["client_secret"]

    verifier, challenge = pkce()
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "claude-state",
        "resource": PUBLIC + "/mcp",
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    google = urlparse(r.headers["location"])
    gq = parse_qs(google.query)
    assert google.netloc == "accounts.google.com"
    assert gq["redirect_uri"] == [PUBLIC + "/oauth/google/callback"]
    assert gq["access_type"] == ["offline"]
    assert "https://www.googleapis.com/auth/photospicker.mediaitems.readonly" in gq["scope"][0]

    # The user approves on Google; Google redirects back to us.
    cb = client.get("/oauth/google/callback", params={"code": "google-code", "state": gq["state"][0]},
                    follow_redirects=False)
    assert cb.status_code == 302, cb.text
    back = urlparse(cb.headers["location"])
    bq = parse_qs(back.query)
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
    assert bq["state"] == ["claude-state"]
    if "error" in bq:
        return client_id, client_secret, bq
    tok = client.post("/token", data={
        "grant_type": "authorization_code", "code": bq["code"][0], "redirect_uri": REDIRECT,
        "client_id": client_id, "client_secret": client_secret, "code_verifier": verifier,
        "resource": PUBLIC + "/mcp",
    })
    assert tok.status_code == 200, tok.text
    return client_id, client_secret, tok.json() | {"_code": bq["code"][0], "_verifier": verifier}


def mcp_call(client, token, method, params=None, session=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if session:
        headers["mcp-session-id"] = session
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = client.post("/mcp", headers=headers, json=body)
    return r


def parse_sse(r):
    for line in r.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:])
    return r.json()


def initialize(client, token):
    r = mcp_call(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    session = r.headers.get("mcp-session-id")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json", **({"mcp-session-id": session} if session else {})}
    client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    return session


def test_full_phone_flow(hosted):
    client, accounts, photos, _ = hosted
    # Unauthenticated requests get a 401 that points at the OAuth metadata.
    r = mcp_call(client, "nope", "tools/list")
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")

    client_id, client_secret, tok = connect(client)
    assert tok["token_type"].lower() == "bearer" and tok["refresh_token"]
    assert "g-access" not in tok["access_token"] and "g-refresh" not in tok["refresh_token"]  # sealed

    # Authorization codes are single-use.
    again = client.post("/token", data={"grant_type": "authorization_code", "code": tok["_code"],
                                        "redirect_uri": REDIRECT, "client_id": client_id,
                                        "client_secret": client_secret, "code_verifier": tok["_verifier"]})
    assert again.status_code == 400

    session = initialize(client, tok["access_token"])
    tools = parse_sse(mcp_call(client, tok["access_token"], "tools/list", session=session))["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "create_picker_session" in names and "copy_picked_to_album" in names
    assert not names & {"index_local_library", "upload_photo", "download_picked_media", "export_local_photos"}

    res = parse_sse(mcp_call(client, tok["access_token"], "tools/call",
                             {"name": "create_picker_session", "arguments": {}}, session))
    assert "photos.google.com/picker" in res["result"]["content"][0]["text"]
    assert photos.tokens[-1] == "Bearer g-access-1"  # the user's own Google token was used

    # Refresh: new tokens carry a fresh Google access token.
    ref = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                      "client_id": client_id, "client_secret": client_secret})
    assert ref.status_code == 200, ref.text
    session2 = initialize(client, ref.json()["access_token"])
    parse_sse(mcp_call(client, ref.json()["access_token"], "tools/call",
                       {"name": "list_albums", "arguments": {}}, session2))
    assert photos.tokens[-1] == "Bearer g-access-2"

    # Revoking revokes the Google grant.
    rv = client.post("/revoke", data={"token": tok["refresh_token"], "client_id": client_id,
                                      "client_secret": client_secret})
    assert rv.status_code == 200
    assert accounts.revoked == ["g-refresh"]


def test_disallowed_account_is_refused(hosted):
    client, accounts, *_ = hosted
    accounts.email = "stranger@gmail.com"
    _, _, result = connect(client)
    assert result["error"] == ["access_denied"]
    assert accounts.revoked == ["g-refresh"]  # we gave the stranger's grant back


def test_missing_photos_permission(hosted):
    client, accounts, *_ = hosted
    accounts.scopes = ["openid", "email"]
    _, _, result = connect(client)
    assert result["error"] == ["access_denied"]
    assert "Google Photos access" in result["error_description"][0]


def test_tampered_and_expired_state(hosted):
    client, *_ = hosted
    r = client.get("/oauth/google/callback", params={"code": "c", "state": "garbage"})
    assert r.status_code == 400 and "expired" in r.text


def test_sealer_binds_purpose_and_age():
    s = Sealer("y" * 40)
    blob = s.seal("access", {"a": 1})
    assert s.open("access", blob)["a"] == 1
    assert s.open("refresh", blob) is None  # can't use one kind of token as another
    assert s.open("access", blob[:-2] + "AA") is None
    assert Sealer("z" * 40).open("access", blob) is None
    old = s.seal("code", {"iat_override": True})
    assert s.open("code", old, max_age=-1) is None
    with pytest.raises(ValueError):
        Sealer("short")


def test_unknown_client_cannot_authorize(hosted):
    client, *_ = hosted
    _, challenge = pkce()
    r = client.get("/authorize", params={"response_type": "code", "client_id": "made-up", "redirect_uri": REDIRECT,
                                         "code_challenge": challenge, "code_challenge_method": "S256"},
                   follow_redirects=False)
    assert r.status_code == 400


def test_from_env_requires_allowlist(monkeypatch):
    for k, v in {"PUBLIC_URL": PUBLIC, "GOOGLE_OAUTH_CLIENT_ID": "a", "GOOGLE_OAUTH_CLIENT_SECRET": "b",
                 "SERVER_SECRET": "s" * 40}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALLOWED_EMAILS", raising=False)
    with pytest.raises(SystemExit, match="ALLOWED_EMAILS"):
        RemoteConfig.from_env()
    monkeypatch.setenv("ALLOWED_EMAILS", "Me@Gmail.com, other@example.com")
    cfg = RemoteConfig.from_env()
    assert cfg.provider.allowed_emails == {"me@gmail.com", "other@example.com"}
    monkeypatch.setenv("PUBLIC_URL", "http://insecure.example.com")
    with pytest.raises(ValueError, match="https"):
        RemoteConfig.from_env()
