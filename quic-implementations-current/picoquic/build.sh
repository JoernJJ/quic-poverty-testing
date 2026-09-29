#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
source "$ROOT/../versions.env"

if [ ! -d "$ROOT/picoquic/.git" ]; then
  git clone https://github.com/private-octopus/picoquic.git "$ROOT/picoquic"
fi
git -C "$ROOT/picoquic" fetch origin "$PICOQUIC_COMMIT"
git -C "$ROOT/picoquic" checkout --detach "$PICOQUIC_COMMIT"
git -C "$ROOT/picoquic" reset --hard "$PICOQUIC_COMMIT"
git -C "$ROOT/picoquic" clean -ffd

rm -rf "$ROOT/build"
cmake -S "$ROOT/picoquic" -B "$ROOT/build" \
  -DPICOQUIC_FETCH_PTLS=ON \
  -DPICOQUIC_FETCH_PTLS_TAG="$PICOTLS_COMMIT" \
  -DBUILD_TESTING=OFF
cmake --build "$ROOT/build" --target picoquicdemo --parallel "$(nproc)"
cp "$ROOT/build/picoquicdemo" "$ROOT/picoquicdemo"

printf 'picoquic_commit=%s\npicotls_commit=%s\n' \
  "$PICOQUIC_COMMIT" "$PICOTLS_COMMIT" > "$ROOT/VERSION"
sha256sum "$ROOT/picoquicdemo"
