#!/bin/bash
# Back up a running PS Audio Bridge over SSH: config, scripts, clips, SSH keys.
# No root needed on the Pi.
#
#   scripts/backup.sh <ssh-host> [out_dir]      e.g. scripts/backup.sh psaudio-admin ~/backups/psaudio
#
# Restore onto a fresh install:  tar xzf <backup>.tgz -C / --keep-newer-files   (as root on the Pi)
set -euo pipefail
HOST=${1:?usage: backup.sh <ssh-host> [out_dir]}
OUT=${2:-backups}
mkdir -p "$OUT"
STAMP=$(date +%Y%m%d-%H%M%S)
FILE=$OUT/psaudio-$HOST-$STAMP.tgz

ssh "$HOST" 'set -e
  H=$HOME
  tar czf - --ignore-failed-read \
    /etc/psaudio /etc/tmpfiles.d/psaudio.conf /etc/systemd/system/psaudio-gadget.service \
    /usr/local/bin/psaudio-ctl /usr/local/bin/psaudio-ctl-ssh /usr/local/bin/psaudio-gadget-setup /usr/local/bin/psaudio-mic-fx-run \
    /usr/local/lib/psaudio /boot/firmware/config.txt /boot/firmware/cmdline.txt \
    "$H/.config/pipewire" "$H/.config/wireplumber" "$H/.config/systemd/user" \
    "$H/.ssh/authorized_keys" "$H/sounds" 2>/dev/null' > "$FILE"

echo "Saved $FILE ($(du -h "$FILE" | cut -f1)) — $(tar tzf "$FILE" | grep -c '\.wav$') clips"
