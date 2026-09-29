#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT/../versions.env"

command -v rustup >/dev/null || { echo "rustup is required" >&2; exit 2; }
command -v clang >/dev/null || { echo "clang is required by bindgen" >&2; exit 2; }

if [ ! -d "$ROOT/quiche/.git" ]; then
  git clone --recursive https://github.com/cloudflare/quiche.git "$ROOT/quiche"
fi
git -C "$ROOT/quiche" fetch origin "$QUICHE_COMMIT"
git -C "$ROOT/quiche" checkout --detach "$QUICHE_COMMIT"
git -C "$ROOT/quiche" reset --hard "$QUICHE_COMMIT"
git -C "$ROOT/quiche" submodule update --init --recursive
cp "$ROOT/Cargo.lock" "$ROOT/quiche/Cargo.lock"

rustup toolchain install "$QUICHE_RUST_TOOLCHAIN"
HOST="$(rustup run "$QUICHE_RUST_TOOLCHAIN" rustc -vV | sed -n 's/^host: //p')"
[ -n "$HOST" ] || { echo "cannot determine Rust host target" >&2; exit 2; }

cd "$ROOT/quiche"
cargo "+$QUICHE_RUST_TOOLCHAIN" build --locked --target "$HOST" \
  --bin quiche-server --bin quiche-client
cp "target/$HOST/debug/quiche-server" "$ROOT/quiche-server"
cp "target/$HOST/debug/quiche-client" "$ROOT/quiche-client"

printf 'quiche_commit=%s\nrust_toolchain=%s\ntarget=%s\n' \
  "$QUICHE_COMMIT" "$QUICHE_RUST_TOOLCHAIN" "$HOST" > "$ROOT/VERSION"
sha256sum "$ROOT/quiche-server" "$ROOT/quiche-client"
