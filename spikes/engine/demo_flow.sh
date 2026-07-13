#!/usr/bin/env bash
# Drive the M0 product flow through the klad CLI: run -> snapshot -> fork -> log.
set -e
cd "$(dirname "$0")"

RUN=$(python3 klad run)
echo "$RUN"
RID=$(echo "$RUN" | awk '/^run/{print $2}')
IID=$(echo "$RUN" | awk '/^instance/{print $2}')

echo "--- snapshot ---"
SNAP=$(python3 klad snapshot "$IID" --label before-fork)
echo "$SNAP"
SID=$(echo "$SNAP" | awk '/^snapshot/{print $2}')

echo "--- fork x4 ---"
python3 klad fork "$SID" -n 4

echo "--- klad log (timeline DAG) ---"
python3 klad log "$RID"
