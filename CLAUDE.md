# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the server

```bash
pip install -r requirements.txt
MUSIC_DIR=/path/to/music uvicorn app:app --host 0.0.0.0 --port 8000
```

Configuration is entirely via environment variables:
- `MUSIC_DIR` — path to the music folder (default: `./music`)
- `DATA_DIR` — where the SQLite database and extracted album art are stored (default: `./data`)

## Architecture

This is a two-file project: a FastAPI backend (`app.py`) and a single-file vanilla-JS SPA (`static/index.html`). There is no build step, no bundler, and no test suite.

### Backend (`app.py`)

- **FastAPI** serves all API routes under `/api/*`; the static directory is mounted last so it only handles non-API paths.
- **SQLite** (WAL mode, `DATA_DIR/library.db`) stores tracks, playlists, playlist membership, and play history. The `db()` helper opens a new connection per call — connections are not shared across requests.
- **Scanning** (`POST /api/scan`) runs in a background `threading.Thread`. Progress is tracked in the module-level `SCAN` dict and polled via `GET /api/scan/status`. Unchanged files (same mtime + size) are skipped for speed. Removed files are detected by comparing `last_scan` timestamps.
- **Metadata** is read with `mutagen`. Cover art is extracted and saved to `DATA_DIR/art/<id>.jpg|.png`; the `has_art` / `art_ext` columns track what was found.
- **Audio streaming** is a plain `FileResponse` — no transcoding, no range-request handling beyond what Starlette provides.

### Frontend (`static/index.html`)

All JS lives in a single IIFE inside the HTML file. State is in a single `S` object. Key design choices:

- **Two persistent `<audio>` elements** (`aud[0]` and `aud[1]`, index toggled with `ai^=1`) are the core trick that enables crossfading and avoids autoplay-policy re-blocking on every track change.
- **Crossfade** is implemented with `requestAnimationFrame` volume ramps (`fade()`) applied to the outgoing and incoming elements simultaneously.
- **Playback state** (queue, position, volume, shuffle, repeat, crossfade duration) is persisted to `localStorage` and restored on boot so playback resumes where the user left off.
- **Smart playlists** are evaluated client-side in `smartIds()` against the in-memory `S.byId` map.
- Rendering is done by rebuilding the DOM directly (no virtual DOM); `content-visibility: auto` is used on rows for scroll performance.
