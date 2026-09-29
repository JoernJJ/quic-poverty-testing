#!/bin/bash
# Build quiche, picoquic, ngtcp2 on the server (x86_64, root). Each isolated.
BASE=/root/pe/quic-implementations
LOGD=/root/pe/build
mkdir -p "$LOGD"
export DEBIAN_FRONTEND=noninteractive
echo "==== BUILD START $(date) ===="

########## picoquic (C / cmake) ##########
{
  echo "=== picoquic $(date) ==="
  cd "$BASE/picoquic" || exit 1
  rm -rf picotls picoquic
  git clone https://github.com/h2o/picotls.git picotls \
    && ( cd picotls && git checkout -q 33a52bbbaf97d4343eb4c346cd065595939b0238 \
         && git submodule update --init && cmake . && make -j"$(nproc)" ) \
    && git clone https://github.com/private-octopus/picoquic.git picoquic \
    && ( cd picoquic && git checkout -q ec4b9263e89b9a9fe3af38885565f5b10c948e3b \
         && cmake . && make -j"$(nproc)" ) \
    && cp picoquic/picoquicdemo . \
    && echo "RESULT: PICOQUIC_OK" || echo "RESULT: PICOQUIC_FAIL"
} > "$LOGD/picoquic.log" 2>&1

########## quiche (Rust nightly + build-std) ##########
{
  echo "=== quiche $(date) ==="
  cd "$BASE/quiche" || exit 1
  rm -rf quiche
  ./setup-env.sh
} > "$LOGD/quiche.log" 2>&1
if find "$BASE/quiche" -type f -name quiche-server | grep -q .; then
  find "$BASE/quiche" -type f \( -name quiche-server -o -name quiche-client \) -exec cp {} "$BASE/quiche/" \;
  echo "RESULT: QUICHE_OK" >> "$LOGD/quiche.log"
else
  echo "RESULT: QUICHE_FAIL" >> "$LOGD/quiche.log"
fi

########## ngtcp2 (boringssl+Go, nghttp3, ngtcp2) ##########
{
  echo "=== ngtcp2 $(date) ==="
  cd "$BASE/ngtcp2" || exit 1
  rm -rf boringssl nghttp3 ngtcp2 build go.tgz
  ./setup-env.sh
} > "$LOGD/ngtcp2.log" 2>&1
if [ -f "$BASE/ngtcp2/build/examples/bsslserver" ]; then
  cp "$BASE/ngtcp2/build/examples/bsslserver" "$BASE/ngtcp2/http_server"
  cp "$BASE/ngtcp2/build/examples/bsslclient" "$BASE/ngtcp2/http_client"
  echo "RESULT: NGTCP2_OK" >> "$LOGD/ngtcp2.log"
else
  echo "RESULT: NGTCP2_FAIL" >> "$LOGD/ngtcp2.log"
fi

echo "==== BUILD END $(date) ===="
grep -h "^RESULT:" "$LOGD"/*.log
touch "$LOGD/ALL_DONE"
