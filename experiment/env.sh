#!/bin/bash
# Shared configuration for the whole campaign. Source it, never execute it.
#
#   source "$(dirname "$(readlink -f "$0")")/env.sh"
#
# Everything here is overridable from the environment, so a single run can be
# reproduced by exporting the same values recorded in its manifest.
#
# Role assignment:
#   sender   = Ubuntu desktop, Intel I226-V (igc), server + self-capture
#   client   = Raspberry Pi 4, bcmgenet, receiver + ingress bottleneck
#   mirror   = only needed for the 5-run capture calibration, not the campaign

# ---------------------------------------------------------------- repo layout
EXP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$EXP_DIR/.." && pwd)"
ARTIFACT="$PROJECT/quic-pacing-paper-main"
ANALYSIS_OWN="$PROJECT/analysis-own"
RESULTS_ROOT="${RESULTS_ROOT:-$PROJECT/results-own}"
export EXP_DIR PROJECT ARTIFACT ANALYSIS_OWN RESULTS_ROOT

# ------------------------------------------------------- measurement network
# Isolated VLAN on the managed switch. No router in the data path.
export SENDER_IFACE="${SENDER_IFACE:-enp7s0}"
export SENDER_IP="${SENDER_IP:-10.0.0.2}"
export CLIENT_IFACE="${CLIENT_IFACE:-eth0}"
export CLIENT_IP="${CLIENT_IP:-10.0.0.1}"
export PORT="${PORT:-4433}"

# Management path to the client. MUST NOT be the measurement interface:
# the Pi has a single Ethernet port, so use its WiFi for SSH.
export CLIENT_SSH="${CLIENT_SSH:-pi-mgmt}"
export CLIENT_PROJECT="${CLIENT_PROJECT:-/home/pi/pe-paper}"
# Optional override. When empty, run-campaign.sh selects the Pi-local artifact
# or current tree from the arm, preventing a mixed-version sender/client pair.
export CLIENT_IMPL_DIR="${CLIENT_IMPL_DIR:-}"

# ---------------------------------------------------------- operating point
# N1 from the anchor paper: 40 Mbit/s, 40 ms minimum RTT. The artifact's
# effective leaf queue is netem, not TBF: attaching netem below TBF replaces
# TBF's default bfifo. Reproduce the artifact's packet-limit calculation rather
# than setting a TBF byte limit that is no longer operative.
export RATE_MBIT="${RATE_MBIT:-40}"
export RTT_MS="${RTT_MS:-40}"
export QUEUE_PACKET_BYTES="${QUEUE_PACKET_BYTES:-1392}"
export BDP_BYTES="${BDP_BYTES:-$(( RATE_MBIT * 1000000 / 8 * RTT_MS / 1000 ))}"
export BOTTLENECK_LIMIT_PKTS="${BOTTLENECK_LIMIT_PKTS:-$(( BDP_BYTES / QUEUE_PACKET_BYTES ))}"
# The original script uses a one-frame TBF bucket and `latency RTT`; `tc`
# reports this as roughly 1535 B and 40 ms at N1.
export TBF_BURST_BYTES="${TBF_BURST_BYTES:-1540}"
export TBF_LATENCY_MS="${TBF_LATENCY_MS:-$RTT_MS}"
export RETURN_NETEM_LIMIT_PKTS="${RETURN_NETEM_LIMIT_PKTS:-1000}"

# The anchor config raises both default and maximum UDP socket buffers on both
# endpoints to 50 MiB.
export SOCKET_BUFFER_BYTES="${SOCKET_BUFFER_BYTES:-52428800}"

# ----------------------------------------------------------------- transfer
export TESTFILE="${TESTFILE:-file_100MiB.bin}"
export TESTFILE_BYTES="${TESTFILE_BYTES:-104857600}"
export TESTCASE="${TESTCASE:-goodput}"

# ------------------------------------------------------------------ capture
# Snaplen 96 keeps Ethernet/IP/UDP headers; the pcap record still stores the
# original on-wire length, which is what the wire-time bound needs. Full frames
# would cost ~107 MB per run instead of ~9 MB.
export CAP_SNAPLEN="${CAP_SNAPLEN:-96}"
export CAP_BUFFER_KIB="${CAP_BUFFER_KIB:-32768}"
# `adapter_unsynced` is offered by the I226-V but yielded zero egress packets.
# `host` captures every egress packet, but timestamps are taken above the NIC.
export CAP_TSTYPE="${CAP_TSTYPE:-host}"

# Application-level UDP GSO is an explicit campaign factor, but this runner
# supports only APP_GSO=0 with non-wire-equivalent sender-host capture.
export APP_GSO="${APP_GSO:-0}"
export CAPTURE_POINT="${CAPTURE_POINT:-sender-host}"
export CAPTURE_WIRE_EQUIVALENT="${CAPTURE_WIRE_EQUIVALENT:-0}"

# The rebuilt artifact retains quiche's original spurious-congestion rollback
# (not the anchor paper's "SF" patch). The state is recorded in every manifest.
export QUICHE_SPURIOUS_ROLLBACK="${QUICHE_SPURIOUS_ROLLBACK:-on}"

# ------------------------------------------------------------ version arms
# Arm A uses the artifact builds, arms B and C the frozen 2026 builds.
# Both trees expose the same run-server.sh / run-client.sh interface.
export IMPL_DIR_ARTIFACT="${IMPL_DIR_ARTIFACT:-$ARTIFACT/quic-implementations}"
export IMPL_DIR_CURRENT="${IMPL_DIR_CURRENT:-$PROJECT/quic-implementations-current}"

# --------------------------------------------------------- cross traffic
export IPERF_CC="${IPERF_CC:-cubic}"
export CROSS_LEAD_S="${CROSS_LEAD_S:-5}"     # start before the QUIC transfer
export CROSS_TAIL_S="${CROSS_TAIL_S:-5}"     # keep running after it

# ------------------------------------------------------------------ helpers
exp_die()  { echo "FATAL: $*" >&2; exit 1; }
exp_warn() { echo "WARN:  $*" >&2; }
exp_log()  { echo "[$(date -u +%H:%M:%S)] $*"; }

# The seven measured configurations: impl:cc:sender_qdisc
# quiche appears twice with CUBIC because it only paces when the sender qdisc
# honours SO_TXTIME timestamps; the default-qdisc cell is the unpaced control.
exp_cells() {
  cat <<'CELLS'
picoquic:cubic:default
picoquic:bbr:default
ngtcp2:cubic:default
ngtcp2:bbr:default
quiche:cubic:default
quiche:cubic:fq
quiche:bbr:fq
CELLS
}
