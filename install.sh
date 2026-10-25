#!/bin/bash
# PS Audio Bridge installer for Raspberry Pi OS Lite (64-bit, Bookworm or Trixie).
#
#   sudo ./install.sh [options]
#   curl -fsSL https://raw.githubusercontent.com/moiz-qureshi/psaudio/main/install.sh | sudo bash
#
# Options:
#   --user NAME              account that runs the audio stack (default: psaudio)
#   --automation-key "KEY"   add an SSH public key that may only run psaudio-ctl commands
#   --admin-key "KEY"        add an SSH public key with a normal shell (your own key)
#   --image                  building an image (chroot): don't start or check services
#
# Safe to run again: it upgrades the scripts and keeps your config, clips and keys.
set -euo pipefail

REPO_URL=${PSAUDIO_REPO_URL:-https://github.com/moiz-qureshi/psaudio}
PSA_USER=psaudio
AUTOMATION_KEY=""
ADMIN_KEY=""
IMAGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --user) PSA_USER=$2; shift ;;
    --automation-key) AUTOMATION_KEY=$2; shift ;;
    --admin-key) ADMIN_KEY=$2; shift ;;
    --image) IMAGE=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
[[ "$PSA_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "bad --user" >&2; exit 2; }

# Run from a checkout, or fetch one (curl | bash).
SRC=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
if [ ! -f "$SRC/bin/psaudio-ctl" ]; then
  say "Fetching $REPO_URL"
  command -v git >/dev/null || { apt-get update -qq && apt-get install -y -qq git; }
  SRC=$(mktemp -d)/psaudio
  git clone -q --depth 1 "$REPO_URL" "$SRC"
fi
VERSION=$(cat "$SRC/VERSION")
say "Installing PS Audio Bridge $VERSION for user '$PSA_USER'"

# -- packages ------------------------------------------------------------------
say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  pipewire pipewire-audio pipewire-pulse pipewire-alsa wireplumber \
  pulseaudio-utils ffmpeg python3 >/dev/null

# -- user ----------------------------------------------------------------------
if ! id "$PSA_USER" >/dev/null 2>&1; then
  say "Creating user $PSA_USER"
  useradd --create-home --shell /bin/bash "$PSA_USER"
  passwd -l "$PSA_USER" >/dev/null   # key-only; set a password yourself if you want one
fi
usermod -aG audio "$PSA_USER"
HOME_DIR=$(getent passwd "$PSA_USER" | cut -d: -f6)

# -- files ---------------------------------------------------------------------
say "Scripts and config"
install -d /usr/local/lib/psaudio /etc/psaudio
install -m 0755 "$SRC"/bin/psaudio-ctl "$SRC"/bin/psaudio-ctl-ssh "$SRC"/bin/psaudio-gadget-setup "$SRC"/bin/psaudio-mic-fx-run /usr/local/bin/
install -m 0644 "$SRC/lib/common.sh" "$SRC/pipewire/20-loopbacks.conf.in" /usr/local/lib/psaudio/
echo "$VERSION" > /usr/local/lib/psaudio/VERSION
if [ ! -f /etc/psaudio/psaudio.conf ]; then
  sed -e "s|^PSAUDIO_USER=.*|PSAUDIO_USER=$PSA_USER|" -e "s|^SOUNDS_DIR=.*|SOUNDS_DIR=$HOME_DIR/sounds|" \
    "$SRC/config/psaudio.conf" > /etc/psaudio/psaudio.conf
else
  say "Keeping your /etc/psaudio/psaudio.conf"
fi
sed "s/psaudio psaudio/$PSA_USER $PSA_USER/" "$SRC/tmpfiles/psaudio.conf" > /etc/tmpfiles.d/psaudio.conf
install -m 0644 "$SRC/systemd/system/psaudio-gadget.service" /etc/systemd/system/
# keep the installed version on the Pi (for adding keys later: sudo /opt/psaudio/install.sh --automation-key …)
if [ "$SRC" != /opt/psaudio ]; then
  rm -rf /opt/psaudio.new && install -d /opt/psaudio.new
  cp -a "$SRC"/{bin,lib,config,systemd,pipewire,wireplumber,sounds,tmpfiles,install.sh,uninstall.sh,VERSION} /opt/psaudio.new/
  cp -a "$SRC"/README.md "$SRC"/LICENSE /opt/psaudio.new/ 2>/dev/null || true
  rm -rf /opt/psaudio && mv /opt/psaudio.new /opt/psaudio
fi

# per-user audio config + units (as the user owns them)
U() { install -o "$PSA_USER" -g "$PSA_USER" "$@"; }
U -d "$HOME_DIR/.config" "$HOME_DIR/.config/systemd" "$HOME_DIR/.config/systemd/user" \
     "$HOME_DIR/.config/systemd/user/default.target.wants" "$HOME_DIR/.config/systemd/user/sockets.target.wants" \
     "$HOME_DIR/.config/systemd/user/pipewire.service.wants" \
     "$HOME_DIR/.config/pipewire" "$HOME_DIR/.config/pipewire/pipewire.conf.d" \
     "$HOME_DIR/.config/wireplumber" "$HOME_DIR/.config/wireplumber/wireplumber.conf.d" "$HOME_DIR/sounds"
U -m 0644 "$SRC/pipewire/10-psaudio.conf" "$HOME_DIR/.config/pipewire/pipewire.conf.d/"
U -m 0644 "$SRC/wireplumber/50-psaudio-policy.conf" "$HOME_DIR/.config/wireplumber/wireplumber.conf.d/"
U -m 0644 "$SRC/systemd/user/psaudio-mic-fx.service" "$SRC/systemd/user/psaudio-configure.service" "$HOME_DIR/.config/systemd/user/"

# enable user units without a running user manager (works in a chroot too)
W=$HOME_DIR/.config/systemd/user
link() { ln -sfn "$1" "$2"; chown -h "$PSA_USER:$PSA_USER" "$2"; }
link /usr/lib/systemd/user/pipewire.service       "$W/default.target.wants/pipewire.service"
link /usr/lib/systemd/user/pipewire-pulse.service "$W/default.target.wants/pipewire-pulse.service"
link /usr/lib/systemd/user/pipewire.socket        "$W/sockets.target.wants/pipewire.socket"
link /usr/lib/systemd/user/pipewire-pulse.socket  "$W/sockets.target.wants/pipewire-pulse.socket"
link /usr/lib/systemd/user/wireplumber.service    "$W/pipewire.service.wants/wireplumber.service"
link "$W/psaudio-mic-fx.service"    "$W/default.target.wants/psaudio-mic-fx.service"
link "$W/psaudio-configure.service" "$W/default.target.wants/psaudio-configure.service"

# the audio stack runs at boot, with nobody logged in
install -d /var/lib/systemd/linger
touch "/var/lib/systemd/linger/$PSA_USER"
if ! systemctl enable psaudio-gadget.service >/dev/null 2>&1; then
  install -d /etc/systemd/system/multi-user.target.wants
  ln -sfn /etc/systemd/system/psaudio-gadget.service /etc/systemd/system/multi-user.target.wants/psaudio-gadget.service
fi

# -- built-in clips --------------------------------------------------------------
if ! ls "$HOME_DIR"/sounds/*.wav >/dev/null 2>&1; then
  say "Built-in clips"
  runuser -u "$PSA_USER" -- python3 "$SRC/sounds/generate.py" "$HOME_DIR/sounds" >/dev/null
fi

# -- USB gadget mode -------------------------------------------------------------
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
NEED_REBOOT=0
if [ -f "$CFG" ]; then
  if ! grep -qE '^\s*dtoverlay=dwc2,dr_mode=peripheral' "$CFG"; then
    say "Enabling USB device mode in $CFG"
    printf '\n[all]\n# PS Audio Bridge: the USB-C port acts as a sound card for the console\ndtoverlay=dwc2,dr_mode=peripheral\n' >> "$CFG"
    NEED_REBOOT=1
  fi
else
  warn "No config.txt found — add 'dtoverlay=dwc2,dr_mode=peripheral' to your boot config by hand"
fi

# -- SSH access --------------------------------------------------------------------
add_key() {  # line to add, comment for the log
  install -d -m 0700 -o "$PSA_USER" -g "$PSA_USER" "$HOME_DIR/.ssh"
  local f=$HOME_DIR/.ssh/authorized_keys
  touch "$f"; chown "$PSA_USER:$PSA_USER" "$f"; chmod 0600 "$f"
  local body; body=$(awk '{for(i=1;i<=NF;i++) if ($i ~ /^(ssh-|ecdsa-)/) {print $(i+1); exit}}' <<<"$1")
  if [ -n "$body" ] && grep -qF "$body" "$f"; then say "SSH key already present ($2)"; return; fi
  echo "$1" >> "$f"; say "Added SSH key ($2)"
}
[ -n "$AUTOMATION_KEY" ] && add_key "command=\"/usr/local/bin/psaudio-ctl-ssh\",no-port-forwarding,no-X11-forwarding,no-agent-forwarding $AUTOMATION_KEY" "automation, psaudio-ctl only"
[ -n "$ADMIN_KEY" ] && add_key "$ADMIN_KEY" "admin"

chown -R "$PSA_USER:$PSA_USER" "$HOME_DIR/.config/pipewire" "$HOME_DIR/.config/wireplumber" "$HOME_DIR/sounds"

# -- start ----------------------------------------------------------------------
if [ "$IMAGE" = 1 ]; then
  say "Image mode: services start on first boot"
  exit 0
fi
systemd-tmpfiles --create /etc/tmpfiles.d/psaudio.conf || true
systemctl daemon-reload
UID_N=$(id -u "$PSA_USER")
loginctl enable-linger "$PSA_USER" 2>/dev/null || true
if [ "$NEED_REBOOT" = 0 ] && [ -d /sys/class/udc ] && [ -n "$(ls /sys/class/udc 2>/dev/null)" ]; then
  systemctl restart psaudio-gadget.service || warn "gadget didn't start — see: journalctl -u psaudio-gadget"
  systemctl start "user@$UID_N.service" || true
  runuser -u "$PSA_USER" -- env XDG_RUNTIME_DIR="/run/user/$UID_N" systemctl --user daemon-reload || true
  runuser -u "$PSA_USER" -- env XDG_RUNTIME_DIR="/run/user/$UID_N" systemctl --user restart pipewire pipewire-pulse wireplumber || true
  runuser -u "$PSA_USER" -- env XDG_RUNTIME_DIR="/run/user/$UID_N" systemctl --user restart psaudio-configure psaudio-mic-fx || true
  say "Done. Check it with: sudo -u $PSA_USER XDG_RUNTIME_DIR=/run/user/$UID_N psaudio-ctl status"
else
  say "Done — reboot to switch the USB-C port into sound-card mode: sudo reboot"
fi
