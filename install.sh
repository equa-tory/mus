#!/usr/bin/env bash
# One-shot installer: system packages (python3, venv, ffmpeg), a virtualenv with the
# Python deps, and a systemd service that starts on boot. Safe to re-run (updates in place).
#
#   ./install.sh                                   # asks for the music folder
#   ./install.sh --music /srv/music --port 8881 -y # non-interactive
#
# Options:
#   --music DIR    music folder to serve      (env MUSIC_DIR, default ~/Music)
#   --port N       listen port                (env PORT, default 8881)
#   --no-service   set up the venv only; don't touch systemd
#   --no-deno      don't install Deno (the YouTube music downloader needs a JS runtime: Deno 2.3+/Node 22+)
#   --dry-run      print what would be done, change nothing
#   -y, --yes      never prompt
set -euo pipefail
shopt -u patsub_replacement 2>/dev/null || true   # bash 5.2: keep '&' literal in ${var//a/b}

cd "$(dirname "$(readlink -f "$0")")"
APP_DIR=$PWD
MUSIC_DIR=${MUSIC_DIR:-}
PORT=${PORT:-8881}
SERVICE=mus
UNIT_PATH=/etc/systemd/system/$SERVICE.service
WANT_SERVICE=1 DRY=0 YES=0 NO_DENO=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --music) MUSIC_DIR=${2:?--music needs a value}; shift 2 ;;
    --port) PORT=${2:?--port needs a value}; shift 2 ;;
    --no-service) WANT_SERVICE=0; shift ;;
    --no-deno) NO_DENO=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -y|--yes) YES=1; shift ;;
    -h|--help) sed -n '2,/^set -e/{/^set -e/!p}' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;33m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }
die()  { warn "$*"; exit 1; }
run()  { if ((DRY)); then printf '  + %s\n' "$*"; else "$@"; fi; }
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

[[ $PORT =~ ^[0-9]+$ ]] && ((PORT >= 1 && PORT <= 65535)) || die "invalid port: $PORT"
SVC_USER=${SUDO_USER:-${USER:-$(id -un)}}

# ---- music folder ----
if [[ -z $MUSIC_DIR ]]; then
  MUSIC_DIR=$HOME/Music
  if ((!YES)) && [[ -t 0 ]]; then
    read -r -p "Music folder [$MUSIC_DIR]: " ans || true
    MUSIC_DIR=${ans:-$MUSIC_DIR}
  fi
fi
MUSIC_DIR=$(readlink -m "${MUSIC_DIR/#\~/$HOME}")
[[ $MUSIC_DIR == *\"* || $MUSIC_DIR == *$'\n'* ]] && die "music folder path can't contain quotes or newlines"
[[ -d $MUSIC_DIR ]] || warn "$MUSIC_DIR doesn't exist yet — the server will start, but a scan will fail until it does"

# ---- system packages ----
say "Checking system packages (python3 >= 3.10, venv, ffmpeg)"
need=()
python3 -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null || need+=(python3)
python3 -c 'import venv, ensurepip' 2>/dev/null || need+=(venv)
command -v ffmpeg >/dev/null || need+=(ffmpeg)
if ((${#need[@]})); then
  say "Missing: ${need[*]}"
  if command -v apt-get >/dev/null; then
    run $SUDO apt-get update -qq
    run $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg
  elif command -v dnf >/dev/null; then
    run $SUDO dnf install -y python3 python3-pip ffmpeg-free
  elif command -v pacman >/dev/null; then
    run $SUDO pacman -S --needed --noconfirm python python-pip ffmpeg
  else
    die "unsupported package manager — install python3 (>=3.10, with venv) and ffmpeg manually, then re-run"
  fi
fi
((DRY)) || python3 -c 'import sys; sys.exit(sys.version_info < (3,10))' || die "python3 >= 3.10 is required"

# ---- virtualenv + python deps ----
say "Setting up virtualenv + Python dependencies"
[[ -x venv/bin/python ]] || run python3 -m venv venv
run venv/bin/pip install -q --upgrade pip || warn "could not upgrade pip (offline?) — continuing"
run venv/bin/pip install -q -r requirements.txt

# ---- JavaScript runtime for yt-dlp (only the "Download music" feature needs it) ----
# yt-dlp silently ignores a runtime older than these, and every download then fails at
# YouTube's challenge step - a distro Node 18 is the classic case.
ver_ge() { [[ -n $1 ]] && [[ $(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1) == "$2" ]]; }
exe_ver() { "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1 || true; }
have_js_runtime() {
  local p
  for p in "$APP_DIR/.deno/bin/deno" "$(command -v deno || true)"; do
    [[ -n $p && -x $p ]] && ver_ge "$(exe_ver "$p")" 2.3.0 && return 0
  done
  p=$(command -v bun || true);  [[ -n $p && -x $p ]] && ver_ge "$(exe_ver "$p")" 1.2.11 && return 0
  p=$(command -v node || true); [[ -n $p && -x $p ]] && ver_ge "$(exe_ver "$p")" 22.0.0 && return 0
  return 1
}
if ((DRY)); then
  say "(dry run) would check for a JS runtime and install Deno into $APP_DIR/.deno if needed"
elif have_js_runtime; then
  say "JavaScript runtime for yt-dlp: OK"
elif ((NO_DENO)); then
  warn "No JS runtime (Deno 2.3+ / Node 22+) - --no-deno given; the music downloader won't work until you install one."
else
  say "No JavaScript runtime new enough for yt-dlp - installing Deno into $APP_DIR/.deno (no root needed)"
  ans=y
  if ((!YES)) && [[ -t 0 ]]; then read -r -p "Install Deno? [Y/n] " ans || ans=y; fi
  if [[ ${ans:-y} =~ ^[Yy] ]]; then
    command -v unzip >/dev/null || warn "unzip is missing (the Deno installer needs it): sudo apt install unzip"
    tmp=$(mktemp)
    if curl -fsSL https://deno.land/install.sh -o "$tmp" && DENO_INSTALL="$APP_DIR/.deno" sh "$tmp" --no-modify-path </dev/null >/dev/null 2>&1; then
      have_js_runtime && say "Deno installed" || warn "Deno installed but isn't new enough? Check $APP_DIR/.deno/bin/deno --version"
    else
      warn "Couldn't install Deno - the music downloader won't work until Deno 2.3+ (or Node 22+) is installed."
    fi
    rm -f "$tmp"
  fi
fi

# ---- download folder (⚙ → Download music; override with DOWNLOAD_DIR in .env) ----
DL=${DOWNLOAD_DIR-/mnt/videos/Loop}
if [[ -n $DL && ! -d $DL ]]; then
  say "Creating the download folder $DL"
  run mkdir -p "$DL" 2>/dev/null || run $SUDO mkdir -p "$DL" || warn "could not create $DL - the downloader will say so until it exists"
  [[ -d $DL && ! -w $DL ]] && run $SUDO chown "$SVC_USER" "$DL" || true
fi

if ((!WANT_SERVICE)); then
  say "Done (no service). Run it with:"
  echo "  MUSIC_DIR=\"$MUSIC_DIR\" venv/bin/uvicorn app:app --host 0.0.0.0 --port $PORT"
  exit 0
fi

# ---- systemd service ----
command -v systemctl >/dev/null || die "systemd not found — re-run with --no-service"
say "Installing systemd service ($UNIT_PATH)"
unit=$(grep -v '^# Template' mus.service)
esc_pct() { printf '%s' "${1//%/%%}"; }     # '%' is special in unit files
unit=${unit//@USER@/$SVC_USER}
unit=${unit//@DIR@/$(esc_pct "$APP_DIR")}
unit=${unit//@MUSIC_DIR@/$(esc_pct "$MUSIC_DIR")}
unit=${unit//@PORT@/$PORT}
if ((DRY)); then
  echo "  + write $UNIT_PATH:"; sed 's/^/      /' <<<"$unit"
else
  tmp=$(mktemp); printf '%s\n' "$unit" >"$tmp"
  $SUDO install -m 644 "$tmp" "$UNIT_PATH"; rm -f "$tmp"
fi
# default backup folder (override with BACKUP_DIR in .env): make sure the service user can write it
BK=${BACKUP_DIR-/mnt/ssd/backups/mus}
if [[ -n $BK && ! -d $BK ]]; then
  say "Creating backup folder $BK"
  run $SUDO mkdir -p "$BK" && run $SUDO chown "$SVC_USER" "$BK" || warn "could not create $BK — backups will report an error until it exists"
fi
run $SUDO systemctl daemon-reload
run $SUDO systemctl enable "$SERVICE"
run $SUDO systemctl restart "$SERVICE"

# ---- verify + first scan ----
if ((!DRY)); then
  ok=0
  for _ in $(seq 1 20); do
    if curl --noproxy '*' -fs "http://127.0.0.1:$PORT/api/scan/status" >/dev/null 2>&1; then ok=1; break; fi
    sleep 0.5
  done
  ((ok)) || { warn "service didn't come up — see: journalctl -u $SERVICE -e"; exit 1; }
  if [[ -d $MUSIC_DIR ]]; then
    say "Service is up — starting first library scan"
    curl --noproxy '*' -fs -X POST "http://127.0.0.1:$PORT/api/scan" >/dev/null || true
  fi
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Done. Open  http://${ip:-localhost}:$PORT"
echo "  status:  systemctl status $SERVICE"
echo "  logs:    journalctl -u $SERVICE -f"
echo "  update:  git pull && ./install.sh -y --music \"$MUSIC_DIR\" --port $PORT"
