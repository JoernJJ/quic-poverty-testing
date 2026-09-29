#!/bin/bash
# Ubuntu / non-root adaptation of setup-env.sh.
# Differences vs. upstream setup-env.sh:
#   - Go is installed into a local prefix ($HOME/.local/go-sdk) instead of
#     /usr/local (which needs root). PATH is exported unconditionally (upstream
#     only exports it inside the download guard, so re-runs lose Go on PATH).
#   - Drops `make check` for nghttp3 (avoids test-only deps); builds lib only.
#   - Same pinned versions/commits as upstream so binaries match the paper.
set -x
set -e

cd "$(dirname "$(readlink -f "$0")")"   # -> quic-implementations/ngtcp2

NGHTTP3_VERSION=v0.11.0
NGTCP2_VERSION=v0.15.0
GO_VERSION=go1.20.4
BORINGSSL_COMMIT=b0341041b03ea71d8371a9692aedae263fc06ee9

# --- Go (local prefix, no root) ---
GO_SDK="$HOME/.local/go-sdk"
if [ ! -x "$GO_SDK/go/bin/go" ]; then
    mkdir -p "$GO_SDK"
    [ -f go.tgz ] || curl -L -o go.tgz "https://dl.google.com/go/${GO_VERSION}.linux-amd64.tar.gz"
    tar -C "$GO_SDK" -xzf go.tgz
fi
export PATH="$GO_SDK/go/bin:$PATH"
go version

# --- BoringSSL (pinned commit, old lib layout: build/ssl, build/crypto) ---
if [ ! -d boringssl ]; then
    git clone https://github.com/google/boringssl
    ( cd boringssl && git checkout "$BORINGSSL_COMMIT" )
fi
if [ ! -f boringssl/build/ssl/libssl.a ]; then
    # Build only the static libs ngtcp2 links against. Upstream `make all`
    # also builds test/tool targets that fail under Ubuntu's GCC with
    # -Werror=ignored-attributes; those are irrelevant to us.
    ( cd boringssl && cmake -B build && make -C build crypto ssl -j"$(nproc)" )
fi

# --- nghttp3 (lib only) ---
if [ ! -d nghttp3 ]; then
    git clone -b "$NGHTTP3_VERSION" https://github.com/ngtcp2/nghttp3
    ( cd nghttp3 && git submodule update --init && autoreconf -i \
        && ./configure --prefix="$PWD/build" --enable-lib-only \
        && make -j"$(nproc)" && make install )
fi

# --- ngtcp2 (static build patch + setup overlay) ---
if [ ! -d ngtcp2 ]; then
    git clone -b "$NGTCP2_VERSION" https://github.com/ngtcp2/ngtcp2
    ( cd ngtcp2 && git apply < ../patches/static_build.patch )
    cp -r setup/* .
    mkdir -p build
    ( cd build && cmake -DCMAKE_BUILD_TYPE=Release \
        -DENABLE_BORINGSSL=1 \
        -DBORINGSSL_LIBRARIES="$PWD/../boringssl/build/ssl/libssl.a;$PWD/../boringssl/build/crypto/libcrypto.a;-lpthread;dl" \
        -DBORINGSSL_INCLUDE_DIR="$PWD/../boringssl/include" \
        -DLIBNGHTTP3_LIBRARY=../nghttp3/build/lib/libnghttp3.a \
        -DLIBNGHTTP3_INCLUDE_DIR=../nghttp3/build/include/ ../ngtcp2 \
        && make bsslclient bsslserver -j"$(nproc)" )
fi

cp build/examples/bsslclient ./http_client
cp build/examples/bsslserver ./http_server
echo "=== ngtcp2 build complete ==="
ls -la http_client http_server
