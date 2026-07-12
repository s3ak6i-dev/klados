#!/usr/bin/env bash
# Install Firecracker + fetch a guest kernel and a minimal rootfs for the spikes.
# Idempotent-ish; safe to re-run. Pins are TODO(host): bump to the latest release you want to test against.
set -euo pipefail

ARCH="$(uname -m)"
FC_VERSION="${FC_VERSION:-v1.7.0}"      # TODO(host): pin to the Firecracker version you are validating
ASSETS_DIR="${ASSETS_DIR:-/opt/klados/assets}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"

mkdir -p "$ASSETS_DIR"

echo "== Firecracker $FC_VERSION ($ARCH) =="
if ! command -v firecracker >/dev/null 2>&1; then
  url="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"
  tmp="$(mktemp -d)"
  curl -fsSL "$url" -o "$tmp/fc.tgz"
  tar -xzf "$tmp/fc.tgz" -C "$tmp"
  install -m 0755 "$tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" "$BIN_DIR/firecracker"
  rm -rf "$tmp"
fi
firecracker --version | head -1

# Guest kernel + rootfs.
# TODO(host): these CI artifacts move around between Firecracker versions. If the URLs 404, grab a
# vmlinux and an ext4 rootfs from the firecracker "getting started" guide for your release, and drop
# them at the paths below. The harness only needs: a bootable vmlinux and a writable ext4 rootfs.
KERNEL="$ASSETS_DIR/vmlinux"
ROOTFS="$ASSETS_DIR/rootfs.ext4"

if [[ ! -f "$KERNEL" ]]; then
  echo "== fetching guest kernel =="
  curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.7/${ARCH}/vmlinux-5.10.bin" -o "$KERNEL" \
    || echo "TODO(host): kernel fetch failed — place a vmlinux at $KERNEL manually"
fi
if [[ ! -f "$ROOTFS" ]]; then
  echo "== fetching rootfs =="
  curl -fsSL "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.7/${ARCH}/ubuntu-22.04.ext4" -o "$ROOTFS" \
    || echo "TODO(host): rootfs fetch failed — place an ext4 rootfs at $ROOTFS manually"
fi

echo
echo "provision done."
echo "  KLADOS_KERNEL=$KERNEL"
echo "  KLADOS_ROOTFS=$ROOTFS"
echo "Export those (or pass --kernel/--rootfs) before running the spikes."
