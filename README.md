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
With one set, anyone can open the site and play any song, but only people who enter the password
(🔒 Log in, top right) can change anything — likes, playlists, scan, output/remote control. Visitors who
haven't logged in get a small corner panel that starts out **listening along** with whatever you're
playing (same track, same position, follows pause/skip/seek); **Stop** lets them play their own music
locally, which is never saved to the server.
