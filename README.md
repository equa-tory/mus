# mus
local self-hosted music streaming

## Install

```bash
git clone https://github.com/equa-tory/mus && cd mus
./install.sh --music /path/to/music        # add --port 8881 to change the port; -y to skip prompts
```

The script installs `python3`/`venv`/`ffmpeg` if missing (apt, dnf or pacman), builds a virtualenv,
installs the Python deps, writes `/etc/systemd/system/mus.service` (from `mus.service`), enables it on boot,
and starts the first library scan. Re-run it any time to update. `./install.sh --help` lists the options
(`--no-service`, `--dry-run`).

`ffmpeg` is required for playing Apple Lossless (ALAC) files, which are transcoded live.

## Password (optional)

Put one line in `.env` next to `app.py` (see `.env.example`) and restart (`sudo systemctl restart mus`):

```
PASSWORD=your-password
```

No username. With no `PASSWORD` line (or an empty one) there is no login and everyone can do everything.

To make the login mandatory — a login page before anything else, no guest browsing or listening — add `REQUIRE_LOGIN=true` next to it. Without that line (or with `false`) visitors can still listen as guests.

For a third mode, add `LISTEN_ONLY=true` (also needs `PASSWORD`): visitors without the password can *only* listen along with what you play — no picking songs, skipping, seeking or playing locally, and no Stop button. It is a UI restriction; the audio endpoints stay readable.
With one set, anyone can open the site and play any song, but only people who enter the password
(🔒 Log in, top right) can change anything — likes, playlists, scan, output/remote control. Visitors who
haven't logged in get a small corner panel that starts out **listening along** with whatever you're
playing (same track, same position, follows pause/skip/seek); **Stop** lets them play their own music
locally, which is never saved to the server.

## Backups

The database (library index, likes, playlists, history) is backed up automatically: by default every 48 h into `/mnt/backup/mus`, keeping the newest 1. Override in `.env` (see `.env.example`): `BACKUP_DIR` (empty = off), `BACKUP_EVERY_HOURS` (`0` = manual only), `BACKUP_KEEP`. The ⚙ button in the header lists the backups and lets you download or restore one, restore from an uploaded file, or back up right now. Cover art isn't included — a scan rebuilds it. Make sure the service user can write to the backup folder (`install.sh` creates the default one).

## Authors

The **Authors** tab groups tracks by artist. If one artist field holds several names (e.g. `name1 / name2`), set the separator under ⚙ Settings → Authors (default `/`, a few characters are fine) and each name becomes its own author. Tap ★ to follow an author — followed authors are listed first. Inside an author, Shuffle play plays a random mix of their tracks (on phones it's the floating button at the bottom).
