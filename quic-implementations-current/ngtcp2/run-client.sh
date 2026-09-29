#!/bin/bash
set -uo pipefail

CC_ALGO="${POS_CC:-}"
[ -n "$CC_ALGO" ] || CC_ALGO="$(pos_get_variable -r cc 2>/dev/null || echo cubic)"
[ "${TESTCASE:-}" = goodput ] || exit 127

url="${REQUESTS#*://}"
hostport="${url%%/*}"
host="${hostport%%:*}"
port="${hostport##*:}"

start="$(date +%s%N)"
./http_client \
  -q --download "$DOWNLOADS" \
  --no-quic-dump --no-http-dump \
  --exit-on-all-streams-close \
  --cc="$CC_ALGO" \
  "$host" "$port" "$REQUESTS"
rc=$?
end="$(date +%s%N)"
printf '{"start": %s, "end": %s}\n' "$start" "$end" > "${LOGS:-.}/time.json"
exit "$rc"
