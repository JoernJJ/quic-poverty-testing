#!/bin/bash
# Build ngtcp2 + quiche on the Raspberry Pi (aarch64), all fixes baked in.
BASE=$HOME/pe/quic-implementations
LOGD=$HOME/pe/build
mkdir -p "$LOGD"
RUST_TARGET=aarch64-unknown-linux-gnu
TOOLCHAIN=nightly-2024-11-11-aarch64-unknown-linux-gnu
export CARGO_HOME=$HOME/.cargo RUSTUP_HOME=$HOME/.rustup
echo "==== PI BUILD START $(date) ===="

########## ngtcp2 (aarch64) ##########
{
  echo "=== ngtcp2 (Pi/aarch64) $(date) ==="
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libev-dev >/dev/null 2>&1
  cd "$BASE/ngtcp2" || exit 1
  rm -rf boringssl nghttp3 ngtcp2 build go go.tgz
  # Go for ARM64 (upstream script hardcodes amd64)
  curl -sSL -o go.tgz https://dl.google.com/go/go1.20.4.linux-arm64.tar.gz
  mkdir -p go && tar -C go --strip-components=1 -xzf go.tgz
  export PATH="$PWD/go/bin:$PATH"
  echo "go: $(go version)"
  # boringssl: build only ssl+crypto lib targets (skip tests -> avoid gcc14 -Werror)
  git clone https://github.com/google/boringssl
  ( cd boringssl && git checkout -q b0341041b03ea71d8371a9692aedae263fc06ee9 && cmake -B build >/dev/null && make -C build -j"$(nproc)" ssl crypto )
  # nghttp3
  git clone -b v0.11.0 https://github.com/ngtcp2/nghttp3
  ( cd nghttp3 && autoreconf -i && ./configure --prefix="$PWD/build" --enable-lib-only && make -j"$(nproc)" && make install )
  # ngtcp2
  git clone -b v0.15.0 https://github.com/ngtcp2/ngtcp2
  ( cd ngtcp2 && git apply < ../patches/static_build.patch )
  cp -r setup/* .
  mkdir -p build && cd build
  cmake -DCMAKE_BUILD_TYPE=Release -DENABLE_BORINGSSL=1 \
    -DBORINGSSL_LIBRARIES="$PWD/../boringssl/build/ssl/libssl.a;$PWD/../boringssl/build/crypto/libcrypto.a;-lpthread;dl" \
    -DBORINGSSL_INCLUDE_DIR="$PWD/../boringssl/include" \
    -DLIBNGHTTP3_LIBRARY=../nghttp3/build/lib/libnghttp3.a \
    -DLIBNGHTTP3_INCLUDE_DIR=../nghttp3/build/include/ ../ngtcp2
  make -j"$(nproc)" bsslclient bsslserver
  cd ..
  if [ -f build/examples/bsslserver ]; then cp build/examples/bsslserver http_server; cp build/examples/bsslclient http_client; echo "RESULT: NGTCP2_OK"; else echo "RESULT: NGTCP2_FAIL"; fi
} > "$LOGD/ngtcp2.log" 2>&1
echo "ngtcp2: $(grep -o 'RESULT: NGTCP2_[A-Z]*' $LOGD/ngtcp2.log | tail -1)"

########## quiche (aarch64) ##########
{
  echo "=== quiche (Pi/aarch64) $(date) ==="
  cd "$BASE/quiche" || exit 1
  # rust nightly-2024-11-11 + rust-src
  if [ ! -f rustup.sh ]; then
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs > rustup.sh && chmod +x rustup.sh
    ./rustup.sh -y --default-toolchain nightly-2024-11-11
    . "$CARGO_HOME/env"; rustup component add rust-src
  fi
  . "$CARGO_HOME/env"
  export PATH="$BASE/ngtcp2/go/bin:$PATH"   # boringssl (quiche vendored) needs go
  rm -rf quiche
  git clone --recursive https://github.com/cloudflare/quiche
  # std time.rs patch at the aarch64 toolchain path
  STD="$RUSTUP_HOME/toolchains/$TOOLCHAIN/lib/rustlib/src/rust/library/std"
  cp setup/unix/time.rs "$STD/src/sys/pal/unix/"
  cp setup/time.rs "$STD/src/"
  cd quiche
  git checkout 5bccde6ead15688326e05364abab51a910242423
  git submodule update --init --recursive           # populate deps/boringssl
  cp -r ../setup/quiche/* .
  cp $HOME/pe/quiche-Cargo.lock Cargo.lock     # server's pinned lock (arch-independent)
  cargo fetch --locked                               # fetch exact locked deps (vanilla nix)
  # re-apply patched nix AFTER fetch so it isn't clobbered
  REG=$(ls -d "$CARGO_HOME"/registry/src/index.crates.io-*/ | head -1)
  cp -r ../setup/nix-0.27.1 "$REG"
  cargo clean -p nix --target "$RUST_TARGET" 2>/dev/null
  cargo build --locked -Z build-std --target "$RUST_TARGET" --bin quiche-server --bin quiche-client
  if [ -f target/$RUST_TARGET/debug/quiche-server ]; then
    cp target/$RUST_TARGET/debug/quiche-server target/$RUST_TARGET/debug/quiche-client ../
    echo "RESULT: QUICHE_OK"
  else echo "RESULT: QUICHE_FAIL"; fi
} > "$LOGD/quiche.log" 2>&1
echo "quiche: $(grep -o 'RESULT: QUICHE_[A-Z]*' $LOGD/quiche.log | tail -1)"

echo "==== PI BUILD END $(date) ===="
grep -h "^RESULT:" "$LOGD/ngtcp2.log" "$LOGD/quiche.log"
touch "$LOGD/PI_ALL_DONE"
