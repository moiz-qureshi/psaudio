#!/bin/bash
# Remove PS Audio Bridge. Keeps your clips and /etc/psaudio unless --purge.
#   sudo ./uninstall.sh [--purge]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
PURGE=0; [ "${1:-}" = --purge ] && PURGE=1
. /usr/local/lib/psaudio/common.sh 2>/dev/null || PSAUDIO_USER=psaudio
HOME_DIR=$(getent passwd "$PSAUDIO_USER" | cut -d: -f6 || true)
UID_N=$(id -u "$PSAUDIO_USER" 2>/dev/null || true)

if [ -n "$UID_N" ]; then
  runuser -u "$PSAUDIO_USER" -- env XDG_RUNTIME_DIR="/run/user/$UID_N" systemctl --user disable --now psaudio-mic-fx psaudio-configure psaudio-route-guard 2>/dev/null || true
fi
systemctl disable --now psaudio-gadget.service 2>/dev/null || true
rm -f /etc/systemd/system/psaudio-gadget.service /etc/tmpfiles.d/psaudio.conf \
      /usr/local/bin/psaudio-ctl /usr/local/bin/psaudio-ctl-ssh /usr/local/bin/psaudio-gadget-setup /usr/local/bin/psaudio-mic-fx-run /usr/local/bin/psaudio-route-guard
rm -rf /usr/local/lib/psaudio /opt/psaudio
if [ -n "$HOME_DIR" ]; then
  rm -f "$HOME_DIR"/.config/systemd/user/psaudio-*.service "$HOME_DIR"/.config/systemd/user/default.target.wants/psaudio-*.service \
        "$HOME_DIR/.config/pipewire/pipewire.conf.d/10-psaudio.conf" "$HOME_DIR/.config/pipewire/pipewire.conf.d/20-loopbacks.conf" \
        "$HOME_DIR/.config/wireplumber/wireplumber.conf.d/50-psaudio-policy.conf"
  sed -i '/psaudio-ctl-ssh/d' "$HOME_DIR/.ssh/authorized_keys" 2>/dev/null || true
fi
systemctl daemon-reload 2>/dev/null || true
if [ "$PURGE" = 1 ]; then
  rm -rf /etc/psaudio
  [ -n "$HOME_DIR" ] && rm -rf "$HOME_DIR/sounds"
fi
echo "Removed. The dwc2 line in config.txt was left in place (remove it to give the USB-C port back to normal use)."
