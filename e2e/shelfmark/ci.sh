#!/usr/bin/env bash
# Starts Shelfmark, the fake Usenet stack and Chaptarr in Docker, then runs run.py against them.
#
# Usage: e2e/shelfmark/ci.sh [chaptarr-image]
#   CHAPTARR_IMAGE   image to test (default ghcr.io/pace-1/chaptarr:develop, or the first argument)
#   SHELFMARK_IMAGE  Shelfmark image (default the pinned release below)
#   PYTHON_IMAGE     image that runs fake_usenet.py (default python:3.12-alpine)
#   E2E_WORK         scratch directory for volumes (default a new temp dir)
#   E2E_EXTRA_ARGS   extra docker run arguments for Chaptarr (e.g. a CA bundle behind a proxy)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
CHAPTARR_IMAGE="${1:-${CHAPTARR_IMAGE:-ghcr.io/pace-1/chaptarr:develop}}"
SHELFMARK_IMAGE="${SHELFMARK_IMAGE:-ghcr.io/calibrain/shelfmark:v1.5.1}"
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-alpine}"
work="${E2E_WORK:-$(mktemp -d)}"
net=chaptarr-e2e

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for c in e2e-chaptarr e2e-shelfmark e2e-fake-usenet; do
      echo "::group::logs: $c"
      docker logs --tail 300 "$c" 2>&1 || true
      echo "::endgroup::"
    done
  fi
  docker rm -f e2e-chaptarr e2e-shelfmark e2e-fake-usenet >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

mkdir -p "$work"/{downloads,books,audiobooks,shelfmark-config,chaptarr-config,library}
# The containers run as different users; the rig only needs them to share files.
chmod -R 777 "$work"

docker network create "$net" >/dev/null

docker run -d --name e2e-fake-usenet --network "$net" -p 9000:9000 \
  -v "$here:/app:ro" -v "$work/downloads:/downloads" \
  --user 0 --entrypoint python3 "$PYTHON_IMAGE" /app/fake_usenet.py >/dev/null

docker run -d --name e2e-shelfmark --network "$net" -p 8084:8084 \
  -e SHELFMARK_API_KEY=e2e-admin-key -e AUTH_METHOD=builtin \
  -e NEWZNAB_ENABLED=true -e NEWZNAB_URL=http://e2e-fake-usenet:9000/newznab -e NEWZNAB_API_KEY=fake-key \
  -e PROWLARR_USENET_CLIENT=sabnzbd -e SABNZBD_URL=http://e2e-fake-usenet:9000/sab -e SABNZBD_API_KEY=fake-key \
  -v "$work/shelfmark-config:/config" -v "$work/downloads:/downloads" \
  -v "$work/books:/books" -v "$work/audiobooks:/audiobooks" \
  "$SHELFMARK_IMAGE" >/dev/null

# shellcheck disable=SC2086
docker run -d --name e2e-chaptarr --network "$net" -p 8789:8789 ${E2E_EXTRA_ARGS:-} \
  -v "$work/chaptarr-config:/config" -v "$work/books:/books" -v "$work/library:/library" \
  "$CHAPTARR_IMAGE" >/dev/null

wait_http() {
  for _ in $(seq 90); do
    if curl -sf -o /dev/null "$@"; then return 0; fi
    sleep 2
  done
  echo "timed out waiting for $*" >&2
  return 1
}

wait_http http://localhost:9000/health
wait_http -H "X-Api-Key: e2e-admin-key" http://localhost:8084/api/status

key=""
for _ in $(seq 90); do
  key="$(sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' "$work/chaptarr-config/config.xml" 2>/dev/null || true)"
  [ -n "$key" ] && break
  sleep 2
done
[ -n "$key" ] || { echo "Chaptarr never wrote an API key" >&2; exit 1; }
wait_http -H "X-Api-Key: $key" http://localhost:8789/api/v1/system/status

CHAPTARR_API_KEY="$key" SHELFMARK_HOST=e2e-shelfmark python3 "$here/run.py"
