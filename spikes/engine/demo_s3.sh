#!/usr/bin/env bash
# Demonstrate S3 cold-storage tiering: snapshot -> chunks pushed to S3, delete the LOCAL hot
# cache, then fork -> the engine reconstructs the mem image by pulling chunks back from S3.
set -e
cd "$(dirname "$0")"
export KLADOS_API_KEY=$(cat /var/lib/klados/root.key)

RUN=$(python3 klad run); echo "$RUN"
RID=$(echo "$RUN" | awk '/^run/{print $2}')
IID=$(echo "$RUN" | awk '/^instance/{print $2}')

SNAP=$(python3 klad snapshot "$IID" --label s3-test | awk '/^snapshot/{print $2}')
echo "snapshot $SNAP  (mem chunked to local hot cache + S3 cold tier)"
echo "local hot cache: $(du -sh /var/lib/klados/chunks 2>/dev/null | cut -f1)"

echo "--- deleting the LOCAL hot cache to force a cold restore from S3 ---"
rm -rf /var/lib/klados/chunks

echo "--- fork: reconstruct mem by pulling chunks from S3 ---"
python3 klad fork "$SNAP" -n 2
echo "local hot cache after (re-warmed from S3): $(du -sh /var/lib/klados/chunks 2>/dev/null | cut -f1)"
python3 klad log "$RID"
