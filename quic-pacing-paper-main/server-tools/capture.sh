#!/bin/bash
# Candidate hardware-timestamped self-capture of server egress.
#
# Use as primary IPG/PTL data for GSO-disabled runs only after matched validation
# against an external switch-mirror capture. Host egress capture may expose GSO
# aggregates before NIC segmentation, so mirror capture is required for GSO-on
# wire-segment timing. See the repository-root DEVICE-PLAN.md and
# METHOD-AND-EXPERIMENT-PLAN.md.
#
# Usage:
#   ./capture.sh start <label> <rep>  -> local/captures/<label>/rep<rep>.pcap
#   ./capture.sh stop
# Needs sudo.
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"
PIDFILE="$LOCAL/captures/.tcpdump.pid"

case "${1:-}" in
  start)
    LABEL="${2:?label}"; REP="${3:?rep number}"
    mkdir -p "$LOCAL/captures/$LABEL"
    OUT="$LOCAL/captures/$LABEL/rep${REP}.pcap"
    sudo tcpdump -i "$IFACE" -j adapter_unsynced --time-stamp-precision=nano -U \
      -w "$OUT" "udp port $PORT" >/dev/null 2>&1 &
    echo $! | sudo tee "$PIDFILE" >/dev/null
    echo "capturing -> $OUT" ;;
  stop)
    if [ -f "$PIDFILE" ]; then
      sudo kill "$(cat "$PIDFILE")" 2>/dev/null || true
      sudo rm -f "$PIDFILE"
    fi
    sudo pkill -f "tcpdump -i $IFACE" 2>/dev/null || true
    echo "capture stopped" ;;
  *)
    echo "usage: $0 {start <label> <rep>|stop}"
    exit 1 ;;
esac
