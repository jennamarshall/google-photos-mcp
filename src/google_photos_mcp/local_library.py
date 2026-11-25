"""A searchable local index of a Google Photos library.

Google's API no longer lets apps read a user's whole library, but users can
export everything with Google Takeout (takeout.google.com -> Google Photos).
This module indexes such an export, or any folder of photos, into SQLite:

* Takeout sidecar JSON gives the taken time, GPS, description, people
  (face-group names), favorites, and album membership.
* For files without a sidecar it falls back to EXIF.
* Copies of a photo that appear in several album folders are merged into one
  record that lists every album.

Claude's own identifications (tags and notes) are stored alongside, so a later
search for "dog" or "receipt" finds photos that were looked at before.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .imaging import media_type_for, read_exif, timestamp_to_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    dedup_key TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,
    root TEXT NOT NULL,
    folder TEXT,
    filename TEXT NOT NULL,
    media_type TEXT,
    mime_type TEXT,
    size INTEGER,
    taken_time TEXT,
    latitude REAL,
    longitude REAL,
    description TEXT,
    camera TEXT,
    width INTEGER,
    height INTEGER,
    favorite INTEGER DEFAULT 0,
    metadata_source TEXT,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS photos_taken ON photos(taken_time);
CREATE TABLE IF NOT EXISTS photo_paths (
    path TEXT PRIMARY KEY,
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    root TEXT NOT NULL,
    mtime REAL,
    size INTEGER
);
CREATE INDEX IF NOT EXISTS photo_paths_photo ON photo_paths(photo_id);
CREATE TABLE IF NOT EXISTS albums (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    PRIMARY KEY (photo_id, name)
);
CREATE TABLE IF NOT EXISTS people (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    PRIMARY KEY (photo_id, name)
);
CREATE TABLE IF NOT EXISTS tags (
    photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (photo_id, tag)
);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
"""

_YEAR_FOLDER = re.compile(r"^Photos from \d{4}$", re.I)
_DUP_MEDIA = re.compile(r"^(?P<stem>.*?)\((?P<n>\d+)\)(?P<ext>\.[^.]+)$")
_DUP_JSON = re.compile(r"^(?P<base>.*?)(?P<dup>\(\d+\))$")
_SUPPLEMENTAL = "supplemental-metadata"
_EDITED = re.compile(r"-(edited|bearbeitet|modifié|editado|modificato|bewerkt)$", re.I)


# ----------------------------------------------------------- sidecar matching


def _json_key(json_name: str) -> tuple[str, str]:
    """'IMG_1.JPG.supplemental-metadata(1).json' -> ('IMG_1.JPG', '(1)')."""
    base = json_name[: -len(".json")]
    dup = ""
    m = _DUP_JSON.match(base)
    if m:
        base, dup = m.group("base"), m.group("dup")
    if "." in base:
        head, last = base.rsplit(".", 1)
        # '.supplemental-metadata' and its truncations ('.supplemental-me', '.s', '.')
        if head and _SUPPLEMENTAL.startswith(last.lower()):
            base = head
    return base, dup


def match_sidecars(media_names: Iterable[str], json_names: Iterable[str]) -> dict[str, str]:
    """Map media filenames to their Takeout sidecar JSON filename."""
    keys: dict[tuple[str, str], str] = {}
    for j in json_names:
        if not j.lower().endswith(".json") or j.lower() in ("metadata.json", "print-subscriptions.json"):
            continue
        keys.setdefault(_json_key(j), j)
    by_dup: dict[str, list[tuple[str, str]]] = {}
    for (base, dup), j in keys.items():
        by_dup.setdefault(dup, []).append((base, j))

    result: dict[str, str] = {}
    for name in media_names:
        dup = ""
        orig = name
        m = _DUP_MEDIA.match(name)
        if m:
            orig, dup = m.group("stem") + m.group("ext"), f"({m.group('n')})"
        stem, ext = os.path.splitext(orig)
        unedited = _EDITED.sub("", stem) + ext
        candidates = [orig, unedited, stem, _EDITED.sub("", stem)]
        found = next((keys[(c, dup)] for c in candidates if (c, dup) in keys), None)
        if found is None:
            # Takeout truncates long names (to ~46 chars) before adding '.json'.
            best = None
            for base, j in by_dup.get(dup, []):
                if len(base) >= 30 and len(base) < len(unedited) and unedited.startswith(base):
                    if best is None or len(base) > len(best[0]):
                        best = (base, j)
            found = best[1] if best else None
        if found:
            result[name] = found
    return result


def _album_title(dirpath: Path, root: Path, files: list[str], has_sidecars: bool) -> str | None:
    if "metadata.json" in files:
        try:
            title = json.loads((dirpath / "metadata.json").read_text("utf-8")).get("title")
            if title:
                return str(title)
        except (OSError, ValueError, AttributeError):
            pass
    if dirpath == root or _YEAR_FOLDER.match(dirpath.name):
        return None
    if has_sidecars and dirpath.name not in ("Google Photos", "Google Fotos", "Takeout"):
        return dirpath.name
    return None


def _from_sidecar(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    t = (data.get("photoTakenTime") or {}).get("timestamp")
    if t and str(t) != "0":
        out["taken_time"] = timestamp_to_iso(t)
    for key in ("geoData", "geoDataExif"):
        geo = data.get(key) or {}
        la, lo = geo.get("latitude"), geo.get("longitude")
        if la and lo:  # Takeout writes 0.0/0.0 for "no location"
            out["latitude"], out["longitude"] = float(la), float(lo)
            break
    if data.get("description"):
        out["description"] = str(data["description"]).strip() or None
    out["people"] = [p["name"] for p in data.get("people", []) if isinstance(p, dict) and p.get("name")]
    out["favorite"] = bool(data.get("favorited"))
    return out


# -------------------------------------------------------------------- library


@dataclass
class IndexResult:
    root: str
    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    with_sidecar: int = 0
    errors: list[str] = field(default_factory=list)


def _haversine_km(la1: float, lo1: float, la2: float, lo2: float) -> float:
    p1, p2 = math.radians(la1), math.radians(la2)
    dp, dl = p2 - p1, math.radians(lo2 - lo1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


class LocalLibrary:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA journal_mode = WAL")
        return db

    # ---------------------------------------------------------------- indexing

    def index(self, root: str | Path) -> IndexResult:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Not a folder: {root}")
        res = IndexResult(root=str(root))
        seen: set[str] = set()
        with closing(self._connect()) as db:
            known = {
                r["path"]: (r["mtime"], r["size"])
                for r in db.execute("SELECT path, mtime, size FROM photo_paths WHERE root = ?", (str(root),))
            }
            for dirpath_s, dirnames, files in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                dirpath = Path(dirpath_s)
                media = [f for f in files if media_type_for(f) and not f.startswith(".")]
                if not media:
                    continue
                jsons = [f for f in files if f.lower().endswith(".json")]
                sidecars = match_sidecars(media, jsons) if jsons else {}
                album = _album_title(dirpath, root, files, bool(sidecars))
                folder = str(dirpath.relative_to(root)) if dirpath != root else ""
                for name in media:
                    p = dirpath / name
                    ps = str(p)
                    seen.add(ps)
                    res.scanned += 1
                    try:
                        st = p.stat()
                    except OSError as e:
                        res.errors.append(f"{ps}: {e}")
                        continue
                    if known.get(ps) == (st.st_mtime, st.st_size):
                        res.unchanged += 1
                        continue
                    try:
                        added = self._index_file(
                            db, root, p, folder, st, album,
                            dirpath / sidecars[name] if name in sidecars else None,
                        )
                    except Exception as e:  # keep going on one bad file
                        res.errors.append(f"{ps}: {e}")
                        continue
                    if name in sidecars:
                        res.with_sidecar += 1
                    if added:
                        res.added += 1
                    else:
                        res.updated += 1
                db.commit()
            stale = [p for p in known if p not in seen]
            for p in stale:
                db.execute("DELETE FROM photo_paths WHERE path = ?", (p,))
            # Repoint photos whose primary path vanished; drop photos with no paths left.
            db.execute(
                """UPDATE photos SET path = (SELECT pp.path FROM photo_paths pp WHERE pp.photo_id = photos.id LIMIT 1)
                   WHERE path NOT IN (SELECT path FROM photo_paths)
                   AND EXISTS (SELECT 1 FROM photo_paths pp WHERE pp.photo_id = photos.id)"""
            )
            cur = db.execute("DELETE FROM photos WHERE id NOT IN (SELECT photo_id FROM photo_paths)")
            res.removed = cur.rowcount
            db.commit()
        return res

    def _index_file(
        self,
        db: sqlite3.Connection,
        root: Path,
        p: Path,
        folder: str,
        st: os.stat_result,
        album: str | None,
        sidecar: Path | None,
    ) -> bool:
        meta: dict[str, Any] = {}
        source = "file"
        if sidecar is not None:
            try:
                meta = _from_sidecar(json.loads(sidecar.read_text("utf-8")))
                source = "takeout"
            except (OSError, ValueError):
                meta = {}
        mtype = media_type_for(p.name)
        exif: dict[str, Any] = {}
        if mtype == "PHOTO":
            exif = read_exif(p)
            if source == "file" and exif:
                source = "exif"
        people = meta.pop("people", [])
        row = {
            "taken_time": meta.get("taken_time") or exif.get("taken_time"),
            "latitude": meta.get("latitude", exif.get("latitude")),
            "longitude": meta.get("longitude", exif.get("longitude")),
            "description": meta.get("description"),
            "camera": exif.get("camera"),
            "width": exif.get("width"),
            "height": exif.get("height"),
            "favorite": int(bool(meta.get("favorite"))),
        }
        name_key = _DUP_MEDIA.sub(r"\g<stem>\g<ext>", p.name).lower()
        # Name + size, not time: sidecar times are UTC while EXIF times are local.
        key = f"{name_key}|{st.st_size}"
        existing = db.execute("SELECT id FROM photos WHERE dedup_key = ?", (key,)).fetchone()
        if existing:
            pid = existing["id"]
            # Fill gaps without clobbering richer data from another copy.
            sets = ", ".join(f"{k} = COALESCE({k}, ?)" for k in row if k != "favorite")
            db.execute(
                f"UPDATE photos SET {sets}, favorite = MAX(favorite, ?) WHERE id = ?",
                [v for k, v in row.items() if k != "favorite"] + [row["favorite"], pid],
            )
            added = False
        else:
            cur = db.execute(
                """INSERT INTO photos (dedup_key, path, root, folder, filename, media_type, mime_type, size,
                   taken_time, latitude, longitude, description, camera, width, height, favorite, metadata_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, str(p), str(root), folder, p.name, mtype, mimetypes.guess_type(p.name)[0],
                 st.st_size, row["taken_time"], row["latitude"], row["longitude"], row["description"],
                 row["camera"], row["width"], row["height"], row["favorite"], source),
            )
            pid = cur.lastrowid
            added = True
        db.execute(
            "INSERT OR REPLACE INTO photo_paths (path, photo_id, root, mtime, size) VALUES (?,?,?,?,?)",
            (str(p), pid, str(root), st.st_mtime, st.st_size),
        )
        if album:
            db.execute("INSERT OR IGNORE INTO albums (photo_id, name) VALUES (?, ?)", (pid, album))
        for person in people:
            db.execute("INSERT OR IGNORE INTO people (photo_id, name) VALUES (?, ?)", (pid, person))
        return added

    # ------------------------------------------------------------------ reading

    def _hydrate(self, db: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        extra: dict[int, dict[str, list[str]]] = {i: {"albums": [], "people": [], "tags": []} for i in ids}
        for table, col, key in (("albums", "name", "albums"), ("people", "name", "people"), ("tags", "tag", "tags")):
            for r in db.execute(f"SELECT photo_id, {col} FROM {table} WHERE photo_id IN ({marks}) ORDER BY {col}", ids):
                extra[r["photo_id"]][key].append(r[col])
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys() if k not in ("dedup_key", "root", "metadata_source")}
            d["favorite"] = bool(d["favorite"])
            d.update({k: v for k, v in extra[r["id"]].items() if v})
            out.append({k: v for k, v in d.items() if v not in (None, "", False)})
        return out

    def get(self, ids: list[int]) -> list[dict[str, Any]]:
        if not ids:
            return []
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT * FROM photos WHERE id IN ({','.join('?' * len(ids))})", ids
            ).fetchall()
            by_id = {r["id"]: r for r in rows}
            return self._hydrate(db, [by_id[i] for i in ids if i in by_id])

    def search(
        self,
        text: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        album: str | None = None,
        person: str | None = None,
        tag: str | None = None,
        media_type: str | None = None,
        favorite: bool | None = None,
        has_location: bool | None = None,
        near: tuple[float, float, float] | None = None,
        untagged: bool = False,
        newest_first: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        where, args = [], []
        if text:
            for word in text.split():
                like = f"%{word}%"
                where.append(
                    """(p.filename LIKE ? OR p.description LIKE ? OR p.notes LIKE ? OR p.camera LIKE ?
                        OR p.folder LIKE ?
                        OR EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id AND t.tag LIKE ?)
                        OR EXISTS (SELECT 1 FROM people x WHERE x.photo_id = p.id AND x.name LIKE ?)
                        OR EXISTS (SELECT 1 FROM albums a WHERE a.photo_id = p.id AND a.name LIKE ?))"""
                )
                args += [like] * 8
        if start_date:
            where.append("p.taken_time >= ?")
            args.append(start_date)
        if end_date:
            where.append("p.taken_time < ?")
            args.append(end_date + "T99")  # inclusive of the whole end day
        if album:
            where.append("EXISTS (SELECT 1 FROM albums a WHERE a.photo_id = p.id AND a.name LIKE ?)")
            args.append(f"%{album}%")
        if person:
            where.append("EXISTS (SELECT 1 FROM people x WHERE x.photo_id = p.id AND x.name LIKE ?)")
            args.append(f"%{person}%")
        if tag:
            where.append("EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id AND t.tag = ?)")
            args.append(tag.strip().lower())
        if media_type:
            where.append("p.media_type = ?")
            args.append(media_type)
        if favorite is not None:
            where.append("p.favorite = ?")
            args.append(int(favorite))
        if has_location is not None:
            where.append("p.latitude IS NOT NULL" if has_location else "p.latitude IS NULL")
        if untagged:
            where.append("NOT EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id) AND p.notes IS NULL")
        if near:
            lat, lon, km = near
            dlat = km / 111.0
            dlon = km / max(0.01, 111.0 * math.cos(math.radians(lat)))
            where.append("p.latitude BETWEEN ? AND ? AND p.longitude BETWEEN ? AND ?")
            args += [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        order = "DESC" if newest_first else "ASC"
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT p.* FROM photos p {sql_where} ORDER BY p.taken_time IS NULL, p.taken_time {order}, p.id",
                args,
            ).fetchall()
            if near:
                lat, lon, km = near
                rows = [r for r in rows if _haversine_km(lat, lon, r["latitude"], r["longitude"]) <= km]
            total = len(rows)
            page = self._hydrate(db, rows[offset : offset + limit])
        return {"total": total, "offset": offset, "photos": page}

    def stats(self) -> dict[str, Any]:
        with closing(self._connect()) as db:
            one = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
            top = lambda sql: {r[0]: r[1] for r in db.execute(sql)}  # noqa: E731
            return {
                "total": one("SELECT COUNT(*) FROM photos"),
                "photos": one("SELECT COUNT(*) FROM photos WHERE media_type = 'PHOTO'"),
                "videos": one("SELECT COUNT(*) FROM photos WHERE media_type = 'VIDEO'"),
                "roots": [r[0] for r in db.execute("SELECT DISTINCT root FROM photos")],
                "earliest": one("SELECT MIN(taken_time) FROM photos"),
                "latest": one("SELECT MAX(taken_time) FROM photos"),
                "with_location": one("SELECT COUNT(*) FROM photos WHERE latitude IS NOT NULL"),
                "favorites": one("SELECT COUNT(*) FROM photos WHERE favorite = 1"),
                "not_yet_identified": one(
                    "SELECT COUNT(*) FROM photos p WHERE notes IS NULL "
                    "AND NOT EXISTS (SELECT 1 FROM tags t WHERE t.photo_id = p.id)"
                ),
                "by_year": top(
                    "SELECT substr(taken_time,1,4) y, COUNT(*) FROM photos WHERE taken_time IS NOT NULL "
                    "GROUP BY y ORDER BY y"
                ),
                "albums": top("SELECT name, COUNT(*) c FROM albums GROUP BY name ORDER BY c DESC LIMIT 50"),
                "people": top("SELECT name, COUNT(*) c FROM people GROUP BY name ORDER BY c DESC LIMIT 50"),
                "tags": top("SELECT tag, COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC LIMIT 100"),
                "cameras": top(
                    "SELECT camera, COUNT(*) c FROM photos WHERE camera IS NOT NULL "
                    "GROUP BY camera ORDER BY c DESC LIMIT 20"
                ),
            }

    # ------------------------------------------------------------------ writing

    def annotate(
        self,
        ids: list[int],
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        notes: str | None = None,
    ) -> int:
        add = sorted({t.strip().lower() for t in add_tags or [] if t.strip()})
        rem = sorted({t.strip().lower() for t in remove_tags or [] if t.strip()})
        with closing(self._connect()) as db:
            found = [r[0] for r in db.execute(
                f"SELECT id FROM photos WHERE id IN ({','.join('?' * len(ids))})", ids
            )] if ids else []
            for pid in found:
                db.executemany("INSERT OR IGNORE INTO tags (photo_id, tag) VALUES (?, ?)", [(pid, t) for t in add])
                db.executemany("DELETE FROM tags WHERE photo_id = ? AND tag = ?", [(pid, t) for t in rem])
                if notes is not None:
                    db.execute("UPDATE photos SET notes = ? WHERE id = ?", (notes.strip() or None, pid))
            db.commit()
        return len(found)

    def export(self, ids: list[int], destination: str | Path, layout: str = "year-month") -> dict[str, Any]:
        """Copy photos into destination, arranged by layout. Never moves or deletes."""
        dest = Path(destination).expanduser().resolve()
        photos = self.get(ids)
        copied, skipped = [], []
        for ph in photos:
            src = Path(ph["path"])
            if layout == "flat":
                sub = Path()
            elif layout == "album":
                sub = Path(_safe_name((ph.get("albums") or ["No album"])[0]))
            elif layout == "year":
                sub = Path((ph.get("taken_time") or "Unknown date")[:4])
            else:  # year-month
                t = ph.get("taken_time")
                sub = Path(t[:4]) / t[5:7] if t else Path("Unknown date")
            target_dir = dest / sub
            target_dir.mkdir(parents=True, exist_ok=True)
            target = _unique(target_dir / src.name)
            try:
                shutil.copy2(src, target)
                copied.append({"id": ph["id"], "to": str(target)})
            except OSError as e:
                skipped.append({"id": ph["id"], "error": str(e)})
        missing = sorted(set(ids) - {p["id"] for p in photos})
        return {"copied": len(copied), "files": copied, "skipped": skipped, "unknown_ids": missing}


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Untitled"


def _unique(target: Path) -> Path:
    stem, ext, n = target.stem, target.suffix, 1
    while target.exists():
        target = target.with_name(f"{stem} ({n}){ext}")
        n += 1
    return target
