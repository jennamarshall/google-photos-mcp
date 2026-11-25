"""MCP server exposing Google Photos as a set of tools.

Google's March 2025 API changes mean a third-party app can no longer browse a
user's whole library. Two access paths remain, and this connector exposes both:

* Picker API: the user picks photos in Google Photos' own UI, and the connector
  can then read just those items (metadata + image bytes).
* Library API: the connector can upload photos, create albums, and read or edit
  albums and media items that it created itself.
"""

from __future__ import annotations

import functools
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .auth import AuthError, StaticTokenProvider, TokenProvider
from .client import GooglePhotosClient, GooglePhotosError

INSTRUCTIONS = """\
Google Photos connector.

To look at a user's existing photos: call create_picker_session, give the user
the picker_uri to open, wait for them to finish selecting, then call
get_picker_session until media_items_set is true, then list_picked_media and
view_picked_media. Access is limited to what the user picks.

Albums and media listed by list_albums / search_app_media are only those this
connector created (Google no longer allows apps to browse the full library).
Use upload_photo and create_album to add new content.
"""

mcp = MCPServer(name="google-photos", title="Google Photos", instructions=INSTRUCTIONS)

_client: GooglePhotosClient | None = None
# id -> picked media item, filled as sessions are listed (baseUrls last ~60 minutes).
_picked_cache: dict[str, dict[str, Any]] = {}


def get_client() -> GooglePhotosClient:
    global _client
    if _client is None:
        static = os.environ.get("GOOGLE_PHOTOS_ACCESS_TOKEN")
        _client = GooglePhotosClient(StaticTokenProvider(static) if static else TokenProvider())
    return _client


def set_client(client: GooglePhotosClient | None) -> None:
    """Swap the client (used by tests)."""
    global _client
    _client = client
    _picked_cache.clear()


def _tool_errors(fn):
    """Turn expected failures into ToolErrors so the model sees the real message."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (GooglePhotosError, AuthError, ValueError, FileNotFoundError) as e:
            raise ToolError(str(e)) from e

    return wrapper


READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


# --------------------------------------------------------------------- helpers


def _summarize_picked(item: dict[str, Any]) -> dict[str, Any]:
    mf = item.get("mediaFile", {})
    meta = mf.get("mediaFileMetadata", {})
    out = {
        "id": item.get("id"),
        "type": item.get("type"),
        "create_time": item.get("createTime"),
        "filename": mf.get("filename"),
        "mime_type": mf.get("mimeType"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "camera": " ".join(x for x in (meta.get("cameraMake"), meta.get("cameraModel")) if x) or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def _summarize_media(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("mediaMetadata", {})
    out = {
        "id": item.get("id"),
        "filename": item.get("filename"),
        "description": item.get("description"),
        "mime_type": item.get("mimeType"),
        "creation_time": meta.get("creationTime"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "product_url": item.get("productUrl"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _summarize_album(album: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": album.get("id"),
        "title": album.get("title"),
        "media_items_count": int(album["mediaItemsCount"]) if "mediaItemsCount" in album else None,
        "product_url": album.get("productUrl"),
        "is_writeable": album.get("isWriteable"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _parse_date(value: str) -> dict[str, int]:
    try:
        d = date.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"Dates must be YYYY-MM-DD, got {value!r}.") from e
    return {"year": d.year, "month": d.month, "day": d.day}


async def _find_picked(session_id: str, media_item_id: str) -> dict[str, Any]:
    if media_item_id in _picked_cache:
        return _picked_cache[media_item_id]
    token = None
    while True:
        page = await get_client().list_picked_media_items(session_id, 100, token)
        for item in page.get("mediaItems", []):
            _picked_cache[item["id"]] = item
        if media_item_id in _picked_cache:
            return _picked_cache[media_item_id]
        token = page.get("nextPageToken")
        if not token:
            raise ValueError(f"Media item {media_item_id} is not in picker session {session_id}.")


# ---------------------------------------------------------------- Picker tools


@mcp.tool(annotations=WRITE)
@_tool_errors
async def create_picker_session(max_items: int | None = None) -> dict[str, Any]:
    """Start a Google Photos picker session so the user can choose photos to share.

    Returns a picker_uri. Give it to the user to open (on any device signed in
    to the same Google account); they select photos there and tap Done. Then
    poll get_picker_session until media_items_set is true.

    Args:
        max_items: Optional cap on how many items the user may select (max 2000).
    """
    if max_items is not None and not 1 <= max_items <= 2000:
        raise ValueError("max_items must be between 1 and 2000.")
    s = await get_client().create_picker_session(max_items)
    return {
        "session_id": s["id"],
        "picker_uri": s["pickerUri"],
        "expire_time": s.get("expireTime"),
        "poll_interval": s.get("pollingConfig", {}).get("pollInterval"),
        "next_step": "Ask the user to open picker_uri, pick photos and press Done, "
        "then call get_picker_session.",
    }


@mcp.tool(annotations=READ)
@_tool_errors
async def get_picker_session(session_id: str) -> dict[str, Any]:
    """Check whether the user has finished picking photos in a picker session."""
    s = await get_client().get_picker_session(session_id)
    done = bool(s.get("mediaItemsSet"))
    return {
        "session_id": s.get("id", session_id),
        "media_items_set": done,
        "expire_time": s.get("expireTime"),
        "poll_interval": s.get("pollingConfig", {}).get("pollInterval"),
        "next_step": "Call list_picked_media."
        if done
        else "The user has not finished yet; wait poll_interval and check again.",
    }


@mcp.tool(annotations=READ)
@_tool_errors
async def list_picked_media(
    session_id: str, page_size: int = 50, page_token: str | None = None
) -> dict[str, Any]:
    """List the photos and videos the user selected in a picker session.

    Args:
        session_id: From create_picker_session.
        page_size: 1-100 items per page.
        page_token: next_page_token from a previous call.
    """
    page = await get_client().list_picked_media_items(
        session_id, max(1, min(page_size, 100)), page_token
    )
    items = page.get("mediaItems", [])
    for item in items:
        _picked_cache[item["id"]] = item
    return {
        "media_items": [_summarize_picked(i) for i in items],
        "next_page_token": page.get("nextPageToken"),
    }


@mcp.tool(annotations=READ)
@_tool_errors
async def view_picked_media(session_id: str, media_item_id: str, max_size: int = 1024) -> list:
    """Fetch a picked photo as an image so you can look at it.

    For videos, this returns the video's thumbnail.

    Args:
        session_id: The picker session the item came from.
        media_item_id: An id from list_picked_media.
        max_size: Longest edge in pixels of the returned image (64-2048).
    """
    max_size = max(64, min(max_size, 2048))
    item = await _find_picked(session_id, media_item_id)
    base_url = item.get("mediaFile", {}).get("baseUrl")
    if not base_url:
        raise ValueError("This media item has no downloadable content.")
    data, mime = await get_client().download(base_url, f"w{max_size}-h{max_size}")
    fmt = mime.split("/")[-1].split(";")[0] if mime.startswith("image/") else "jpeg"
    return [_summarize_picked(item), Image(data=data, format=fmt)]


@mcp.tool(annotations=WRITE)
@_tool_errors
async def download_picked_media(
    session_id: str, media_item_id: str, destination_dir: str
) -> dict[str, Any]:
    """Save the original file of a picked photo or video to a local folder.

    Args:
        session_id: The picker session the item came from.
        media_item_id: An id from list_picked_media.
        destination_dir: Folder to write into (created if missing).
    """
    item = await _find_picked(session_id, media_item_id)
    mf = item.get("mediaFile", {})
    if not mf.get("baseUrl"):
        raise ValueError("This media item has no downloadable content.")
    suffix = "dv" if item.get("type") == "VIDEO" else "d"
    data, _ = await get_client().download(mf["baseUrl"], suffix)
    dest = Path(destination_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    name = Path(mf.get("filename") or f"{media_item_id}").name  # strip any path parts
    target = dest / name
    stem, ext, n = target.stem, target.suffix, 1
    while target.exists():
        target = dest / f"{stem} ({n}){ext}"
        n += 1
    target.write_bytes(data)
    return {"path": str(target), "bytes": len(data)}


@mcp.tool(annotations=EDIT)
@_tool_errors
async def delete_picker_session(session_id: str) -> str:
    """End a picker session once you are done with it (frees its media URLs)."""
    await get_client().delete_picker_session(session_id)
    _picked_cache.clear()
    return f"Deleted picker session {session_id}."


# --------------------------------------------------------------- Library tools


@mcp.tool(annotations=READ)
@_tool_errors
async def list_albums(page_size: int = 20, page_token: str | None = None) -> dict[str, Any]:
    """List albums created by this connector.

    Args:
        page_size: 1-50 albums per page.
        page_token: next_page_token from a previous call.
    """
    page = await get_client().list_albums(max(1, min(page_size, 50)), page_token)
    return {
        "albums": [_summarize_album(a) for a in page.get("albums", [])],
        "next_page_token": page.get("nextPageToken"),
    }


@mcp.tool(annotations=READ)
@_tool_errors
async def get_album(album_id: str) -> dict[str, Any]:
    """Get details of an album created by this connector."""
    return _summarize_album(await get_client().get_album(album_id))


@mcp.tool(annotations=WRITE)
@_tool_errors
async def create_album(title: str) -> dict[str, Any]:
    """Create a new album in the user's Google Photos library."""
    if not title.strip():
        raise ValueError("Album title cannot be empty.")
    if len(title) > 500:
        raise ValueError("Album title must be at most 500 characters.")
    return _summarize_album(await get_client().create_album(title))


@mcp.tool(annotations=EDIT)
@_tool_errors
async def update_album(
    album_id: str, title: str | None = None, cover_media_item_id: str | None = None
) -> dict[str, Any]:
    """Rename an album and/or set its cover photo (connector-created albums only)."""
    return _summarize_album(await get_client().update_album(album_id, title, cover_media_item_id))


@mcp.tool(annotations=WRITE)
@_tool_errors
async def add_album_text(album_id: str, text: str) -> str:
    """Append a text caption block to the end of an album."""
    await get_client().add_album_text(album_id, text)
    return f"Added text to album {album_id}."


@mcp.tool(annotations=EDIT)
@_tool_errors
async def add_media_to_album(album_id: str, media_item_ids: list[str]) -> str:
    """Add connector-uploaded media items to a connector-created album."""
    if not media_item_ids:
        raise ValueError("Give at least one media item id.")
    await get_client().add_to_album(album_id, media_item_ids)
    return f"Added {len(media_item_ids)} item(s) to album {album_id}."


@mcp.tool(annotations=EDIT)
@_tool_errors
async def remove_media_from_album(album_id: str, media_item_ids: list[str]) -> str:
    """Remove media items from a connector-created album (does not delete them)."""
    if not media_item_ids:
        raise ValueError("Give at least one media item id.")
    await get_client().remove_from_album(album_id, media_item_ids)
    return f"Removed {len(media_item_ids)} item(s) from album {album_id}."


@mcp.tool(annotations=READ)
@_tool_errors
async def search_app_media(
    album_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    media_type: Literal["ALL_MEDIA", "PHOTO", "VIDEO"] | None = None,
    page_size: int = 25,
    page_token: str | None = None,
) -> dict[str, Any]:
    """List or search media items uploaded by this connector.

    Either give album_id, or filter by date range / media type (Google does not
    allow both at once).

    Args:
        album_id: Only items in this album.
        start_date: YYYY-MM-DD, inclusive.
        end_date: YYYY-MM-DD, inclusive.
        media_type: Restrict to photos or videos.
        page_size: 1-100 items per page.
        page_token: next_page_token from a previous call.
    """
    filters: dict[str, Any] = {}
    if start_date or end_date:
        rng = {
            "startDate": _parse_date(start_date or "1900-01-01"),
            "endDate": _parse_date(end_date or "9999-12-31"),
        }
        filters["dateFilter"] = {"ranges": [rng]}
    if media_type:
        filters["mediaTypeFilter"] = {"mediaTypes": [media_type]}
    page = await get_client().search_media_items(
        album_id, filters or None, max(1, min(page_size, 100)), page_token
    )
    return {
        "media_items": [_summarize_media(m) for m in page.get("mediaItems", [])],
        "next_page_token": page.get("nextPageToken"),
    }


@mcp.tool(annotations=READ)
@_tool_errors
async def view_app_media(media_item_id: str, max_size: int = 1024) -> list:
    """Fetch a connector-uploaded photo as an image so you can look at it."""
    max_size = max(64, min(max_size, 2048))
    item = await get_client().get_media_item(media_item_id)
    data, mime = await get_client().download(item["baseUrl"], f"w{max_size}-h{max_size}")
    fmt = mime.split("/")[-1].split(";")[0] if mime.startswith("image/") else "jpeg"
    return [_summarize_media(item), Image(data=data, format=fmt)]


@mcp.tool(annotations=EDIT)
@_tool_errors
async def update_media_description(media_item_id: str, description: str) -> dict[str, Any]:
    """Set the description (caption) of a connector-uploaded media item."""
    if len(description) > 1000:
        raise ValueError("Descriptions must be at most 1000 characters.")
    return _summarize_media(await get_client().update_media_description(media_item_id, description))


@mcp.tool(annotations=WRITE)
@_tool_errors
async def upload_photo(
    file_path: str, album_id: str | None = None, description: str | None = None
) -> dict[str, Any]:
    """Upload a local photo or video file to the user's Google Photos library.

    Args:
        file_path: Path to an image or video on this machine.
        album_id: Optional connector-created album to add it to.
        description: Optional caption (max 1000 characters).
    """
    if description and len(description) > 1000:
        raise ValueError("Descriptions must be at most 1000 characters.")
    result = await get_client().upload_file(Path(file_path), album_id, description)
    status = result.get("status", {})
    if "mediaItem" not in result:
        raise ValueError(f"Upload failed: {status.get('message', 'unknown error')}")
    return _summarize_media(result["mediaItem"])
