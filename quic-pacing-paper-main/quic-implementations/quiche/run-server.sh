#!/bin/bash

set -x

CC=${POS_CC:-}
GSO=${POS_GSO:-}

# Prefer the explicit campaign environment. Fall back to the POS harness when
# the runner is invoked independently of experiment/run-campaign.sh.
if [ -z "$CC" ]; then
	CC=$(pos_get_variable -r cc 2>/dev/null) || CC="cubic"
fi
if [ -z "$GSO" ]; then
	GSO=$(pos_get_variable -r gso 2>/dev/null) || GSO=1
fi

if [ "$GSO" -eq "0" ]; then
	GSO="--disable-gso"
else
	GSO=""
fi

WWW=${WWW::-1}

if [[ $TESTCASE == "goodput" ]]; then
	cd quiche
	rm -f dump.log loss.log
	BIN=target/x86_64-unknown-linux-gnu/debug/quiche-server
	if [ ! -x "$BIN" ]; then
		echo "missing prebuilt quiche server: $PWD/$BIN" >&2
		exit 127
	fi
	exec env RUST_LOG="info" SSLKEYLOGFILE="sslkeys.log" "$BIN" \
	--cc-algorithm "${CC}" \
	--name "quiche-interop" \
	--listen "${IP}:${PORT}" \
	--root "$WWW" \
	--no-retry \
	--no-grease \
	--cert "$CERTS/cert.pem" \
	--key "$CERTS/priv.key" \
	$GSO
else
    echo "exited with code 127"
    exit 127
fi
