#!/usr/bin/env bash
# Verify control-plane auth: unauthenticated -> 401, root key works, second project is isolated.
set -e
cd "$(dirname "$0")"
API=http://127.0.0.1:7070
ROOT_KEY=$(cat /var/lib/klados/root.key)

echo "== 1. no API key -> 401 =="
curl -s -o /dev/null -w "  GET /v1/runs  -> HTTP %{http_code}\n" "$API/v1/runs"

echo "== 2. root key -> create a run, list it =="
export KLADOS_API_KEY="$ROOT_KEY"
RUN=$(python3 klad run); echo "$RUN"
RID=$(echo "$RUN" | awk '/^run/{print $2}')
echo "  root project runs:"; python3 klad runs | sed 's/^/    /'

echo "== 3. second project is isolated =="
NEWKEY=$(curl -s -X POST -H "X-Api-Key: $ROOT_KEY" -H "Content-Type: application/json" \
         -d '{"name":"team-b"}' "$API/v1/projects" \
         | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')
echo "  created project team-b (its own key)"
echo "  team-b runs (should be empty):"
KLADOS_API_KEY="$NEWKEY" python3 klad runs | sed 's/^/    /'
CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "X-Api-Key: $NEWKEY" "$API/v1/runs/$RID/timeline")
echo "  team-b reading root's run timeline -> HTTP $CODE  (expect 403)"
