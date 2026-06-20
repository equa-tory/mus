"""
Minimal self-hosted music server.

Run:
    MUSIC_DIR=/path/to/music uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://<this-machine-ip>:8000 on phone / PC / TV.
"""

import os
import time
import json
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# --- Configuration (override with env vars) --------------------------------
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "./music")).expanduser().resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()
ART_DIR = DATA_DIR / "art"
DB_PATH = DATA_DIR / "library.db"
STATIC_DIR = Path(__file__).parent / "static"

AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".aac", ".ogg", ".opus", ".wav"}
MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav",
}

DATA_DIR.mkdir(parents=True, exist_ok=True)
ART_DIR.mkdir(parents=True, exist_ok=True)

# --- Database --------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            title TEXT, artist TEXT, album TEXT, album_artist TEXT,
            track_no INTEGER, duration REAL DEFAULT 0,
            ext TEXT, size INTEGER, mtime REAL,
            has_art INTEGER DEFAULT 0, art_ext TEXT,
            added_at REAL, last_scan INTEGER DEFAULT 0,
            play_count INTEGER DEFAULT 0, liked INTEGER DEFAULT 0,
            last_played_at REAL
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL, is_smart INTEGER DEFAULT 0,
            rules TEXT, created_at REAL
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            playlist_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            PRIMARY KEY (playlist_id, track_id),
            FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY,
            track_id INTEGER NOT NULL,
            played_at REAL,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
        );
        """)

# --- Metadata reading (mutagen) -------------------------------------------
def read_metadata(path: str):
    """Return (title, artist, album, album_artist, track_no, duration, art_bytes, art_mime)."""
    from mutagen import File as MutagenFile
    title = artist = album = album_artist = None
    track_no = None
    duration = 0.0
    art = None
    art_mime = None

    try:
        easy = MutagenFile(path, easy=True)
        if easy is not None and easy.tags is not None:
            def g(k):
                v = easy.tags.get(k)
                return v[0] if v else None
            title = g("title"); artist = g("artist")
            album = g("album"); album_artist = g("albumartist")
            tn = g("tracknumber")
            if tn:
                try: track_no = int(str(tn).split("/")[0])
                except ValueError: pass
        if easy is not None and easy.info is not None:
            duration = float(getattr(easy.info, "length", 0) or 0)
    except Exception:
        pass

    try:
        raw = MutagenFile(path)
        if raw is not None:
            if not duration and raw.info is not None:
                duration = float(getattr(raw.info, "length", 0) or 0)
            tags = raw.tags
            if tags is not None:
                # MP4 / m4a cover art
                if "covr" in getattr(tags, "keys", lambda: [])():
                    covers = tags["covr"]
                    if covers:
                        cov = covers[0]
                        art = bytes(cov)
                        art_mime = "image/png" if getattr(cov, "imageformat", None) == 14 else "image/jpeg"
                # ID3 (mp3) cover art
                if art is None and hasattr(tags, "getall"):
                    apic = tags.getall("APIC")
                    if apic:
                        art = apic[0].data
                        art_mime = apic[0].mime or "image/jpeg"
                # FLAC / Ogg pictures
                if art is None and getattr(raw, "pictures", None):
                    pic = raw.pictures[0]
                    art = pic.data
                    art_mime = pic.mime or "image/jpeg"
    except Exception:
        pass

    return title, artist, album, album_artist, track_no, duration, art, art_mime

# --- Scanning --------------------------------------------------------------
SCAN = {"running": False, "scanned": 0, "total": 0, "added": 0,
        "updated": 0, "removed": 0, "error": None, "finished_at": None}
SCAN_LOCK = threading.Lock()

def _scan_worker():
    global SCAN
    try:
        if not MUSIC_DIR.exists():
            raise FileNotFoundError(f"Music folder not found: {MUSIC_DIR}")

        files = [p for p in MUSIC_DIR.rglob("*") if p.suffix.lower() in AUDIO_EXTS and p.is_file()]
        SCAN["total"] = len(files)

        conn = db()
        cur = conn.cursor()
        scan_id = int(time.time())
        cur.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_scan',?)", (str(scan_id),))

        existing = {row["path"]: row for row in cur.execute(
            "SELECT path,id,mtime,size,has_art,art_ext FROM tracks")}

        added = updated = 0
        for p in files:
            SCAN["scanned"] += 1
            sp = str(p)
            st = p.stat()
            row = existing.get(sp)

            # Skip unchanged files (fast re-scan); just mark as seen.
            if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                cur.execute("UPDATE tracks SET last_scan=? WHERE id=?", (scan_id, row["id"]))
                continue

            title, artist, album, album_artist, track_no, duration, art, art_mime = read_metadata(sp)
            title = title or p.stem
            artist = artist or "Unknown Artist"
            album = album or "Unknown Album"
            ext = p.suffix.lower()
            art_ext = None
            has_art = 0
            if art:
                has_art = 1
                art_ext = ".png" if art_mime == "image/png" else ".jpg"

            if row:  # update existing
                cur.execute("""UPDATE tracks SET title=?,artist=?,album=?,album_artist=?,
                    track_no=?,duration=?,ext=?,size=?,mtime=?,has_art=?,art_ext=?,last_scan=?
                    WHERE id=?""",
                    (title, artist, album, album_artist, track_no, duration, ext,
                     st.st_size, st.st_mtime, has_art, art_ext, scan_id, row["id"]))
                tid = row["id"]
                # clear stale art if format changed / removed
                if row["has_art"] and row["art_ext"]:
                    old = ART_DIR / f"{tid}{row['art_ext']}"
                    if old.exists() and (not art or row["art_ext"] != art_ext):
                        old.unlink(missing_ok=True)
                updated += 1
            else:  # insert new
                cur.execute("""INSERT INTO tracks
                    (path,title,artist,album,album_artist,track_no,duration,ext,size,mtime,
                     has_art,art_ext,added_at,last_scan)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sp, title, artist, album, album_artist, track_no, duration, ext,
                     st.st_size, st.st_mtime, has_art, art_ext, time.time(), scan_id))
                tid = cur.lastrowid
                added += 1

            if art and art_ext:
                (ART_DIR / f"{tid}{art_ext}").write_bytes(art)

            SCAN["added"], SCAN["updated"] = added, updated

        # Remove tracks whose files are gone.
        gone = list(cur.execute("SELECT id,art_ext FROM tracks WHERE last_scan!=?", (scan_id,)))
        for r in gone:
            if r["art_ext"]:
                (ART_DIR / f"{r['id']}{r['art_ext']}").unlink(missing_ok=True)
        cur.execute("DELETE FROM tracks WHERE last_scan!=?", (scan_id,))
        SCAN["removed"] = len(gone)

        conn.commit()
        conn.close()
    except Exception as e:
        SCAN["error"] = str(e)
    finally:
        SCAN["running"] = False
        SCAN["finished_at"] = time.time()

# --- App -------------------------------------------------------------------
app = FastAPI(title="Music")
init_db()

def track_dict(r: sqlite3.Row):
    return {
        "id": r["id"], "title": r["title"], "artist": r["artist"],
        "album": r["album"], "albumArtist": r["album_artist"],
        "trackNo": r["track_no"], "duration": r["duration"],
        "hasArt": bool(r["has_art"]), "addedAt": r["added_at"],
        "playCount": r["play_count"], "liked": bool(r["liked"]),
        "lastPlayedAt": r["last_played_at"], "ext": r["ext"],
    }

@app.get("/api/tracks")
def get_tracks():
    with db() as c:
        rows = c.execute("SELECT * FROM tracks ORDER BY album_artist, album, track_no, title")
        return [track_dict(r) for r in rows]

@app.get("/api/stream/{track_id}")
def stream(track_id: int):
    with db() as c:
        r = c.execute("SELECT path,ext FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "Track not found")
    return FileResponse(r["path"], media_type=MIME.get(r["ext"], "application/octet-stream"))

@app.get("/api/art/{track_id}")
def art(track_id: int):
    with db() as c:
        r = c.execute("SELECT has_art,art_ext FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not r or not r["has_art"]:
        raise HTTPException(404, "No art")
    p = ART_DIR / f"{track_id}{r['art_ext']}"
    if not p.exists():
        raise HTTPException(404, "No art")
    return FileResponse(p, media_type="image/png" if r["art_ext"] == ".png" else "image/jpeg")

@app.post("/api/scan")
def scan():
    with SCAN_LOCK:
        if SCAN["running"]:
            return JSONResponse({"status": "already running"}, status_code=409)
        for k in ("scanned", "total", "added", "updated", "removed"):
            SCAN[k] = 0
        SCAN["error"] = None
        SCAN["finished_at"] = None
        SCAN["running"] = True
    threading.Thread(target=_scan_worker, daemon=True).start()
    return {"status": "started"}

@app.get("/api/scan/status")
def scan_status():
    return SCAN

@app.post("/api/like/{track_id}")
def like(track_id: int):
    with db() as c:
        r = c.execute("SELECT liked FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Track not found")
        new = 0 if r["liked"] else 1
        c.execute("UPDATE tracks SET liked=? WHERE id=?", (new, track_id))
    return {"liked": bool(new)}

@app.post("/api/play/{track_id}")
def play(track_id: int):
    now = time.time()
    with db() as c:
        r = c.execute("SELECT id FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Track not found")
        c.execute("UPDATE tracks SET play_count=play_count+1, last_played_at=? WHERE id=?",
                  (now, track_id))
        c.execute("INSERT INTO history(track_id,played_at) VALUES(?,?)", (track_id, now))
    return {"ok": True}

@app.get("/api/history")
def history(limit: int = 100):
    with db() as c:
        rows = c.execute("""SELECT h.played_at, t.* FROM history h
            JOIN tracks t ON t.id=h.track_id ORDER BY h.played_at DESC LIMIT ?""", (limit,))
        out = []
        for r in rows:
            d = track_dict(r)
            d["playedAt"] = r["played_at"]
            out.append(d)
        return out

# --- Playlists -------------------------------------------------------------
@app.get("/api/playlists")
def list_playlists():
    with db() as c:
        pls = c.execute("SELECT * FROM playlists ORDER BY name").fetchall()
        out = []
        for p in pls:
            d = {"id": p["id"], "name": p["name"], "isSmart": bool(p["is_smart"]),
                 "rules": json.loads(p["rules"]) if p["rules"] else None}
            if not p["is_smart"]:
                tids = [row["track_id"] for row in c.execute(
                    "SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                    (p["id"],))]
                d["trackIds"] = tids
            out.append(d)
        return out

@app.post("/api/playlists")
def create_playlist(body: dict = Body(...)):
    name = (body.get("name") or "Untitled").strip()
    is_smart = 1 if body.get("isSmart") else 0
    rules = json.dumps(body.get("rules")) if body.get("rules") is not None else None
    with db() as c:
        cur = c.execute("INSERT INTO playlists(name,is_smart,rules,created_at) VALUES(?,?,?,?)",
                        (name, is_smart, rules, time.time()))
        return {"id": cur.lastrowid}

@app.put("/api/playlists/{pid}")
def update_playlist(pid: int, body: dict = Body(...)):
    with db() as c:
        if "name" in body:
            c.execute("UPDATE playlists SET name=? WHERE id=?", (body["name"].strip(), pid))
        if "rules" in body:
            c.execute("UPDATE playlists SET rules=? WHERE id=?", (json.dumps(body["rules"]), pid))
    return {"ok": True}

@app.delete("/api/playlists/{pid}")
def delete_playlist(pid: int):
    with db() as c:
        c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    return {"ok": True}

@app.post("/api/playlists/{pid}/tracks")
def add_to_playlist(pid: int, body: dict = Body(...)):
    tid = body["trackId"]
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 AS n FROM playlist_tracks WHERE playlist_id=?",
                        (pid,)).fetchone()["n"]
        c.execute("INSERT OR IGNORE INTO playlist_tracks(playlist_id,track_id,position) VALUES(?,?,?)",
                  (pid, tid, pos))
    return {"ok": True}

@app.delete("/api/playlists/{pid}/tracks/{tid}")
def remove_from_playlist(pid: int, tid: int):
    with db() as c:
        c.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pid, tid))
    return {"ok": True}

# Serve the single-page frontend (declared last so /api/* wins).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
