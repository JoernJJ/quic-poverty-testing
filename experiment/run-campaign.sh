#!/bin/bash
# Campaign driver. RUNS ON THE SENDER.
#
#   ./run-campaign.sh --arm A --scenario n1 --gso 0 --reps 10 [--seed 20260830] [--dry-run]
#
#   arm A   artifact versions, idle link      -> commodity reproduction anchor
#   arm B   current versions,  idle link      -> separate version-drift comparison
#   arm C   current versions + competing flow -> contention extension
#
# Scenario and GSO are mandatory and become part of the output path and run ID.
# This prevents results from different caps, RTTs or batching modes from being
# silently mixed into one campaign.
#
# Design:
#
#   * Blocked randomisation. Each repetition block contains every cell exactly
#     once, in an order derived from a stored seed. Running ten repetitions of
#     one cell back to back would align thermal drift and system state with a
#     single configuration.
#   * The run is the experimental unit. Every run gets its own directory,
#     manifest, counter snapshots and hashes; nothing is aggregated here.
#   * A failed run is recorded, not silently retried. It keeps its directory and
#     status=failed, and a rerun gets a fresh run id.
#   * Validation is per run and mechanical: byte count, client exit status,
#     capture drops, packet count, no frame above the MTU, and for quiche cells
#     an explicit confirmation that pacing was actually enabled. A quiche server
#     that fails to set SO_TXTIME continues unpaced while printing only a debug
#     message.

set -uo pipefail
source "$(dirname "$(readlink -f "$0")")/env.sh"

ARM=""; SCENARIO=""; GSO_ARG=""; REPS=10; SEED=20260830; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --arm)      ARM="${2:?}"; shift 2 ;;
    --scenario) SCENARIO="${2:?}"; shift 2 ;;
    --gso)      GSO_ARG="${2:?}"; shift 2 ;;
    --reps)     REPS="${2:?}"; shift 2 ;;
    --seed)     SEED="${2:?}"; shift 2 ;;
    --dry-run)  DRY=1; shift ;;
    *) exp_die "unknown argument: $1" ;;
  esac
done
case "$ARM" in
  A)
    IMPL_DIR="$IMPL_DIR_ARTIFACT"
    CLIENT_RUN_IMPL_DIR="${CLIENT_IMPL_DIR:-$CLIENT_PROJECT/quic-implementations}"
    CROSS=0
    ;;
  B)
    IMPL_DIR="$IMPL_DIR_CURRENT"
    CLIENT_RUN_IMPL_DIR="${CLIENT_IMPL_DIR:-$CLIENT_PROJECT/quic-implementations-current}"
    CROSS=0
    ;;
  C)
    IMPL_DIR="$IMPL_DIR_CURRENT"
    CLIENT_RUN_IMPL_DIR="${CLIENT_IMPL_DIR:-$CLIENT_PROJECT/quic-implementations-current}"
    CROSS=1
    ;;
  *) exp_die "--arm must be A, B or C" ;;
esac
[[ "$SCENARIO" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || exp_die "--scenario must contain only letters, digits, dot, underscore or hyphen"
case "$GSO_ARG" in
  0|1) APP_GSO="$GSO_ARG" ;;
  *) exp_die "--gso must be explicit: 0 or 1" ;;
esac

CAMPAIGN="$RESULTS_ROOT/$SCENARIO/arm-$ARM-gso$APP_GSO"

GIT_COMMIT="$(git -C "$PROJECT" rev-parse HEAD 2>/dev/null || echo unknown)"
PROJECT_DIRTY=0
PROJECT_PATCH_SHA256=""
PROJECT_SNAPSHOT_ID=""
PROJECT_PATCH_PATH=""
PROJECT_STATUS_PATH=""
if [ "$DRY" -eq 0 ]; then
  mkdir -p "$CAMPAIGN/provenance"
  status_tmp="$(mktemp)"
  patch_tmp="$(mktemp)"
  git -C "$PROJECT" status --porcelain --untracked-files=no >"$status_tmp"
  git -C "$PROJECT" diff --binary HEAD >"$patch_tmp"
  [ -s "$status_tmp" ] && PROJECT_DIRTY=1
  PROJECT_PATCH_SHA256="$(sha256sum "$patch_tmp" | cut -d' ' -f1)"
  PROJECT_SNAPSHOT_ID="${GIT_COMMIT}-${PROJECT_PATCH_SHA256}"
  PROJECT_PATCH_PATH="provenance/${PROJECT_SNAPSHOT_ID}.patch"
  PROJECT_STATUS_PATH="provenance/${PROJECT_SNAPSHOT_ID}.status.txt"
  if [ ! -e "$CAMPAIGN/$PROJECT_PATCH_PATH" ]; then
    mv "$patch_tmp" "$CAMPAIGN/$PROJECT_PATCH_PATH"
  else
    cmp "$patch_tmp" "$CAMPAIGN/$PROJECT_PATCH_PATH"
    rm -f "$patch_tmp"
  fi
  if [ ! -e "$CAMPAIGN/$PROJECT_STATUS_PATH" ]; then
    mv "$status_tmp" "$CAMPAIGN/$PROJECT_STATUS_PATH"
  else
    cmp "$status_tmp" "$CAMPAIGN/$PROJECT_STATUS_PATH"
    rm -f "$status_tmp"
  fi
fi
KERNEL="$(uname -r)"

ssh_client() {
  ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$CLIENT_SSH" \
    "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; $*"
}

# Deterministic shuffle from the stored seed: same seed, same order, forever.
# Done in Python rather than with `shuf --random-source`, because that reads a
# byte stream whose consumption pattern differs between coreutils versions, and
# because it would add openssl as a dependency just to generate the stream.
seeded_shuffle() {
  python3 -c '
import random, sys
cells = [line for line in sys.stdin.read().splitlines() if line.strip()]
random.Random(sys.argv[1]).shuffle(cells)
print("\n".join(cells))
' "$1"
}

# --------------------------------------------------------------- one run
run_one() {
  local rep="$1" base_cell="$2"
  local impl cc qdisc cell base_run_id run_id run_dir
  IFS=: read -r impl cc qdisc <<<"$base_cell"
  cell="${base_cell}:gso${APP_GSO}"
  base_run_id="${SCENARIO}-arm${ARM}-gso${APP_GSO}-${impl}-${cc}-${qdisc}-rep${rep}"
  run_id="$base_run_id"
  run_dir="$CAMPAIGN/$run_id"

  if [ "$DRY" -eq 1 ]; then
    echo "  would run $run_id"
    return 0
  fi
  local server_binary server_binary_sha256
  case "$impl" in
    picoquic) server_binary="$IMPL_DIR/$impl/picoquicdemo" ;;
    ngtcp2)   server_binary="$IMPL_DIR/$impl/http_server" ;;
    quiche)   server_binary="$IMPL_DIR/$impl/quiche/target/x86_64-unknown-linux-gnu/debug/quiche-server" ;;
  esac
  [ -x "$server_binary" ] || exp_die "server binary missing: $server_binary"
  server_binary_sha256="$(sha256sum "$server_binary" | cut -d' ' -f1)"

  local completed
  for completed in "$run_dir" "$CAMPAIGN/${base_run_id}-attempt"*; do
    if [ -d "$completed" ] && grep -q '"status": "ok"' "$completed/manifest.json" 2>/dev/null; then
      exp_warn "$(basename "$completed") already completed successfully, skipping"
      return 0
    fi
  done
  if [ -d "$run_dir" ]; then
    local attempt=2
    while [ -e "$CAMPAIGN/${base_run_id}-attempt${attempt}" ]; do
      attempt=$((attempt + 1))
    done
    run_id="${base_run_id}-attempt${attempt}"
    run_dir="$CAMPAIGN/$run_id"
    exp_warn "$base_run_id is failed or incomplete; preserving it and using $run_id"
  fi

  mkdir -p "$run_dir"
  local status="ok" reason=""
  exp_log "--- $run_id"

  # 1. sender qdisc for this cell. quiche's kernel pacing needs a qdisc that
  #    honours transmission timestamps; fq_codel and mq accept the cmsg and
  #    ignore it. The default FQ horizon of 10 s is preserved on purpose: a
  #    horizon drop means the timestamps are wrong and must not be hidden.
  if [ "$qdisc" = "fq" ]; then
    sudo tc qdisc replace dev "$SENDER_IFACE" root fq
  else
    sudo tc qdisc del dev "$SENDER_IFACE" root 2>/dev/null || true
  fi
  tc -s -j qdisc show dev "$SENDER_IFACE" >"$run_dir/sender-qdisc-before.json"

  # 2. counters before
  ethtool -S "$SENDER_IFACE" >"$run_dir/sender-ethtool-before.txt" 2>/dev/null || true
  cp /proc/net/softnet_stat "$run_dir/sender-softnet-before.txt" 2>/dev/null || true
  ssh_client "tc -s -j qdisc show dev ifb0" >"$run_dir/client-qdisc-before.json" 2>/dev/null || true
  ssh_client "cat /proc/net/softnet_stat" >"$run_dir/client-softnet-before.txt" 2>/dev/null || true
  ethtool -k "$SENDER_IFACE" >"$run_dir/sender-offloads.txt" 2>/dev/null || true

  # 3. server. Each runner gets its own process group so teardown cannot leave
  # a child holding UDP/4433 for the next randomized cell. quiche needs root
  # for SO_TXTIME; the others do not.
  local server_log="$run_dir/server.log"
  (
    cd "$IMPL_DIR/$impl" || exit 1
    export POS_CC="$cc" POS_GSO="$APP_GSO" IP="$SENDER_IP" PORT="$PORT" SERVERNAME="$SENDER_IP"
    export CERTS="$ARTIFACT/local/certs/" WWW="$ARTIFACT/local/www/" TESTCASE="$TESTCASE"
    export PATH="$HOME/.local/bin:$HOME/bin:$PATH"
    if [ "$impl" = "quiche" ]; then
      exec setsid sudo -E ./run-server.sh
    else
      exec setsid ./run-server.sh
    fi
  ) >"$server_log" 2>&1 &
  local server_pid=$!
  sleep 3
  sudo kill -0 -- "-$server_pid" 2>/dev/null \
    || { status="failed"; reason="server did not start"; }

  # 4. capture, then cross traffic, then the transfer
  local pcap="$run_dir/sender.pcap"
  if [ "$status" = "ok" ]; then
    "$EXP_DIR/capture.sh" start "$pcap" >"$run_dir/capture-start.log" 2>&1 \
      || { status="failed"; reason="capture did not start"; }
  fi

  local cross_json="$run_dir/cross-traffic.json"
  local cross_start_file="${cross_json%.json}.start-ns" cross_stop_file="${cross_json%.json}.stop-ns"
  local cross_start_ns="n/a" cross_stop_ns="n/a"
  if [ "$status" = "ok" ] && [ "$CROSS" -eq 1 ]; then
    # Run until explicit SIGINT after the client/marker pipeline and configured
    # tail; iperf3 then closes a complete JSON document.
    "$EXP_DIR/cross-traffic.sh" start "$cross_json" \
      || { status="failed"; reason="competing flow did not start"; }
    [ "$status" = "ok" ] && sleep "$CROSS_LEAD_S"
  fi

  local t0 t1
  local client_markers="$run_dir/client-transfer-markers.json"
  t0="$(date -u +%s%N)"
  if [ "$status" = "ok" ]; then
    ssh_client "cd '$CLIENT_PROJECT/experiment' && ./client-run.sh '$impl' '$cc' \
                '$CLIENT_RUN_IMPL_DIR' '$CLIENT_PROJECT/results-own/tmp/$run_id'" 2>&1 \
      | python3 "$EXP_DIR/record-client-markers.py" --out "$client_markers" \
      >"$run_dir/client-invoke.log"
    local client_pipeline_status=("${PIPESTATUS[@]}")
    if [ "${client_pipeline_status[0]}" -ne 0 ] || [ "${client_pipeline_status[1]}" -ne 0 ]; then
      status="failed"; reason="client transfer or marker recording failed"
    fi
  fi
  t1="$(date -u +%s%N)"

  # Keep contention active after all client markers arrive, without extending
  # the recorded client/marker pipeline window. Invalid or interrupted waits
  # fail the run but still reach teardown and JSON finalization.
  if [ "$status" = "ok" ] && [ "$CROSS" -eq 1 ]; then
    if ! [[ "$CROSS_TAIL_S" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]]; then
      status="failed"; reason="CROSS_TAIL_S must be a nonnegative number of seconds"
    elif ! sleep "$CROSS_TAIL_S"; then
      status="failed"; reason="competing flow tail wait failed"
    fi
  fi

  # 5. tear down in reverse order. SIGINT lets iperf3 finalize its JSON.
  [ "$CROSS" -eq 1 ] && "$EXP_DIR/cross-traffic.sh" stop >/dev/null 2>&1
  "$EXP_DIR/capture.sh" stop >"$run_dir/capture-stop.log" 2>&1 || true
  # Kill the isolated server process group, including root-owned quiche
  # children, and wait until it is gone.
  sudo kill -TERM -- "-$server_pid" 2>/dev/null || true
  local stop_wait=0
  while sudo kill -0 -- "-$server_pid" 2>/dev/null && [ "$stop_wait" -lt 50 ]; do
    sleep 0.1
    stop_wait=$((stop_wait + 1))
  done
  sudo kill -KILL -- "-$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true

  # Snapshot sender FQ after the transfer so horizon drops can be validated.
  tc -s -j qdisc show dev "$SENDER_IFACE" >"$run_dir/sender-qdisc-after.json"
  ethtool -S "$SENDER_IFACE" >"$run_dir/sender-ethtool-after.txt" 2>/dev/null || true
  cp /proc/net/softnet_stat "$run_dir/sender-softnet-after.txt" 2>/dev/null || true
  ssh_client "tc -s -j qdisc show dev ifb0" >"$run_dir/client-qdisc-after.json" 2>/dev/null || true
  ssh_client "cat /proc/net/softnet_stat" >"$run_dir/client-softnet-after.txt" 2>/dev/null || true
  scp -q -r "$CLIENT_SSH:$CLIENT_PROJECT/results-own/tmp/$run_id/." "$run_dir/client/" 2>/dev/null || true
  ssh_client "rm -rf '$CLIENT_PROJECT/results-own/tmp/$run_id'" 2>/dev/null || true

  # 7. validation
  local cap_drops pkts bytes_got pacing="n/a" horizon_drops=0 bottleneck_drops="unknown"
  cap_drops="$(sed -n 's/^\([0-9][0-9]*\) packet[s]* dropped by kernel$/\1/p' \
                "$run_dir/capture-stop.log" | tail -1)"
  cap_drops="${cap_drops:-unknown}"
  [ "$cap_drops" != "0" ] && \
    { status="failed"; reason="${reason:-capture drop count is $cap_drops}"; }

  bytes_got="$(sed -n 's/^downloaded_bytes=//p' "$run_dir/client/client-result.env" 2>/dev/null)"
  bytes_got="${bytes_got:-0}"
  [ "$bytes_got" != "$TESTFILE_BYTES" ] && \
    { status="failed"; reason="${reason:-short transfer: $bytes_got}"; }

  bottleneck_drops="$(python3 - \
    "$run_dir/client-qdisc-before.json" "$run_dir/client-qdisc-after.json" <<'PY'
import json, sys

def leaf(path):
    with open(path, encoding="utf-8") as handle:
        qdiscs = json.load(handle)
    matches = [
        q for q in qdiscs
        if q.get("kind") == "netem" and q.get("parent") == "1:1"
    ]
    if len(matches) != 1:
        raise SystemExit("expected exactly one bottleneck netem parent 1:1")
    return int(matches[0].get("drops", 0))

before, after = leaf(sys.argv[1]), leaf(sys.argv[2])
if after < before:
    raise SystemExit("bottleneck drop counter moved backwards")
print(after - before)
PY
)" || { status="failed"; reason="${reason:-cannot read bottleneck leaf drop delta}"; }
  [[ "$bottleneck_drops" =~ ^[0-9]+$ ]] \
    || { status="failed"; reason="${reason:-invalid bottleneck drop delta: $bottleneck_drops}"; }

  if [ "$CROSS" -eq 1 ]; then
    cross_start_ns="$(cat "$cross_start_file" 2>/dev/null || echo n/a)"
    cross_stop_ns="$(cat "$cross_stop_file" 2>/dev/null || echo n/a)"
    if ! [[ "$cross_start_ns" =~ ^[0-9]+$ && "$cross_stop_ns" =~ ^[0-9]+$ ]]; then
      status="failed"; reason="${reason:-missing exact cross-traffic boundary timestamps}"
    elif ! python3 "$ANALYSIS_OWN/validate_contention.py" \
        --iperf "$cross_json" \
        --client-time "$run_dir/client/time.json" \
        --client-markers "$client_markers" \
        --cross-start-ns "$cross_start_ns" \
        --cross-stop-ns "$cross_stop_ns" \
        --lead-s "$CROSS_LEAD_S" \
        --out "$run_dir/cross-traffic-check.json"; then
      status="failed"; reason="${reason:-invalid or incomplete cross-traffic coverage}"
    fi
  fi

  # Frames above the MTU would mean the capture saw a pre-segmentation
  # aggregate rather than wire frames. This also produces the per-run
  # *.pcap.json that run_metrics.py consumes.
  if [ -s "$pcap" ]; then
    if python3 "$ANALYSIS_OWN/pcap_reader.py" "$pcap" --sport "$PORT" \
         --max-frame 1514 --min-packets 10000 \
         --out "$run_dir/${run_id}.pcap.json" >"$run_dir/pcap-check.log" 2>&1; then
      pkts="$(sed -n 's/^\([0-9][0-9]*\) packets.*/\1/p' "$run_dir/pcap-check.log" | head -1)"
    else
      status="failed"; reason="${reason:-pcap validation failed, see pcap-check.log}"
    fi
  else
    status="failed"; reason="${reason:-empty pcap}"
  fi

  # quiche must state that pacing is on. An unlabelled fallback is an invalid
  # run, not an unpaced one.
  if [ "$impl" = "quiche" ]; then
    if grep -qi 'successfully set SO_TXTIME' "$server_log"; then
      pacing="enabled"
    elif grep -qi 'setsockopt failed' "$server_log"; then
      pacing="disabled-silently"; status="failed"
      reason="${reason:-quiche fell back to unpaced sending}"
    else
      pacing="unknown"; status="failed"
      reason="${reason:-quiche pacing state not confirmed in the server log}"
    fi
    if [ "$qdisc" = "fq" ]; then
      horizon_drops="$(python3 - \
        "$run_dir/sender-qdisc-before.json" "$run_dir/sender-qdisc-after.json" <<'PY'
import json, sys

def total(value):
    if isinstance(value, dict):
        return sum(
            int(child) if key == "horizon_drops" and isinstance(child, (int, float)) else total(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return sum(total(child) for child in value)
    return 0

def load(path):
    with open(path, encoding="utf-8") as handle:
        return total(json.load(handle))

before, after = load(sys.argv[1]), load(sys.argv[2])
print(after - before if after >= before else after)
PY
)"
      if [ "$horizon_drops" -gt 0 ]; then
        status="failed"; reason="${reason:-FQ horizon drops: $horizon_drops}"
      fi
    fi
  fi

  local client_binary client_binary_sha256
  client_binary="$(sed -n 's/^client_binary=//p' "$run_dir/client/client-result.env" 2>/dev/null)"
  client_binary_sha256="$(sed -n 's/^client_binary_sha256=//p' "$run_dir/client/client-result.env" 2>/dev/null)"
  [ -n "$client_binary_sha256" ] \
    || { status="failed"; reason="${reason:-client binary hash missing}"; }

  # 8. manifest
  {
    echo "run_id=$run_id"
    echo "scenario=$SCENARIO"
    echo "arm=$ARM"
    echo "cell=$cell"
    echo "implementation=$impl"
    echo "congestion_control=$cc"
    echo "sender_qdisc=$qdisc"
    echo "repetition=$rep"
    echo "seed=$SEED"
    echo "status=$status"
    echo "failure_reason=$reason"
    echo "wall_start_ns=$t0"
    echo "wall_end_ns=$t1"
    echo "downloaded_bytes=$bytes_got"
    echo "expected_bytes=$TESTFILE_BYTES"
    echo "captured_packets=${pkts:-0}"
    echo "capture_kernel_drops=$cap_drops"
    echo "bottleneck_drops=$bottleneck_drops"
    echo "quiche_pacing=$pacing"
    echo "quiche_spurious_rollback=$QUICHE_SPURIOUS_ROLLBACK"
    echo "fq_horizon_drops=$horizon_drops"
    echo "cross_traffic=$CROSS"
    echo "cross_traffic_cc=$IPERF_CC"
    # Requested durations; cross_traffic records whether they apply to this run.
    echo "cross_lead_s=$CROSS_LEAD_S"
    echo "cross_tail_s=$CROSS_TAIL_S"
    echo "cross_stop_ns=$cross_stop_ns"
    echo "cross_start_ns=$cross_start_ns"
    echo "sender_ntp_synchronized=$SENDER_NTP_SYNC"
    echo "client_ntp_synchronized=$CLIENT_NTP_SYNC"
    echo "rate_mbit=$RATE_MBIT"
    echo "rtt_ms=$RTT_MS"
    echo "bdp_bytes=$BDP_BYTES"
    echo "queue_packet_bytes=$QUEUE_PACKET_BYTES"
    echo "bottleneck_limit_packets=$BOTTLENECK_LIMIT_PKTS"
    echo "tbf_burst_bytes=$TBF_BURST_BYTES"
    echo "tbf_latency_ms=$TBF_LATENCY_MS"
    echo "socket_buffer_bytes=$SOCKET_BUFFER_BYTES"
    echo "gso=$APP_GSO"
    echo "sender_kernel=$KERNEL"
    echo "sender_realtime=$SENDER_REALTIME"
    echo "client_kernel=$CLIENT_KERNEL"
    echo "client_realtime=$CLIENT_REALTIME"
    echo "sender_os=$SENDER_OS"
    echo "client_os=$CLIENT_OS"
    echo "sender_iface=$SENDER_IFACE"
    echo "capture_point=$CAPTURE_POINT"
    echo "capture_wire_equivalent=$CAPTURE_WIRE_EQUIVALENT"
    echo "capture_snaplen=$CAP_SNAPLEN"
    echo "capture_tstype=$CAP_TSTYPE"
    echo "implementation_dir=$IMPL_DIR"
    echo "client_implementation_dir=$CLIENT_RUN_IMPL_DIR"
    echo "project_commit=$GIT_COMMIT"
    echo "project_dirty=$PROJECT_DIRTY"
    echo "project_patch_sha256=$PROJECT_PATCH_SHA256"
    echo "project_snapshot_id=$PROJECT_SNAPSHOT_ID"
    echo "project_patch_path=$PROJECT_PATCH_PATH"
    echo "project_status_path=$PROJECT_STATUS_PATH"
    echo "server_binary=$server_binary"
    echo "server_binary_sha256=$server_binary_sha256"
    echo "client_binary=$client_binary"
    echo "client_binary_sha256=$client_binary_sha256"
  } >"$run_dir/manifest.env"
  python3 - "$run_dir/manifest.env" "$run_dir/manifest.json" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
out = {}
for line in open(src, encoding="utf-8"):
    line = line.rstrip("\n")
    if not line or "=" not in line:
        continue
    key, _, value = line.partition("=")
    if value.isdigit():
        value = int(value)
    out[key] = value
json.dump(out, open(dst, "w", encoding="utf-8"), indent=2, sort_keys=True)
PY

  {
    echo "$server_binary_sha256  $server_binary"
    echo "$client_binary_sha256  $client_binary"
  } >"$run_dir/binary-hashes.sha256"

  # 9. hashes, including the binaries that produced the run
  ( cd "$run_dir" && find . -type f ! -name hashes.sha256 -print0 \
      | xargs -0 sha256sum >hashes.sha256 ) 2>/dev/null || true

  if [ "$status" = "ok" ]; then
    exp_log "    ok: ${pkts:-0} packets, $bytes_got bytes"
  else
    FAILED_RUNS=$((FAILED_RUNS + 1))
    exp_warn "    FAILED: $reason"
  fi
}

# ------------------------------------------------------------------ main
FAILED_RUNS=0
SENDER_REALTIME="$(cat /sys/kernel/realtime 2>/dev/null || echo 0)"
SENDER_NTP_SYNC="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown)"
SENDER_OS="$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}")"
CLIENT_KERNEL="unknown"; CLIENT_REALTIME="unknown"; CLIENT_NTP_SYNC="unknown"; CLIENT_OS="unknown"
exp_log "scenario $SCENARIO, arm $ARM, GSO $APP_GSO, $REPS blocked repetitions, seed $SEED, output $CAMPAIGN"
if [ "$DRY" -eq 0 ]; then
  CROSS_TRAFFIC="$CROSS" APP_GSO="$APP_GSO" \
    "$EXP_DIR/preflight.sh" >"$CAMPAIGN/preflight-$(date -u +%Y%m%dT%H%M%SZ).log" 2>&1 \
    || exp_die "preflight failed; see $CAMPAIGN/preflight-*.log"
  CLIENT_KERNEL="$(ssh_client "uname -r")"
  CLIENT_REALTIME="$(ssh_client "cat /sys/kernel/realtime 2>/dev/null || echo 0")"
  CLIENT_NTP_SYNC="$(ssh_client "timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown")"
  CLIENT_OS="$(ssh_client ". /etc/os-release 2>/dev/null; echo \"\${PRETTY_NAME:-unknown}\"")"
fi

for rep in $(seq 1 "$REPS"); do
  exp_log "=== repetition block $rep/$REPS"
  # Seed per block so the order differs between blocks but stays reproducible.
  while read -r cell; do
    cell="${cell%$'\r'}"   # tolerate a CRLF-converted checkout
    [ -n "$cell" ] && run_one "$rep" "$cell"
  done < <(exp_cells | seeded_shuffle "${SEED}-${rep}")
done

exp_log "arm $ARM complete"
grep -h '"status"' "$CAMPAIGN"/*/manifest.json 2>/dev/null | sort | uniq -c || true
[ "$FAILED_RUNS" -eq 0 ] || exp_die "$FAILED_RUNS run(s) failed; inspect their preserved manifests"
echo
echo "Next: python3 $ANALYSIS_OWN/run_metrics.py --data $CAMPAIGN --out $ANALYSIS_OWN/build/${SCENARIO}-arm-${ARM}-gso${APP_GSO}.csv"
