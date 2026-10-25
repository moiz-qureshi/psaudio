#!/bin/bash
# Build a flashable PS Audio Bridge image from the official Raspberry Pi OS Lite (64-bit).
#
#   sudo image/build-image.sh [out_dir]
#
# Needs an arm64 Linux host (GitHub's ubuntu-24.04-arm runner, or a Pi) with
# losetup, parted, e2fsprogs, xz and curl. Output: out_dir/psaudio-<version>-arm64.img.xz
# (+ .sha256). Flash it with Raspberry Pi Imager → "Use custom", then set user,
# Wi-Fi and SSH in Imager's OS customisation.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo "needs an arm64 host (the installer runs in a chroot)" >&2; exit 1; }

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$(mkdir -p "${1:-$ROOT/dist}" && cd "${1:-$ROOT/dist}" && pwd)
VERSION=$(cat "$ROOT/VERSION")
BASE_URL=${BASE_URL:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}
GROW_MB=${GROW_MB:-1536}
WORK=$(mktemp -d)
MNT=$WORK/mnt
LOOP=""

cleanup() {
  set +e
  for m in dev/pts dev proc sys boot/firmware ""; do mountpoint -q "$MNT/$m" && umount -l "$MNT/$m"; done
  [ -n "$LOOP" ] && losetup -d "$LOOP"
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "==> Base image: $BASE_URL"
curl -fL --retry 3 -o "$WORK/base.img.xz" "$BASE_URL"
xz -d -T0 "$WORK/base.img.xz"
IMG=$WORK/base.img

echo "==> Growing by ${GROW_MB} MB for packages"
truncate -s +"${GROW_MB}M" "$IMG"
LOOP=$(losetup -fP --show "$IMG")
parted -s "$LOOP" resizepart 2 100%
e2fsck -fy "${LOOP}p2" >/dev/null || true
resize2fs "${LOOP}p2" >/dev/null

echo "==> Mounting"
mkdir -p "$MNT"
mount "${LOOP}p2" "$MNT"
mount "${LOOP}p1" "$MNT/boot/firmware"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts"
mount -t proc proc "$MNT/proc"
mount -t sysfs sys "$MNT/sys"

# DNS for apt, and no service starts inside the chroot
RESOLV_BAK=$WORK/resolv.conf
cp -a "$MNT/etc/resolv.conf" "$RESOLV_BAK" 2>/dev/null || true
rm -f "$MNT/etc/resolv.conf"; cp /etc/resolv.conf "$MNT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"; chmod +x "$MNT/usr/sbin/policy-rc.d"

echo "==> Installing PS Audio Bridge $VERSION"
rm -rf "$MNT/opt/psaudio"; mkdir -p "$MNT/opt/psaudio"
cp -a "$ROOT"/{bin,lib,config,systemd,pipewire,wireplumber,sounds,tmpfiles,install.sh,uninstall.sh,VERSION,README.md,LICENSE} "$MNT/opt/psaudio/"
chroot "$MNT" /opt/psaudio/install.sh --image
chroot "$MNT" apt-get clean
echo "PS Audio Bridge $VERSION — built $(date -u +%F) from $(basename "$BASE_URL")" > "$MNT/etc/psaudio/image-info"

rm -f "$MNT/usr/sbin/policy-rc.d" "$MNT/etc/resolv.conf"
[ -e "$RESOLV_BAK" ] || [ -L "$RESOLV_BAK" ] && cp -a "$RESOLV_BAK" "$MNT/etc/resolv.conf"
cleanup_mounts() { for m in dev/pts dev proc sys boot/firmware ""; do umount "$MNT/$m"; done; }
sync; cleanup_mounts
e2fsck -fy "${LOOP}p2" >/dev/null || true
losetup -d "$LOOP"; LOOP=""

NAME=psaudio-$VERSION-arm64.img
echo "==> Compressing $NAME.xz"
xz -T0 -6 -c "$IMG" > "$OUT/$NAME.xz"
(cd "$OUT" && sha256sum "$NAME.xz" > "$NAME.xz.sha256")
ls -lh "$OUT/$NAME.xz"
