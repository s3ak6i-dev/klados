#!/usr/bin/env bash
# Fast repack: re-copy the klados guest files into the already-built browser base (with Chrome)
# and rebuild the ext4 image. Use this to iterate on guest code without re-running debootstrap.
set -euo pipefail
ROOT=/opt/klados/build/browser-root
IMG=/opt/klados/assets/rootfs-browser.ext4
MNT=/mnt/kr-pack
SRC="$(cd "$(dirname "$0")/../guest" && pwd)"
[ -d "$ROOT" ] || { echo "no build root — run build_browser_rootfs.sh first"; exit 1; }

cp "$SRC/guest_agent.py" "$SRC/sitecustomize.py" "$ROOT/klados/"
cp "$SRC/browser_workload.py" "$ROOT/browser_workload.py"
cp "$SRC/init_browser" "$ROOT/init_browser"
chmod +x "$ROOT/init_browser" "$ROOT/browser_workload.py"

rm -f "$IMG"
dd if=/dev/zero of="$IMG" bs=1M count=2600 status=none
mkfs.ext4 -q -F "$IMG"
mkdir -p "$MNT"; mount -o loop "$IMG" "$MNT"
cp -a "$ROOT/." "$MNT/"
sync; umount "$MNT"
chmod a+r "$IMG"
echo "REPACK_DONE"
