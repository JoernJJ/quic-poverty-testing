#!/bin/bash
# Shared configuration for the server host (Intel I226-V, direct-driver mode).
# Source this from the other server-tools scripts:  source "$(dirname "$0")/env.sh"

# Repo layout
SERVER_TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SERVER_TOOLS/.." && pwd)"                 # quic-pacing-paper-main
IMPL_DIR="$REPO/quic-implementations"
LOCAL="$REPO/local"                                    # runtime data (gitignored)

# Toolchain + pos_get_variable shim on PATH
export PATH="$HOME/bin:$HOME/.cargo/bin:$HOME/.local/go-sdk/go/bin:$PATH"

# Measurement network (VLAN-isolated switch, no Fritzbox in path)
export IFACE="${IFACE:-enp7s0}"
export SERVER_IP="${SERVER_IP:-10.0.0.2}"
export CLIENT_IP="${CLIENT_IP:-10.0.0.1}"
export PORT="${PORT:-4433}"
export SERVERNAME="${SERVERNAME:-server}"

# Runtime paths. Trailing slashes are REQUIRED:
#   - ngtcp2 run-server.sh uses ${CERTS}priv.key  (needs trailing /)
#   - quiche/tcp-tls run-server.sh do WWW=${WWW::-1} (strip one char -> needs trailing /)
export CERTS="$LOCAL/certs/"
export WWW="$LOCAL/www/"
export TESTFILE="${TESTFILE:-file_100MiB.bin}"
export TESTCASE="${TESTCASE:-goodput}"
