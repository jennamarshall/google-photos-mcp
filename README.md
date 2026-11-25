# Google Photos connector

An [MCP](https://modelcontextprotocol.io) server that connects Claude (Claude Desktop, Claude Code, or any MCP client) to Google Photos.

## What it can do (and why it's shaped this way)

On **31 March 2025** Google removed the scopes that let third-party apps browse a user's whole Google Photos library. Two ways in are left, and this connector supports both:

| Access path | What Claude can do |
|---|---|
| **Picker API** | You pick photos in Google Photos' own picker. Claude can then list, view (as images) and download **only the items you picked**. |
| **Library API (app-created data)** | Upload photos and videos, create and rename albums, add captions and text, and list, search and view **the content this connector created**. |

### Tools

**Picker (your existing photos)**
- `create_picker_session` returns a `picker_uri` for you to open and pick photos
- `get_picker_session` checks whether you've finished picking
- `list_picked_media` lists the items you picked, with metadata
- `view_picked_media` returns a resized image so Claude can look at it
- `download_picked_media` saves the original file to a local folder
- `delete_picker_session` ends the session

**Library (content this connector created)**
- `create_album`, `get_album`, `list_albums`, `update_album`, `add_album_text`
- `upload_photo` uploads a local image or video, optionally into an album with a caption
- `add_media_to_album`, `remove_media_from_album`
- `search_app_media` lists items by album, date range or media type
- `view_app_media`, `update_media_description`

A typical conversation: *"Pick the photos from our trip and write captions for them."* Claude creates a picker session and gives you the link. You choose the photos, and Claude views each one and writes the captions.

## Setup

### 1. Create Google OAuth credentials (one-time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, or pick an existing one.
2. **APIs & Services → Library**: enable **Photos Library API** and **Google Photos Picker API**.
3. **APIs & Services → OAuth consent screen**: configure it. While it's in *Testing*, add your own Google account as a test user.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Download the JSON.
5. Save it as `~/.config/google-photos-mcp/client_secret.json`, or point `GOOGLE_PHOTOS_CLIENT_SECRETS` at it.

### 2. Install and sign in

```bash
pip install .            # or: pipx install . / uv tool install .
google-photos-mcp auth   # opens a browser for consent; stores a refresh token
```

The token is saved to `~/.config/google-photos-mcp/token.json` with permissions `0600`, and it refreshes automatically.

### 3. Add it to Claude

**Claude Code**
```bash
claude mcp add google-photos -- google-photos-mcp serve
```

**Claude Desktop** (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "google-photos": {
      "command": "google-photos-mcp",
      "args": ["serve"]
    }
  }
}
```

**Remote / HTTP**: `google-photos-mcp serve --transport streamable-http --host 127.0.0.1 --port 8000` serves the MCP endpoint at `/mcp`. It uses the single token stored on the host and has **no authentication of its own**. Don't expose it publicly without putting an authenticating proxy in front of it.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_PHOTOS_CONFIG_DIR` | `~/.config/google-photos-mcp` | Where the secrets and token live |
| `GOOGLE_PHOTOS_CLIENT_SECRETS` | `<config>/client_secret.json` | OAuth client JSON |
| `GOOGLE_PHOTOS_TOKEN_FILE` | `<config>/token.json` | Cached token |
| `GOOGLE_PHOTOS_ACCESS_TOKEN` | — | Use a fixed access token instead (for testing) |

## Limits to know about

- Picked-item URLs expire about 60 minutes after the session's items are listed. Picker sessions also expire. Start a new one if needed.
- Library tools only see albums and media that this connector created, so photos picked through the Picker can't be added to albums.
- Uploads are capped at 200 MB per file here.
- Nothing in this connector deletes photos (the Google API doesn't allow it). `remove_media_from_album` only takes an item out of the album.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests run every tool against a fake Google API (`httpx.MockTransport`), so they need no network or credentials.
