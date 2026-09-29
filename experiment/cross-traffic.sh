#!/bin/bash
# Competing TCP flow for the contention arm. RUNS ON THE SENDER.
#
#   ./cross-traffic.sh serve-hint      print the command to run on the client
#   ./cross-traffic.sh start <json>    start one long-lived TCP flow
#   ./cross-traffic.sh stop            stop it and finalize the JSON
#
# The competing flow travels sender -> client, sharing the receiver-side
# drop-tail bottleneck queue with QUIC.
#
# The campaign runner starts the flow CROSS_LEAD_S seconds before QUIC. After a
# successful client/marker pipeline, it waits CROSS_TAIL_S nonnegative seconds
# before stopping TCP; failed runs tear down immediately. These waits bracket
# the transfer, but do not guarantee that the bottleneck queue remains occupied.
# Throughput is evaluated only over the estimated QUIC client window, using the
# per-interval JSON.

set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

PIDFILE="/tmp/quic-pacing-cross.pid"
OUTFILE="/tmp/quic-pacing-cross.out"

case "${1:-}" in
  serve-hint)
    cat <<EOF
Run this on the client (Raspberry Pi) once per session:

    iperf3 --server --daemon --bind $CLIENT_IP

Verify with:  ss -lntp | grep 5201
EOF
    ;;

  start)
    OUT="${2:?output json path}"
    START_NS="${OUT%.json}.start-ns"
    STOP_NS="${OUT%.json}.stop-ns"
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      exp_die "competing flow already running with PID $(cat "$PIDFILE")"
    fi
    rm -f "$PIDFILE" "$OUTFILE" "$START_NS" "$STOP_NS"
    mkdir -p "$(dirname "$OUT")"
    # -B pins the source address to the measurement interface on a multi-homed
    # host. -C sets the sender-side congestion control explicitly. The long
    # duration is interrupted with SIGINT after the runner's configured tail
    # (or on failure), finalizing the interval JSON. Sender timestamps bracket the process;
    # the stop timestamp and final interval end locate iperf's internal
    # relative-time origin.
    (
      date -u +%s%N >"$START_NS"
      exec iperf3 --client "$CLIENT_IP" \
        --bind "$SENDER_IP" \
        --time 86400 \
        --interval 1 \
        --congestion "$IPERF_CC" \
        --forceflush \
        --json --logfile "$OUT"
    ) &
    PID=$!
    echo "$PID" >"$PIDFILE"
    printf '%s\n' "$OUT" >"$OUTFILE"
    for _ in $(seq 1 100); do
      [ -s "$START_NS" ] && break
      sleep 0.01
    done
    if ! [[ "$(cat "$START_NS" 2>/dev/null)" =~ ^[0-9]+$ ]]; then
      kill -TERM "$PID" 2>/dev/null || true
      rm -f "$PIDFILE"
      exp_die "failed to record competing-flow start timestamp"
    fi
    exp_log "competing TCP flow ($IPERF_CC) started -> $OUT"
    ;;

  stop)
    if [ -f "$PIDFILE" ]; then
      PID="$(cat "$PIDFILE")"
      OUT="$(cat "$OUTFILE" 2>/dev/null || true)"
      if [ -n "$OUT" ]; then
        date -u +%s%N >"${OUT%.json}.stop-ns"
      fi
      kill -INT "$PID" 2>/dev/null || true
      for _ in $(seq 1 50); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.1
      done
      kill -TERM "$PID" 2>/dev/null || true
      rm -f "$PIDFILE" "$OUTFILE"
    fi
    exp_log "competing flow stopped"
    ;;

  *)
    echo "usage: $0 {serve-hint|start <json>|stop}"
    exit 1 ;;
esac
