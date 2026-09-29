#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT/../versions.env"

CC_BIN="${CC:-$(command -v gcc-14 || command -v gcc)}"
CXX_BIN="${CXX:-$(command -v g++-14 || command -v g++)}"
[ -n "$CC_BIN" ] && [ -n "$CXX_BIN" ] || { echo "GCC/G++ 14 or newer is required" >&2; exit 2; }
CXX_MAJOR="$($CXX_BIN -dumpfullversion | cut -d. -f1)"
[ "$CXX_MAJOR" -ge 14 ] || { echo "G++ 14 or newer is required for std::print" >&2; exit 2; }
case "$(uname -m)" in
  x86_64) GO_ARCH=amd64; GO_SHA256="$GO_LINUX_AMD64_SHA256" ;;
  aarch64) GO_ARCH=arm64; GO_SHA256="$GO_LINUX_ARM64_SHA256" ;;
  *) echo "unsupported Go host architecture: $(uname -m)" >&2; exit 2 ;;
esac
if [ ! -x "$ROOT/go/bin/go" ] \
  || ! "$ROOT/go/bin/go" version | grep -q "go$GO_VERSION "; then
  rm -rf "$ROOT/go" "$ROOT/go.tgz"
  curl -fsSL -o "$ROOT/go.tgz" \
    "https://go.dev/dl/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"
  printf '%s  %s\n' "$GO_SHA256" "$ROOT/go.tgz" | sha256sum -c -
  tar -C "$ROOT" -xzf "$ROOT/go.tgz"
fi
export PATH="$ROOT/go/bin:$PATH"

checkout_repo() {
  local url="$1" dir="$2" commit="$3" recursive="${4:-0}"
  if [ ! -d "$dir/.git" ]; then
    if [ "$recursive" = 1 ]; then
      git clone --recursive "$url" "$dir"
    else
      git clone "$url" "$dir"
    fi
  fi
  git -C "$dir" fetch origin "$commit"
  git -C "$dir" checkout --detach "$commit"
  git -C "$dir" reset --hard "$commit"
  git -C "$dir" clean -ffd
  [ "$recursive" = 0 ] || git -C "$dir" submodule update --init --recursive
}

checkout_repo https://boringssl.googlesource.com/boringssl \
  "$ROOT/boringssl" "$BORINGSSL_COMMIT"
checkout_repo https://github.com/ngtcp2/nghttp3.git \
  "$ROOT/nghttp3" "$NGHTTP3_COMMIT" 1
checkout_repo https://github.com/ngtcp2/ngtcp2.git \
  "$ROOT/ngtcp2" "$NGTCP2_COMMIT" 1
git -C "$ROOT/ngtcp2" apply "$ROOT/static-link.patch"

rm -rf "$ROOT/boringssl/build"
CC="$CC_BIN" CXX="$CXX_BIN" cmake -S "$ROOT/boringssl" -B "$ROOT/boringssl/build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_SHARED_LIBS=OFF
cmake --build "$ROOT/boringssl/build" --target ssl crypto --parallel "$(nproc)"

rm -rf "$ROOT/nghttp3/build"
(
  cd "$ROOT/nghttp3"
  autoreconf -i
  CC="$CC_BIN" CXX="$CXX_BIN" ./configure \
    --prefix="$ROOT/nghttp3/build" --enable-lib-only --disable-shared
  make -j"$(nproc)"
  make install
)

rm -rf "$ROOT/build"
BORINGSSL_LIBS="$ROOT/boringssl/build/libssl.a;$ROOT/boringssl/build/libcrypto.a;-lpthread;dl"
CC="$CC_BIN" CXX="$CXX_BIN" cmake -S "$ROOT/ngtcp2" -B "$ROOT/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DENABLE_OPENSSL=OFF \
  -DENABLE_BORINGSSL=ON \
  -DENABLE_STATIC_LIB=ON \
  -DENABLE_SHARED_LIB=OFF \
  "-DBORINGSSL_LIBRARIES=$BORINGSSL_LIBS" \
  -DBORINGSSL_INCLUDE_DIR="$ROOT/boringssl/include" \
  -DLIBNGHTTP3_LIBRARY="$ROOT/nghttp3/build/lib/libnghttp3.a" \
  -DLIBNGHTTP3_INCLUDE_DIR="$ROOT/nghttp3/build/include"
cmake --build "$ROOT/build" --target bsslclient bsslserver --parallel "$(nproc)"
cp "$ROOT/build/examples/bsslclient" "$ROOT/http_client"
cp "$ROOT/build/examples/bsslserver" "$ROOT/http_server"

printf 'ngtcp2_commit=%s\nnghttp3_commit=%s\nboringssl_commit=%s\ncompiler=%s\n' \
  "$NGTCP2_COMMIT" "$NGHTTP3_COMMIT" "$BORINGSSL_COMMIT" "$($CXX_BIN --version | sed -n '1p')" \
  > "$ROOT/VERSION"
sha256sum "$ROOT/http_server" "$ROOT/http_client"
