#!/usr/bin/env bash
# Run some instances, let the metering sampler accrue, then show per-project usage + cost.
set -e
cd "$(dirname "$0")"
export KLADOS_API_KEY=$(cat /var/lib/klados/root.key)

RUN=$(python3 klad run); echo "$RUN"
IID=$(echo "$RUN" | awk '/^instance/{print $2}')
SNAP=$(python3 klad snapshot "$IID" --label metered | awk '/^snapshot/{print $2}')
python3 klad fork "$SNAP" -n 3 >/dev/null
echo "5 instances running; sampling usage for 10s…"
sleep 10
echo "== klad usage =="
python3 klad usage
