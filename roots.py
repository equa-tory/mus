"""The library's music folders.

Any number of folders make up the library: every one is scanned, and exactly one is the *main*
folder -- where pulled songs are saved. The list lives in the `meta` table (key `music_roots`, JSON
`[{"path", "main"}]`), so it is part of the database backups. With no saved list the library is
just `MUSIC_DIR` (the env var), which is also the main folder -- nothing changes until the owner
saves a list.

`tracks.path` is absolute, so adding, removing or re-pointing a folder never rewrites rows: a folder
that is taken away simply stops being scanned, and its songs leave the library on the next scan
(the files themselves are never touched). Playlists / likes of those songs go with them.
"""
import json
import os
from pathlib import Path

MAX_FOLDERS = 30
_CFG = {}


def init(default_dir, db_factory):
    _CFG["default"] = Path(default_dir)
    _CFG["db"] = db_factory
    with _conn():
        pass


def _conn():
    c = _CFG["db"]()
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return c


def _saved():
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key='music_roots'").fetchone()
    try:
        rows = json.loads(row[0]) if row and row[0] else []
    except ValueError:
        rows = []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("path"), str)]


def config():
    """[{"path", "main"}] in display order. Never empty."""
    rows = _saved()
    if not rows:
        return [{"path": os.path.normpath(str(_CFG["default"])), "main": True}]
    main = next((r for r in rows if r.get("main")), rows[0])
    return [{"path": os.path.normpath(r["path"]), "main": r is main} for r in rows]


def roots():
    return [Path(f["path"]) for f in config()]


def main_root():
    return next(Path(f["path"]) for f in config() if f["main"])


def is_custom():
    return bool(_saved())


def _inside(a, b):
    """True when path `a` is `b` or lives under it."""
    a, b = a.rstrip(os.sep) + os.sep, b.rstrip(os.sep) + os.sep
    return a.startswith(b)


def set_config(folders):
    """Validate and save [{"path", "main"?}]. [] = back to the MUSIC_DIR default."""
    if not isinstance(folders, list) or len(folders) > MAX_FOLDERS:
        raise ValueError(f"Give a list of at most {MAX_FOLDERS} folders")
    clean, seen = [], []
    for f in folders:
        raw = f.get("path") if isinstance(f, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Every folder needs a path")
        raw = raw.strip()
        if len(raw) > 500 or "\n" in raw or "\x00" in raw:
            raise ValueError("That doesn't look like a folder path")
        p = os.path.normpath(os.path.expanduser(raw))
        if not os.path.isabs(p):
            raise ValueError(f"“{raw}” isn't a full path (start it with /)")
        if not os.path.isdir(p):
            raise ValueError(f"“{p}” isn't a folder on the server")
        if not os.access(p, os.R_OK | os.X_OK):
            raise ValueError(f"The server can't read “{p}”")
        if p in seen:
            continue
        for q in seen:
            if _inside(p, q) or _inside(q, p):
                raise ValueError(f"“{p}” and “{q}” overlap - one is inside the other")
        seen.append(p)
        clean.append({"path": p, "main": bool(f.get("main"))})
    if clean:
        main = next((r for r in clean if r["main"]), clean[0])
        for r in clean:
            r["main"] = r is main
        if not os.access(main["path"], os.W_OK):
            raise ValueError(f"The main folder “{main['path']}” isn't writable by the server")
    with _conn() as c:
        if clean:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('music_roots',?)", (json.dumps(clean),))
        else:
            c.execute("DELETE FROM meta WHERE key='music_roots'")
    return config()
