"""MCP server exposing Google Photos as a set of tools.

Google's March 2025 API changes mean a third-party app can no longer browse a
user's whole library. Two access paths remain, and this connector exposes both:

* Picker API: the user picks photos in Google Photos' own UI, and the connector
  can then read just those items (metadata + image bytes).
* Library API: the connector can upload photos, create albums, and read or edit
  albums and media items that it created itself.

For the whole library, the user exports it with Google Takeout and the
connector indexes that export locally (see local_library.py).
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

from .auth import AuthError, StaticTokenProvider, TokenProvider, config_dir
from .client import GooglePhotosClient, GooglePhotosError
from .imaging import read_exif, thumbnail_jpeg
from .local_library import LocalLibrary

INSTRUCTIONS = """\
Google Photos connector. Google no longer lets apps browse a user's whole
library through its API, so there are three ways to work with photos:

1. Whole library (best for searching, identifying, organizing): the user
   exports Google Photos with Google Takeout and unzips it. Call
   index_local_library on that folder once, then local_library_stats,
   search_local_photos and view_local_photos. To identify photos, view them in
   batches (search_local_photos with untagged=true), then save what you see with
   annotate_local_photos (tags + a short note) so later searches find them.
   Organize with export_local_photos (copies into folders) or
   upload_local_photos (into a Google Photos album).

2. Photos the user picks right now: create_picker_session, give the user the
   picker_uri, poll get_picker_session until media_items_set is true, then
   list_picked_media, view_picked_media_batch / view_picked_media, and
   get_picked_media_details for EXIF. copy_picked_to_album puts copies of picked
   photos into a connector-created album.

3. Content this connector created: list_albums, search_app_media, etc.
   Albums made elsewhere in Google Photos are not visible to the API.

Copying or uploading creates new items in the user's library; confirm with the
user before doing that in bulk.
"""

mcp = MCPServer(name="google-photos", title="Google Photos", instructions=INSTRUCTIONS)

_client: GooglePhotosClient | None = None
# id -> picked media item, filled as sessions are listed (baseUrls last ~60 minutes).
_picked_cache: dict[str, dict[str, Any]] = {}
_library: LocalLibrary | None = None


def get_client() -> GooglePhotosClient:
    global _client
    if _client is None:
        static = os.environ.get("GOOGLE_PHOTOS_ACCESS_TOKEN")
        _client = GooglePhotosClient(StaticTokenProvider(static) if static else TokenProvider())
    return _client


def get_library() -> LocalLibrary:
    global _library
    if _library is None:
        _library = LocalLibrary(os.environ.get("GOOGLE_PHOTOS_LIBRARY_DB") or config_dir() / "library.db")
    return _library


def set_library(library: LocalLibrary | None) -> None:
    """Swap the local library (used by tests)."""
    global _library
    _library = library


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
        except (GooglePhotosError, AuthError, ValueError, OSError) as e:
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


# ----------------------------------------------------------- more picker tools

MAX_BATCH_IMAGES = 20


@mcp.tool(annotations=READ)
@_tool_errors
async def view_picked_media_batch(
    session_id: str,
    media_item_ids: list[str] | None = None,
    page_token: str | None = None,
    limit: int = 12,
    max_size: int = 512,
) -> list:
    """Look at several picked photos at once, e.g. to identify or sort them.

    Give media_item_ids, or omit them to view the next page of the session.

    Args:
        session_id: The picker session.
        media_item_ids: Specific ids from list_picked_media (max 20).
        page_token: When paging through the whole session.
        limit: Items to view when paging (1-20).
        max_size: Longest edge in pixels (64-1024).
    """
    max_size = max(64, min(max_size, 1024))
    next_token = None
    if media_item_ids:
        items = [await _find_picked(session_id, i) for i in media_item_ids[:MAX_BATCH_IMAGES]]
    else:
        page = await get_client().list_picked_media_items(
            session_id, max(1, min(limit, MAX_BATCH_IMAGES)), page_token
        )
        items = page.get("mediaItems", [])
        next_token = page.get("nextPageToken")
        for item in items:
            _picked_cache[item["id"]] = item
    out: list = []
    for item in items:
        out.append(_summarize_picked(item))
        base_url = item.get("mediaFile", {}).get("baseUrl")
        if base_url:
            data, _ = await get_client().download(base_url, f"w{max_size}-h{max_size}")
            out.append(Image(data=thumbnail_jpeg(data, max_size), format="jpeg"))
    out.append({"next_page_token": next_token} if next_token else {"done": True})
    return out


@mcp.tool(annotations=READ)
@_tool_errors
async def get_picked_media_details(session_id: str, media_item_id: str) -> dict[str, Any]:
    """Full metadata for a picked photo, including EXIF (camera, lens, exposure, GPS if kept).

    Downloads the original file to read it, so it is slower than list_picked_media.
    """
    item = await _find_picked(session_id, media_item_id)
    info = _summarize_picked(item)
    base_url = item.get("mediaFile", {}).get("baseUrl")
    if item.get("type") == "PHOTO" and base_url:
        data, _ = await get_client().download(base_url, "d")
        info["exif"] = read_exif(data) or "No EXIF data in the file."
    return info


@mcp.tool(annotations=WRITE)
@_tool_errors
async def copy_picked_to_album(
    session_id: str,
    media_item_ids: list[str],
    album_id: str | None = None,
    new_album_title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Put copies of picked photos into a connector-created album.

    The API cannot add existing library items to albums, so this downloads each
    original and uploads it again. The user's library gets a second copy of each
    item; tell them that before doing it.

    Args:
        session_id: The picker session.
        media_item_ids: Ids from list_picked_media.
        album_id: An existing connector-created album, or
        new_album_title: create a new album with this title.
        description: Optional caption applied to every copy.
    """
    if not media_item_ids:
        raise ValueError("Give at least one media item id.")
    album_id = await _resolve_album(album_id, new_album_title)
    items = [await _find_picked(session_id, i) for i in media_item_ids]
    files = []
    for item in items:
        mf = item.get("mediaFile", {})
        if not mf.get("baseUrl"):
            raise ValueError(f"Media item {item['id']} has no downloadable content.")
        suffix = "dv" if item.get("type") == "VIDEO" else "d"

        async def load(url=mf["baseUrl"], suffix=suffix) -> bytes:
            return (await get_client().download(url, suffix))[0]

        files.append({
            "load": load,
            "mime_type": mf.get("mimeType") or "image/jpeg",
            "file_name": mf.get("filename") or f"{item['id']}.jpg",
            "description": description,
        })
    results = await get_client().upload_many(files, album_id)
    return _upload_report(album_id, [i["id"] for i in items], results)


async def _resolve_album(album_id: str | None, new_album_title: str | None) -> str | None:
    if album_id and new_album_title:
        raise ValueError("Give album_id or new_album_title, not both.")
    if new_album_title:
        if not new_album_title.strip():
            raise ValueError("Album title cannot be empty.")
        return (await get_client().create_album(new_album_title))["id"]
    return album_id


def _upload_report(album_id: str | None, source_ids: list, results: list[dict[str, Any]]) -> dict[str, Any]:
    ok, failed = [], []
    for src, r in zip(source_ids, results):
        if "mediaItem" in r:
            ok.append({"source": src, "media_item_id": r["mediaItem"]["id"]})
        else:
            failed.append({"source": src, "error": r.get("status", {}).get("message", "unknown error")})
    return {"album_id": album_id, "uploaded": len(ok), "items": ok, "failed": failed}


# ------------------------------------------------------- whole-library tools


def _run(fn, *args, **kwargs):
    import asyncio

    return asyncio.to_thread(fn, *args, **kwargs)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@_tool_errors
async def index_local_library(folder: str) -> dict[str, Any]:
    """Index a Google Takeout export of Google Photos, or any folder of photos.

    How the user gets the export: takeout.google.com -> deselect all -> Google
    Photos -> export, then unzip every part into one folder. Point this at that
    folder (or its "Takeout/Google Photos" subfolder).

    Reads Takeout's JSON sidecars (date, GPS, description, people, favorites,
    albums) with EXIF as fallback. Safe to re-run: unchanged files are skipped,
    and files that were deleted are dropped from the index. Never modifies photos.
    For very large libraries the CLI is faster: `google-photos-mcp index <folder>`.
    """
    res = await _run(get_library().index, folder)
    out = {k: v for k, v in res.__dict__.items() if k != "errors"}
    if res.errors:
        out["error_count"] = len(res.errors)
        out["errors"] = res.errors[:20]
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@_tool_errors
async def local_library_stats() -> dict[str, Any]:
    """Overview of the indexed library: counts, years, albums, people, tags, cameras."""
    return await _run(get_library().stats)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@_tool_errors
async def search_local_photos(
    text: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    album: str | None = None,
    person: str | None = None,
    tag: str | None = None,
    media_type: Literal["PHOTO", "VIDEO"] | None = None,
    favorite: bool | None = None,
    has_location: bool | None = None,
    near_latitude: float | None = None,
    near_longitude: float | None = None,
    radius_km: float = 10.0,
    untagged: bool = False,
    newest_first: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search the indexed library. All filters combine (AND).

    Args:
        text: Words matched against filename, description, notes, tags, people, albums, camera, folder.
        start_date: YYYY-MM-DD, inclusive.
        end_date: YYYY-MM-DD, inclusive.
        album: Album name (substring).
        person: Person name from Google Photos face groups (substring).
        tag: Exact tag previously saved with annotate_local_photos.
        media_type: PHOTO or VIDEO.
        favorite: Only favorites (true) or non-favorites (false).
        has_location: Only items with (true) or without (false) GPS.
        near_latitude: With near_longitude and radius_km, items taken near a point.
        near_longitude: See near_latitude.
        radius_km: Search radius for near_*.
        untagged: Only items not yet identified (no tags and no notes).
        newest_first: Sort by date taken, newest first (default) or oldest first.
        limit: 1-200 results.
        offset: For paging.
    """
    for d in (start_date, end_date):
        if d:
            _parse_date(d)
    near = None
    if (near_latitude is None) != (near_longitude is None):
        raise ValueError("Give both near_latitude and near_longitude.")
    if near_latitude is not None:
        near = (near_latitude, near_longitude, radius_km)
    return await _run(
        get_library().search, text=text, start_date=start_date, end_date=end_date, album=album,
        person=person, tag=tag, media_type=media_type, favorite=favorite, has_location=has_location,
        near=near, untagged=untagged, newest_first=newest_first,
        limit=max(1, min(limit, 200)), offset=max(0, offset),
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
@_tool_errors
async def view_local_photos(photo_ids: list[int], max_size: int = 768) -> list:
    """Look at indexed photos (up to 20 at a time) with their metadata.

    Use this to identify what's in photos, then save the result with
    annotate_local_photos. Videos and RAW files are described but not shown.
    """
    if not photo_ids:
        raise ValueError("Give at least one photo id.")
    max_size = max(64, min(max_size, 2048 if len(photo_ids) == 1 else 1024))
    photos = await _run(get_library().get, photo_ids[:MAX_BATCH_IMAGES])
    out: list = []
    for ph in photos:
        out.append(ph)
        if ph.get("media_type") != "PHOTO":
            continue
        try:
            out.append(Image(data=await _run(thumbnail_jpeg, ph["path"], max_size), format="jpeg"))
        except Exception as e:  # unsupported format, missing file
            out.append(f"(Could not render photo {ph['id']}: {e})")
    missing = sorted(set(photo_ids[:MAX_BATCH_IMAGES]) - {p["id"] for p in photos})
    if missing:
        out.append({"unknown_ids": missing})
    if len(photo_ids) > MAX_BATCH_IMAGES:
        out.append(f"Showed the first {MAX_BATCH_IMAGES}; call again for the rest.")
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
@_tool_errors
async def annotate_local_photos(
    photo_ids: list[int],
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    notes: str | None = None,
) -> str:
    """Save identifications for photos: tags (e.g. "dog", "beach", "receipt") and a note.

    Stored in the local index only (Google's API cannot edit existing photos),
    and searchable with search_local_photos. Tags are lower-cased. A note
    replaces any previous note; pass "" to clear it. The same tags/note apply to
    every id given, so batch photos that share them.
    """
    if not photo_ids:
        raise ValueError("Give at least one photo id.")
    if not (add_tags or remove_tags or notes is not None):
        raise ValueError("Nothing to save: give add_tags, remove_tags or notes.")
    n = await _run(get_library().annotate, photo_ids, add_tags, remove_tags, notes)
    return f"Updated {n} photo(s)." + ("" if n == len(set(photo_ids)) else " Some ids were not found.")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
@_tool_errors
async def export_local_photos(
    photo_ids: list[int],
    destination_dir: str,
    layout: Literal["year-month", "year", "album", "flat"] = "year-month",
) -> dict[str, Any]:
    """Copy indexed photos into an organized folder structure.

    Originals are never moved or deleted. Name clashes get " (1)" suffixes.

    Args:
        photo_ids: Photos to copy (e.g. from search_local_photos).
        destination_dir: Folder to copy into.
        layout: year-month -> 2024/06/, year -> 2024/, album -> <album name>/, flat.
    """
    if not photo_ids:
        raise ValueError("Give at least one photo id.")
    res = await _run(get_library().export, photo_ids, destination_dir, layout)
    if len(res["files"]) > 50:
        res["files"] = res["files"][:50] + [f"... and {len(res['files']) - 50} more"]
    return res


@mcp.tool(annotations=WRITE)
@_tool_errors
async def upload_local_photos(
    photo_ids: list[int],
    album_id: str | None = None,
    new_album_title: str | None = None,
    use_notes_as_description: bool = False,
) -> dict[str, Any]:
    """Upload indexed photos to Google Photos, optionally into an album.

    Use this to build albums from a search (e.g. all photos tagged "wedding").
    If the photos came from a Takeout export they are already in the user's
    library, so this adds copies; confirm with the user first.

    Args:
        photo_ids: Photos to upload.
        album_id: An existing connector-created album, or
        new_album_title: create a new album with this title.
        use_notes_as_description: Use each photo's saved note (or Takeout description) as its caption.
    """
    if not photo_ids:
        raise ValueError("Give at least one photo id.")
    photos = await _run(get_library().get, photo_ids)
    if not photos:
        raise ValueError("None of those photo ids are in the index.")
    album_id = await _resolve_album(album_id, new_album_title)
    files = []
    for ph in photos:
        path = Path(ph["path"])

        async def load(path=path) -> bytes:
            return await _run(path.read_bytes)

        desc = (ph.get("notes") or ph.get("description")) if use_notes_as_description else None
        files.append({
            "load": load,
            "mime_type": ph.get("mime_type") or "application/octet-stream",
            "file_name": path.name,
            "description": desc[:1000] if desc else None,
        })
    results = await get_client().upload_many(files, album_id)
    return _upload_report(album_id, [p["id"] for p in photos], results)
