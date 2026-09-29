#!/bin/bash
set -euo pipefail

CC_ALGO="${POS_CC:-}"
GSO="${POS_GSO:-}"
[ -n "$CC_ALGO" ] || CC_ALGO="$(pos_get_variable -r cc 2>/dev/null || echo cubic)"
[ -n "$GSO" ] || GSO="$(pos_get_variable -r gso 2>/dev/null || echo 1)"
[ "${TESTCASE:-}" = goodput ] || exit 127

args=(
  -q
  -d "${WWW%/}"
  "$IP" "$PORT"
  --cc="$CC_ALGO"
  "${CERTS%/}/priv.key"
  "${CERTS%/}/cert.pem"
)
[ "$GSO" = 0 ] && args+=(--no-gso)

exec env SSLKEYLOGFILE=sslkeys.log ./http_server "${args[@]}"
