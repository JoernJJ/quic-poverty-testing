#!/bin/bash
set -euo pipefail

CC_ALGO="${POS_CC:-}"
GSO="${POS_GSO:-}"
[ -n "$CC_ALGO" ] || CC_ALGO="$(pos_get_variable -r cc 2>/dev/null || echo cubic)"
[ -n "$GSO" ] || GSO="$(pos_get_variable -r gso 2>/dev/null || echo 1)"
[ "${TESTCASE:-}" = goodput ] || exit 127

args=(
  --cc-algorithm "$CC_ALGO"
  --name quiche-interop
  --listen "$IP:$PORT"
  --root "${WWW%/}"
  --no-retry
  --no-grease
  --cert "${CERTS%/}/cert.pem"
  --key "${CERTS%/}/priv.key"
)
[ "$GSO" = 0 ] && args+=(--disable-gso)

exec env RUST_LOG=info,quiche_server=debug SSLKEYLOGFILE=sslkeys.log \
  ./quiche-server "${args[@]}"
