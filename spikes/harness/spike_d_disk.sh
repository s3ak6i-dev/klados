#!/usr/bin/env bash
# Spike D (disk) — prove the "forks are free" DISK economics via overlayfs (PRD §5.1/§3.3):
# one shared read-only base + a fresh empty upper per fork; per-fork cost = written bytes only,
# fork-time cost = 0 (empty upper). The disk analogue of Spike B's memory-sharing result.
#
# Run as root (needs mount): wsl -d Ubuntu -u root -- bash spike_d_disk.sh [N] [BASE_MB]
set -euo pipefail

N=${1:-16}
BASEMB=${2:-100}
ROOT=$(mktemp -d /tmp/klados-disk-XXXXXX)
base="$ROOT/base"; mkdir -p "$base"

# read-only base image content, shared by every fork
dd if=/dev/urandom of="$base/blob" bs=1M count="$BASEMB" status=none
base_kib=$(du -sk "$base" | cut -f1)

mounts=()
cleanup() { for m in "${mounts[@]:-}"; do umount "$m" 2>/dev/null || true; done; rm -rf "$ROOT"; }
trap cleanup EXIT

total_upper=0
empty_upper_kib=0
for i in $(seq 1 "$N"); do
  up="$ROOT/up_$i"; wk="$ROOT/wk_$i"; mp="$ROOT/mnt_$i"
  mkdir -p "$up" "$wk" "$mp"
  # fork = create fresh empty upper + overlay mount. Zero copy of the base.
  mount -t overlay overlay -o lowerdir="$base",upperdir="$up",workdir="$wk" "$mp"
  mounts+=("$mp")
  empty_upper_kib=$((empty_upper_kib + $(du -sk "$up" | cut -f1)))   # cost at fork time (pre-write)
  # each fork writes a unique amount (i MiB) -> divergence only
  dd if=/dev/urandom of="$mp/fork_data" bs=1M count="$i" status=none
  sync
done

for i in $(seq 1 "$N"); do
  total_upper=$((total_upper + $(du -sk "$ROOT/up_$i" | cut -f1)))
done

naive=$((N * base_kib))
actual=$((base_kib + total_upper))
ratio=$(awk "BEGIN{printf \"%.3f\", $actual/$naive}")

echo "forks                 = $N"
echo "base_kib (shared once)= $base_kib"
echo "upper_at_fork_kib_sum = $empty_upper_kib   (disk cost of forking, pre-write)"
echo "sum_upper_kib (final) = $total_upper   (= 1+2+..+$N MiB of divergence)"
echo "naive_Nxbase_kib      = $naive"
echo "actual_kib            = $actual"
echo "savings_ratio         = $ratio   [want << 1.0]"
[ "$empty_upper_kib" -lt 100 ] && echo "VERDICT: fork-time disk cost ~0 (empty uppers) — disk CoW confirmed" || echo "VERDICT: unexpected fork-time cost"
