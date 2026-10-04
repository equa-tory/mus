"""
Minimal self-hosted music server.

Run:
    MUSIC_DIR=/path/to/music uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://<this-machine-ip>:8000 on phone / PC / TV.
"""

import os
import re
import hmac
import time
import json
import hashlib
import struct
import shutil
import asyncio
import sqlite3
import threading
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException, Body, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import queue as _queue

def _load_dotenv(path: Path):
    """Tiny .env reader (KEY=VALUE lines); real environment variables win."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip().removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)

_load_dotenv(Path(__file__).parent / ".env")

# --- Configuration (override with env vars) --------------------------------
# PASSWORD (env or .env): when set, only people who enter it can change anything;
# everyone else is a read-only listener. Empty/unset = no login, everyone is an owner.
PASSWORD = os.environ.get("PASSWORD", "")
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "./music")).expanduser().resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).expanduser().resolve()
ART_DIR = DATA_DIR / "art"
DB_PATH = DATA_DIR / "library.db"
STATIC_DIR = Path(__file__).parent / "static"

def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default

# Automatic DB backups: BACKUP_DIR="" disables them, BACKUP_EVERY_HOURS=0 = manual only.
BACKUP_DIR_RAW = os.environ.get("BACKUP_DIR", "/mnt/ssd/backups/mus").strip()
BACKUP_DIR = Path(BACKUP_DIR_RAW).expanduser() if BACKUP_DIR_RAW else None
BACKUP_EVERY_HOURS = max(0.0, _env_num("BACKUP_EVERY_HOURS", 48))
BACKUP_KEEP = max(1, int(_env_num("BACKUP_KEEP", 1)))

AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".aac", ".ogg", ".opus", ".wav"}
MIME = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".flac": "audio/flac", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".wav": "audio/wav",
}

DATA_DIR.mkdir(parents=True, exist_ok=True)
ART_DIR.mkdir(parents=True, exist_ok=True)

# --- Auth (single shared password, no usernames) ---------------------------
# Logging in sets an HttpOnly cookie holding an HMAC derived from the password, so
# changing the password logs everybody out. Reads (library, art, audio, sync feed)
# stay open; every route that mutates anything depends on owner_only.
AUTH_COOKIE = "mus_auth"
_TOKEN = hmac.new(PASSWORD.encode(), b"mus-auth-v1", hashlib.sha256).hexdigest() if PASSWORD else ""
_AUTH_LOCK = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}     # ip -> timestamps of recent bad passwords

def _is_owner(request: Request) -> bool:
    if not PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _TOKEN)

def owner_only(request: Request):
    if not _is_owner(request):
        raise HTTPException(401, "Listen-only — enter the password to change things")
OWNER = [Depends(owner_only)]

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

# --- Cross-device sync ---
# One connected client ("output") owns audio playback; every other connected
# client is a "controller" that mirrors state and sends intent (commands).
# The server is the sole authority over who the output is, so a client can
# never accidentally steal or duplicate audio just by opening the page.
_SYNC_LOCK = threading.Lock()
_SYNC_CLIENTS: dict[str, _queue.Queue] = {}   # cid -> per-client event queue
_SYNC_LISTENERS: dict[str, _queue.Queue] = {}  # read-only guests: get state/role, never counted or output
_SYNC_OUTPUT: str | None = None               # cid that currently owns audio
_SYNC_STATE: dict = {}                        # last full state published by the output

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
        "broken": not r["size"],
        # Apple Lossless: played via the live ffmpeg transcode (cached lookup, see _alac_info)
        "alac": bool(r["size"]) and r["ext"] == ".m4a" and _alac_info(r["path"], r["mtime"]) is not None,
    }

@app.get("/api/tracks")
def get_tracks():
    with db() as c:
        rows = c.execute("SELECT * FROM tracks ORDER BY album_artist, album, track_no, title")
        return [track_dict(r) for r in rows]

# --- Live ALAC -> WAV transcode ---------------------------------------------
# Browsers (Chrome/Firefox) can't decode Apple Lossless. We serve those files as
# a *virtual* 16-bit PCM WAV: its exact length is known up front from the
# metadata, so the browser sees an ordinary fixed-size, seekable file, while the
# bytes are produced on the fly by ffmpeg (nothing is written to disk). A Range
# request starting mid-file just starts ffmpeg at the matching sample offset.
_ALAC_CACHE: dict = {}
_HAS_FFMPEG = shutil.which("ffmpeg") is not None

def _alac_info(path: str, mtime: float):
    """(sample_rate, channels, nsamples) if `path` is ALAC, else None."""
    key = (path, mtime)
    if key in _ALAC_CACHE:
        return _ALAC_CACHE[key]
    info = None
    try:
        from mutagen.mp4 import MP4
        i = MP4(path).info
        if getattr(i, "codec", None) == "alac" and i.sample_rate and i.channels:
            info = (int(i.sample_rate), int(i.channels), round(i.length * i.sample_rate))
    except Exception:
        pass
    _ALAC_CACHE[key] = info
    return info

def _warm_alac_cache():
    """Probe every m4a once in the background so the first /api/tracks isn't slow."""
    try:
        with db() as c:
            rows = c.execute("SELECT path,mtime FROM tracks WHERE ext='.m4a' AND size>0").fetchall()
        for r in rows:
            _alac_info(r["path"], r["mtime"])
    except Exception:
        pass
threading.Thread(target=_warm_alac_cache, daemon=True).start()

def _wav_header(sr: int, ch: int, nbytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + nbytes) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, ch, sr, sr * ch * 2, ch * 2, 16) +
            b"data" + struct.pack("<I", nbytes))

async def _pcm_stream(path: str, sr: int, ch: int, total: int, start: int, end: int):
    """Yield bytes [start, end] (inclusive) of the virtual WAV."""
    remaining = end - start + 1
    if start < 44:
        hdr = _wav_header(sr, ch, total - 44)[start:end + 1]
        yield hdr
        remaining -= len(hdr)
        start = 44
    if remaining <= 0:
        return
    block = ch * 2
    sample0, skip = divmod(start - 44, block)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-nostdin", "-ss", f"{sample0 / sr:.6f}", "-i", path,
        "-map", "0:a:0", "-vn", "-f", "s16le", "-ar", str(sr), "-ac", str(ch), "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        while remaining > 0:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            if skip:                       # land on the exact byte inside a sample frame
                drop = min(skip, len(chunk)); chunk = chunk[drop:]; skip -= drop
                if not chunk:
                    continue
            chunk = chunk[:remaining]
            remaining -= len(chunk)
            yield chunk
        while remaining > 0:               # ffmpeg ended early: pad so Content-Length holds
            n = min(remaining, 65536)
            remaining -= n
            yield bytes(n)
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()

def _parse_range(header: str | None, total: int):
    """Return (start, end) inclusive, None for no/invalid-format header, or False if unsatisfiable."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":
            n = int(b)
            if n <= 0:
                return False
            start, end = max(0, total - n), total - 1
        else:
            start = int(a)
            end = int(b) if b else total - 1
    except ValueError:
        return None
    end = min(end, total - 1)
    if start > end or start >= total:
        return False
    return start, end

@app.get("/api/stream/{track_id}")
async def stream(track_id: int, request: Request):
    with db() as c:
        r = c.execute("SELECT path,ext,mtime FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not r or not Path(r["path"]).exists():
        raise HTTPException(404, "Track not found")
    path = r["path"]
    if os.path.getsize(path) == 0:
        raise HTTPException(422, "Empty file")
    if r["ext"] == ".m4a" and _HAS_FFMPEG:
        info = await asyncio.to_thread(_alac_info, path, r["mtime"])
        if info:
            sr, ch, nsamples = info
            total = 44 + nsamples * ch * 2
            rng = _parse_range(request.headers.get("range"), total)
            headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
            if rng is False:
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
            if rng is None:
                start, end, status = 0, total - 1, 200
            else:
                (start, end), status = rng, 206
                headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(end - start + 1)
            return StreamingResponse(_pcm_stream(path, sr, ch, total, start, end),
                                     status_code=status, media_type="audio/wav", headers=headers)
    return FileResponse(path, media_type=MIME.get(r["ext"], "application/octet-stream"))

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

@app.get("/api/auth")
def auth_status(request: Request):
    return {"required": bool(PASSWORD), "authed": _is_owner(request)}

@app.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    if not PASSWORD:
        return {"ok": True}
    ip = request.client.host if request.client else "?"
    now = time.time()
    with _AUTH_LOCK:
        recent = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < 300]
        _LOGIN_FAILS[ip] = recent
        if len(recent) >= 5:
            raise HTTPException(429, "Too many attempts — try again in a few minutes")
    pw = body.get("password")
    if isinstance(pw, str) and hmac.compare_digest(pw.encode(), PASSWORD.encode()):
        with _AUTH_LOCK:
            _LOGIN_FAILS.pop(ip, None)
        response.set_cookie(AUTH_COOKIE, _TOKEN, max_age=365 * 86400, httponly=True,
                            samesite="lax", secure=request.url.scheme == "https")
        return {"ok": True}
    with _AUTH_LOCK:
        _LOGIN_FAILS[ip].append(now)
    raise HTTPException(401, "Wrong password")

@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(AUTH_COOKIE)
    return {"ok": True}

@app.post("/api/scan", dependencies=OWNER)
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

# --- Backups ---------------------------------------------------------------
# Only library.db is backed up (library index, likes, playlists, history); cover art
# is rebuilt by a scan. Snapshots go through sqlite's online backup API, so they're
# consistent while the server is running.
BACKUP_RE = re.compile(r"^mus-\d{8}-\d{6}\.db$")
BACKUP_LOCK = threading.Lock()
BACKUP = {"error": None}
MAX_UPLOAD = 1 << 30

def _backup_files() -> list[Path]:
    if not BACKUP_DIR or not BACKUP_DIR.is_dir():
        return []
    fs = [p for p in BACKUP_DIR.iterdir() if BACKUP_RE.match(p.name) and p.is_file()]
    return sorted(fs, key=lambda p: p.name, reverse=True)

def _make_backup() -> Path:
    if not BACKUP_DIR:
        raise HTTPException(400, "Backups are disabled (BACKUP_DIR is empty)")
    with BACKUP_LOCK:
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            final = BACKUP_DIR / datetime.now().strftime("mus-%Y%m%d-%H%M%S.db")
            tmp = final.with_name(final.name + ".tmp")
            src = db(); dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close(); src.close()
            os.replace(tmp, final)
            for old in _backup_files()[BACKUP_KEEP:]:
                old.unlink(missing_ok=True)
            BACKUP["error"] = None
            return final
        except Exception as e:
            BACKUP["error"] = f"{type(e).__name__}: {e}"
            raise HTTPException(500, f"Backup failed: {e}")

def _backup_loop():
    while True:
        wait = 3600.0
        if BACKUP_DIR and BACKUP_EVERY_HOURS > 0:
            fs = _backup_files()
            age = time.time() - fs[0].stat().st_mtime if fs else float("inf")
            due = BACKUP_EVERY_HOURS * 3600 - age
            if due <= 0:
                try:
                    _make_backup(); fs = _backup_files(); due = BACKUP_EVERY_HOURS * 3600
                except HTTPException:
                    due = 3600           # e.g. drive not mounted: retry in an hour
            wait = min(max(due, 5), 3600)
        time.sleep(wait)
threading.Thread(target=_backup_loop, daemon=True).start()

def _restore_from(path: Path):
    """Validate a sqlite file and copy its contents over the live database."""
    if SCAN["running"]:
        raise HTTPException(409, "A scan is running — try again when it finishes")
    try:
        src = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        try:
            if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("integrity check failed")
            have = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"tracks", "playlists", "playlist_tracks", "history"} <= have:
                raise ValueError("not a mus database")
        except sqlite3.DatabaseError as e:
            src.close()
            raise ValueError(str(e))
        except ValueError:
            src.close()
            raise
    except ValueError as e:
        raise HTTPException(400, f"Not a valid backup: {e}")
    except sqlite3.Error as e:
        raise HTTPException(400, f"Not a valid backup: {e}")
    with BACKUP_LOCK:
        live = db(); keep = sqlite3.connect(DATA_DIR / "pre-restore.db")
        try:
            live.backup(keep)               # safety net: what was there before
            src.backup(live)
        finally:
            keep.close(); live.close(); src.close()

def _backup_info(p: Path) -> dict:
    st = p.stat()
    return {"name": p.name, "size": st.st_size, "mtime": st.st_mtime}

@app.get("/api/backups", dependencies=OWNER)
def backups_list():
    items = [_backup_info(p) for p in _backup_files()]
    last = items[0]["mtime"] if items else None
    nxt = last + BACKUP_EVERY_HOURS * 3600 if last and BACKUP_EVERY_HOURS else None
    return {"enabled": bool(BACKUP_DIR), "dir": str(BACKUP_DIR or ""), "everyHours": BACKUP_EVERY_HOURS,
            "keep": BACKUP_KEEP, "error": BACKUP["error"], "last": last, "next": nxt, "items": items}

@app.post("/api/backups", dependencies=OWNER)
def backups_now():
    return _backup_info(_make_backup())

# declared before /{name} routes so "upload" isn't taken for a backup name
@app.post("/api/backups/upload", dependencies=OWNER)
async def backups_upload(request: Request):
    tmp = DATA_DIR / "upload-restore.tmp"
    n = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                n += len(chunk)
                if n > MAX_UPLOAD:
                    raise HTTPException(413, "File too large")
                f.write(chunk)
        await asyncio.to_thread(_restore_from, tmp)
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True}

def _named_backup(name: str) -> Path:
    p = BACKUP_DIR / name if BACKUP_DIR else None
    if not p or not BACKUP_RE.match(name) or not p.is_file():
        raise HTTPException(404, "No such backup")
    return p

@app.get("/api/backups/{name}", dependencies=OWNER)
def backups_download(name: str):
    return FileResponse(_named_backup(name), media_type="application/octet-stream", filename=name)

@app.post("/api/backups/{name}/restore", dependencies=OWNER)
def backups_restore(name: str):
    _restore_from(_named_backup(name))
    return {"ok": True}

# --- Settings (stored in the meta table, so they sync across devices and are backed up) ---
# authorSep: text that splits a track's artist into several authors ("a / b" with "/").
# following: lower-cased author keys pinned to the top of the Authors tab.
DEFAULT_AUTHOR_SEP = "/"
_SETTINGS_LOCK = threading.Lock()

def _read_settings(c) -> dict:
    m = {r["key"]: r["value"] for r in c.execute(
        "SELECT key,value FROM meta WHERE key IN ('author_sep','following')")}
    try:
        following = [k for k in json.loads(m.get("following") or "[]") if isinstance(k, str)]
    except ValueError:
        following = []
    return {"authorSep": m.get("author_sep", DEFAULT_AUTHOR_SEP), "following": following}

@app.get("/api/settings")
def get_settings():
    with db() as c:
        return _read_settings(c)

@app.put("/api/settings", dependencies=OWNER)
def put_settings(body: dict = Body(...)):
    sep = body.get("authorSep")
    if not isinstance(sep, str) or len(sep.strip()) > 20:
        raise HTTPException(400, "authorSep must be text of up to 20 characters")
    with _SETTINGS_LOCK, db() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('author_sep',?)", (sep.strip(),))
        return _read_settings(c)

@app.post("/api/authors/follow", dependencies=OWNER)
def follow_author(body: dict = Body(...)):
    key, on = body.get("key"), body.get("follow")
    if not isinstance(key, str) or not key.strip() or len(key) > 300 or not isinstance(on, bool):
        raise HTTPException(400, "Need key (text) and follow (true/false)")
    key = key.strip().lower()
    with _SETTINGS_LOCK, db() as c:
        cur = _read_settings(c)["following"]
        nxt = [k for k in cur if k != key] + ([key] if on else [])
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('following',?)", (json.dumps(nxt),))
        return _read_settings(c)

@app.post("/api/like/{track_id}", dependencies=OWNER)
def like(track_id: int):
    with db() as c:
        r = c.execute("SELECT liked FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not r:
            raise HTTPException(404, "Track not found")
        new = 0 if r["liked"] else 1
        c.execute("UPDATE tracks SET liked=? WHERE id=?", (new, track_id))
    return {"liked": bool(new)}

@app.post("/api/play/{track_id}", dependencies=OWNER)
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

@app.post("/api/playlists", dependencies=OWNER)
def create_playlist(body: dict = Body(...)):
    name = (body.get("name") or "Untitled").strip()
    is_smart = 1 if body.get("isSmart") else 0
    rules = json.dumps(body.get("rules")) if body.get("rules") is not None else None
    with db() as c:
        cur = c.execute("INSERT INTO playlists(name,is_smart,rules,created_at) VALUES(?,?,?,?)",
                        (name, is_smart, rules, time.time()))
        return {"id": cur.lastrowid}

@app.put("/api/playlists/{pid}", dependencies=OWNER)
def update_playlist(pid: int, body: dict = Body(...)):
    with db() as c:
        if "name" in body:
            c.execute("UPDATE playlists SET name=? WHERE id=?", (body["name"].strip(), pid))
        if "rules" in body:
            c.execute("UPDATE playlists SET rules=? WHERE id=?", (json.dumps(body["rules"]), pid))
    return {"ok": True}

@app.delete("/api/playlists/{pid}", dependencies=OWNER)
def delete_playlist(pid: int):
    with db() as c:
        c.execute("DELETE FROM playlists WHERE id=?", (pid,))
    return {"ok": True}

@app.post("/api/playlists/{pid}/tracks", dependencies=OWNER)
def add_to_playlist(pid: int, body: dict = Body(...)):
    tid = body["trackId"]
    with db() as c:
        pos = c.execute("SELECT COALESCE(MAX(position),0)+1 AS n FROM playlist_tracks WHERE playlist_id=?",
                        (pid,)).fetchone()["n"]
        c.execute("INSERT OR IGNORE INTO playlist_tracks(playlist_id,track_id,position) VALUES(?,?,?)",
                  (pid, tid, pos))
    return {"ok": True}

@app.delete("/api/playlists/{pid}/tracks/{tid}", dependencies=OWNER)
def remove_from_playlist(pid: int, tid: int):
    with db() as c:
        c.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pid, tid))
    return {"ok": True}

def _sync_send(msg: dict, exclude: str | None = None):
    """Broadcast msg to every connected client except `exclude`, dropping dead queues.
    Read-only listeners get everything except controller commands."""
    with _SYNC_LOCK:
        for group in (_SYNC_CLIENTS, _SYNC_LISTENERS):
            if group is _SYNC_LISTENERS and msg.get("type") == "cmd":
                continue
            dead = []
            for cid, cq in group.items():
                if cid == exclude:
                    continue
                try: cq.put_nowait(msg)
                except _queue.Full: dead.append(cid)
            for cid in dead: group.pop(cid, None)

def _sync_roles():
    with _SYNC_LOCK:
        peers = len(_SYNC_CLIENTS)
    _sync_send({"type": "role", "output": _SYNC_OUTPUT, "peers": peers})

@app.get("/api/sync/events")
def sync_events(request: Request, cid: str, want: str = "controller"):
    global _SYNC_OUTPUT
    owner = _is_owner(request)
    cq: _queue.Queue = _queue.Queue(maxsize=50)
    with _SYNC_LOCK:
        claimed_output = False
        if owner:
            _SYNC_CLIENTS[cid] = cq
            if want == "output" and _SYNC_OUTPUT in (None, cid):
                _SYNC_OUTPUT = cid
                claimed_output = True
        else:
            _SYNC_LISTENERS[cid] = cq     # guests can watch, never drive
        peers = len(_SYNC_CLIENTS)
    cq.put_nowait({"type": "hello", "output": _SYNC_OUTPUT, "state": _SYNC_STATE, "peers": peers})
    if claimed_output:
        _sync_roles()
    def gen():
        try:
            yield "data: connected\n\n"
            while True:
                try:
                    msg = cq.get(timeout=25)
                    yield f"data: {json.dumps(msg)}\n\n"
                except _queue.Empty:
                    yield ": ka\n\n"
        finally:
            global _SYNC_OUTPUT
            was_output = False
            with _SYNC_LOCK:
                (_SYNC_CLIENTS if owner else _SYNC_LISTENERS).pop(cid, None)
                if owner and _SYNC_OUTPUT == cid:
                    _SYNC_OUTPUT = None
                    was_output = True
            if was_output:
                _sync_roles()
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.post("/api/sync/state", dependencies=OWNER)
def sync_state(body: dict = Body(...)):
    global _SYNC_STATE
    cid = body.get("cid")
    if cid != _SYNC_OUTPUT:
        return {"ok": False, "output": _SYNC_OUTPUT}
    state = body.get("state") or {}
    _SYNC_STATE = dict(state)
    _sync_send({"type": "state", "state": _SYNC_STATE}, exclude=cid)
    return {"ok": True}

_SETTINGS_CMDS = {"volume", "crossfade", "shuffle", "repeat", "sleep"}

@app.post("/api/sync/cmd", dependencies=OWNER)
def sync_cmd(body: dict = Body(...)):
    global _SYNC_STATE
    cid = body.get("cid")
    cmd = body.get("cmd") or {}
    name = cmd.get("name")
    if name in _SETTINGS_CMDS and isinstance(cmd.get("value"), (int, float, bool, str, type(None), dict)):
        _SYNC_STATE = {**_SYNC_STATE, name: cmd.get("value")}
    _sync_send({"type": "cmd", "from": cid, "cmd": cmd}, exclude=cid)
    return {"ok": True}

@app.post("/api/sync/claim", dependencies=OWNER)
def sync_claim(body: dict = Body(...)):
    global _SYNC_OUTPUT
    cid = body.get("cid")
    with _SYNC_LOCK:
        if cid not in _SYNC_CLIENTS:
            return {"ok": False}
        _SYNC_OUTPUT = cid
    _sync_roles()
    return {"ok": True, "output": _SYNC_OUTPUT}

@app.post("/api/sync/release", dependencies=OWNER)
def sync_release(body: dict = Body(...)):
    global _SYNC_OUTPUT
    cid = body.get("cid")
    changed = False
    with _SYNC_LOCK:
        if _SYNC_OUTPUT == cid:
            _SYNC_OUTPUT = None
            changed = True
    if changed:
        _sync_roles()
    return {"ok": True}

@app.post("/api/sync/leave", dependencies=OWNER)
def sync_leave(body: dict = Body(...)):
    global _SYNC_OUTPUT
    cid = body.get("cid")
    changed = False
    with _SYNC_LOCK:
        _SYNC_CLIENTS.pop(cid, None)
        if _SYNC_OUTPUT == cid:
            _SYNC_OUTPUT = None
            changed = True
    if changed:
        _sync_roles()
    return {"ok": True}

# Serve the single-page frontend (declared last so /api/* wins).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
