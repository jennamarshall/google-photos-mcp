"""Hosted (remote) mode: an OAuth bridge between Claude and Google sign-in.

Claude's connectors (claude.ai and the Claude mobile apps) talk to a remote MCP
server over HTTPS and sign the user in with OAuth. This server is the OAuth
authorization server Claude sees. When the user connects, it sends them on to
Google's consent screen, and it keeps the resulting Google tokens inside the
tokens it hands Claude.

It is stateless: client registrations, authorization codes, access tokens and
refresh tokens are all AES-GCM-sealed blobs, keyed by SERVER_SECRET. There is
no database, so it runs on Cloud Run (or anywhere) and scales to zero. The
trade-off is that a token can't be revoked here before it expires. Revoking
does revoke the Google grant, which makes every derived token useless.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import SCOPES as PHOTOS_SCOPES
from .auth import AuthError

log = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPES = ["openid", "email", *PHOTOS_SCOPES]

CALLBACK_PATH = "/oauth/google/callback"
MCP_SCOPE = "photos"
AUTHZ_TTL = 15 * 60  # time allowed on Google's consent screen
CODE_TTL = 5 * 60
MAX_ACCESS_TTL = 60 * 60


# ------------------------------------------------------------------ sealing


class Sealer:
    """Authenticated encryption of small JSON payloads into URL-safe strings.

    The purpose ("client", "code", "access", ...) is bound in as associated data,
    so a blob sealed for one purpose cannot be passed off as another.
    """

    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("SERVER_SECRET must be at least 32 characters.")
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"google-photos-mcp", info=b"seal-v1"
        ).derive(secret.encode())
        self._aead = AESGCM(key)

    def seal(self, purpose: str, payload: dict[str, Any]) -> str:
        nonce = secrets.token_bytes(12)
        body = json.dumps({**payload, "iat": int(time.time())}, separators=(",", ":")).encode()
        ct = self._aead.encrypt(nonce, body, purpose.encode())
        return base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")

    def open(self, purpose: str, blob: str, max_age: float | None = None) -> dict[str, Any] | None:
        try:
            raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
            data = json.loads(self._aead.decrypt(raw[:12], raw[12:], purpose.encode()))
        except Exception:
            return None
        if max_age is not None and time.time() - data.get("iat", 0) > max_age:
            return None
        return data


# ------------------------------------------------------------- token types


class GoogleAuthCode(AuthorizationCode):
    google: dict[str, Any]


class GoogleRefreshToken(RefreshToken):
    google_refresh_token: str


class GoogleAccessToken(AccessToken):
    google_access_token: str


class RequestTokenSource:
    """TokenSource for GooglePhotosClient: the Google token of the current request's user."""

    def token(self) -> str:
        at = get_access_token()
        if not isinstance(at, GoogleAccessToken):
            raise AuthError("Not signed in to Google Photos. Reconnect the connector.")
        return at.google_access_token


# ----------------------------------------------------------------- provider


class GooglePhotosOAuthProvider:
    def __init__(
        self,
        public_url: str,
        google_client_id: str,
        google_client_secret: str,
        server_secret: str,
        allowed_emails: set[str] | None,
        http: httpx.AsyncClient | None = None,
    ):
        self.public_url = public_url.rstrip("/")
        self.google_client_id = google_client_id
        self.google_client_secret = google_client_secret
        self.allowed_emails = {e.lower() for e in allowed_emails} if allowed_emails else None
        self.sealer = Sealer(server_secret)
        self.http = http or httpx.AsyncClient(timeout=30)
        self._used_codes: dict[str, float] = {}  # best-effort replay guard (per instance)

    @property
    def resource_url(self) -> str:
        return self.public_url + "/mcp"

    @property
    def callback_url(self) -> str:
        return self.public_url + CALLBACK_PATH

    # -- clients (dynamic registration; the client_id *is* the sealed registration)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.sealer.open("client", client_id)
        if data is None:
            return None
        data.pop("iat", None)
        return OAuthClientInformationFull.model_validate({**data, "client_id": client_id})

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # The SDK returns this same object to the client, so replacing client_id
        # here hands the client a self-describing id.
        record = client_info.model_dump(mode="json", exclude_none=True, exclude={"client_id"})
        client_info.client_id = self.sealer.seal("client", record)

    # -- authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = self.sealer.seal(
            "authz",
            {
                "client_id": client.client_id,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "code_challenge": params.code_challenge,
                "state": params.state,
                "scopes": params.scopes or [MCP_SCOPE],
                "resource": params.resource,
            },
        )
        return GOOGLE_AUTH_URL + "?" + urlencode(
            {
                "client_id": self.google_client_id,
                "redirect_uri": self.callback_url,
                "response_type": "code",
                "scope": " ".join(GOOGLE_SCOPES),
                "access_type": "offline",
                "prompt": "consent",  # always return a refresh token
                "include_granted_scopes": "true",
                "state": state,
            }
        )

    async def google_callback(self, request: Request) -> Response:
        q = request.query_params
        pending = self.sealer.open("authz", q.get("state", ""), max_age=AUTHZ_TTL)
        if pending is None:
            return _page("This sign-in link has expired. Go back to Claude and connect again.", 400)

        def back(**params: str | None) -> Response:
            return RedirectResponse(
                construct_redirect_uri(pending["redirect_uri"], state=pending["state"], **params),
                status_code=302,
            )

        if q.get("error"):
            return back(error="access_denied", error_description=f"Google sign-in failed: {q['error']}")
        code = q.get("code")
        if not code:
            return back(error="invalid_request", error_description="Google returned no code.")

        resp = await self.http.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": self.google_client_id,
                "client_secret": self.google_client_secret,
                "redirect_uri": self.callback_url,
                "grant_type": "authorization_code",
            },
        )
        if not resp.is_success:
            log.warning("Google code exchange failed: %s %s", resp.status_code, resp.text[:300])
            return back(error="server_error", error_description="Could not complete Google sign-in.")
        g = resp.json()
        granted = set(g.get("scope", "").split())
        missing = [s for s in PHOTOS_SCOPES if s not in granted]
        if missing:
            return back(
                error="access_denied",
                error_description="Google Photos access was not granted. Connect again and tick every box.",
            )

        info = await self.http.get(
            GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {g['access_token']}"}
        )
        user = info.json() if info.is_success else {}
        email = (user.get("email") or "").lower()
        if self.allowed_emails is not None and (email not in self.allowed_emails or not user.get("email_verified")):
            await self._revoke_google(g.get("refresh_token") or g["access_token"])
            return back(error="access_denied", error_description=f"{email or 'This account'} is not allowed on this server.")
        if not g.get("refresh_token"):
            return back(error="server_error", error_description="Google did not return a refresh token.")

        auth_code = self.sealer.seal(
            "code",
            {
                **{k: pending[k] for k in ("client_id", "redirect_uri", "redirect_uri_provided_explicitly",
                                            "code_challenge", "scopes", "resource")},
                "subject": user.get("sub") or email,
                "google": {
                    "access_token": g["access_token"],
                    "refresh_token": g["refresh_token"],
                    "expires_at": int(time.time()) + int(g.get("expires_in", 3600)),
                },
            },
        )
        return back(code=auth_code)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> GoogleAuthCode | None:
        d = self.sealer.open("code", authorization_code, max_age=CODE_TTL)
        if d is None or d["client_id"] != client.client_id or authorization_code in self._used_codes:
            return None
        return GoogleAuthCode(
            code=authorization_code,
            scopes=d["scopes"],
            expires_at=d["iat"] + CODE_TTL,
            client_id=d["client_id"],
            code_challenge=d["code_challenge"],
            redirect_uri=d["redirect_uri"],
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
            resource=d.get("resource"),
            subject=d.get("subject"),
            google=d["google"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: GoogleAuthCode
    ) -> OAuthToken:
        now = time.time()
        self._used_codes = {c: t for c, t in self._used_codes.items() if t > now}
        self._used_codes[authorization_code.code] = now + CODE_TTL
        g = authorization_code.google
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource,
                           authorization_code.subject, g["access_token"], g["expires_at"], g["refresh_token"])

    # -- refresh

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> GoogleRefreshToken | None:
        d = self.sealer.open("refresh", refresh_token)
        if d is None or d["client_id"] != client.client_id:
            return None
        return GoogleRefreshToken(
            token=refresh_token, client_id=d["client_id"], scopes=d["scopes"], resource=d.get("resource"),
            subject=d.get("subject"), google_refresh_token=d["grt"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: GoogleRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        resp = await self.http.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": self.google_client_id,
                "client_secret": self.google_client_secret,
                "refresh_token": refresh_token.google_refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not resp.is_success:
            # e.g. the user revoked access, or a "Testing" app's 7-day token lifetime ran out
            raise TokenError("invalid_grant", "Google sign-in expired. Reconnect the connector.")
        g = resp.json()
        return self._issue(
            client.client_id, scopes or refresh_token.scopes, refresh_token.resource, refresh_token.subject,
            g["access_token"], int(time.time()) + int(g.get("expires_in", 3600)),
            g.get("refresh_token") or refresh_token.google_refresh_token,
        )

    def _issue(self, client_id, scopes, resource, subject, g_access, g_expires_at, g_refresh) -> OAuthToken:
        expires_at = min(int(g_expires_at) - 60, int(time.time()) + MAX_ACCESS_TTL)
        common = {"client_id": client_id, "scopes": scopes, "resource": self.resource_url, "subject": subject}
        access = self.sealer.seal("access", {**common, "gat": g_access, "exp": expires_at})
        refresh = self.sealer.seal("refresh", {**common, "grt": g_refresh})
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=max(1, expires_at - int(time.time())),
            refresh_token=refresh, scope=" ".join(scopes),
        )

    # -- verification / revocation

    async def load_access_token(self, token: str) -> GoogleAccessToken | None:
        d = self.sealer.open("access", token)
        if d is None or d["exp"] < time.time():
            return None
        return GoogleAccessToken(
            token=token, client_id=d["client_id"], scopes=d["scopes"], expires_at=d["exp"],
            resource=d.get("resource"), subject=d.get("subject"), google_access_token=d["gat"],
        )

    async def revoke_token(self, token: GoogleAccessToken | GoogleRefreshToken) -> None:
        g = getattr(token, "google_refresh_token", None) or getattr(token, "google_access_token", None)
        if g:
            await self._revoke_google(g)

    async def _revoke_google(self, token: str) -> None:
        try:
            await self.http.post(GOOGLE_REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            log.warning("Could not reach Google to revoke a token.")

    async def exchange_identity_assertion(self, client, params):  # not offered
        raise TokenError("unsupported_grant_type")


def _page(message: str, status: int = 200) -> HTMLResponse:
    from html import escape

    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width'>"
        f"<body style='font:16px system-ui;padding:2rem;max-width:32rem'><h1>Google Photos</h1>"
        f"<p>{escape(message)}</p>",
        status_code=status,
    )


# ------------------------------------------------------------------- config


@dataclass
class RemoteConfig:
    provider: GooglePhotosOAuthProvider
    auth_settings: AuthSettings

    @property
    def transport_security(self) -> TransportSecuritySettings:
        """Accept requests addressed to PUBLIC_URL's host (DNS-rebinding guard)."""
        host = urlparse(self.provider.public_url).netloc
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[self.provider.public_url, "https://claude.ai", "https://*.claude.ai"],
        )

    @property
    def token_source(self) -> RequestTokenSource:
        return RequestTokenSource()

    def add_routes(self, server) -> None:
        server.custom_route(CALLBACK_PATH, methods=["GET"], include_in_schema=False)(self.provider.google_callback)

        async def health(_: Request) -> Response:
            return JSONResponse({"ok": True})

        server.custom_route("/healthz", methods=["GET"], include_in_schema=False)(health)

    @classmethod
    def build(
        cls,
        public_url: str,
        google_client_id: str,
        google_client_secret: str,
        server_secret: str,
        allowed_emails: set[str] | None,
        http: httpx.AsyncClient | None = None,
    ) -> "RemoteConfig":
        if not public_url.startswith("https://") and "localhost" not in public_url and "127.0.0.1" not in public_url:
            raise ValueError("PUBLIC_URL must be https:// (Claude only connects to HTTPS servers).")
        public_url = public_url.rstrip("/")
        provider = GooglePhotosOAuthProvider(
            public_url, google_client_id, google_client_secret, server_secret, allowed_emails, http
        )
        settings = AuthSettings(
            issuer_url=AnyHttpUrl(public_url),
            resource_server_url=AnyHttpUrl(public_url + "/mcp"),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[MCP_SCOPE], default_scopes=[MCP_SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[MCP_SCOPE],
            validate_token_resource=True,
        )
        return cls(provider, settings)

    @classmethod
    def from_env(cls) -> "RemoteConfig":
        def need(name: str) -> str:
            v = os.environ.get(name, "").strip()
            if not v:
                raise SystemExit(f"Remote mode needs the {name} environment variable (see README).")
            return v

        allow_any = os.environ.get("ALLOW_ANY_GOOGLE_ACCOUNT", "").lower() in ("1", "true", "yes")
        emails = {e.strip() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
        if not emails and not allow_any:
            raise SystemExit(
                "Set ALLOWED_EMAILS to the Google account(s) allowed to sign in, e.g. "
                "ALLOWED_EMAILS=you@gmail.com (or ALLOW_ANY_GOOGLE_ACCOUNT=1 to allow anyone)."
            )
        return cls.build(
            need("PUBLIC_URL"), need("GOOGLE_OAUTH_CLIENT_ID"), need("GOOGLE_OAUTH_CLIENT_SECRET"),
            need("SERVER_SECRET"), None if allow_any else emails,
        )
