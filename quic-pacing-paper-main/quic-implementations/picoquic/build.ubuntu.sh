#!/bin/bash
# Ubuntu / non-root adaptation of build.sh.
# Differences vs. upstream build.sh:
#   - Removes the in-script `apt update && apt install` (needs root; deps already
#     installed system-wide: cmake pkg-config build-essential openssl libssl-dev).
#   - Drops the artifact.zip packaging step (not needed for direct-driver).
#   - Same pinned commits as upstream so the binary matches the paper.
set -ex

cd "$(dirname "$(readlink -f "$0")")"   # -> quic-implementations/picoquic

PICOQUIC_VERSION=ec4b9263e89b9a9fe3af38885565f5b10c948e3b
PICOQUIC_REPO=https://github.com/private-octopus/picoquic.git
PICOTLS_VERSION=33a52bbbaf97d4343eb4c346cd065595939b0238
PICOTLS_REPO=https://github.com/h2o/picotls.git

# picotls
if [ ! -d picotls ]; then
    git clone "$PICOTLS_REPO" picotls
    ( cd picotls && git checkout "$PICOTLS_VERSION" && git submodule update --init )
fi
( cd picotls && cmake . && make -j"$(nproc)" )

# picoquic
if [ ! -d picoquic ]; then
    git clone "$PICOQUIC_REPO" picoquic
    ( cd picoquic && git checkout "$PICOQUIC_VERSION" )
fi
( cd picoquic && cmake . && make -j"$(nproc)" )

cp picoquic/picoquicdemo .
echo "=== picoquic build complete ==="
ls -la picoquicdemo
