#!/bin/bash
set -uo pipefail

CC_ALGO="${POS_CC:-}"
[ -n "$CC_ALGO" ] || CC_ALGO="$(pos_get_variable -r cc 2>/dev/null || echo cubic)"
[ "${TESTCASE:-}" = goodput ] || exit 127
[ -n "${REQUESTS:-}" ] || exit 0

start="$(date +%s%N)"
env RUST_LOG=info ./quiche-client \
  --no-verify \
  --cc-algorithm "$CC_ALGO" \
  --wire-version 00000001 \
  --dump-responses "$DOWNLOADS" \
  --no-grease \
  "$REQUESTS"
rc=$?
end="$(date +%s%N)"
printf '{"start": %s, "end": %s}\n' "$start" "$end" > "${LOGS:-.}/time.json"
[ "$rc" -ne 101 ] || rc=1
exit "$rc"
