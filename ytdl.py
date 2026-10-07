"""Download songs from YouTube playlists into one folder (default /mnt/videos/Loop).

Logic only -- the HTTP routes live in app.py (all owner-only). The flow:

  saved playlist link -> fetch the list (yt-dlp --flat-playlist) -> every entry gets a
  status (new / downloaded / in library / skipped / unavailable) -> the owner ticks songs
  and downloads them (m4a, cover + artist/album/title tags) or marks many as skipped at once.

Ported from Lasso's audio mode and HomeFlix's downloader (same cookie / JS-runtime /
success-marker lessons). Files are flat `<video title>.m4a` in the download folder; the
tags are what makes them useful (see derive_tags).
"""
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from importlib import metadata
from pathlib import Path
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("mus.ytdl")

APP_DIR = Path(__file__).resolve().parent
AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".aac", ".ogg", ".opus", ".wav", ".webm"}
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]", "[unavailable video]"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}

NEW, DOWNLOADED, IN_LIBRARY, SIMILAR, ARCHIVED, SKIPPED, UNAVAILABLE = (
    "new", "downloaded", "library", "similar", "archived", "skipped", "unavailable")

DEFAULT_DIR = "/mnt/videos/Loop"
MIN_FREE_BYTES = 1024 ** 3          # refuse to start below this
RATE = 20_000                       # bytes/second of m4a (~160 kbit/s): size estimate only
FETCH_TIMEOUT = 180
MAX_IDS = 2000

_CFG = {"data": None, "dir": None, "db": None}


def init(data_dir, download_dir, db_factory):
    """Called once by app.py (and by the tests with scratch folders)."""
    _CFG["data"] = Path(data_dir)
    _CFG["dir"] = Path(download_dir).expanduser()
    _CFG["db"] = db_factory
    ytdl_dir().mkdir(parents=True, exist_ok=True)
    with _conn():
        pass


def ytdl_dir():
    return _CFG["data"] / "ytdl"


DEFAULT_DIR_KEY = "dl_dir"


def default_dir():
    """The folder from DOWNLOAD_DIR / the built-in default (what 'Reset' goes back to)."""
    return _CFG["dir"]


def download_dir():
    """The folder songs are saved into: the one chosen in Settings (stored in the `meta`
    table, so it syncs and is backed up) or else the DOWNLOAD_DIR default."""
    try:
        with _conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (DEFAULT_DIR_KEY,)).fetchone()
        if r and r["value"].strip():
            return Path(r["value"])
    except sqlite3.Error:
        pass
    return _CFG["dir"]


def set_download_dir(raw):
    """Validate and store a new download folder ('' = back to the default). It must be an
    absolute path that exists or can be created, and that we can write to. Returns disk_info()."""
    raw = (raw or "").strip()
    if not raw:
        with _conn() as c:
            c.execute("DELETE FROM meta WHERE key=?", (DEFAULT_DIR_KEY,))
        return dir_info()
    if "\n" in raw or "\0" in raw or len(raw) > 500:
        raise ValueError("That isn't a valid folder path")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("Use a full path, like /mnt/videos/Loop")
    p = Path(os.path.normpath(p))
    if p == Path(p.anchor):
        raise ValueError("Pick a folder, not the filesystem root")
    try:
        p.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=p)
        os.close(fd)
        os.remove(probe)
    except OSError as e:
        raise ValueError(f"Can't write to {p}: {e.strerror or e}")
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (DEFAULT_DIR_KEY, str(p)))
    return dir_info()


def dir_info():
    d = download_dir()
    free, total = free_space(d)
    return {"dir": str(d), "default": str(default_dir()), "custom": d != default_dir(), "free": free, "total": total}


def _conn():
    """A DB connection with our two tables guaranteed to exist (a restored older
    backup doesn't have them, and the app isn't re-initialised after a restore)."""
    c = _CFG["db"]()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS dl_sources (
        id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, name TEXT DEFAULT '',
        entries_json TEXT DEFAULT '[]', fetched REAL DEFAULT 0, created_at REAL);
    CREATE TABLE IF NOT EXISTS dl_skipped (
        source_id INTEGER NOT NULL, vid TEXT NOT NULL,
        PRIMARY KEY (source_id, vid),
        FOREIGN KEY (source_id) REFERENCES dl_sources(id) ON DELETE CASCADE);
    """)
    return c


# ---- Links, names ----------------------------------------------------------

def normalize_url(raw):
    """Validate a pasted link (YouTube hosts only). A watch?v=..&list=.. link becomes the
    plain playlist URL -- that's the form Lasso saves and hashes for its archive file, so
    both tools share one archive."""
    u = (raw or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise ValueError("Paste a full https:// YouTube link")
    p = urlparse(u)
    if (p.hostname or "").lower() not in _YT_HOSTS:
        raise ValueError("Only YouTube links are supported")
    lst = parse_qs(p.query).get("list", [""])[0]
    if lst and re.fullmatch(r"[A-Za-z0-9_-]+", lst):
        return f"https://www.youtube.com/playlist?list={lst}"
    return u


def name_key(s):
    """Comparable form of a title/filename: NFKC, case-folded, everything that isn't a
    letter or digit removed (yt-dlp sanitises `/ : ? " |` differently across versions)."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[\W_]+", "", s)


def archive_path(url):
    """Lasso's archive file for this playlist, inside the download folder."""
    h = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]
    return download_dir() / f"_yt_archive_{h}.txt"


def read_archive_ids(path):
    ids = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    ids.add(parts[1])
    except OSError:
        pass
    return ids


def forget_in_archive(path, ids):
    """Drop ids from a yt-dlp archive so a deliberate re-download isn't skipped."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    kept = [l for l in lines if not (len(l.split()) >= 2 and l.split()[1] in ids)]
    if len(kept) != len(lines):
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(kept)


# ---- Tags: artist / album / title from what YouTube tells us ---------------

# "(Official Video)", "[MV]", "【Official Audio】" ... -- only these well-known tags are
# removed; other bracketed text (Japanese titles, "(feat. X)", "(Remix)") is kept.
_NOISE_WORD = (r"official(?:\s+(?:music\s+)?(?:video|audio|mv|m/v|lyric\s+video|lyrics?|visuali[sz]er|"
               r"performance\s+video))?|(?:official\s+)?lyrics?(?:\s+video)?|(?:official\s+)?audio|"
               r"(?:official\s+)?(?:music\s+)?video|m/?v|visuali[sz]er|hd|hq|4k|full\s+(?:song|version)|"
               r"(?:オリジナル|公式)(?:曲|mv|ミュージックビデオ)?")
_NOISE_RE = re.compile(r"\s*[\(\[（【]\s*(?:%s)\s*[\)\]）】]" % _NOISE_WORD, re.I)
_SPLIT_RE = re.compile(r"^(.{1,100}?)\s+[-–—―]\s+(.+)$")
_CHAN_NOISE = re.compile(r"\s*(?:-\s*topic|vevo|official(?:\s+channel)?)\s*$", re.I)


def _s(v):
    return v.strip() if isinstance(v, str) else ""


def clean_title(t):
    t = _s(t)
    for _ in range(3):                       # "(Official Video) [HD]" -> both go
        n = _NOISE_RE.sub("", t).strip()
        if n == t:
            break
        t = n
    return t.strip(" -–—")


def clean_channel(c):
    c = _s(c)
    for _ in range(2):
        n = _CHAN_NOISE.sub("", c).strip()
        if n == c:
            break
        c = n
    return c


def derive_tags(info):
    """{title, artist, album} for one downloaded video from the metadata fields yt-dlp gave
    (title, track, artist, artists, album, uploader, channel, creator).

      1. YouTube Music / auto-generated "Topic" uploads carry the real track, artists and
         album -> use them (several artists are joined with " / ", like the library).
      2. else a video title "Artist - Title" is split (official-video noise removed).
      3. else artist = the channel (minus "- Topic", "VEVO", "Official"), title = the title.
    Album is the YouTube album, else the track title (a single), like existing files.
    Empty values are returned as "" and never written."""
    track = _s(info.get("track"))
    artists, seen = [], set()
    for a in info.get("artists") or []:             # YouTube lists composers/performers twice sometimes
        if isinstance(a, str) and a.strip() and a.strip().casefold() not in seen:
            seen.add(a.strip().casefold())
            artists.append(a.strip())
    artist_str = _s(info.get("artist"))
    album = _s(info.get("album"))
    if track and (artists or artist_str):
        artist = " / ".join(artists) if artists else artist_str
        return {"title": track, "artist": artist, "album": album or track}
    raw = _s(info.get("title"))
    title = clean_title(raw) or raw
    chan = clean_channel(info.get("channel") or info.get("uploader") or info.get("creator") or "")
    artist = " / ".join(artists) if artists else (artist_str or chan)
    m = _SPLIT_RE.match(title)
    if m:
        left, right = m.group(1).strip(), (clean_title(m.group(2)) or m.group(2).strip())
        # "A - B" doesn't say which half is the artist ("Song - Artist" is as common as
        # "Artist - Song"), so only split when the channel confirms one side; with no channel
        # at all fall back to the usual order. Otherwise the channel is the artist (Lasso's rule).
        def same(a, b):
            ka, kb = name_key(a), name_key(b)
            return len(ka) >= 2 and len(kb) >= 2 and (ka in kb or kb in ka)
        if not chan:
            artist, title = left, right
        elif same(left, chan):
            artist, title = left, right
        elif same(right, chan):
            artist, title = right, left
    return {"title": title, "artist": artist, "album": album or title}


def retag(path, tags):
    """Write title/artist/album into an .m4a, leaving the cover alone. Returns True if the
    file was changed."""
    if not str(path).lower().endswith(".m4a") or not os.path.isfile(path):
        return False
    try:
        from mutagen.mp4 import MP4
        f = MP4(path)
        if f.tags is None:
            f.add_tags()
        for key, val in (("\xa9nam", tags.get("title")), ("\xa9ART", tags.get("artist")),
                         ("\xa9alb", tags.get("album"))):
            if val:
                f.tags[key] = [val]
        f.save()
        return True
    except Exception as e:                                  # a bad tag must not fail the download
        log.warning("retag failed for %s: %s", path, e)
        return False


# ---- Download folder / disk ------------------------------------------------

def ensure_download_dir():
    """Create the folder if needed and prove we can write to it; ValueError for the UI."""
    d = download_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".write-test-", dir=d)
        os.close(fd)
        os.remove(probe)
    except OSError as e:
        raise ValueError(f"Can't write to {d}: {e.strerror or e}")
    return d


def free_space(path):
    p = str(path)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        u = shutil.disk_usage(p)
        return u.free, u.total
    except OSError:
        return None, None


def disk_info():
    return {**dir_info(), "rate": RATE}


# ---- Cookies ---------------------------------------------------------------
# They carry the owner's YouTube login: stored 0600 under data/ytdl/ (git-ignored), never sent
# back to the browser -- the UI only learns set / count / dates / whether they still work.

_LOGIN_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO"}
_FAR_FUTURE = "2147483647"
COOKIE_BAD = ("Your YouTube cookies don't work anymore (expired or signed out). "
              "Paste fresh ones in the cookies box.")
CHECK_FRESH = 15 * 60


def cookie_path():
    return ytdl_dir() / "cookies.txt"


def normalize_cookies(text):
    """Netscape cookies.txt *or* a raw `Cookie:` header -> (netscape_text, count, has_login).
    Whitespace-separated, because phone keyboards turn tabs into spaces."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Nothing pasted")
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        f = re.split(r"\s+", line, maxsplit=6)
        if (len(f) >= 6 and f[1].upper() in ("TRUE", "FALSE")
                and f[3].upper() in ("TRUE", "FALSE") and re.fullmatch(r"\d+", f[4])):
            rows.append((f[0], f[1].upper(), f[2], f[3].upper(), f[4], f[5], f[6] if len(f) > 6 else ""))
    if not rows:
        m = re.search(r"^\s*cookie\s*:\s*(.+)$", text, re.I | re.M)
        header = m.group(1) if m else " ".join(text.split("\n"))
        for part in header.split(";"):
            name, sep, value = part.strip().partition("=")
            name, value = name.strip(), value.strip()
            if sep and name and re.fullmatch(r"[^\s=;,\"\\]+", name):
                rows.append((".youtube.com", "TRUE", "/", "TRUE", _FAR_FUTURE, name, value))
    if not rows:
        raise ValueError("Couldn't find any cookies in that. Paste the whole cookies.txt file, "
                         "or the Cookie: header value.")
    lines = ["# Netscape HTTP Cookie File"]
    for r in rows:
        if "\t" in r[6]:
            continue
        lines.append("\t".join(r))
    count = len(lines) - 1
    if count == 0:
        raise ValueError("Those cookies were malformed")
    return "\n".join(lines) + "\n", count, any(r[5] in _LOGIN_COOKIES for r in rows)


def save_cookies(text):
    body, count, has_login = normalize_cookies(text)
    path = cookie_path()
    tmp = Path(str(path) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    os.replace(tmp, path)
    return count, has_login


def clear_cookies():
    try:
        os.remove(cookie_path())
    except OSError:
        pass


def _cookie_rows():
    try:
        with open(cookie_path(), encoding="utf-8") as f:
            rows = [l.rstrip("\n").split("\t") for l in f if l.strip() and not l.startswith("# ")]
    except OSError:
        return None
    out = []
    for r in rows:
        if len(r) >= 6:
            try:
                exp = int(r[4])
            except ValueError:
                exp = 0
            out.append((r[5], exp))
    return out


def _check_file():
    return ytdl_dir() / "cookie_check.json"


def read_cookie_check():
    """The last health result for the *current* cookie file, else None."""
    try:
        with open(_check_file(), encoding="utf-8") as f:
            c = json.load(f)
        if abs(c.get("cookie_mtime", 0) - os.path.getmtime(cookie_path())) > 1e-3:
            return None
        return c
    except (OSError, ValueError):
        return None


def _write_check(ok, msg):
    try:
        mt = os.path.getmtime(cookie_path())
    except OSError:
        return None
    c = {"ok": ok, "msg": msg, "checked": time.time(), "cookie_mtime": mt}
    tmp = Path(str(_check_file()) + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, _check_file())
    except OSError:
        pass
    return c


def cookie_status():
    rows = _cookie_rows()
    if rows is None:
        return {"set": False}
    try:
        saved = os.path.getmtime(cookie_path())
    except OSError:
        return {"set": False}
    names = {n for n, _ in rows}
    exps = [e for n, e in rows if n in _LOGIN_COOKIES and 0 < e < int(_FAR_FUTURE)]
    login_expires = min(exps) if exps else None
    return {"set": True, "count": len(rows), "has_login": bool(names & _LOGIN_COOKIES),
            "saved": saved, "login_expires": login_expires,
            "expired": bool(login_expires and login_expires < time.time()),
            "check": read_cookie_check()}


def check_cookies(timeout=60):
    """Do the saved cookies still work? YouTube invalidates them silently and yt-dlp then
    carries on *logged out*, so ask for something that needs a login (Watch Later) on a temp
    copy. ok: True / False / None (couldn't tell -- never blocks anything)."""
    if not cookie_path().is_file():
        return {"ok": False, "msg": "No cookies saved", "checked": time.time()}
    if not ytdlp_available():
        return _write_check(None, "yt-dlp isn't installed")
    fd, tmp = tempfile.mkstemp(prefix="check_", suffix=".txt", dir=ytdl_dir())
    os.close(fd)
    try:
        shutil.copyfile(cookie_path(), tmp)
        cmd = [sys.executable, "-m", "yt_dlp", "--cookies", tmp, "--flat-playlist", "--dump-single-json",
               "--playlist-end", "1", "--no-warnings", "https://www.youtube.com/playlist?list=WL"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env(),
                               creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return _write_check(None, "YouTube took too long to answer")
        except OSError as e:
            return _write_check(None, f"Couldn't run yt-dlp: {e}")
        err = (r.stderr or "").lower()
        if "no longer valid" in err or "sign in" in err or "log in" in err or "login" in err:
            return _write_check(False, COOKIE_BAD)
        if r.returncode == 0:
            try:
                if isinstance(json.loads(r.stdout), dict):
                    return _write_check(True, "Signed in")
            except ValueError:
                pass
        if any(w in err for w in ("playlist does not exist", "unviewable", "private", "not available")):
            return _write_check(False, COOKIE_BAD)           # Watch Later is always visible to its owner
        return _write_check(None, "Couldn't tell (" + (err.strip().splitlines() or ["no answer"])[-1][:120] + ")")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def check_cookies_async():
    threading.Thread(target=check_cookies, daemon=True).start()


def cookies_ok_for_job():
    """None if downloading may go ahead, else the message to refuse with."""
    if not cookie_path().is_file():
        return "YouTube cookies are required. Paste them first."
    st = cookie_status()
    if st.get("expired"):
        return COOKIE_BAD
    c = st.get("check")
    if not c or time.time() - c.get("checked", 0) > CHECK_FRESH or c.get("ok") is None:
        c = check_cookies()
    return COOKIE_BAD if c and c.get("ok") is False else None


# ---- yt-dlp / JS runtime ---------------------------------------------------

def ytdlp_available():
    return importlib.util.find_spec("yt_dlp") is not None


def ytdlp_version():
    try:
        return metadata.version("yt-dlp")
    except metadata.PackageNotFoundError:
        return ""


# Oldest runtimes yt-dlp's challenge solver accepts (mirrors yt_dlp/utils/_jsruntime.py). An older
# one (a distro Node 18) is silently ignored and every download dies with "n challenge solving failed".
_MIN_RUNTIME = {"deno": (2, 3, 0), "bun": (1, 2, 11), "node": (22, 0, 0)}
_VERSION_CACHE = {}


def _exe_version(path):
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key in _VERSION_CACHE:
        return _VERSION_CACHE[key]
    try:
        r = subprocess.run([path, "--version"], capture_output=True, timeout=15,
                           creationflags=_NO_WINDOW, **_TEXT)
        m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout or "")
        ver = tuple(int(x) for x in m.groups()) if m else None
    except (OSError, subprocess.SubprocessError):
        ver = None
    _VERSION_CACHE[key] = ver
    return ver


def local_deno_path():
    return str(APP_DIR / ".deno" / "bin" / ("deno.exe" if os.name == "nt" else "deno"))


def js_runtime():
    """Best JS runtime as {name, path, version, supported, args}; args is the explicit
    --js-runtimes flag (empty unless supported)."""
    cands = []
    for name in ("deno", "bun", "node"):
        found = shutil.which(name)
        if found:
            cands.append((name, found))
        if name == "deno" and os.path.isfile(local_deno_path()):
            cands.append(("deno", local_deno_path()))
    fallback = None
    for name, path in cands:
        ver = _exe_version(path)
        if ver is None:
            continue
        ok = ver >= _MIN_RUNTIME[name]
        info = {"name": name, "path": path, "version": ".".join(map(str, ver)), "supported": ok,
                "args": ["--js-runtimes", f"{name}:{path}"] if ok else []}
        if ok:
            return info
        fallback = fallback or info
    return fallback or {"name": "", "path": "", "version": "", "supported": False, "args": []}


def runtime_problem(rt=None):
    rt = rt or js_runtime()
    if rt["supported"]:
        return ""
    if rt["name"]:
        need = ".".join(map(str, _MIN_RUNTIME[rt["name"]]))
        return (f"{rt['name'].capitalize()} {rt['version']} is too old for yt-dlp (needs {rt['name']} {need}+). "
                "Install Deno 2.3+ (run ./install.sh, or https://deno.com) and restart mus.")
    return ("No JavaScript runtime found. yt-dlp needs Deno 2.3+ (or Node 22+) to solve YouTube's "
            "challenge. Run ./install.sh, or install Deno from https://deno.com, and restart mus.")


def _env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def hint_for(text):
    t = (text or "").lower()
    if "no longer valid" in t:
        return COOKIE_BAD
    if "not a bot" in t or "sign in" in t or ("cookies" in t and "invalid" in t):
        return "YouTube wants a sign-in. Paste fresh cookies."
    if "private" in t or "members-only" in t or "join this channel" in t:
        return "Private or members-only. Cookies from an account that can see it are needed."
    if ("challenge" in t or "page needs to be reloaded" in t or "requested format is not available" in t):
        return runtime_problem() or "yt-dlp couldn't solve YouTube's challenge. Try Update yt-dlp, then retry."
    return ""


# ---- Playlist listing ------------------------------------------------------

def _flatten(node, out, seen):
    for e in node.get("entries") or []:
        if not e:
            continue
        if e.get("entries"):
            _flatten(e, out, seen)
            continue
        vid = e.get("id") or ""
        if not VIDEO_ID_RE.match(vid) or vid in seen:
            continue
        seen.add(vid)
        dur = e.get("duration")
        out.append({"id": vid, "title": e.get("title") or vid,
                    "duration": int(dur) if isinstance(dur, (int, float)) else None,
                    "index": len(out) + 1,
                    "channel": e.get("channel") or e.get("uploader") or ""})


def fetch_playlist(url, timeout=FETCH_TIMEOUT):
    """(name, entries, error) from `yt-dlp --flat-playlist`, nothing is downloaded. Uses a
    temp *copy* of the cookies so yt-dlp's cookie write-back can't race a running download."""
    if not ytdlp_available():
        return "", [], "yt-dlp isn't installed on the server (pip install -r requirements.txt)."
    tmp = None
    try:
        cmd = [sys.executable, "-m", "yt_dlp"]
        if cookie_path().is_file():
            fd, tmp = tempfile.mkstemp(prefix="fetch_", suffix=".txt", dir=ytdl_dir())
            os.close(fd)
            shutil.copyfile(cookie_path(), tmp)
            cmd += ["--cookies", tmp]
        cmd += ["--flat-playlist", "--dump-single-json", "--ignore-errors", "--no-warnings", url]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout, env=_env(),
                               creationflags=_NO_WINDOW, **_TEXT)
        except subprocess.TimeoutExpired:
            return "", [], "YouTube took too long to answer. Try again."
        except OSError as e:
            return "", [], f"Couldn't run yt-dlp: {e}"
        try:
            data = json.loads(r.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            errs = [l for l in (r.stderr or "").splitlines() if l.startswith("ERROR")]
            msg = errs[-1] if errs else ((r.stderr or "").strip()[-300:] or "No data returned")
            return "", [], f"{msg[:300]} {hint_for(r.stderr)}".strip()
        entries, seen = [], set()
        if data.get("entries") is not None:
            _flatten(data, entries, seen)
        elif VIDEO_ID_RE.match(data.get("id") or ""):
            _flatten({"entries": [data]}, entries, seen)
        return data.get("title") or "", entries, ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


# ---- Sources (saved links) + classification --------------------------------

def _source_row(c, sid):
    r = c.execute("SELECT * FROM dl_sources WHERE id=?", (sid,)).fetchone()
    if not r:
        raise LookupError("No such playlist")
    return r


def list_sources():
    with _conn() as c:
        rows = c.execute("SELECT id,url,name,fetched,entries_json FROM dl_sources ORDER BY id").fetchall()
    out = []
    for r in rows:
        try:
            n = len(json.loads(r["entries_json"] or "[]"))
        except ValueError:
            n = 0
        out.append({"id": r["id"], "url": r["url"], "name": r["name"], "fetched": r["fetched"], "count": n})
    return out


def add_source(url, name=""):
    url = normalize_url(url)
    with _conn() as c:
        if c.execute("SELECT 1 FROM dl_sources WHERE url=?", (url,)).fetchone():
            raise ValueError("That playlist is already saved")
        cur = c.execute("INSERT INTO dl_sources(url,name,created_at) VALUES(?,?,?)",
                        (url, (name or "").strip()[:200], time.time()))
        return cur.lastrowid


def delete_source(sid):
    with _conn() as c:
        _source_row(c, sid)
        c.execute("DELETE FROM dl_skipped WHERE source_id=?", (sid,))
        c.execute("DELETE FROM dl_sources WHERE id=?", (sid,))


def fetch_source(sid):
    """Re-read the playlist from YouTube and cache the entries. Raises ValueError with the
    reason on failure (the cache is left as it was)."""
    with _conn() as c:
        url = _source_row(c, sid)["url"]
    name, entries, err = fetch_playlist(url)
    if err:
        raise ValueError(err)
    with _conn() as c:
        c.execute("UPDATE dl_sources SET entries_json=?, fetched=?, name=CASE WHEN name='' THEN ? ELSE name END "
                  "WHERE id=?", (json.dumps(entries, ensure_ascii=False), time.time(), name[:200], sid))


def mark(sid, ids, skip=True):
    """Bulk skip/unskip: only ids that are in the cached list are accepted."""
    with _conn() as c:
        row = _source_row(c, sid)
        try:
            known = {e["id"] for e in json.loads(row["entries_json"] or "[]")}
        except ValueError:
            known = set()
        ids = [i for i in dict.fromkeys(ids) if i in known]
        if skip:
            c.executemany("INSERT OR IGNORE INTO dl_skipped(source_id,vid) VALUES(?,?)", [(sid, i) for i in ids])
        else:
            c.executemany("DELETE FROM dl_skipped WHERE source_id=? AND vid=?", [(sid, i) for i in ids])
    return len(ids)


_ARTIST_SPLIT = re.compile(r"\s*(?:/|,|&|;|\bfeat\.?\b|\bft\.?\b|×)\s*", re.I)
_FUZZY = {"sig": None, "memo": {}}


def local_index(url):
    """What we already have: name keys of files in the download folder (recursive), the
    library's tracks (for exact *and* fuzzy matching, so a song already in mus isn't offered
    again) and the ids in Lasso's archive."""
    names, lib, tracks = set(), {}, []
    d = download_dir()
    if d.is_dir():
        for dirpath, _dirs, files in os.walk(d):
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext.lower() in AUDIO_EXTS:
                    k = name_key(stem)
                    if k:
                        names.add(k)
    sig = None
    try:
        with _conn() as c:
            for r in c.execute("SELECT title, artist, path FROM tracks"):
                label = " - ".join(x for x in ((r["artist"] or "").strip(), (r["title"] or "").strip()) if x)
                kt = name_key(r["title"])
                for k in (kt, name_key((r["artist"] or "") + (r["title"] or "")),
                          name_key(os.path.splitext(os.path.basename(r["path"] or ""))[0])):
                    if k:
                        lib.setdefault(k, label)
                tracks.append({"kt": kt, "label": label,
                               "ka": [k for k in (name_key(a) for a in _ARTIST_SPLIT.split(r["artist"] or "")) if len(k) >= 2]})
            sig = tuple(c.execute("SELECT COUNT(*), COALESCE(MAX(id),0), COALESCE(SUM(mtime),0) FROM tracks").fetchone())
    except sqlite3.Error:
        pass
    if _FUZZY["sig"] != sig:
        _FUZZY["sig"], _FUZZY["memo"] = sig, {}
    return {"names": names, "library": lib, "tracks": tracks, "archive": read_archive_ids(archive_path(url))}


def fuzzy_library(key, chan_key, tracks, memo=None):
    """Looser comparison of one playlist entry with the library, for the many songs whose YouTube
    title is not the tag title ("【MV】Song / Artist", "Artist - Song (Official Video)").
    A library title that appears inside the YouTube title (or the reverse) is a match:
      * (`library`, label) when an artist of that track also shows up in the YouTube title or
        channel -- confident enough to treat as already owned;
      * (`similar`, label) when only the title lines up (4+ characters) -- worth a look, so
        it's flagged but not hidden among the "new" ones.
    None when nothing is close."""
    mk = (key, chan_key)
    if memo is not None and mk in memo:
        return memo[mk]
    weak, hit = None, None
    for L in tracks:
        lt = L["kt"]
        if len(lt) < 3:
            continue
        if lt in key or (len(key) >= 6 and key in lt):
            if any(a in key or a in chan_key or (len(chan_key) >= 3 and chan_key in a) for a in L["ka"]):
                hit = (IN_LIBRARY, L["label"])
                break
            if weak is None and len(lt) >= 4 and lt in key:
                weak = (SIMILAR, L["label"])
    hit = hit or weak
    if memo is not None:
        memo[mk] = hit
    return hit


def classify(entries, index, skipped_ids):
    """Status for every entry (plus `match`: the library track it was matched to, if any).
    Precedence: unavailable > downloaded (a file in the download folder) > in library (exact or
    confident fuzzy) > skipped (your own decision beats a weak guess) > similar (title-only
    guess) > archived (only in Lasso's archive) > new."""
    out = []
    for e in entries:
        vid, title = e["id"], e.get("title") or ""
        key = name_key(title)
        t = derive_tags({"title": title, "channel": e.get("channel")})
        pair = name_key(t["artist"] + t["title"]) if t["artist"] else ""
        match = None
        lib_hit = index["library"].get(key) if key else None
        lib_hit = lib_hit or (index["library"].get(pair) if pair else None)
        fz = None
        if title.strip().lower() in _UNAVAILABLE_TITLES:
            status = UNAVAILABLE
        elif key and key in index["names"]:
            status = DOWNLOADED
        elif lib_hit is not None:
            status, match = IN_LIBRARY, lib_hit
        else:
            fz = fuzzy_library(key, name_key(e.get("channel") or ""), index.get("tracks") or [], _FUZZY["memo"]) if key else None
            if fz and fz[0] == IN_LIBRARY:
                status, match = IN_LIBRARY, fz[1]
            elif vid in skipped_ids:
                status = SKIPPED
            elif fz:
                status, match = SIMILAR, fz[1]
            elif vid in index["archive"]:
                status = ARCHIVED
            else:
                status = NEW
        out.append({**e, "status": status, **({"match": match} if match else {})})
    return out


def source_state(sid):
    with _conn() as c:
        row = _source_row(c, sid)
        skipped = {r["vid"] for r in c.execute("SELECT vid FROM dl_skipped WHERE source_id=?", (sid,))}
    try:
        entries = json.loads(row["entries_json"] or "[]")
    except ValueError:
        entries = []
    items = classify(entries, local_index(row["url"]), skipped)
    counts = {}
    for e in items:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {"source": {"id": row["id"], "url": row["url"], "name": row["name"], "fetched": row["fetched"]},
            "entries": items, "counts": counts, "disk": disk_info()}


def tools_state():
    rt = js_runtime()
    return {"installed": ytdlp_available(), "version": ytdlp_version(), "runtime": rt,
            "runtime_problem": runtime_problem(rt), "cookies": cookie_status(),
            "disk": disk_info(), "update": update_status(), "sources": list_sources()}


# ---- Running a download job ------------------------------------------------

def build_cmd(ids, out_dir, archive, cookie_file):
    """yt-dlp argv for a hand-picked list of ids -> (cmd, before_file, after_file, batch_file).

    Audio: best m4a (AAC is copied, anything else converted to m4a), the thumbnail embedded as
    cover, YouTube's own metadata embedded. before/after are --print-to-file markers: yt-dlp's
    exit code means little with --ignore-errors, so counts come from them. The `after` line
    carries the final file path and the fields derive_tags() needs (id TAB path TAB json).
    Ids go through a batch file so a long selection can't hit the command-line length limit."""
    run_id = uuid.uuid4().hex[:8]
    before = str(ytdl_dir() / f"run_{run_id}_attempt.txt")
    after = str(ytdl_dir() / f"run_{run_id}_success.txt")
    batch = str(ytdl_dir() / f"run_{run_id}_urls.txt")
    with open(batch, "w", encoding="utf-8") as f:
        f.write("\n".join(f"https://www.youtube.com/watch?v={i}" for i in ids) + "\n")
    cmd = [sys.executable, "-m", "yt_dlp", *js_runtime()["args"],
           "--cookies", str(cookie_file),
           # With cookies YouTube forces the default clients into SABR streaming; web_embedded keeps
           # the full DASH ladder (see Lasso/CLAUDE.md, yt-dlp issue #12482).
           "--extractor-args", "youtube:player_client=web_embedded,default",
           "--remote-components", "ejs:github",
           "-f", "bestaudio[ext=m4a]/bestaudio",
           "-x", "--audio-format", "m4a", "--audio-quality", "0",
           "--embed-thumbnail", "--convert-thumbnails", "jpg",
           "--embed-metadata",
           "--download-archive", str(archive),
           "-o", os.path.join(str(out_dir), "%(title)s.%(ext)s"),
           "--retries", "6", "--fragment-retries", "10", "--concurrent-fragments", "4",
           "--ignore-errors", "--geo-bypass", "--newline",
           "--progress-template", "download:HFP|%(info.id)s|%(progress._percent_str)s|%(info.title)s",
           "--print-to-file", "before_dl:%(id)s", before,
           "--print-to-file",
           "after_move:%(id)s\t%(filepath)s\t%(.{title,track,artist,artists,album,uploader,channel,creator})j",
           after,
           "-a", batch]
    return cmd, before, after, batch


def read_after(path):
    """Parse + delete the success marker -> [(id, filepath, info_dict)]. Tolerant of short lines
    (a stand-in or older yt-dlp that only wrote the id)."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                parts = line.split("\t", 2)
                info = {}
                if len(parts) == 3:
                    try:
                        info = json.loads(parts[2])
                    except ValueError:
                        info = {}
                out.append((parts[0].strip(), parts[1] if len(parts) > 1 else "", info))
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError:
        pass
    return out


def _count_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for l in f if l.strip())
    except OSError:
        return 0


def _kill_tree(proc):
    """Stop yt-dlp and the ffmpeg it spawned."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=_NO_WINDOW)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


JOB = {"status": "idle"}
_JOB_LOCK = threading.Lock()
_PROC = {"p": None}


def job_state():
    with _JOB_LOCK:
        return dict(JOB)


def _set(**kw):
    with _JOB_LOCK:
        JOB.update(kw)


def start_job(sid, ids):
    """Validate and start a download in a background thread -> (ok, error). One job at a time."""
    ids = [i for i in dict.fromkeys(ids) if VIDEO_ID_RE.match(i or "")]
    if not ids:
        return False, "Nothing selected"
    if len(ids) > MAX_IDS:
        return False, f"Select at most {MAX_IDS} songs at once"
    with _conn() as c:
        row = _source_row(c, sid)
    if not ytdlp_available():
        return False, "yt-dlp isn't installed on the server (pip install -r requirements.txt)."
    if not cookie_path().is_file():
        return False, "YouTube cookies are required for downloads. Paste them first."
    if cookie_status().get("expired"):
        return False, COOKIE_BAD
    problem = runtime_problem()
    if problem:
        return False, problem
    try:
        ensure_download_dir()
    except ValueError as e:
        return False, str(e)
    free, _ = free_space(download_dir())
    if free is not None and free < MIN_FREE_BYTES:
        return False, f"Only {free / 1024 ** 2:.0f} MB free in {download_dir()}."
    with _JOB_LOCK:
        if JOB.get("status") in ("running", "starting"):
            return False, "A download is already running."
        JOB.clear()
        JOB.update(status="starting", source_id=sid, total=len(ids), done=0, fail=0, title="", pct=0,
                   summary="", log="", started=time.time(), finished=None)
    threading.Thread(target=_run_job, args=(row["url"], ids), daemon=True).start()
    return True, ""


def cancel_job():
    with _JOB_LOCK:
        running = JOB.get("status") in ("running", "starting")
        if running:
            JOB.update(status="cancelled", summary="Cancelled", finished=time.time())
    p = _PROC["p"]
    if running and p is not None:
        _kill_tree(p)
    return running


def _run_job(url, ids):
    try:
        _run_job_inner(url, ids)
    except Exception as e:                      # never leave a job stuck on "running"
        log.exception("download job crashed")
        _set(status="failed", summary=f"Crashed: {e}"[:250], finished=time.time())


def _run_job_inner(url, ids):
    out_dir = download_dir()
    archive = archive_path(url)
    forget_in_archive(archive, set(ids))        # an explicit pick must not be skipped as "already done"

    bad = cookies_ok_for_job()
    if bad:                                     # dead cookies would quietly download logged out
        _set(status="failed", summary=(bad + " Nothing was downloaded.")[:250], finished=time.time())
        return
    cmd, before, after, batch = build_cmd(ids, out_dir, archive, cookie_path())
    log_lines, last_flush = [], 0.0
    cur_title, cur_pct, cookie_dead = "", 0, False

    def flush(force=False):
        nonlocal last_flush
        now = time.time()
        if not force and now - last_flush < 1.0:
            return
        last_flush = now
        with _JOB_LOCK:
            if JOB.get("status") in ("running", "starting"):
                JOB.update(status="running", title=cur_title[:300], pct=cur_pct,
                           done=_count_lines(after), log="\n".join(log_lines[-40:]))

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env(),
                                creationflags=_NO_WINDOW, start_new_session=(os.name != "nt"), **_TEXT)
    except OSError as e:
        _set(status="failed", summary=f"Couldn't start yt-dlp: {e}"[:250], finished=time.time())
        return
    _PROC["p"] = proc
    with _JOB_LOCK:
        if JOB.get("status") == "cancelled":    # cancelled in the instant before spawn
            _kill_tree(proc)
        else:
            JOB["status"] = "running"
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("HFP|"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                try:
                    cur_pct = int(float(re.sub(r"[^\d.]", "", parts[2]) or 0))
                except ValueError:
                    pass
                cur_title = parts[3]
        elif line:
            log_lines.append(line[:300])
            if "no longer valid" in line.lower() and not cookie_dead:
                cookie_dead = True              # yt-dlp would carry on without the login: stop it
                _write_check(False, COOKIE_BAD)
                _kill_tree(proc)
        flush()
    proc.stdout.close()
    proc.wait()
    _PROC["p"] = None

    for p in (before,):
        try:
            os.remove(p)
        except OSError:
            pass
    finished = read_after(after)
    try:
        os.remove(batch)
    except OSError:
        pass
    wanted = set(ids)
    done = [(vid, path, info) for vid, path, info in finished if vid in wanted]
    for vid, path, info in done:                # artist / album / title from what YouTube knows
        retag(path, derive_tags(info))
    ok = len(done)
    fail = len(ids) - ok
    tail = "\n".join(log_lines[-40:])
    if cookie_dead:
        status, summary = "failed", f"{COOKIE_BAD} ({ok} downloaded before it stopped.)"
    elif fail == 0:
        status, summary = "done", f"{ok} downloaded"
    elif ok == 0:
        status, summary = "failed", ("Nothing downloaded. " + hint_for(tail)).strip()
    else:
        status, summary = "done", f"{ok} downloaded, {fail} failed or unavailable"
    with _JOB_LOCK:
        cancelled = JOB.get("status") == "cancelled"
        JOB.update(done=ok, fail=fail, pct=0, title="", log=tail, finished=time.time())
        if not cancelled:
            JOB.update(status=status, summary=summary[:250])


# ---- Updating yt-dlp -------------------------------------------------------

def _update_file():
    return ytdl_dir() / "update.json"


def update_status():
    try:
        with open(_update_file(), encoding="utf-8") as f:
            st = json.load(f)
        if st.get("state") == "running" and time.time() - st.get("at", 0) > 900:
            return {"state": "error", "msg": "Update didn't finish"}
        return st
    except (OSError, ValueError):
        return {"state": "idle"}


def start_update():
    """pip-upgrade yt-dlp in the background (YouTube breaks it regularly)."""
    if update_status().get("state") == "running":
        return False

    def write(state, msg=""):
        with open(_update_file(), "w", encoding="utf-8") as f:
            json.dump({"state": state, "msg": msg, "at": time.time()}, f)

    def run():
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]", "yt-dlp-ejs"],
                               capture_output=True, timeout=600, creationflags=_NO_WINDOW, **_TEXT)
            if r.returncode == 0:
                write("ok", f"yt-dlp {ytdlp_version()}")
            else:
                write("error", (r.stderr or r.stdout or "pip failed").strip()[-300:])
        except Exception as e:
            write("error", str(e)[:300])

    write("running")
    threading.Thread(target=run, daemon=True).start()
    return True
