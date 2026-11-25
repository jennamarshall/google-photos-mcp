"""Image helpers: thumbnails and EXIF extraction (Pillow, optional HEIC support)."""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps

try:  # HEIC/HEIF (iPhone photos) if pillow-heif is installed
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover
    HEIC_SUPPORTED = False

Image.MAX_IMAGE_PIXELS = 200_000_000  # allow big panoramas, still guard against bombs

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif",
}
RAW_EXTS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".raf"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".webm", ".mts", ".m2ts", ".wmv", ".mpg"}
MEDIA_EXTS = IMAGE_EXTS | RAW_EXTS | VIDEO_EXTS

_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
_GPS_IFD = 0x8825
_EXIF_IFD = 0x8769


def media_type_for(path: str | Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS:
        return "VIDEO"
    if ext in IMAGE_EXTS or ext in RAW_EXTS:
        return "PHOTO"
    return None


def thumbnail_jpeg(source: str | Path | bytes, max_size: int = 1024, quality: int = 85) -> bytes:
    """Return a JPEG no larger than max_size on its longest edge, upright per EXIF."""
    src = io.BytesIO(source) if isinstance(source, bytes) else source
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_size, max_size))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)
        return out.getvalue()


def _ratio(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return float(v[0]) / float(v[1])


def _gps(gps: dict) -> tuple[float, float] | None:
    try:
        lat = gps[2]
        lon = gps[4]
        to_deg = lambda d: _ratio(d[0]) + _ratio(d[1]) / 60 + _ratio(d[2]) / 3600  # noqa: E731
        la, lo = to_deg(lat), to_deg(lon)
        if gps.get(1) in ("S", b"S"):
            la = -la
        if gps.get(3) in ("W", b"W"):
            lo = -lo
        if la == 0 and lo == 0:
            return None
        return round(la, 6), round(lo, 6)
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError):
        return None


def read_exif(source: str | Path | bytes) -> dict[str, Any]:
    """Best-effort EXIF summary: taken time, camera, lens, GPS, dimensions."""
    src = io.BytesIO(source) if isinstance(source, bytes) else source
    out: dict[str, Any] = {}
    try:
        with Image.open(src) as im:
            out["width"], out["height"] = im.size
            exif = im.getexif()
            sub = exif.get_ifd(_EXIF_IFD)
            gps = exif.get_ifd(_GPS_IFD)
    except Exception:  # unreadable / unsupported (e.g. RAW, video)
        return out

    def g(name: str):
        tag = _TAGS.get(name)
        v = sub.get(tag) if tag in sub else exif.get(tag)
        return v.strip("\x00 ").strip() if isinstance(v, str) else v

    taken = g("DateTimeOriginal") or g("DateTime")
    if isinstance(taken, str):
        try:
            out["taken_time"] = datetime.strptime(taken, "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    camera = " ".join(x for x in (g("Make"), g("Model")) if isinstance(x, str) and x)
    if camera:
        out["camera"] = camera
    if isinstance(g("LensModel"), str) and g("LensModel"):
        out["lens"] = g("LensModel")
    for key, name in (("iso", "ISOSpeedRatings"), ("f_number", "FNumber"), ("focal_length", "FocalLength")):
        v = g(name)
        if v is not None:
            try:
                out[key] = round(_ratio(v), 2)
            except Exception:
                pass
    exposure = g("ExposureTime")
    if exposure is not None:
        try:
            e = _ratio(exposure)
            out["exposure"] = f"1/{round(1 / e)}s" if 0 < e < 1 else f"{e}s"
        except Exception:
            pass
    loc = _gps(gps) if gps else None
    if loc:
        out["latitude"], out["longitude"] = loc
    return out


def timestamp_to_iso(ts: str | int | float) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
