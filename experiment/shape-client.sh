#!/bin/bash
# Receiver-side bottleneck. RUNS ON THE CLIENT (Raspberry Pi), needs root.
#
#   sudo ./shape-client.sh up      install the bottleneck
#   sudo ./shape-client.sh down    remove everything
#   sudo ./shape-client.sh show    print qdisc state and measured RTT
#   sudo ./shape-client.sh stats   machine-readable qdisc counters (JSON)
#
# WHY THE BOTTLENECK IS HERE AND NOT ON THE SENDER
# ------------------------------------------------
# A shaper on the sender's egress replaces the QUIC library's own packet
# spacing with the shaper's spacing (every gap collapses to the TBF
# serialization interval, ~0.296 ms at 40 Mbit/s). The sender stays unshaped so
# its egress capture sees the library's pacing; the bottleneck lives on client
# ingress, as in the anchor paper.
#
# PATH
# ----
#   eth0 ingress --(mirred redirect)--> ifb0 root: TBF ---> netem (forward delay)
#   eth0 egress  --> netem (return delay on the ACK path)
#
# TBF first, then netem, matching the anchor artifact. netem is the actual leaf
# queue after it is attached to TBF class 1:1, so its packet limit controls
# backlog and drops. A large netem limit silently turns this into a large-buffer
# experiment even if a byte limit was supplied while creating TBF.

set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

[ "$(id -u)" -eq 0 ] || exp_die "must run as root"

IFB="${IFB:-ifb0}"
HALF_RTT_MS=$(( RTT_MS / 2 ))
RATE="${RATE_MBIT}mbit"

down() {
  tc qdisc del dev "$CLIENT_IFACE" ingress 2>/dev/null || true
  tc qdisc del dev "$CLIENT_IFACE" root    2>/dev/null || true
  tc qdisc del dev "$IFB"          root    2>/dev/null || true
  ip link set "$IFB" down 2>/dev/null || true
  ip link del "$IFB" 2>/dev/null || true
}

up() {
  for key in net.core.rmem_max net.core.rmem_default net.core.wmem_max net.core.wmem_default; do
    sysctl -q -w "$key=$SOCKET_BUFFER_BYTES"
  done
  if ethtool -a "$CLIENT_IFACE" >/dev/null 2>&1; then
    ethtool -A "$CLIENT_IFACE" rx off tx off autoneg off
  fi

  down
  modprobe ifb numifbs=1 2>/dev/null || modprobe ifb 2>/dev/null || true
  ip link show "$IFB" >/dev/null 2>&1 || ip link add "$IFB" type ifb
  ip link set "$IFB" up

  # Redirect all ingress traffic of the measurement interface into ifb0, where
  # it can be shaped like egress traffic.
  tc qdisc add dev "$CLIENT_IFACE" handle ffff: ingress
  if ! tc filter add dev "$CLIENT_IFACE" parent ffff: \
        protocol all matchall action mirred egress redirect dev "$IFB" 2>/dev/null; then
    exp_warn "matchall unavailable, falling back to u32"
    tc filter add dev "$CLIENT_IFACE" parent ffff: \
      protocol all u32 match u32 0 0 action mirred egress redirect dev "$IFB"
  fi

  # Exact anchor hierarchy. `latency` is required by tc when TBF is created,
  # but the attached netem leaf owns the queue and enforces the packet limit.
  tc qdisc add dev "$IFB" root handle 1: tbf \
    rate "$RATE" burst "${TBF_BURST_BYTES}b" latency "${TBF_LATENCY_MS}ms"

  # The artifact sizes this leaf for rate * full RTT / 1392-byte packet. At N1
  # this is 143 packets. It contains both propagation-delay occupancy and the
  # bottleneck backlog.
  tc qdisc add dev "$IFB" parent 1:1 handle 10: netem \
    delay "${HALF_RTT_MS}ms" limit "${BOTTLENECK_LIMIT_PKTS}"

  # Return delay on the ACK path; the artifact leaves its default at 1000.
  tc qdisc add dev "$CLIENT_IFACE" root handle 2: netem \
    delay "${HALF_RTT_MS}ms" limit "${RETURN_NETEM_LIMIT_PKTS}"

  exp_log "bottleneck up: ${RATE}, RTT ${RTT_MS}ms (${HALF_RTT_MS}ms each way), leaf limit ${BOTTLENECK_LIMIT_PKTS} packets (${QUEUE_PACKET_BYTES}B accounting size); socket buffers ${SOCKET_BUFFER_BYTES}B"
}

show() {
  echo "=== ifb0 (bottleneck: TBF then forward netem) ==="
  tc -s -d qdisc show dev "$IFB" || true
  echo
  echo "=== ${CLIENT_IFACE} (ingress redirect + return netem) ==="
  tc -s -d qdisc show dev "$CLIENT_IFACE" || true
  echo
  echo "=== filters ==="
  tc -s filter show dev "$CLIENT_IFACE" parent ffff: || true
  echo
  echo "=== measured RTT to sender (empty queue) ==="
  if ping -c 10 -i 0.2 -q "$SENDER_IP" 2>/dev/null | tail -2; then
    :
  else
    exp_warn "ping to $SENDER_IP failed"
  fi
  echo
  echo "Target minimum RTT: ${RTT_MS} ms. Investigate any deviation above 10%."
}

# Counters for the before/after difference of a run. The bottleneck qdisc is
# selected explicitly by handle, never by position in the output: taking the
# n-th regex match silently reports the wrong counter when the hierarchy
# changes, which is how the artifact's 03_stats.py breaks.
stats() {
  tc -s -j qdisc show dev "$IFB" 2>/dev/null || echo '[]'
}

case "${1:-}" in
  up)    up ;;
  down)  down; exp_log "bottleneck removed" ;;
  show)  show ;;
  stats) stats ;;
  *) echo "usage: $0 {up|down|show|stats}"; exit 1 ;;
esac
