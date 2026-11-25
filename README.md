# Google Photos connector

An [MCP](https://modelcontextprotocol.io) server that connects Claude (Claude Desktop, Claude Code, or any MCP client) to Google Photos.

## What it can do

On **31 March 2025** Google removed the API scopes that let third-party apps read a user's whole Google Photos library. No connector can browse, scan or reorganize your entire library through Google's API any more. This connector gets as close as Google allows in three ways:

| Mode | Covers | What Claude can do |
|---|---|---|
| **Whole library, via Google Takeout** | Everything you export | Search by date, place, person, album, favorite or text. View photos, **identify** what's in them and save tags and notes. **Organize** copies into folders by year, month or album, or upload a selection into a Google Photos album. |
| **Picker** | Photos you pick in Google Photos' own picker | List and view many at once, read EXIF (camera, exposure, GPS if present), download originals, copy them into albums. |
| **Connector-created content** | Albums and uploads made by this connector | Create, rename and caption albums. Add or remove items, search, view, edit descriptions. |

What still isn't possible: adding existing library photos to an album, editing existing photos or their dates and locations, or deleting anything. Google's API doesn't allow any of these. Tools that "move" photos into albums upload **copies**, and they say so.

### Whole library (Takeout)

1. Go to [takeout.google.com](https://takeout.google.com), click **Deselect all**, tick **Google Photos**, and export.
2. Unzip every part into one folder.
3. Ask Claude to index it: *"Index my photos in ~/Takeout"*. Or run `google-photos-mcp index ~/Takeout`, which is faster for very large libraries.

The index is a SQLite file at `~/.config/google-photos-mcp/library.db`. Your photo files are never modified. The index reads Takeout's JSON sidecars, including the newer `.supplemental-metadata.json` names, Takeout's truncated names, `(1)` duplicates and `-edited` copies. For a plain folder of photos with no sidecars, it reads EXIF instead. A photo that appears in several album folders becomes a single entry that lists all of its albums.

**Identifying photos.** Claude asks for photos it hasn't identified yet (`search_local_photos untagged=true`), views them in batches of up to 20, and saves tags and a note with `annotate_local_photos`. After that, *"find the receipts"* or *"photos of the dog at the beach"* work as searches. Re-indexing keeps the tags.

### Tools

**Whole library:** `index_local_library`, `local_library_stats`, `search_local_photos`, `view_local_photos`, `annotate_local_photos`, `export_local_photos` (copy only), `upload_local_photos`

**Picker (photos you pick):**
- `create_picker_session` returns a `picker_uri` for you to open
- `get_picker_session` checks whether you've finished picking
- `list_picked_media`, `view_picked_media`, `view_picked_media_batch`
- `get_picked_media_details` reads EXIF
- `download_picked_media` saves originals
- `copy_picked_to_album`, `delete_picker_session`

**Connector-created content:** `create_album`, `get_album`, `list_albums`, `update_album`, `add_album_text`, `upload_photo`, `add_media_to_album`, `remove_media_from_album`, `search_app_media`, `view_app_media`, `update_media_description`

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
| `GOOGLE_PHOTOS_LIBRARY_DB` | `<config>/library.db` | Local library index |
| `GOOGLE_PHOTOS_ACCESS_TOKEN` | — | Use a fixed access token instead (for testing) |

## Limits to know about

- Picked-item URLs expire about 60 minutes after the session's items are listed. Picker sessions also expire. Start a new one if needed.
- Album and search tools on the Google side only see albums and media that this connector created. Use the Takeout index for everything else.
- The Takeout index is a snapshot. Export again and re-index to pick up new photos. Re-indexing is incremental.
- HEIC/HEIF (iPhone) photos are supported. RAW files and videos are indexed and searchable, but Claude can't view them.
- Uploads are capped at 200 MB per file here.
- Nothing in this connector deletes photos (the Google API doesn't allow it). `remove_media_from_album` only takes an item out of the album.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

The tests run every tool against a fake Google API (`httpx.MockTransport`) and a generated Takeout-style folder, so they need no network or credentials.
