#!/usr/bin/env bash
# Build a bootable browser-capable rootfs from scratch (the CI rootfs is too minimal for apt).
# debootstrap a jammy base, install headless Chrome + python3, wire in the klados guest files,
# and pack it into an ext4 image. Run as root. Produces /opt/klados/assets/rootfs-browser.ext4.
set -euo pipefail

ROOT=/opt/klados/build/browser-root
IMG=/opt/klados/assets/rootfs-browser.ext4
MNT=/mnt/kr-pack
SRC="$(cd "$(dirname "$0")/../guest" && pwd)"

command -v debootstrap >/dev/null 2>&1 || { apt-get update -y && apt-get install -y debootstrap; }

echo "== debootstrap jammy base =="
rm -rf "$ROOT"; mkdir -p "$ROOT"
debootstrap --variant=minbase --include=python3,ca-certificates,curl \
    jammy "$ROOT" http://archive.ubuntu.com/ubuntu

echo "== install headless Chrome in the chroot =="
mount --bind /dev "$ROOT/dev"; mount --bind /proc "$ROOT/proc"; mount --bind /sys "$ROOT/sys"
cp /etc/resolv.conf "$ROOT/etc/resolv.conf"
chroot "$ROOT" bash -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y --no-install-recommends /tmp/chrome.deb
  google-chrome --version
  rm -f /tmp/chrome.deb; apt-get clean
'
umount "$ROOT/dev" "$ROOT/proc" "$ROOT/sys"

echo "== wire in klados guest files =="
mkdir -p "$ROOT/klados"
cp "$SRC/guest_agent.py" "$SRC/sitecustomize.py" "$ROOT/klados/"
cp "$SRC/browser_workload.py" "$ROOT/browser_workload.py"
cp "$SRC/init_browser" "$ROOT/init_browser"
chmod +x "$ROOT/init_browser" "$ROOT/browser_workload.py"

echo "== pack into ext4 image =="
rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count=2600 status=none
mkfs.ext4 -q -F "$IMG"
mkdir -p "$MNT"; mount -o loop "$IMG" "$MNT"
cp -a "$ROOT/." "$MNT/"
sync; umount "$MNT"
chmod a+r "$IMG"
du -h "$IMG"
echo "BUILD_DONE -> $IMG"
