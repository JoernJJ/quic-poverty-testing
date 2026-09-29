#!/bin/bash
set -uo pipefail

CC_ALGO="${POS_CC:-}"
[ -n "$CC_ALGO" ] || CC_ALGO="$(pos_get_variable -r cc 2>/dev/null || echo cubic)"
[ "${TESTCASE:-}" = goodput ] || exit 127

url="${REQUESTS#*://}"
hostport="${url%%/*}"
host="${hostport%%:*}"
port="${hostport##*:}"
path="${url#*/}"

start="$(date +%s%N)"
./picoquicdemo \
  -n "$host" \
  -o "$DOWNLOADS" \
  -G "$CC_ALGO" \
  "$host" "$port" "$path"
rc=$?
end="$(date +%s%N)"
printf '{"start": %s, "end": %s}\n' "$start" "$end" > "${LOGS:-.}/time.json"
exit "$rc"
