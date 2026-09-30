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
- `PASSWORD` — optional; read from the environment or a git-ignored `.env` next to `app.py` (`_load_dotenv`, real env vars win). Unset/empty = no auth at all. Set = single shared password, no usernames (see Auth below).

## Architecture

This is a two-file project: a FastAPI backend (`app.py`) and a single-file vanilla-JS SPA (`static/index.html`). There is no build step, no bundler, and no test suite.

### Backend (`app.py`)

- **FastAPI** serves all API routes under `/api/*`; the static directory is mounted last so it only handles non-API paths.
- **SQLite** (WAL mode, `DATA_DIR/library.db`) stores tracks, playlists, playlist membership, and play history. The `db()` helper opens a new connection per call — connections are not shared across requests.
- **Scanning** (`POST /api/scan`) runs in a background `threading.Thread`. Progress is tracked in the module-level `SCAN` dict and polled via `GET /api/scan/status`. Unchanged files (same mtime + size) are skipped for speed. Removed files are detected by comparing `last_scan` timestamps.
- **Metadata** is read with `mutagen`. Cover art is extracted and saved to `DATA_DIR/art/<id>.jpg|.png`; the `has_art` / `art_ext` columns track what was found.
- **Audio streaming** (`GET /api/stream/{id}`) is a plain `FileResponse` (Starlette handles Range), with two exceptions. 0-byte files return 422 (they're flagged `broken` in `/api/tracks`). Apple Lossless (ALAC) `.m4a` files, which Chrome/Firefox can't decode, are transcoded live: the response is a *virtual* 16-bit PCM WAV whose exact length is computed from the metadata (`_alac_info`), with Range parsed by hand and each request running `ffmpeg -ss <sample offset>` piped out (`_pcm_stream`; killed when the client disconnects). Nothing is cached or written to disk. Needs `ffmpeg` on PATH; without it ALAC falls back to the raw file and the client skips it.
- **Cross-device sync** (`GET /api/sync/events`, `POST /api/sync/{state,cmd,claim,release,leave}`) is an SSE fan-out where the server is the sole authority over which connected client owns audio (`_SYNC_OUTPUT`). `GET /api/sync/events?cid=&want=` registers a client and grants it output only if none is currently assigned; `POST /api/sync/state` is rejected unless it comes from the current output. This split — one publisher of truth, everyone else sends intent — is what stops a client from taking over or corrupting playback just by opening the page.

- **Auth** (only when `PASSWORD` is set): `POST /api/login` sets an HttpOnly cookie `mus_auth` = HMAC of the password (so changing it logs everyone out; 5 bad tries per IP → 429 for 5 min), `GET /api/auth` reports `{required, authed}`. Reads (tracks, art, stream, history, playlists GET, scan status, the sync feed) stay open; every mutating route has `dependencies=OWNER` (`owner_only` → 401), including all of `/api/sync/{state,cmd,claim,release,leave}` — authority comes from the cookie, never from knowing a `cid`. Unauthenticated `sync/events` connections are registered in `_SYNC_LISTENERS`: they receive `hello`/`role`/`state` (never `cmd`), are not counted in `peers` and can never be granted output.

### Frontend (`static/index.html`)

All JS lives in a single IIFE inside the HTML file. State is in a single `S` object. Key design choices:

- **Two persistent `<audio>` elements** (`aud[0]` and `aud[1]`, index toggled with `ai^=1`) are the core trick that enables crossfading and avoids autoplay-policy re-blocking on every track change.
- **Crossfade** is implemented with `requestAnimationFrame` volume ramps (`fade()`) applied to the outgoing and incoming elements simultaneously.
- **Playback state** (queue, position, volume, shuffle, repeat, crossfade duration) is persisted to `localStorage` and restored on boot so playback resumes where the user left off.
- **Smart playlists** are evaluated client-side in `smartIds()` against the in-memory `S.byId` map.
- **Cross-device sync/remote control**: the server assigns each connected browser a `role` of `output` (owns the `<audio>` elements, publishes full state via `pushState()`) or `controller` (mirrors state, sends intent via `sendCmd()`); `S.role` is never chosen client-side, only requested (`rolePref` in `localStorage`, sent as `want=` on connect) and confirmed by the server's `hello`/`role` SSE messages. Controllers never publish state — only the output does, and the server enforces this — so a newly opened tab can't clobber a device that's already playing; taking over audio requires an explicit tap (`claimOutput()`/`releaseOutput()`, `POST /api/sync/{claim,release}`). `applyState()` treats the published track `id` as authoritative and repairs `pos` to agree with it, which is what keeps the displayed title and the loaded audio from diverging. `applyCmd()` is where the output executes a controller's command (play/pause/seek/volume/crossfade/shuffle/repeat/sleep/queue edits) by calling the same local functions its own UI uses, so results flow back out through the normal `pushState()` path.
- **Playback errors**: each `<audio>` has an `error` listener (`onAudioError`) that flags the track (`t.failed`), toasts, and skips to the next one (gives up after 5 in a row).
- **Mini bar (>640px)**: centre column stacks the transport buttons over a seek slider (`#barSeek`, shares `bindSeek()` with the sheet's slider); right side has like / add-to-playlist picker / sleep popover / output-role toggle. Phones keep the thin progress line.
- **Desktop side-panel mode**: `body.side-mode` (toggle `#dockBtn`, saved as `localStorage.sheetMode`) docks the now-playing sheet to the right, resizable via `#sideGrip` (width in `localStorage.sideW`). All its CSS is gated on `(min-width:900px) and (hover:hover)`. `#sideQueue` shows up to 25 history tracks (from `/api/history`), now playing, and up next via `fillQueue(body,true)`.
- **Guest mode** (`S.guest` = password required and not logged in; `body.guest`): `S.role` is pinned to `"output"` so the local player works unchanged, but `pushState/syncState/sendCmd/claim/release/leave` are no-ops and `countPlay` doesn't hit the server; writes (like, playlists, scan) call `denyGuest()` which toasts instead. The `#live` corner panel shows the owner's published state (`S.live`, via `guestSyncMsg`); on the first `hello` the guest auto-follows (`startFollow` → `followApply`): loads the owner's track/queue, mirrors play/pause and re-seeks when drift exceeds 1.5 s. While `S.follow`, local transport/seek/queue actions are blocked by `followLocked()` and auto-advance is disabled; **Stop** (`stopFollow`) hands control back locally.
- Rendering is done by rebuilding the DOM directly (no virtual DOM); `content-visibility: auto` is used on rows for scroll performance.
- The app is installable as a PWA (`static/manifest.json` + icons); there's no service worker, so it requires network connectivity.
