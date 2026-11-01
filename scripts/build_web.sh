#!/usr/bin/env bash
# Build the Horizon client for the browser and publish it into `public/`.
#
# Two modules are built: the single-threaded one every browser can run, and the
# threaded one whose chunk workers are Web Workers over one shared memory
# (docs/WEB.md, "Threads"). The threaded build needs a nightly toolchain with
# the `rust-src` component for `-Z build-std`; without one it is skipped with a
# warning and the page runs single-threaded.
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
WRAPPER="$TARGET_DIR/release/wasm-bindgen"
OUT="$TARGET_DIR/wasm-bindgen-web"

# `--cfg getrandom_backend="wasm_js"`: getrandom's wasm backend. The stack is
# raised from the 1 MB default: world generation and the mesher recurse.
# Symbols are stripped: the module is 33 MB instead of 42, and ~11 MB gzipped.
export PATH="$RUST_PREFIX/bin:$PATH"
export CARGO_TARGET_DIR="$TARGET_DIR"
BASE_RUSTFLAGS='--cfg getrandom_backend="wasm_js" -C debuginfo=0 -C strip=symbols -C link-arg=-zstack-size=8388608'

# The wasm-bindgen CLI (`crates/horizon-wasm-bindgen`, which wraps
# wasm-bindgen-cli-support) has to match the crate the engine links against.
if [ ! -x "$WRAPPER" ]; then
  cargo build --offline --release -p horizon-wasm-bindgen --bin wasm-bindgen
fi

# 1. The single-threaded module: the page's fallback, and what a browser
#    without shared memory runs.
RUSTFLAGS="$BASE_RUSTFLAGS" cargo build --offline --profile "$PROFILE" \
  --target wasm32-unknown-unknown -p horizon-client --bin horizon-client
"$WRAPPER" --target web --no-typescript --out-dir "$OUT" \
  "$TARGET_DIR/wasm32-unknown-unknown/$PROFILE/horizon-client.wasm"
cp "$OUT/horizon-client.js" "$OUT/horizon-client_bg.wasm" public/

# 2. The threaded module. `std` is rebuilt with atomics (its wasm thread
#    creation is a stub either way, but `Mutex`, `Condvar` and `sleep` need
#    them), and the module imports the one shared memory that every Web Worker
#    instantiates against. The `__tls_*` exports are what wasm-bindgen's thread
#    transform uses to give each worker its own stack and thread-locals.
#    `--max-memory` sizes that shared memory; 2 GiB is the largest a browser
#    reliably gives a `SharedArrayBuffer`.
THREAD_TARGET_DIR="$TARGET_DIR/threads"
THREAD_RUSTFLAGS="$BASE_RUSTFLAGS \
  -C target-feature=+atomics,+bulk-memory,+mutable-globals \
  -C link-arg=--import-memory \
  -C link-arg=--shared-memory \
  -C link-arg=--max-memory=2147483648 \
  -C link-arg=--export=__wasm_init_tls \
  -C link-arg=--export=__tls_size \
  -C link-arg=--export=__tls_align \
  -C link-arg=--export=__tls_base"
NIGHTLY="${HORIZON_NIGHTLY:-nightly}"

build_threaded() {
  RUSTFLAGS="$THREAD_RUSTFLAGS" CARGO_TARGET_DIR="$THREAD_TARGET_DIR" \
    cargo +"$NIGHTLY" build -Z build-std=std,panic_abort --offline \
      --profile "$PROFILE" --target wasm32-unknown-unknown \
      -p horizon-client --bin horizon-client
  local out="$TARGET_DIR/wasm-bindgen-web-threaded"
  "$WRAPPER" --target web --no-typescript --out-dir "$out" \
    "$THREAD_TARGET_DIR/wasm32-unknown-unknown/$PROFILE/horizon-client.wasm"
  # Renamed: the page passes the wasm path itself, so the glue need not point
  # at its sibling (`public/app.js`).
  cp "$out/horizon-client.js" public/horizon-client-threaded.js
  cp "$out/horizon-client_bg.wasm" public/horizon-client-threaded_bg.wasm
}

if cargo +"$NIGHTLY" --version >/dev/null 2>&1; then
  # `std`'s own dependencies (`object`, `addr2line`, `compiler_builtins`,
  # `dlmalloc`, …) are in rust-src's lockfile, not the project's, so they are
  # not vendored by the scripts that build this directory.
  VENDOR_DIR="$(sed -n 's/^directory *= *"\(.*\)"/\1/p' .cargo/config.toml | head -1)"
  if [ -n "$VENDOR_DIR" ] && [ -d "$VENDOR_DIR" ]; then
    python3 scripts/fetch_std_crates.py "$VENDOR_DIR" "$NIGHTLY" >/dev/null 2>&1 \
      || echo "warning: could not vendor std's build-std crates; see scripts/fetch_std_crates.py" >&2
  fi
  if build_threaded; then
    echo "published the threaded module: public/horizon-client-threaded.js and _bg.wasm"
  else
    echo "warning: the threaded module failed to build; the page will run single-threaded" >&2
    echo "         (-Z build-std also needs std's own crates vendored: object, addr2line," >&2
    echo "          compiler_builtins, dlmalloc, wasi*)" >&2
  fi
else
  echo "warning: no '$NIGHTLY' toolchain (needs rustup, rust-src): skipping the threaded module" >&2
fi

echo "published public/horizon-client.js and public/horizon-client_bg.wasm"
echo "serve with: python3 scripts/serve_web.py 5173"
