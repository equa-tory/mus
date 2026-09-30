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
