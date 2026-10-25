#!/bin/bash
# Full SD-card image of a running Pi, streamed over SSH and compressed here.
# The result is an exact clone (hostname, users, keys, Wi-Fi) — keep it private.
#
#   scripts/backup-image.sh <ssh-host> [out_dir]
#
# It asks for the Pi user's sudo password (sent only to the Pi's sudo, not stored).
# Restore: Raspberry Pi Imager → "Use custom" → the .img.xz, or
#          xz -dc <file>.img.xz | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync
set -euo pipefail
HOST=${1:?usage: backup-image.sh <ssh-host> [out_dir]}
OUT=${2:-backups}
mkdir -p "$OUT"
DISK=${DISK:-/dev/mmcblk0}
FILE=$OUT/psaudio-$HOST-$(date +%Y%m%d)-sd.img.xz

read -rsp "sudo password for $HOST: " PW; echo
SIZE=$(ssh "$HOST" "lsblk -bdno SIZE $DISK")
echo "Imaging $DISK ($((SIZE / 1024 / 1024 / 1024)) GB) from $HOST → $FILE"
echo "Takes 10-30 min. The Pi keeps running; stop playing audio for a clean copy."
ssh "$HOST" "sync; sudo -S -p '' dd if=$DISK bs=4M status=none" <<<"$PW" \
  | { command -v pv >/dev/null && pv -s "$SIZE" || cat; } \
  | xz -T0 -3 > "$FILE"
unset PW
(cd "$OUT" && sha256sum "$(basename "$FILE")" > "$(basename "$FILE").sha256")
echo "Done: $(du -h "$FILE" | cut -f1)"
