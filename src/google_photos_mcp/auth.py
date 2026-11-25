"""OAuth 2.0 handling for the Google Photos connector.

Tokens are obtained with the installed-app (loopback) flow and cached on disk so
the user only has to consent once. The cached refresh token is used to mint new
access tokens automatically.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Scopes still available to third-party apps after Google's March 2025 changes.
SCOPES = [
    # Picker API: read media items the user explicitly selects in Google Photos.
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly",
    # Library API: upload, and create albums.
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    # Library API: read / edit albums and media items created by this app.
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata",
    "https://www.googleapis.com/auth/photoslibrary.edit.appcreateddata",
]

_CONFIG_DIR = Path(
    os.environ.get("GOOGLE_PHOTOS_CONFIG_DIR")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "google-photos-mcp"
)


def client_secrets_path() -> Path:
    return Path(os.environ.get("GOOGLE_PHOTOS_CLIENT_SECRETS", _CONFIG_DIR / "client_secret.json"))


def token_path() -> Path:
    return Path(os.environ.get("GOOGLE_PHOTOS_TOKEN_FILE", _CONFIG_DIR / "token.json"))


class AuthError(RuntimeError):
    """Raised when no usable credentials are available."""


def _save(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # tokens are secrets: 0600


def load_credentials() -> Credentials:
    """Return valid credentials from the token cache, refreshing if needed.

    Never opens a browser: the MCP server runs non-interactively, so the user
    must run ``google-photos-mcp auth`` once beforehand.
    """
    path = token_path()
    if not path.exists():
        raise AuthError(
            f"No Google Photos token found at {path}. "
            "Run `google-photos-mcp auth` once to sign in."
        )
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds, path)
        return creds
    raise AuthError(
        "Stored Google Photos token is invalid. Run `google-photos-mcp auth` again."
    )


def run_auth_flow(port: int = 0) -> Path:
    """Interactive consent flow; stores the resulting token and returns its path."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = client_secrets_path()
    if not secrets.exists():
        raise AuthError(
            f"OAuth client secrets not found at {secrets}. Download a 'Desktop app' "
            "OAuth client JSON from Google Cloud Console and put it there, or set "
            "GOOGLE_PHOTOS_CLIENT_SECRETS."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")
    path = token_path()
    _save(creds, path)
    return path


class TokenProvider:
    """Hands out a current access token, refreshing lazily."""

    def __init__(self, creds: Credentials | None = None):
        self._creds = creds

    def token(self) -> str:
        if self._creds is None or not self._creds.valid:
            if self._creds is not None and self._creds.expired and self._creds.refresh_token:
                self._creds.refresh(Request())
                _save(self._creds, token_path())
            else:
                self._creds = load_credentials()
        return self._creds.token


class StaticTokenProvider:
    """Uses a fixed access token (e.g. from GOOGLE_PHOTOS_ACCESS_TOKEN or tests)."""

    def __init__(self, token: str):
        self._token = token

    def token(self) -> str:
        return self._token
