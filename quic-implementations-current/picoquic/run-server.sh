#!/bin/bash
set -euo pipefail

CC_ALGO="${POS_CC:-}"
GSO="${POS_GSO:-}"
[ -n "$CC_ALGO" ] || CC_ALGO="$(pos_get_variable -r cc 2>/dev/null || echo cubic)"
[ -n "$GSO" ] || GSO="$(pos_get_variable -r gso 2>/dev/null || echo 1)"
[ "${TESTCASE:-}" = goodput ] || exit 127

args=(
  -c "${CERTS%/}/cert.pem"
  -k "${CERTS%/}/priv.key"
  -p "$PORT"
  -w "${WWW%/}"
  -G "$CC_ALGO"
  -n "$SERVERNAME"
)
[ "$GSO" = 0 ] && args=(-0 "${args[@]}")

exec ./picoquicdemo "${args[@]}"
