"""Pull songs that aren't in the library yet from another machine (a Mac's Apple Music folder) over ssh.

Logic only -- the HTTP routes live in app.py (all owner-only). The flow:

  list the remote tree (rsync --list-only, audio files only) -> drop everything already in
  MUSIC_DIR -> copy the rest into a staging folder -> move it into place -> rescan.

Nothing is ever deleted or overwritten, locally or remotely. The listing is compared by
Unicode-normalised path (NFC): macOS stores accented/Japanese names decomposed (NFD), Linux keeps
whatever bytes it is given, so plain `rsync --ignore-existing` re-copies songs that are already
here (126 of 165 "new" files in the first dry run). New files are stored under NFC names.

Needs rsync + ssh on this machine and key-based ssh access to the source (no password prompts).
"""
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import unicodedata
from pathlib import Path

DEFAULT_SOURCE = "equa@192.168.1.73:/Users/equa/Music/Music/Media.localized/Music/"
AUDIO_EXTS = (".m4a", ".mp3", ".flac", ".aac", ".ogg", ".opus", ".wav")
MIN_FREE_BYTES = 1024 ** 3
SOURCE_RE = re.compile(r"^([A-Za-z0-9_][\w.\-]*)@([A-Za-z0-9][A-Za-z0-9.\-]*):(/.*|~.*)$")
LIST_RE = re.compile(r"^(\S{10})\s+([\d,]+)\s+\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (.+)$")
SSH = "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15"
FILTERS = ["--include=*/"] + [f"--include=*{e}" for e in AUDIO_EXTS] + ["--exclude=*"]

_CFG = {}


def init(data_dir, music_dir, db_factory, after=None):
    """Called once by app.py (and by the tests with scratch folders). `after()` runs when new songs landed."""
    _CFG.update(data=Path(data_dir), music=Path(music_dir), db=db_factory, after=after)
    with _conn():
        pass


def _conn():
    c = _CFG["db"]()
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    return c


def normalize_source(raw):
    """`user@host:/abs/path` (a trailing `/` or `/*` is fine) -> `user@host:/abs/path/`."""
    s = (raw or "").strip()
    if len(s) > 500 or re.search(r"[\x00-\x1f\x7f]", s):
        raise ValueError("That doesn't look like user@ip:/full/path")
    s = re.sub(r"/?\*?$", "", s) + "/"
    if not SOURCE_RE.match(s):
        raise ValueError("Use the form user@ip:/full/path (e.g. equa@192.168.1.73:/Users/equa/Music)")
    return s


def get_source():
    with _conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key='pull_src'").fetchone()
    return (row[0] if row else None) or DEFAULT_SOURCE


def set_source(raw):
    """Save a new source ('' = back to the default)."""
    val = normalize_source(raw) if (raw or "").strip() else ""
    with _conn() as c:
        if val:
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('pull_src',?)", (val,))
        else:
            c.execute("DELETE FROM meta WHERE key='pull_src'")
    return source_info()


def source_info():
    src = get_source()
    return {"source": src, "custom": src != DEFAULT_SOURCE, "default": DEFAULT_SOURCE, "job": job_state()}


def parse_listing(text):
    """`rsync --list-only` output -> [(relative path, bytes)] for regular files."""
    out = []
    for line in text.splitlines():
        m = LIST_RE.match(line)
        if m and m.group(1)[0] == "-":
            out.append((m.group(3), int(m.group(2).replace(",", ""))))
    return out


def _nfc(s):
    return unicodedata.normalize("NFC", s)


def local_keys(music):
    """NFC relative paths of every audio file already in the library folder."""
    keys = set()
    for d, _, files in os.walk(music):
        for f in files:
            if f.lower().endswith(AUDIO_EXTS):
                keys.add(_nfc(os.path.relpath(os.path.join(d, f), music)))
    return keys


def pick_new(listing, have):
    """Split the remote listing into (new, too_long): files not in `have` that can be stored here."""
    new, long_ = [], []
    seen = set(have)
    for rel, size in listing:
        key = _nfc(rel)
        if key in seen:
            continue
        seen.add(key)
        if any(len(part.encode()) > 255 for part in key.split("/")):
            long_.append(rel)            # ext4 limit: the file name can't be stored as-is
        else:
            new.append((rel, size))
    return new, long_


def _place(music, rel):
    """Where an NFC relative path goes, reusing existing folders whose name only differs in normalisation."""
    cur = Path(music)
    parts = _nfc(rel).split("/")
    for i, part in enumerate(parts):
        if i < len(parts) - 1 and cur.is_dir():
            for e in os.scandir(cur):
                if e.is_dir() and _nfc(e.name) == part:
                    part = e.name
                    break
        cur = cur / part
    return cur


JOB = {"status": "idle"}
_JOB_LOCK = threading.Lock()
_PROC = {"p": None}
_CANCEL = threading.Event()


def job_state():
    with _JOB_LOCK:
        return dict(JOB)


def _set(**kw):
    with _JOB_LOCK:
        JOB.update(kw)


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


def cancel():
    _CANCEL.set()
    p = _PROC["p"]
    if p and p.poll() is None:
        _kill(p)


def start():
    """-> (ok, error). One pull at a time; the work happens in a daemon thread."""
    if not shutil.which("rsync") or not shutil.which("ssh"):
        return False, "rsync and ssh must be installed on the server."
    music = _CFG["music"]
    if not music.is_dir():
        return False, f"Music folder not found: {music}"
    with _JOB_LOCK:
        if JOB.get("status") == "running":
            return False, "A pull is already running."
        _CANCEL.clear()
        JOB.clear()
        JOB.update(status="running", phase="listing", source=get_source(), total=0, done=0, bytes=0,
                   bytes_done=0, started=time.time(), message="Connecting…", new=[], errors=[], skipped_long=0)
    threading.Thread(target=_run, daemon=True).start()
    return True, ""


def _run():
    try:
        _run_inner()
    except Exception as e:                                   # never leave the job "running"
        _set(status="error", message=str(e))
    finally:
        _set(finished=time.time())
        _PROC["p"] = None


def _rsync(args, feed=False):
    p = subprocess.Popen(["rsync", *args], stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                         env={**os.environ, "LC_ALL": "C.UTF-8"})
    _PROC["p"] = p
    return p


def _tail(text, n=3):
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return " ".join(lines[-n:])[:400]


def _run_inner():
    music, src = _CFG["music"], get_source()
    base = ["-8", "-e", SSH]
    # 1. what does the other side have?
    p = _rsync(["-r", "--list-only", *base, *FILTERS, src])
    out, err = p.communicate()
    if _CANCEL.is_set():
        return _set(status="cancelled", message="Cancelled")
    listing = parse_listing(out.decode("utf-8", "replace"))
    if p.returncode not in (0, 23) or (p.returncode == 23 and not listing):
        return _set(status="error", message="Couldn't read " + src + ": " + (_tail(err.decode("utf-8", "replace")) or f"rsync exit {p.returncode}"))
    new, long_ = pick_new(listing, local_keys(music))
    total_bytes = sum(s for _, s in new)
    _set(listed=len(listing), total=len(new), bytes=total_bytes, skipped_long=len(long_),
         new=[r for r, _ in new[:200]])
    if not new:
        return _set(status="done", phase="done", message=f"Nothing new - all {len(listing)} songs are already here.")
    free = shutil.disk_usage(music).free
    stage_free = shutil.disk_usage(_CFG["data"]).free
    if free < total_bytes + MIN_FREE_BYTES or stage_free < total_bytes + MIN_FREE_BYTES:
        return _set(status="error", message=f"Not enough free space for {len(new)} songs ({total_bytes / 1024 ** 2:.0f} MB).")
    # 2. copy them into a staging folder
    stage = _CFG["data"] / "pull"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    _set(phase="copying", message=f"Copying {len(new)} songs…")
    sizes = dict(new)
    p = _rsync(["-t", "--from0", "--files-from=-", "--out-format=%n", *base, src, str(stage) + "/"], feed=True)
    names = "\0".join(r for r, _ in new).encode() + b"\0"
    errs = []
    t = threading.Thread(target=lambda: errs.append(p.stderr.read().decode("utf-8", "replace")), daemon=True)
    t.start()
    threading.Thread(target=lambda: (p.stdin.write(names), p.stdin.close()), daemon=True).start()
    for raw in p.stdout:
        name = raw.decode("utf-8", "replace").rstrip("\n")
        if name in sizes:
            with _JOB_LOCK:
                JOB["done"] += 1
                JOB["bytes_done"] += sizes[name]
    p.wait()
    t.join(2)
    err_text = errs[0] if errs else ""
    if _CANCEL.is_set():
        shutil.rmtree(stage, ignore_errors=True)
        return _set(status="cancelled", message="Cancelled")
    # 3. move into the library (never overwriting)
    added = []
    for d, _, files in os.walk(stage):
        for f in files:
            sp = Path(d) / f
            rel = os.path.relpath(sp, stage)
            dest = _place(music, rel)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(sp), str(dest))
            added.append(_nfc(rel))
    shutil.rmtree(stage, ignore_errors=True)
    failed = len(new) - len(added)
    msg = f"Added {len(added)} new song{'s' if len(added) != 1 else ''}"
    if failed:
        msg += f" ({failed} failed: {_tail(err_text) or 'see server log'})"
    if long_:
        msg += f"; {len(long_)} skipped (file name too long for this disk)"
    _set(status="done" if added or not failed else "error", phase="done", message=msg, added=len(added),
         errors=[_tail(err_text)] if failed else [])
    if added and _CFG.get("after"):
        try:
            _CFG["after"]()
        except Exception:
            pass
