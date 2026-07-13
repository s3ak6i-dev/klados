#!/usr/bin/env bash
# Demonstrate per-fork writable disk + fs-diff:
#   base snapshot (/data has origin.txt) -> fork -> each child writes /data/branch.txt on FORKED
#   -> snapshot a child -> diff shows the added file.
set -e
cd "$(dirname "$0")"

RUN=$(python3 klad run); echo "$RUN"
IID=$(echo "$RUN" | awk '/^instance/{print $2}')

SNAP_A=$(python3 klad snapshot "$IID" --label base | awk '/^snapshot/{print $2}')
echo "base snapshot: $SNAP_A"

FORK=$(python3 klad fork "$SNAP_A" -n 2); echo "$FORK"
CHILD=$(echo "$FORK" | awk 'NR==2{print $1}')

SNAP_B=$(python3 klad snapshot "$CHILD" --label after-fork | awk '/^snapshot/{print $2}')
echo "child snapshot: $SNAP_B"

echo "--- klad diff  (base -> child)  /data layer ---"
python3 klad diff "$SNAP_A" "$SNAP_B"
