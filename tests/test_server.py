import json

import httpx
import pytest

from google_photos_mcp import server
from google_photos_mcp.auth import StaticTokenProvider
from google_photos_mcp.client import GooglePhotosClient
from mcp.server.mcpserver.exceptions import ToolError

from helpers import make_jpeg

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = make_jpeg(gps=(48.8584, 2.2945), make="Google", model="Pixel 8")


class FakeGoogle:
    """Minimal stand-in for the Picker + Library APIs."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.albums: dict[str, dict] = {}
        self.media: dict[str, dict] = {}
        self.uploads: list[tuple[str, bytes]] = []
        self.picked = [
            {
                "id": f"p{i}",
                "type": "PHOTO",
                "createTime": "2024-06-01T10:00:00Z",
                "mediaFile": {
                    "baseUrl": f"https://lh3.googleusercontent.com/picked{i}",
                    "mimeType": "image/jpeg",
                    "filename": f"IMG_{i}.jpg",
                    "mediaFileMetadata": {"width": 4000, "height": 3000, "cameraMake": "Pixel"},
                },
            }
            for i in range(3)
        ]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        assert req.headers["authorization"] == "Bearer test-token"
        url, m = req.url, req.method
        path = url.path
        body = json.loads(req.content) if req.content and req.headers.get("content-type") == "application/json" else None

        if url.host == "photospicker.googleapis.com":
            if m == "POST" and path == "/v1/sessions":
                return httpx.Response(200, json={
                    "id": "sess1", "pickerUri": "https://photos.google.com/picker/sess1",
                    "pollingConfig": {"pollInterval": "5s"}, "mediaItemsSet": False,
                    "_body": body,
                })
            if m == "GET" and path == "/v1/sessions/sess1":
                return httpx.Response(200, json={"id": "sess1", "mediaItemsSet": True})
            if m == "DELETE" and path == "/v1/sessions/sess1":
                return httpx.Response(200, json={})
            if m == "GET" and path == "/v1/mediaItems":
                assert url.params["sessionId"] == "sess1"
                size = int(url.params["pageSize"])
                start = int(url.params.get("pageToken", 0))
                page = self.picked[start : start + size]
                nxt = str(start + size) if start + size < len(self.picked) else None
                return httpx.Response(200, json={"mediaItems": page, **({"nextPageToken": nxt} if nxt else {})})
            if m == "GET" and path == "/v1/sessions/missing":
                return httpx.Response(404, json={"error": {"code": 404, "message": "Session not found"}})

        if url.host == "lh3.googleusercontent.com":
            if path.startswith("/picked"):
                return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
            return httpx.Response(200, content=PNG + path.encode(), headers={"content-type": "image/png"})

        if url.host == "photoslibrary.googleapis.com":
            if m == "POST" and path == "/v1/albums":
                aid = f"a{len(self.albums)}"
                self.albums[aid] = {"id": aid, "title": body["album"]["title"], "isWriteable": True}
                return httpx.Response(200, json=self.albums[aid])
            if m == "GET" and path == "/v1/albums":
                assert url.params["excludeNonAppCreatedData"] == "true"
                return httpx.Response(200, json={"albums": list(self.albums.values())})
            if m == "PATCH" and path.startswith("/v1/albums/"):
                aid = path.rsplit("/", 1)[1]
                assert url.params["updateMask"] == "title"
                self.albums[aid]["title"] = body["title"]
                return httpx.Response(200, json=self.albums[aid])
            if m == "POST" and path.endswith(":batchAddMediaItems"):
                assert len(body["mediaItemIds"]) <= 50
                return httpx.Response(200, json={})
            if m == "POST" and path == "/v1/uploads":
                assert req.headers["x-goog-upload-protocol"] == "raw"
                assert req.headers["x-goog-upload-content-type"].startswith("image/")
                self.uploads.append((req.headers["x-goog-upload-file-name"], req.content))
                return httpx.Response(200, text=f"upload-token-{len(self.uploads)}")
            if m == "POST" and path == "/v1/mediaItems:batchCreate":
                assert len(body["newMediaItems"]) <= 50
                results = []
                for item in body["newMediaItems"]:
                    mid = f"m{len(self.media)}"
                    self.media[mid] = {
                        "id": mid, "filename": item["simpleMediaItem"]["fileName"],
                        "description": item.get("description"), "mimeType": "image/png",
                        "baseUrl": f"https://lh3.googleusercontent.com/lib{mid}",
                    }
                    results.append({
                        "uploadToken": item["simpleMediaItem"]["uploadToken"],
                        "status": {"message": "Success"}, "mediaItem": self.media[mid],
                    })
                return httpx.Response(200, json={"newMediaItemResults": results})
            if m == "POST" and path == "/v1/mediaItems:search":
                return httpx.Response(200, json={"mediaItems": list(self.media.values()), "_body": body})
            if m == "GET" and path.startswith("/v1/mediaItems/"):
                return httpx.Response(200, json=self.media[path.rsplit("/", 1)[1]])

        return httpx.Response(500, json={"error": {"message": f"unhandled {m} {url}"}})


@pytest.fixture
def fake():
    f = FakeGoogle()
    client = GooglePhotosClient(
        StaticTokenProvider("test-token"), httpx.AsyncClient(transport=httpx.MockTransport(f))
    )
    server.set_client(client)
    yield f
    server.set_client(None)


def _text(result) -> dict:
    return json.loads(result.content[0].text)


async def call(name, **args):
    return await server.mcp.call_tool(name, args)


async def test_tools_registered():
    names = {t.name for t in await server.mcp.list_tools()}
    assert {
        "create_picker_session", "get_picker_session", "list_picked_media", "view_picked_media",
        "download_picked_media", "delete_picker_session", "list_albums", "create_album",
        "upload_photo", "search_app_media", "view_app_media", "add_media_to_album",
    } <= names


async def test_picker_flow(fake, tmp_path):
    s = _text(await call("create_picker_session", max_items=10))
    assert s["picker_uri"].endswith("sess1")
    assert fake.requests[-1].content == b'{"pickingConfig":{"maxItemCount":"10"}}'

    assert _text(await call("get_picker_session", session_id="sess1"))["media_items_set"] is True

    page = _text(await call("list_picked_media", session_id="sess1", page_size=2))
    assert [i["id"] for i in page["media_items"]] == ["p0", "p1"]
    assert page["media_items"][0]["camera"] == "Pixel"
    assert page["next_page_token"] == "2"

    # p2 not cached yet: the tool must page through the session to find it.
    res = await call("view_picked_media", session_id="sess1", media_item_id="p2", max_size=512)
    img = res.content[1]
    assert img.type == "image" and img.mime_type == "image/jpeg"
    assert fake.requests[-1].url.path == "/picked2=w512-h512"

    out = _text(await call("download_picked_media", session_id="sess1", media_item_id="p0",
                           destination_dir=str(tmp_path)))
    assert fake.requests[-1].url.path == "/picked0=d"
    assert (tmp_path / "IMG_0.jpg").read_bytes() == JPEG
    out2 = _text(await call("download_picked_media", session_id="sess1", media_item_id="p0",
                            destination_dir=str(tmp_path)))
    assert out2["path"].endswith("IMG_0 (1).jpg") and out["path"] != out2["path"]


async def test_api_error_surfaces_message(fake):
    with pytest.raises(ToolError, match="Session not found"):
        await call("get_picker_session", session_id="missing")


async def test_album_and_upload_flow(fake, tmp_path):
    album = _text(await call("create_album", title="Trip"))
    assert album["id"] == "a0"
    assert _text(await call("update_album", album_id="a0", title="Trip 2024"))["title"] == "Trip 2024"
    assert _text(await call("list_albums"))["albums"][0]["title"] == "Trip 2024"

    photo = tmp_path / "beach.png"
    photo.write_bytes(PNG)
    item = _text(await call("upload_photo", file_path=str(photo), album_id="a0", description="Sunset"))
    assert item == {"id": "m0", "filename": "beach.png", "description": "Sunset", "mime_type": "image/png"}
    create_req = fake.requests[-1]
    assert json.loads(create_req.content)["albumId"] == "a0"

    ids = [f"m{i}" for i in range(120)]
    await call("add_media_to_album", album_id="a0", media_item_ids=ids)
    adds = [r for r in fake.requests if r.url.path.endswith(":batchAddMediaItems")]
    assert len(adds) == 3  # 50 + 50 + 20

    res = await call("view_app_media", media_item_id="m0", max_size=99999)
    assert res.content[1].type == "image"
    assert fake.requests[-1].url.path == "/libm0=w2048-h2048"


async def test_search_filters(fake):
    await call("search_app_media", start_date="2024-01-01", media_type="PHOTO")
    body = json.loads(fake.requests[-1].content)
    assert body["filters"] == {
        "dateFilter": {"ranges": [{"startDate": {"year": 2024, "month": 1, "day": 1},
                                   "endDate": {"year": 9999, "month": 12, "day": 31}}]},
        "mediaTypeFilter": {"mediaTypes": ["PHOTO"]},
        "excludeNonAppCreatedData": True,
    }
    await call("search_app_media", album_id="a0")
    assert json.loads(fake.requests[-1].content) == {"albumId": "a0", "pageSize": 25}

    with pytest.raises(ToolError, match="filters together"):
        await call("search_app_media", album_id="a0", media_type="PHOTO")
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await call("search_app_media", start_date="June 2024")


async def test_upload_rejects_non_media(fake, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hi")
    with pytest.raises(ToolError, match="photo or video"):
        await call("upload_photo", file_path=str(f))
    with pytest.raises(ToolError, match="No such file"):
        await call("upload_photo", file_path=str(tmp_path / "nope.jpg"))


async def test_view_picked_batch_and_details(fake):
    res = await call("view_picked_media_batch", session_id="sess1", limit=2)
    kinds = [c.type for c in res.content]
    assert kinds == ["text", "image", "text", "image", "text"]
    assert json.loads(res.content[-1].text) == {"next_page_token": "2"}

    details = _text(await call("get_picked_media_details", session_id="sess1", media_item_id="p1"))
    assert details["exif"]["camera"] == "Google Pixel 8"
    assert details["exif"]["latitude"] == pytest.approx(48.8584, abs=1e-4)
    assert fake.requests[-1].url.path == "/picked1=d"


async def test_copy_picked_to_new_album(fake):
    out = _text(await call("copy_picked_to_album", session_id="sess1",
                           media_item_ids=["p0", "p2"], new_album_title="Best of"))
    assert out["album_id"] == "a0" and out["uploaded"] == 2
    assert [u[0] for u in fake.uploads] == ["IMG_0.jpg", "IMG_2.jpg"]
    assert fake.uploads[0][1] == JPEG  # original bytes, not a thumbnail
    create = [r for r in fake.requests if r.url.path == "/v1/mediaItems:batchCreate"][-1]
    assert json.loads(create.content)["albumId"] == "a0"

    with pytest.raises(ToolError, match="not both"):
        await call("copy_picked_to_album", session_id="sess1", media_item_ids=["p0"],
                   album_id="a0", new_album_title="x")
