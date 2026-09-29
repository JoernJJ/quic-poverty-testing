#!/bin/bash
# Launch one implementation's server via its unmodified upstream run-server.sh,
# feeding it the env vars the POS harness would normally provide. The
# pos_get_variable shim (experiment/pos_get_variable, on PATH) maps -r <var>
# to the exported env var.
#
# Usage:  ./start-server.sh <impl> <cc> [gso]
#   impl : quiche | picoquic | ngtcp2 | tcp-tls
#   cc   : cubic | bbr | reno
#   gso  : 1 (default) | 0
#
# Runs in the foreground (Ctrl-C to stop). quiche's run-server.sh expects a
# prebuilt quiche-server.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

IMPL="${1:?impl: quiche|picoquic|ngtcp2|tcp-tls}"
# POS_-prefixed so they reach the impl scripts via the shim WITHOUT polluting
# the C-compiler CC that cargo reads (see experiment/pos_get_variable).
export POS_CC="${2:?cc: cubic|bbr|reno}"
export POS_GSO="${3:-1}"
export IP="$SERVER_IP"

[ -d "$IMPL_DIR/$IMPL" ] || { echo "unknown impl: $IMPL"; exit 1; }
command -v pos_get_variable >/dev/null || { echo "pos_get_variable shim not on PATH"; exit 1; }

echo "==> $IMPL  cc=$POS_CC gso=$POS_GSO  listen=$IP:$PORT  www=$WWW  certs=$CERTS"
cd "$IMPL_DIR/$IMPL"
exec ./run-server.sh
