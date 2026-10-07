#!/bin/bash
# Pre-campaign checks. RUNS ON THE SENDER. Read-only unless --fix is given.
#
#   ./preflight.sh            check everything, exit non-zero on any failure
#   ./preflight.sh --fix      additionally disable offloads and set the client
#                             receive buffer
#
# Every check here corresponds to a failure that has already happened in this
# project or to a validity requirement from ../REVIEW-AND-PLAN-2026-08-30.md.
# Run it before each session and after any reboot, kernel update or cable move.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

FIX=0
[ "${1:-}" = "--fix" ] && FIX=1

PASS=0; FAIL=0; WARN=0
TMPDIR_PREFLIGHT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_PREFLIGHT"' EXIT
ok()   { printf '  PASS  %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$*"; FAIL=$((FAIL+1)); }
warn() { printf '  WARN  %s\n' "$*"; WARN=$((WARN+1)); }
sec()  { printf '\n== %s\n' "$*"; }

ssh_client() {
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$CLIENT_SSH" \
    "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; $*"
}

check_local_sysctl() {
  local name="$1" value
  value="$(sysctl -n "$name" 2>/dev/null || echo 0)"
  if [ "${value:-0}" -ge "$SOCKET_BUFFER_BYTES" ]; then
    ok "$name = $value"
  elif [ "$FIX" -eq 1 ]; then
    sudo sysctl -w "$name=$SOCKET_BUFFER_BYTES" >/dev/null
    value="$(sysctl -n "$name" 2>/dev/null || echo 0)"
    [ "${value:-0}" -ge "$SOCKET_BUFFER_BYTES" ] \
      && ok "$name raised to $value by --fix" \
      || bad "$name remains '$value' after --fix"
  else
    bad "$name = ${value:-unknown}, need >= $SOCKET_BUFFER_BYTES; rerun with --fix"
  fi
}

check_remote_sysctl() {
  local name="$1" value
  value="$(ssh_client "sysctl -n '$name'" 2>/dev/null || echo 0)"
  if [ "${value:-0}" -ge "$SOCKET_BUFFER_BYTES" ]; then
    ok "$name = $value"
  elif [ "$FIX" -eq 1 ]; then
    ssh_client "sudo -n sysctl -w '$name=$SOCKET_BUFFER_BYTES' >/dev/null" 2>/dev/null || true
    value="$(ssh_client "sysctl -n '$name'" 2>/dev/null || echo 0)"
    [ "${value:-0}" -ge "$SOCKET_BUFFER_BYTES" ] \
      && ok "$name raised to $value by --fix" \
      || bad "$name remains '$value' after --fix"
  else
    bad "$name = ${value:-unknown}, need >= $SOCKET_BUFFER_BYTES; rerun with --fix"
  fi
}

# ------------------------------------------------------------------ sender
sec "Sender link and routing ($SENDER_IFACE)"

SPEED="$(cat "/sys/class/net/$SENDER_IFACE/speed" 2>/dev/null || echo '?')"
[ "$SPEED" = "1000" ] && ok "link speed 1000 Mbit/s" \
  || bad "link speed is '$SPEED', expected 1000 (the wire-time bound assumes 1 Gbit/s)"

# The pilot lost a whole run set because a multi-homed host routed measurement
# traffic over the management NIC.
ROUTE_IF="$(ip -o route get "$CLIENT_IP" 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
[ "$ROUTE_IF" = "$SENDER_IFACE" ] && ok "route to $CLIENT_IP uses $SENDER_IFACE" \
  || bad "route to $CLIENT_IP uses '$ROUTE_IF', not $SENDER_IFACE"

sec "Campaign factors"
case "$APP_GSO" in
  0) ok "application UDP GSO explicitly disabled" ;;
  1) bad "APP_GSO=1 is unsupported by this sender-host capture runner" ;;
  *) bad "APP_GSO must be 0 or 1, got '$APP_GSO'" ;;
esac
[ "$CAPTURE_POINT" = "sender-host" ] \
  && ok "capture point is sender-host" \
  || bad "CAPTURE_POINT must be sender-host; external capture is not integrated"
[ "$CAPTURE_WIRE_EQUIVALENT" = "0" ] \
  && ok "sender-host capture declared non-wire-equivalent" \
  || bad "CAPTURE_WIRE_EQUIVALENT must be 0 for sender-host capture"
[ "$BOTTLENECK_LIMIT_PKTS" -gt 0 ] 2>/dev/null \
  && ok "bottleneck leaf limit computed as $BOTTLENECK_LIMIT_PKTS packets" \
  || bad "invalid bottleneck leaf limit '$BOTTLENECK_LIMIT_PKTS'"

sec "Sender offloads"
if [ "$APP_GSO" = "0" ]; then
  for feat in generic-segmentation-offload tcp-segmentation-offload generic-receive-offload; do
    STATE="$(ethtool -k "$SENDER_IFACE" 2>/dev/null | awk -v f="$feat:" '$1==f {print $2}')"
    if [ "$STATE" = "off" ]; then
      ok "$feat off"
    elif [ "$FIX" -eq 1 ]; then
      case "$feat" in
        generic-segmentation-offload) sudo ethtool -K "$SENDER_IFACE" gso off ;;
        tcp-segmentation-offload)    sudo ethtool -K "$SENDER_IFACE" tso off ;;
        generic-receive-offload)     sudo ethtool -K "$SENDER_IFACE" gro off ;;
      esac
      STATE="$(ethtool -k "$SENDER_IFACE" 2>/dev/null | awk -v f="$feat:" '$1==f {print $2}')"
      [ "$STATE" = "off" ] && ok "$feat disabled by --fix" \
        || bad "$feat remains '$STATE' after --fix"
    else
      bad "$feat is '$STATE'; rerun with --fix"
    fi
  done
else
  warn "sender offload checks skipped because APP_GSO is unsupported"
fi

sec "Sender qdisc (no bottleneck may live here)"
QD="$(tc qdisc show dev "$SENDER_IFACE" | head -1)"
if echo "$QD" | grep -qE '\b(tbf|netem|htb|tbf)\b'; then
  bad "shaping qdisc on the sender: $QD"
  echo "        A sender-side shaper replaces the library's pacing with its own."
else
  ok "no shaping qdisc: $QD"
fi

sec "Sender capture capability"
if ethtool -T "$SENDER_IFACE" 2>/dev/null | grep -q 'hardware-transmit'; then
  ok "NIC advertises hardware transmit timestamping"
else
  warn "NIC does not advertise hardware transmit timestamping"
fi
if sudo tcpdump --list-time-stamp-types -i "$SENDER_IFACE" 2>&1 \
    | awk -v requested="$CAP_TSTYPE" '$1 == requested { found=1 } END { exit !found }'; then
  ok "tcpdump offers timestamp type '$CAP_TSTYPE'"
else
  bad "'$CAP_TSTYPE' not offered; capture requires the exact requested timestamp type"
fi

sec "Sender clock"
# NTP state is provenance. Arm-C markers map remote wall-clock times using
# management delivery samples; residual delay is not exact synchronization.
SENDER_NTP_SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
if [ "$SENDER_NTP_SYNC" = "yes" ]; then
  ok "clock NTP-synchronised"
else
  warn "clock not reported as synchronised"
fi

sec "Sender runtime controls"
# Mainline RT kernels may omit the legacy realtime sysfs attribute.
# Inspect the running kernel's build flags, never an installed package name.
SENDER_RT="$(cat /sys/kernel/realtime 2>/dev/null || {
  uname -v | grep -qw PREEMPT_RT && echo 1 || echo 0
})"
[ "$SENDER_RT" = "1" ] \
  && ok "PREEMPT_RT kernel active" \
  || warn "PREEMPT_RT not active; anchor used 6.1.112-rt30"
GOVERNORS="$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u | tr '\n' ',' | sed 's/,$//')"
[ -n "$GOVERNORS" ] && ok "CPU governor(s): $GOVERNORS" \
  || warn "CPU governor unavailable"
for key in net.core.rmem_max net.core.rmem_default net.core.wmem_max net.core.wmem_default; do
  check_local_sysctl "$key"
done
if PAUSE="$(ethtool -a "$SENDER_IFACE" 2>/dev/null)"; then
  RX_PAUSE="$(printf '%s\n' "$PAUSE" | sed -n 's/^[[:space:]]*RX:[[:space:]]*//p')"
  TX_PAUSE="$(printf '%s\n' "$PAUSE" | sed -n 's/^[[:space:]]*TX:[[:space:]]*//p')"
  if [ "$RX_PAUSE" = "off" ] && [ "$TX_PAUSE" = "off" ]; then
    ok "Ethernet PAUSE RX/TX off"
  elif [ "$FIX" -eq 1 ]; then
    sudo ethtool -A "$SENDER_IFACE" rx off tx off autoneg off 2>/dev/null || true
    PAUSE="$(ethtool -a "$SENDER_IFACE" 2>/dev/null || true)"
    RX_PAUSE="$(printf '%s\n' "$PAUSE" | sed -n 's/^[[:space:]]*RX:[[:space:]]*//p')"
    TX_PAUSE="$(printf '%s\n' "$PAUSE" | sed -n 's/^[[:space:]]*TX:[[:space:]]*//p')"
    [ "$RX_PAUSE" = "off" ] && [ "$TX_PAUSE" = "off" ] \
      && ok "Ethernet PAUSE disabled by --fix" \
      || bad "Ethernet PAUSE remains RX=$RX_PAUSE TX=$TX_PAUSE after --fix"
  else
    bad "Ethernet PAUSE RX=$RX_PAUSE TX=$TX_PAUSE; rerun with --fix"
  fi
else
  warn "NIC does not expose Ethernet PAUSE controls"
fi

sec "Sender payload and certificates"
for arm_dir in "$IMPL_DIR_ARTIFACT" "$IMPL_DIR_CURRENT"; do
  if [ -d "$arm_dir" ]; then ok "implementation tree present: $arm_dir"
  else warn "missing implementation tree: $arm_dir (needed for its arm only)"; fi
done
WWWFILE="$ARTIFACT/local/www/$TESTFILE"
if [ -f "$WWWFILE" ]; then
  SZ="$(stat -c %s "$WWWFILE")"
  [ "$SZ" = "$TESTFILE_BYTES" ] && ok "$TESTFILE is $SZ bytes" \
    || bad "$TESTFILE is $SZ bytes, expected $TESTFILE_BYTES"
else
  bad "missing test payload $WWWFILE"
fi
[ -f "$ARTIFACT/local/certs/cert.pem" ] && ok "certificates present" \
  || bad "missing $ARTIFACT/local/certs/cert.pem"
command -v pos_get_variable >/dev/null && ok "pos_get_variable shim on sender PATH" \
  || bad "pos_get_variable shim missing on sender PATH"

# ------------------------------------------------------------------ client
sec "Client reachability over the management path ($CLIENT_SSH)"
if ssh_client true 2>/dev/null; then
  ok "SSH to $CLIENT_SSH works"

  # Management SSH must not share the measurement interface: interactive
  # traffic on the measured link perturbs exactly what is being measured.
  MGMT_IF="$(ssh_client "ip -o route get \$(echo \$SSH_CONNECTION | cut -d' ' -f1) 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p'" 2>/dev/null || true)"
  if [ -n "$MGMT_IF" ] && [ "$MGMT_IF" = "$CLIENT_IFACE" ]; then
    bad "management SSH reaches the client over $CLIENT_IFACE, the measurement interface"
    echo "        Use the Pi's WiFi for management and keep Ethernet measurement-only."
  elif [ -n "$MGMT_IF" ]; then
    ok "management SSH uses '$MGMT_IF', separate from $CLIENT_IFACE"
  fi

  if [ "${CROSS_TRAFFIC:-0}" = "1" ]; then
    CLIENT_MGMT_HOST="$(ssh -G "$CLIENT_SSH" 2>/dev/null | sed -n 's/^hostname //p' | head -1)"
    MGMT_RTT_MAX_MS="$(ping -n -q -c 5 -W 1 "$CLIENT_MGMT_HOST" 2>/dev/null \
      | sed -n 's|.* = [^/]*/[^/]*/\([^/]*\)/.*|\1|p')"
    if [ -z "$MGMT_RTT_MAX_MS" ]; then
      bad "cannot bound management-path delay for client transfer markers"
    elif python3 -c 'import sys; raise SystemExit(float(sys.argv[1]) > 50)' "$MGMT_RTT_MAX_MS"; then
      ok "client-marker delivery bounded by ${MGMT_RTT_MAX_MS} ms management RTT"
    else
      bad "management RTT ${MGMT_RTT_MAX_MS} ms exceeds 50 ms marker bound"
    fi
  fi

  sec "Client bottleneck"
  IFB_JSON="$TMPDIR_PREFLIGHT/ifb.json"
  CLIENT_QDISC_JSON="$TMPDIR_PREFLIGHT/client-qdisc.json"
  if ssh_client "tc -s -j qdisc show dev ifb0" >"$IFB_JSON" 2>/dev/null \
      && ssh_client "tc -s -j qdisc show dev '$CLIENT_IFACE'" >"$CLIENT_QDISC_JSON" 2>/dev/null; then
    if QDISC_SUMMARY="$("$EXP_DIR/validate_qdisc.py" \
         --ifb "$IFB_JSON" --client "$CLIENT_QDISC_JSON" \
         --rate-mbit "$RATE_MBIT" --rtt-ms "$RTT_MS" \
         --burst-bytes "$TBF_BURST_BYTES" \
         --bottleneck-limit "$BOTTLENECK_LIMIT_PKTS" \
         --return-limit "$RETURN_NETEM_LIMIT_PKTS" 2>&1)"; then
      ok "exact bottleneck hierarchy: $QDISC_SUMMARY"
    else
      bad "bottleneck hierarchy mismatch: $QDISC_SUMMARY"
      echo "        Sync experiment/ to the client and run: sudo ./shape-client.sh up"
    fi
  else
    bad "cannot read client qdiscs; install the bottleneck with shape-client.sh"
  fi

  sec "Client runtime controls"
  CLIENT_NTP_SYNC="$(ssh_client "timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown")"
  if [ "$CLIENT_NTP_SYNC" = "yes" ]; then
    ok "client clock NTP-synchronised"
  else
    warn "client clock not reported as synchronised"
  fi
  CLIENT_RT="$(ssh_client "cat /sys/kernel/realtime 2>/dev/null || {
    uname -v | grep -qw PREEMPT_RT && echo 1 || echo 0
  }")"
  [ "$CLIENT_RT" = "1" ] \
    && ok "PREEMPT_RT kernel active" \
    || warn "PREEMPT_RT not active; anchor used 6.1.112-rt30"
  CLIENT_GOVERNORS="$(ssh_client "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort -u | tr '\\n' ',' | sed 's/,\$//'" 2>/dev/null || true)"
  [ -n "$CLIENT_GOVERNORS" ] && ok "CPU governor(s): $CLIENT_GOVERNORS" \
    || warn "CPU governor unavailable"
  for key in net.core.rmem_max net.core.rmem_default net.core.wmem_max net.core.wmem_default; do
    check_remote_sysctl "$key"
  done
  if CLIENT_PAUSE="$(ssh_client "ethtool -a '$CLIENT_IFACE'" 2>/dev/null)"; then
    CLIENT_RX_PAUSE="$(printf '%s\n' "$CLIENT_PAUSE" | sed -n 's/^[[:space:]]*RX:[[:space:]]*//p')"
    CLIENT_TX_PAUSE="$(printf '%s\n' "$CLIENT_PAUSE" | sed -n 's/^[[:space:]]*TX:[[:space:]]*//p')"
    if [ "$CLIENT_RX_PAUSE" = "off" ] && [ "$CLIENT_TX_PAUSE" = "off" ]; then
      ok "Ethernet PAUSE RX/TX off"
    elif [ "$FIX" -eq 1 ]; then
      ssh_client "sudo -n ethtool -A '$CLIENT_IFACE' rx off tx off autoneg off" 2>/dev/null || true
      CLIENT_PAUSE="$(ssh_client "ethtool -a '$CLIENT_IFACE'" 2>/dev/null || true)"
      CLIENT_RX_PAUSE="$(printf '%s\n' "$CLIENT_PAUSE" | sed -n 's/^[[:space:]]*RX:[[:space:]]*//p')"
      CLIENT_TX_PAUSE="$(printf '%s\n' "$CLIENT_PAUSE" | sed -n 's/^[[:space:]]*TX:[[:space:]]*//p')"
      [ "$CLIENT_RX_PAUSE" = "off" ] && [ "$CLIENT_TX_PAUSE" = "off" ] \
        && ok "Ethernet PAUSE disabled by --fix" \
        || bad "Ethernet PAUSE remains RX=$CLIENT_RX_PAUSE TX=$CLIENT_TX_PAUSE after --fix"
    else
      bad "Ethernet PAUSE RX=$CLIENT_RX_PAUSE TX=$CLIENT_TX_PAUSE; rerun with --fix"
    fi
  else
    warn "NIC does not expose Ethernet PAUSE controls"
  fi

  sec "Client measured RTT with an empty queue"
  RTT="$(ping -c 10 -i 0.2 -q "$CLIENT_IP" 2>/dev/null | sed -n 's#.*= \([0-9.]*\)/.*#\1#p')"
  if [ -n "$RTT" ]; then
    DEV="$(awk -v r="$RTT" -v t="$RTT_MS" 'BEGIN{printf "%.1f", (r-t)/t*100}')"
    ok "minimum RTT ${RTT} ms (target ${RTT_MS} ms, deviation ${DEV}%)"
    awk -v d="$DEV" 'BEGIN{exit (d<-10||d>10)?0:1}' && \
      warn "RTT deviates more than 10% from the target"
  else
    bad "no ping response from $CLIENT_IP"
  fi

  sec "Client tooling"
  ssh_client "command -v pos_get_variable >/dev/null" \
    && ok "pos_get_variable shim on PATH" || bad "pos_get_variable shim missing on the client"
  ssh_client "command -v iperf3 >/dev/null" \
    && ok "iperf3 installed (contention arm)" || warn "iperf3 missing; needed for arm C only"
  ssh_client "ss -lnt 2>/dev/null | grep -q ':5201'" \
    && ok "iperf3 server listening" || warn "iperf3 server not listening; see ./cross-traffic.sh serve-hint"
else
  bad "cannot SSH to $CLIENT_SSH (set CLIENT_SSH, install a key, use the management path)"
fi

# ----------------------------------------------------------------- capture
sec "Capture smoke test"
SMOKE="/tmp/preflight-capture.pcap"
if "$EXP_DIR/capture.sh" start "$SMOKE" >/dev/null 2>&1; then
  # The capture filter is UDP/4433, so ICMP ping cannot prove that packets are
  # actually delivered. A one-byte UDP datagram needs no listener.
  ( printf x >"/dev/udp/$CLIENT_IP/$PORT" ) 2>/dev/null || true
  # Allow libpcap's kernel read timeout to deliver the packet before SIGINT.
  sleep 1.5
  OUT="$("$EXP_DIR/capture.sh" stop 2>&1)"
  CAPTURED="$(echo "$OUT" | sed -n 's/^\([0-9][0-9]*\) packet[s]* captured$/\1/p' | tail -1)"
  DROPS="$(echo "$OUT" | sed -n 's/^\([0-9][0-9]*\) packet[s]* dropped by kernel$/\1/p' | tail -1)"
  if [ "${CAPTURED:-0}" -gt 0 ]; then ok "tcpdump captured $CAPTURED matching UDP packet(s)"
  else bad "tcpdump captured no matching UDP packets with timestamp type '$CAP_TSTYPE'"; fi
  if [ "${DROPS:-unknown}" = "0" ]; then ok "tcpdump reported 0 packets dropped by kernel"
  else bad "tcpdump drop count is '${DROPS:-unknown}'; raise CAP_BUFFER_KIB or inspect capture shutdown"; fi
  sudo rm -f "$SMOKE"
else
  bad "capture.sh start failed"
fi

printf '\n== Summary: %d pass, %d warn, %d fail\n' "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -eq 0 ] || { echo "Do not start a campaign with failures outstanding."; exit 1; }
echo "Preflight clean. Warnings must still be recorded in the paper's validity section."
