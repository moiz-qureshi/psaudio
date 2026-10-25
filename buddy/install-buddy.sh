#!/bin/bash
# Install / update psaudio-buddy for the current user (psaudio) - no root needed.
# Run on the Pi from this directory:  ./install-buddy.sh
# It never touches the existing bridge (pipewire config, mic-fx, gadget, psaudio-ctl).
set -euo pipefail
cd "$(dirname "$0")"
APP=$HOME/.local/lib/psaudio-buddy
CONF=$HOME/.config/psaudio-buddy
UNIT=$HOME/.config/systemd/user/psaudio-buddy.service
umask 022
mkdir -p "$APP" "$CONF" "$(dirname "$UNIT")" "$HOME/.local/bin" "$HOME/.local/state/psaudio-buddy/test"
# keep the previous version for rollback
[ -f "$APP/buddy.py" ] && cp -a "$APP/buddy.py" "$APP/buddy.py.prev"
[ -f "$APP/local_backend.py" ] && cp -a "$APP/local_backend.py" "$APP/local_backend.py.prev"
[ -f "$UNIT" ] && cp -a "$UNIT" "$UNIT.prev"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --require-virtualenv -r requirements.lock
# offline wake-word model (Vosk small English, Apache-2.0, ~40 MB)
M=vosk-model-small-en-us-0.15
if [ ! -d "$APP/models/$M" ]; then
  mkdir -p "$APP/models" && curl -fsSL -o "/tmp/$M.zip" "https://alphacephei.com/vosk/models/$M.zip"
  python3 -m zipfile -e "/tmp/$M.zip" "$APP/models/" && rm -f "/tmp/$M.zip"
fi
install -m 755 buddy.py "$APP/buddy.py"
install -m 644 local_backend.py "$APP/local_backend.py"
install -m 644 requirements.lock "$APP/requirements.lock"
install -m 755 buddy-key "$HOME/.local/bin/buddy-key"
install -m 644 psaudio-buddy.service "$UNIT"
[ -f "$CONF/buddy.toml" ] || install -m 644 buddy.toml.example "$CONF/buddy.toml"
umask 077
touch "$CONF/secrets.env"; chmod 600 "$CONF/secrets.env"
if ! grep -q '^BUDDY_TOKEN=' "$CONF/secrets.env"; then
  echo "BUDDY_TOKEN=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)" >> "$CONF/secrets.env"
fi
systemctl --user daemon-reload
systemctl --user enable psaudio-buddy.service >/dev/null
systemctl --user restart psaudio-buddy.service
sleep 2
systemctl --user --no-pager --lines=5 status psaudio-buddy.service | head -12
grep -q '^GEMINI_API_KEY=' "$CONF/secrets.env" || echo "No Gemini key yet - run:  buddy-key"
