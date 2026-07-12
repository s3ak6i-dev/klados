#!/usr/bin/env bash
# Install Firecracker + a guest kernel and writable rootfs, using URLs verified present
# on 2026-07-12. Run as root (e.g. `wsl -d Ubuntu -u root -- bash setup/install.sh`).
# Bump the pins below when the CI bucket rotates (rediscover with setup/probe or curl -I).
set -euo pipefail

ARCH="$(uname -m)"
[ "$ARCH" = "x86_64" ] || { echo "this pinned set is x86_64 only (got $ARCH)"; exit 1; }

FC_VERSION="${FC_VERSION:-v1.16.1}"
KERNEL_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.12/x86_64/vmlinux-6.1.128"
ROOTFS_URL="https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.10/x86_64/ubuntu-22.04.ext4"

ASSETS_DIR="${ASSETS_DIR:-/opt/klados/assets}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
mkdir -p "$ASSETS_DIR"

echo "== firecracker $FC_VERSION =="
tmp="$(mktemp -d)"
curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" -o "$tmp/fc.tgz"
tar -xzf "$tmp/fc.tgz" -C "$tmp"
install -m 0755 "$tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" "$BIN_DIR/firecracker"
rm -rf "$tmp"
"$BIN_DIR/firecracker" --version | head -1

echo "== guest kernel =="
[ -f "$ASSETS_DIR/vmlinux" ] || curl -fsSL "$KERNEL_URL" -o "$ASSETS_DIR/vmlinux"
echo "  $(du -h "$ASSETS_DIR/vmlinux" | cut -f1)  $ASSETS_DIR/vmlinux"

echo "== rootfs (ext4, writable) =="
[ -f "$ASSETS_DIR/rootfs.ext4" ] || curl -fsSL "$ROOTFS_URL" -o "$ASSETS_DIR/rootfs.ext4"
echo "  $(du -h "$ASSETS_DIR/rootfs.ext4" | cut -f1)  $ASSETS_DIR/rootfs.ext4"

# make assets world-readable so the non-root spike user can use them
chmod -R a+rX "$ASSETS_DIR"

echo
echo "install done."
echo "  export KLADOS_KERNEL=$ASSETS_DIR/vmlinux"
echo "  export KLADOS_ROOTFS=$ASSETS_DIR/rootfs.ext4"
