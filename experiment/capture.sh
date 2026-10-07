#!/bin/bash
# Sender-side egress capture. RUNS ON THE SENDER, needs sudo.
#
#   ./capture.sh start <outfile>   begin capturing, block until tcpdump is ready
#   ./capture.sh stop              SIGINT, flush, and report kernel drops
#
# Differences from the artifact's server-tools/capture.sh, all of which matter:
#   * snaplen 96 instead of full frames: 9 MB instead of 107 MB per run, while
#     the pcap record still carries the original on-wire length that the
#     wire-time bound needs;
#   * a large kernel buffer instead of -U packet-buffered writes, so the capture
#     does not drop packets under load;
#   * the requested timestamp type is verified rather than assumed, because
#     generic hardware transmit timestamping is not available on every NIC and
#     libpcap falls back silently;
#   * stop sends SIGINT and waits, so tcpdump prints its "packets dropped by
#     kernel" statistics, which are a per-run validity gate;
#   * readiness is awaited, so no transfer starts before the capture is live.

set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

PIDFILE="/tmp/quic-pacing-capture.pid"
LOGFILE="/tmp/quic-pacing-capture.log"

# Require an exact offered timestamp mode. Never let libpcap's default silently
# replace the mode recorded for the run.
tstype_available() {
  sudo tcpdump --list-time-stamp-types -i "$SENDER_IFACE" 2>&1 \
    | awk -v requested="$CAP_TSTYPE" '$1 == requested { found=1 } END { exit !found }'
}

start() {
  local out="${1:?output pcap path}"
  # This runner always captures on the sender; declarations cannot turn it
  # into an external capture or make pre-segmentation GSO packets wire-equivalent.
  # Validate before removing output or touching the capture log.
  if [ "$APP_GSO" != "0" ]; then
    exp_warn "sender-host capture requires APP_GSO=0, got '$APP_GSO'"
    return 1
  fi
  if [ "$CAPTURE_POINT" != "sender-host" ] || [ "$CAPTURE_WIRE_EQUIVALENT" != "0" ]; then
    exp_warn "capture.sh requires CAPTURE_POINT=sender-host and CAPTURE_WIRE_EQUIVALENT=0"
    return 1
  fi
  if ! tstype_available; then
    exp_warn "timestamp type '$CAP_TSTYPE' not offered by $SENDER_IFACE; refusing capture"
    return 1
  fi

  mkdir -p "$(dirname "$out")"
  rm -f "$out" "$LOGFILE"

  sudo tcpdump -i "$SENDER_IFACE" \
    -j "$CAP_TSTYPE" \
    --time-stamp-precision=nano \
    -s "$CAP_SNAPLEN" \
    -B "$CAP_BUFFER_KIB" \
    -n \
    -w "$out" \
    "udp port $PORT" >>"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"

  # The pcap header can exist before tcpdump has attached its packet socket.
  # Wait for tcpdump's explicit readiness line and fail if the process exits.
  local waited=0 pid
  pid="$(cat "$PIDFILE")"
  while ! grep -qi 'listening on' "$LOGFILE" && [ "$waited" -lt 100 ]; do
    if ! sudo kill -0 "$pid" 2>/dev/null; then
      cat "$LOGFILE" >&2
      rm -f "$PIDFILE"
      return 1
    fi
    sleep 0.05
    waited=$(( waited + 1 ))
  done
  if ! grep -qi 'listening on' "$LOGFILE"; then
    exp_warn "tcpdump did not become ready"
    sudo kill -INT "$pid" 2>/dev/null || true
    rm -f "$PIDFILE"
    return 1
  fi
  exp_log "capturing -> $out"
}

stop() {
  [ -f "$PIDFILE" ] || { exp_warn "no capture running"; return 0; }
  local pid
  pid="$(cat "$PIDFILE")"
  # SIGINT, not SIGKILL: tcpdump must flush the pcap and print its statistics.
  sudo kill -INT "$pid" 2>/dev/null || true
  local waited=0
  while sudo kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 100 ]; do
    sleep 0.1
    waited=$(( waited + 1 ))
  done
  sudo kill -0 "$pid" 2>/dev/null && { exp_warn "tcpdump did not exit, forcing"; sudo kill -9 "$pid"; }
  rm -f "$PIDFILE"

  # Echo the statistics so the caller can gate on them.
  grep -Ei 'packets (captured|received by filter|dropped)' "$LOGFILE" || true
  cat "$LOGFILE"
}

case "${1:-}" in
  start) shift; start "$@" ;;
  stop)  stop ;;
  *) echo "usage: $0 {start <outfile>|stop}"; exit 1 ;;
esac
