"""Async HTTP client for the Google Photos Picker API and Library API."""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any, Protocol

import httpx

LIBRARY_BASE = "https://photoslibrary.googleapis.com/v1"
PICKER_BASE = "https://photospicker.googleapis.com/v1"

# Google's documented limit for a single upload is 200 MB (photos) / 20 GB (video);
# we keep raw uploads to something sensible for a single request.
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
BATCH_LIMIT = 50  # max items per batchCreate / batchAdd / batchRemove call


class TokenSource(Protocol):
    def token(self) -> str: ...


class GooglePhotosError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Google Photos API error {status}: {message}")
        self.status = status
        self.message = message


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    message = resp.text
    try:
        body = resp.json()
        message = body.get("error", {}).get("message", message)
    except ValueError:
        pass
    raise GooglePhotosError(resp.status_code, message)


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class GooglePhotosClient:
    def __init__(self, tokens: TokenSource, http: httpx.AsyncClient | None = None):
        self._tokens = tokens
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _headers(self) -> dict[str, str]:
        # Refreshing may hit the network synchronously; keep it off the event loop.
        token = await asyncio.to_thread(self._tokens.token)
        return {"Authorization": f"Bearer {token}"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        h = await self._headers()
        if headers:
            h.update(headers)
        resp = await self._http.request(
            method, url, params=_clean(params or {}), json=json, content=content, headers=h
        )
        _raise_for_status(resp)
        return resp

    async def _json(self, method: str, url: str, **kw: Any) -> dict[str, Any]:
        resp = await self._request(method, url, **kw)
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------ Picker API

    async def create_picker_session(self, max_item_count: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if max_item_count:
            body["pickingConfig"] = {"maxItemCount": str(max_item_count)}
        return await self._json("POST", f"{PICKER_BASE}/sessions", json=body)

    async def get_picker_session(self, session_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{PICKER_BASE}/sessions/{session_id}")

    async def delete_picker_session(self, session_id: str) -> None:
        await self._request("DELETE", f"{PICKER_BASE}/sessions/{session_id}")

    async def list_picked_media_items(
        self, session_id: str, page_size: int = 50, page_token: str | None = None
    ) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"{PICKER_BASE}/mediaItems",
            params={"sessionId": session_id, "pageSize": page_size, "pageToken": page_token},
        )

    # ----------------------------------------------------------------- Library API

    async def list_albums(self, page_size: int = 20, page_token: str | None = None) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"{LIBRARY_BASE}/albums",
            params={
                "pageSize": page_size,
                "pageToken": page_token,
                "excludeNonAppCreatedData": "true",
            },
        )

    async def get_album(self, album_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{LIBRARY_BASE}/albums/{album_id}")

    async def create_album(self, title: str) -> dict[str, Any]:
        return await self._json("POST", f"{LIBRARY_BASE}/albums", json={"album": {"title": title}})

    async def update_album(
        self, album_id: str, title: str | None = None, cover_media_item_id: str | None = None
    ) -> dict[str, Any]:
        album: dict[str, Any] = {}
        mask = []
        if title is not None:
            album["title"] = title
            mask.append("title")
        if cover_media_item_id is not None:
            album["coverPhotoMediaItemId"] = cover_media_item_id
            mask.append("coverPhotoMediaItemId")
        if not mask:
            raise ValueError("Nothing to update: give a title and/or a cover media item id.")
        return await self._json(
            "PATCH",
            f"{LIBRARY_BASE}/albums/{album_id}",
            params={"updateMask": ",".join(mask)},
            json=album,
        )

    async def add_album_text(self, album_id: str, text: str) -> dict[str, Any]:
        return await self._json(
            "POST",
            f"{LIBRARY_BASE}/albums/{album_id}:addEnrichment",
            json={
                "newEnrichmentItem": {"textEnrichment": {"text": text}},
                "albumPosition": {"position": "LAST_IN_ALBUM"},
            },
        )

    async def add_to_album(self, album_id: str, media_item_ids: list[str]) -> None:
        for i in range(0, len(media_item_ids), BATCH_LIMIT):
            await self._request(
                "POST",
                f"{LIBRARY_BASE}/albums/{album_id}:batchAddMediaItems",
                json={"mediaItemIds": media_item_ids[i : i + BATCH_LIMIT]},
            )

    async def remove_from_album(self, album_id: str, media_item_ids: list[str]) -> None:
        for i in range(0, len(media_item_ids), BATCH_LIMIT):
            await self._request(
                "POST",
                f"{LIBRARY_BASE}/albums/{album_id}:batchRemoveMediaItems",
                json={"mediaItemIds": media_item_ids[i : i + BATCH_LIMIT]},
            )

    async def search_media_items(
        self,
        album_id: str | None = None,
        filters: dict[str, Any] | None = None,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        if album_id and filters:
            raise ValueError("Google Photos does not allow filters together with an album id.")
        body = _clean(
            {"albumId": album_id, "filters": filters, "pageSize": page_size, "pageToken": page_token}
        )
        if not album_id:
            body.setdefault("filters", {})["excludeNonAppCreatedData"] = True
        return await self._json("POST", f"{LIBRARY_BASE}/mediaItems:search", json=body)

    async def get_media_item(self, media_item_id: str) -> dict[str, Any]:
        return await self._json("GET", f"{LIBRARY_BASE}/mediaItems/{media_item_id}")

    async def update_media_description(self, media_item_id: str, description: str) -> dict[str, Any]:
        return await self._json(
            "PATCH",
            f"{LIBRARY_BASE}/mediaItems/{media_item_id}",
            params={"updateMask": "description"},
            json={"description": description},
        )

    async def upload_bytes(self, data: bytes, mime_type: str, file_name: str) -> str:
        resp = await self._request(
            "POST",
            f"{LIBRARY_BASE}/uploads",
            content=data,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Goog-Upload-Content-Type": mime_type,
                "X-Goog-Upload-File-Name": file_name,
                "X-Goog-Upload-Protocol": "raw",
            },
        )
        return resp.text.strip()

    async def batch_create(
        self, items: list[dict[str, Any]], album_id: str | None = None
    ) -> dict[str, Any]:
        """items: [{"uploadToken", "fileName", "description"?}] (max 50)."""
        new_items = [
            _clean(
                {
                    "description": it.get("description"),
                    "simpleMediaItem": {"uploadToken": it["uploadToken"], "fileName": it["fileName"]},
                }
            )
            for it in items
        ]
        return await self._json(
            "POST",
            f"{LIBRARY_BASE}/mediaItems:batchCreate",
            json=_clean({"albumId": album_id, "newMediaItems": new_items}),
        )

    async def upload_file(
        self, path: Path, album_id: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        path = path.expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"No such file: {path}")
        size = path.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(f"{path.name} is {size} bytes; the limit here is {MAX_UPLOAD_BYTES}.")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not (mime.startswith("image/") or mime.startswith("video/")):
            raise ValueError(f"{path.name} does not look like a photo or video ({mime}).")
        data = await asyncio.to_thread(path.read_bytes)
        token = await self.upload_bytes(data, mime, path.name)
        result = await self.batch_create(
            [{"uploadToken": token, "fileName": path.name, "description": description}], album_id
        )
        return result["newMediaItemResults"][0]

    # ------------------------------------------------------------------ Content

    async def download(self, base_url: str, suffix: str) -> tuple[bytes, str]:
        """Fetch bytes for a baseUrl. Picker baseUrls need the bearer token too."""
        resp = await self._request("GET", f"{base_url}={suffix}")
        return resp.content, resp.headers.get("content-type", "application/octet-stream")
