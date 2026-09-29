#!/bin/bash
# Ubuntu / non-root adaptation of setup-env.sh (direct-driver mode).
# Differences vs. upstream setup-env.sh:
#   - Rust toolchain paths use $HOME instead of the hardcoded /root.
#   - Assumes nightly-2024-11-11 + rust-src are already installed.
#   - Faithfully applies the same patched std (time.rs) and patched nix-0.27.1
#     crate the paper used, but extraction-order-safe for modern cargo.
set -x
set -o pipefail

. "$HOME/.cargo/env"

TOOLCHAIN="nightly-2024-11-11-x86_64-unknown-linux-gnu"
TC="$HOME/.rustup/toolchains/$TOOLCHAIN"
CARGO_HOME="${CARGO_HOME:-$HOME/.cargo}"
QUICHE_COMMIT=5bccde6ead15688326e05364abab51a910242423
TARGET=x86_64-unknown-linux-gnu

cd "$(dirname "$(readlink -f "$0")")"   # -> quic-implementations/quiche

# 1) Patched std source used with -Z build-std
cp setup/unix/time.rs "$TC/lib/rustlib/src/rust/library/std/src/sys/pal/unix/time.rs"
cp setup/time.rs      "$TC/lib/rustlib/src/rust/library/std/src/time.rs"

# 2) Clone + pin quiche, overlay the paper's patched apps/quiche source
if [ ! -d quiche ]; then
    git clone --recursive https://github.com/cloudflare/quiche
    # Sync submodules (incl. the vendored deps/boringssl that quiche's build.rs
    # compiles) to the pinned commit; a plain checkout leaves them stale/empty.
    ( cd quiche && git checkout "$QUICHE_COMMIT" && git submodule update --init --recursive )
    cp -r setup/quiche/* quiche/
fi

cd quiche

# 2b) Pin transitive deps back below what the 2024-11-11 nightly can handle.
#     A fresh clone ships a drifted Cargo.lock resolving to crates that now
#     require `edition2024` (idna_adapter/icu4x via url 2.5.8) or rustc >=1.88
#     (serde_with 3.21). These pins are no-ops if the lock is already fixed.
cargo update -p indexmap --precise 2.6.0    || true
cargo update -p url@2.5.8 --precise 2.5.0   || true
cargo update -p serde_with --precise 3.9.0  || true

cargo fetch

# 3) Patch nix-0.27.1 with the paper's ControlMessage::UdpGsoSegmentPacingRate
#    (used by apps/src/sendto.rs). Patching the registry copy in place does not
#    work: cargo treats registry sources as immutable and reverts/ignores the
#    edit. Instead vendor a full copy outside the registry and point a
#    [patch.crates-io] override at it, which cargo cannot re-extract.
cargo fetch
cargo build -Z build-std --target "$TARGET" --bin quiche-server 2>/dev/null || true  # force dep extraction
REG="$(find "$CARGO_HOME/registry/src" -maxdepth 1 -name 'index.crates.io-*' 2>/dev/null | head -1)"
if [ ! -d "$REG/nix-0.27.1" ]; then
    echo "ERROR: nix-0.27.1 not extracted; cannot apply GSO-pacing patch"; exit 1
fi
rm -rf vendored/nix-0.27.1; mkdir -p vendored
cp -a "$REG/nix-0.27.1" vendored/nix-0.27.1
rm -f vendored/nix-0.27.1/.cargo-ok
cp setup/nix-0.27.1/src/sys/socket/mod.rs vendored/nix-0.27.1/src/sys/socket/mod.rs
if ! grep -q '^\[patch.crates-io\]' Cargo.toml; then
    printf '\n[patch.crates-io]\nnix = { path = "vendored/nix-0.27.1" }\n' >> Cargo.toml
fi
cargo update -p nix 2>/dev/null || true

# 4) Build client + server against patched std
cargo build -Z build-std --target "$TARGET" --bin quiche-client
cargo build -Z build-std --target "$TARGET" --bin quiche-server

cd ..
cp quiche/target/$TARGET/debug/quiche-server .
cp quiche/target/$TARGET/debug/quiche-client .
echo "=== quiche build complete ==="
ls -la quiche-server quiche-client
