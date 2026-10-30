#!/usr/bin/env bash
# Build the Horizon client for the browser and publish it into `public/`.
#
# The build needs the wasm toolchain and a vendored `Cargo.lock`; both are
# prepared by `scripts/fetch_crates.py`, `scripts/fetch_extra_crates.py` and
# `scripts/assemble_vendor.py` (see docs/WEB.md). Usage:
#
#   scripts/build_web.sh [RUST_PREFIX] [TARGET_DIR]
#
# RUST_PREFIX defaults to /tmp/rust (bin/rustc, bin/cargo, lib/rustlib with the
# wasm32 target) and TARGET_DIR to /tmp/horizon-build/target.
set -euo pipefail
cd "$(dirname "$0")/.."

RUST_PREFIX="${1:-/tmp/rust}"
TARGET_DIR="${2:-/tmp/horizon-build/target}"
PROFILE=fast
WASM="$TARGET_DIR/wasm32-unknown-unknown/$PROFILE/horizon-client.wasm"
WRAPPER="$TARGET_DIR/release/wasm-bindgen"

# `--cfg getrandom_backend="wasm_js"`: getrandom's wasm backend. The stack is
# raised from the 1 MB default: world generation and the mesher recurse.
export PATH="$RUST_PREFIX/bin:$PATH"
export CARGO_TARGET_DIR="$TARGET_DIR"
export RUSTFLAGS='--cfg getrandom_backend="wasm_js" -C debuginfo=1 -C link-arg=-zstack-size=8388608'

cargo build --offline --profile "$PROFILE" --target wasm32-unknown-unknown \
  -p horizon-client --bin horizon-client

# The wasm-bindgen CLI is built once from the wasm-bindgen 0.2.129 sources
# (crates/cli); it has to match the crate the engine links against.
if [ ! -x "$WRAPPER" ]; then
  CLI_SRC="${WASM_BINDGEN_CLI_SRC:-/tmp/horizon-gh-cache/extra__wasm-bindgen__wasm-bindgen__0.2.129/crates/cli}"
  cargo build --offline --release --manifest-path "$CLI_SRC/Cargo.toml"
  WRAPPER="$TARGET_DIR/release/wasm-bindgen"
fi

OUT="$TARGET_DIR/wasm-bindgen-web"
"$WRAPPER" --target web --no-typescript --out-dir "$OUT" --out-name horizon-client "$WASM"
cp "$OUT/horizon-client.js" "$OUT/horizon-client_bg.wasm" public/

echo "published public/horizon-client.js and public/horizon-client_bg.wasm"
echo "serve with: python3 scripts/serve_web.py 5173"
