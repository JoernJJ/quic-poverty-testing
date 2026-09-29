#!/bin/bash
# One client transfer. RUNS ON THE CLIENT (Raspberry Pi), invoked over SSH by
# run-campaign.sh on the management path.
#
#   ./client-run.sh <impl> <cc> <impl_dir> <run_dir>
#
# It drives the artifact's unmodified run-client.sh through the same environment
# the POS harness would provide, so the client side of a run is byte-for-byte
# the upstream code path. On the Pi, the artifact-era trees are built with
# ../reproduction/build-pi-aarch64.sh.
#
# Contract: exit 0 only if the client exited 0 AND the downloaded file has
# exactly TESTFILE_BYTES bytes. Every other outcome is a failed run, never a
# quietly short transfer.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

IMPL="${1:?impl: quiche|picoquic|ngtcp2}"
CC="${2:?cc: cubic|bbr|reno}"
IMPL_DIR="${3:?implementation tree}"
RUN_DIR="${4:?run output directory}"

mkdir -p "$RUN_DIR/downloads" "$RUN_DIR/logs"

# The shim maps `pos_get_variable -r cc` to POS_CC. POS_ prefixes keep the CCA
# name out of the C compiler's CC variable, which cargo would otherwise read.
export POS_CC="$CC"
export TESTCASE
export REQUESTS="https://${SENDER_IP}:${PORT}/${TESTFILE}"
export DOWNLOADS="$RUN_DIR/downloads"
export LOGS="$RUN_DIR/logs"

command -v pos_get_variable >/dev/null \
  || { echo "pos_get_variable shim not on PATH" >&2; exit 2; }
[ -d "$IMPL_DIR/$IMPL" ] || { echo "no such implementation: $IMPL_DIR/$IMPL" >&2; exit 2; }

case "$IMPL" in
  picoquic) CLIENT_BINARY="$IMPL_DIR/$IMPL/picoquicdemo" ;;
  ngtcp2)   CLIENT_BINARY="$IMPL_DIR/$IMPL/http_client" ;;
  quiche)
    CLIENT_BINARY=""
    for candidate in "$IMPL_DIR/$IMPL/quiche-client" \
      "$IMPL_DIR/$IMPL/quiche"/target/*/debug/quiche-client; do
      if [ -x "$candidate" ]; then CLIENT_BINARY="$candidate"; break; fi
    done
    ;;
  *) echo "unsupported implementation: $IMPL" >&2; exit 2 ;;
esac
[ -x "$CLIENT_BINARY" ] || { echo "client binary missing for $IMPL" >&2; exit 2; }
CLIENT_BINARY_SHA256="$(sha256sum "$CLIENT_BINARY" | cut -d' ' -f1)"

emit_markers() {
  local kind="$1" index timestamp
  for index in 1 2 3 4 5; do
    timestamp="$(date -u +%s%N)"
    printf '%s:%s:%s%4096s\n' "$kind" "$index" "$timestamp" ''
  done
}

cd "$IMPL_DIR/$IMPL"
emit_markers QUIC_CLIENT_TRANSFER_START
./run-client.sh >"$RUN_DIR/logs/client.log" 2>&1
CLIENT_RC=$?
emit_markers QUIC_CLIENT_TRANSFER_END

# Locate the downloaded object regardless of the name each client chooses.
GOT="$(find "$DOWNLOADS" -type f -printf '%s\n' 2>/dev/null | sort -n | tail -1)"
GOT="${GOT:-0}"

{
  echo "client_rc=$CLIENT_RC"
  echo "downloaded_bytes=$GOT"
  echo "expected_bytes=$TESTFILE_BYTES"
  echo "client_binary=$CLIENT_BINARY"
  echo "client_binary_sha256=$CLIENT_BINARY_SHA256"
} >"$RUN_DIR/client-result.env"

# The upstream run-client.sh writes logs/time.json with start/end in ns.
if [ -f "$RUN_DIR/logs/time.json" ]; then
  cp "$RUN_DIR/logs/time.json" "$RUN_DIR/time.json"
fi

if [ "$CLIENT_RC" -ne 0 ]; then
  echo "client exited $CLIENT_RC" >&2
  exit "$CLIENT_RC"
fi
if [ "$GOT" != "$TESTFILE_BYTES" ]; then
  echo "short transfer: got $GOT, expected $TESTFILE_BYTES" >&2
  exit 3
fi
exit 0
