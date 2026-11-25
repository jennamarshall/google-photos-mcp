import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from google_photos_mcp import server
from google_photos_mcp.auth import StaticTokenProvider
from google_photos_mcp.client import GooglePhotosClient
from google_photos_mcp.local_library import LocalLibrary, match_sidecars
from helpers import make_jpeg
from mcp.server.mcpserver.exceptions import ToolError
from test_server import FakeGoogle


def ts(s: str) -> str:
    return str(int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()))


def sidecar(path: Path, taken: str, **extra):
    data = {"title": path.name, "photoTakenTime": {"timestamp": ts(taken)},
            "geoData": {"latitude": 0.0, "longitude": 0.0}, **extra}
    path.write_text(json.dumps(data))


@pytest.fixture
def takeout(tmp_path):
    """A small Google Takeout-shaped tree."""
    gp = tmp_path / "Takeout" / "Google Photos"
    y2023, y2024, paris = gp / "Photos from 2023", gp / "Photos from 2024", gp / "Paris trip"
    for d in (y2023, y2024, paris):
        d.mkdir(parents=True)

    eiffel = make_jpeg(color=(10, 10, 200))
    (y2024 / "IMG_0001.JPG").write_bytes(eiffel)
    sidecar(y2024 / "IMG_0001.JPG.supplemental-metadata.json", "2024-06-01T09:30:00",
            geoData={"latitude": 48.8584, "longitude": 2.2945},
            people=[{"name": "Alice"}], description="Eiffel tower", favorited=True)
    # Same photo copied into an album folder (Takeout duplicates album members).
    (paris / "IMG_0001.JPG").write_bytes(eiffel)
    sidecar(paris / "IMG_0001.JPG.supplemental-metadata.json", "2024-06-01T09:30:00")
    (paris / "metadata.json").write_text(json.dumps({"title": "Paris trip 🇫🇷"}))

    # Duplicate-name file with the "(1)" counter, truncated sidecar name.
    (y2024 / "IMG_0001(1).JPG").write_bytes(make_jpeg(size=(30, 30)))
    sidecar(y2024 / "IMG_0001.JPG.supplemental-metad(1).json", "2024-06-02T12:00:00",
            people=[{"name": "Bob"}])

    # Edited version uses the original's sidecar; old-style .json naming.
    (y2023 / "PXL_20230704.jpg").write_bytes(make_jpeg(size=(40, 40)))
    (y2023 / "PXL_20230704-edited.jpg").write_bytes(make_jpeg(size=(41, 40)))
    sidecar(y2023 / "PXL_20230704.jpg.json", "2023-07-04T20:00:00")

    # No sidecar: EXIF only.
    (y2023 / "scan.jpg").write_bytes(
        make_jpeg(taken="2023:01:15 08:00:00", gps=(40.7128, -74.006), make="Canon", model="EOS R5")
    )
    (y2023 / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (y2023 / "notes.txt").write_text("not media")
    return tmp_path


@pytest.fixture
def lib(tmp_path):
    library = LocalLibrary(tmp_path / "db" / "library.db")
    server.set_library(library)
    yield library
    server.set_library(None)


def test_match_sidecars():
    media = ["a.jpg", "a(1).jpg", "b-edited.jpg", "c.HEIC", "x" * 45 + ".jpg", "d.jpg", "e.mp4"]
    jsons = ["a.jpg.supplemental-metadata.json", "a.jpg.supplemental-metadata(1).json",
             "b.jpg.json", "c.HEIC.s.json", "x" * 45 + ".j.json", "metadata.json", "e.json"]
    got = match_sidecars(media, jsons)
    assert got == {
        "a.jpg": "a.jpg.supplemental-metadata.json",
        "a(1).jpg": "a.jpg.supplemental-metadata(1).json",
        "b-edited.jpg": "b.jpg.json",
        "c.HEIC": "c.HEIC.s.json",
        "x" * 45 + ".jpg": "x" * 45 + ".j.json",
        "e.mp4": "e.json",
    }


def test_index_takeout(takeout, lib):
    res = lib.index(takeout)
    assert res.scanned == 7 and res.errors == []
    assert res.added == 6  # the album copy merged into the year-folder record
    assert res.with_sidecar == 5

    stats = lib.stats()
    assert stats["total"] == 6 and stats["videos"] == 1
    assert stats["albums"] == {"Paris trip 🇫🇷": 1}
    assert stats["people"] == {"Alice": 1, "Bob": 1}
    assert stats["by_year"] == {"2023": 3, "2024": 2}
    assert stats["favorites"] == 1 and stats["with_location"] == 2

    eiffel = lib.search(text="eiffel")["photos"]
    assert len(eiffel) == 1
    e = eiffel[0]
    assert e["taken_time"] == "2024-06-01T09:30:00"
    assert e["albums"] == ["Paris trip 🇫🇷"] and e["people"] == ["Alice"] and e["favorite"]

    scan = lib.search(text="scan")["photos"][0]
    assert scan["camera"] == "Canon EOS R5" and scan["taken_time"] == "2023-01-15T08:00:00"
    assert scan["latitude"] == pytest.approx(40.7128, abs=1e-4)

    # Re-index is incremental and notices deletions.
    again = lib.index(takeout)
    assert again.unchanged == 7 and again.added == 0
    (takeout / "Takeout/Google Photos/Photos from 2023/scan.jpg").unlink()
    assert lib.index(takeout).removed == 1
    assert lib.stats()["total"] == 5


def test_search_filters(takeout, lib):
    lib.index(takeout)
    s = lambda **kw: [p["filename"] for p in lib.search(**kw)["photos"]]  # noqa: E731
    assert s(start_date="2024-06-01", end_date="2024-06-01") == ["IMG_0001.JPG"]
    assert s(person="bob") == ["IMG_0001(1).JPG"]
    assert s(album="paris") == ["IMG_0001.JPG"]
    assert s(media_type="VIDEO") == ["clip.mp4"]
    assert s(near=(48.85, 2.29, 5)) == ["IMG_0001.JPG"]
    assert s(near=(48.85, 2.29, 0.1)) == []
    assert set(s(has_location=True)) == {"IMG_0001.JPG", "scan.jpg"}
    assert s(newest_first=False)[0] == "scan.jpg"
    assert lib.search(limit=2, offset=1)["total"] == 6


def test_annotate_and_untagged(takeout, lib):
    lib.index(takeout)
    ids = [p["id"] for p in lib.search(media_type="PHOTO")["photos"]]
    assert lib.annotate(ids[:2], add_tags=["Dog", " beach "], notes="Golden retriever on sand") == 2
    assert lib.search(tag="dog")["total"] == 2
    assert lib.search(text="retriever")["total"] == 2
    assert lib.search(untagged=True)["total"] == 4
    lib.annotate(ids[:1], remove_tags=["dog"])
    assert lib.search(tag="dog")["total"] == 1
    assert lib.stats()["tags"] == {"beach": 2, "dog": 1}


def test_export_layouts(takeout, lib, tmp_path):
    lib.index(takeout)
    ids = [p["id"] for p in lib.search()["photos"]]
    res = lib.export(ids + [9999], tmp_path / "out")
    assert res["copied"] == 6 and res["unknown_ids"] == [9999]
    assert (tmp_path / "out/2024/06/IMG_0001.JPG").exists()
    assert (tmp_path / "out/2023/07/PXL_20230704.jpg").exists()
    lib.export(ids, tmp_path / "out")  # second run must not overwrite
    assert (tmp_path / "out/2024/06/IMG_0001 (1).JPG").exists()
    lib.export(ids, tmp_path / "albums", layout="album")
    assert (tmp_path / "albums/Paris trip 🇫🇷/IMG_0001.JPG").exists()
    # originals untouched
    assert (takeout / "Takeout/Google Photos/Photos from 2024/IMG_0001.JPG").exists()


async def test_local_tools_end_to_end(takeout, lib):
    fake = FakeGoogle()
    server.set_client(GooglePhotosClient(
        StaticTokenProvider("test-token"), httpx.AsyncClient(transport=httpx.MockTransport(fake))))
    try:
        res = await server.mcp.call_tool("index_local_library", {"folder": str(takeout)})
        assert json.loads(res.content[0].text)["added"] == 6

        found = json.loads((await server.mcp.call_tool(
            "search_local_photos", {"untagged": True, "media_type": "PHOTO", "limit": 3})).content[0].text)
        ids = [p["id"] for p in found["photos"]]
        assert found["total"] == 5 and len(ids) == 3

        view = await server.mcp.call_tool("view_local_photos", {"photo_ids": ids + [4242]})
        assert [c.type for c in view.content].count("image") == 3
        assert "4242" in view.content[-1].text

        await server.mcp.call_tool("annotate_local_photos",
                                   {"photo_ids": ids[:1], "add_tags": ["sunset"], "notes": "Sunset over the river"})
        with pytest.raises(ToolError, match="Nothing to save"):
            await server.mcp.call_tool("annotate_local_photos", {"photo_ids": ids})

        up = json.loads((await server.mcp.call_tool("upload_local_photos", {
            "photo_ids": ids[:1], "new_album_title": "Sunsets", "use_notes_as_description": True,
        })).content[0].text)
        assert up["uploaded"] == 1 and up["album_id"] == "a0"
        assert fake.media["m0"]["description"] == "Sunset over the river"

        with pytest.raises(ToolError, match="YYYY-MM-DD"):
            await server.mcp.call_tool("search_local_photos", {"start_date": "last june"})
        with pytest.raises(ToolError, match="Not a folder"):
            await server.mcp.call_tool("index_local_library", {"folder": str(takeout / "nope")})
    finally:
        server.set_client(None)
