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
THREADS_DIR_ROOT="$TARGET_DIR/threads"
LOG="$TARGET_DIR/threaded-build.log"
THREAD_RUSTFLAGS="$BASE_RUSTFLAGS \
  -C target-feature=+atomics,+bulk-memory,+mutable-globals \
  -C link-arg=--import-memory \
  -C link-arg=--shared-memory \
  -C link-arg=--max-memory=2147483648 \
  -C link-arg=--export=__wasm_init_tls \
  -C link-arg=--export=__tls_size \
  -C link-arg=--export=__tls_align \
  -C link-arg=--export=__tls_base"
# Pinned. Newer nightlies do not compile this crate at all: 2026-10-31
# (f9ceb4a4d) stops with `error[E0391]: cycle detected when computing when
# `ItemStack` needs async drop`, and its `std` build has also failed with
# `duplicate lang item in crate `core``. 2026-10-30 (92b5b8c87) builds both
# modules. Override with HORIZON_NIGHTLY to try another nightly.
NIGHTLY="${HORIZON_NIGHTLY:-nightly-2026-10-30}"
NIGHTLY_HINT="rustup toolchain install $NIGHTLY -c rust-src -t wasm32-unknown-unknown"

# One target directory per compiler. `-Z build-std` caches the `core` it builds
# for std's own dependencies (`dlmalloc`, `cfg_if`, …) in
# `<dir>/wasm32-unknown-unknown/fast/build/core/*/out`. One of those left by a
# *different* compiler sits next to the current compiler's sysroot `core`, and
# every std dependency then fails with `error[E0152]: duplicate lang item in
# crate core`. Keying the directory by the compiler's version means such an
# artifact cannot be reached at all, so no stamp or wipe can be forgotten.
THREAD_VERSION="$(rustc +"$NIGHTLY" --version 2>/dev/null || echo unknown)"
THREAD_KEY="$(printf '%s' "$THREAD_VERSION" | sha256sum | cut -c1-8)"
THREAD_TARGET_DIR="$THREADS_DIR_ROOT/$THREAD_KEY"
mkdir -p "$THREADS_DIR_ROOT"

threaded_cargo() {
  RUSTFLAGS="$THREAD_RUSTFLAGS" CARGO_TARGET_DIR="$THREAD_TARGET_DIR" \
    cargo +"$NIGHTLY" build -Z build-std=std,panic_abort --offline \
      --profile "$PROFILE" --target wasm32-unknown-unknown \
      -p horizon-client --bin horizon-client 2>&1 | tee "$LOG"
}

build_threaded() {
  local wasm="$THREAD_TARGET_DIR/wasm32-unknown-unknown/$PROFILE/horizon-client.wasm"
  local out="$TARGET_DIR/wasm-bindgen-web-threaded"
  # Drop the previous module first: a failed build leaves it in place, and
  # wasm-bindgen would then package that one and this script would report a
  # success that did not happen. Each step is checked explicitly as well,
  # because `set -e` does not apply inside a function called from an `if`.
  rm -f "$wasm" "$out/horizon-client.js" "$out/horizon-client_bg.wasm"
  if ! threaded_cargo; then
    # `-Z build-std` caches a rebuilt `core` for std's own dependencies. Since
    # the directory belongs to this compiler alone, an `E0152 duplicate lang
    # item` here means the cache itself is damaged (an interrupted build, a
    # half-written `out/`), so throwing it away and building again is the fix
    # that does not require guesswork. Only std was built, so the retry costs
    # minutes, not the whole build.
    if grep -q 'duplicate lang item' "$LOG"; then
      echo "note: the std cache in $THREAD_TARGET_DIR cannot be used" >&2
      echo "      clearing it and building again ($THREAD_VERSION)" >&2
      rm -rf "$THREAD_TARGET_DIR"
      if ! threaded_cargo; then
        echo "error: the threaded build failed again with nothing left over from" >&2
        echo "       an earlier run, so this is not a stale cache: $LOG" >&2
        return 1
      fi
    else
      echo "      the full cargo log is in $LOG" >&2
      return 1
    fi
  fi
  if [ ! -f "$wasm" ]; then
    echo "error: cargo reported success but $wasm is missing" >&2
    return 1
  fi
  "$WRAPPER" --target web --no-typescript --out-dir "$out" "$wasm" || return 1
  # Renamed: the page passes the wasm path itself, so the glue need not point
  # at its sibling (`public/app.js`).
  cp "$out/horizon-client.js" public/horizon-client-threaded.js || return 1
  cp "$out/horizon-client_bg.wasm" public/horizon-client-threaded_bg.wasm || return 1
}

threads_failed=0
if cargo +"$NIGHTLY" --version >/dev/null 2>&1; then
  # Say which compiler and directory this run used: a log without it cannot be
  # told apart from one an older copy of this script made.
  echo "build_web.sh @$(git rev-parse --short HEAD 2>/dev/null || echo unknown) with $THREAD_VERSION -> $THREAD_TARGET_DIR" >&2
  # `std`'s own dependencies (`object`, `addr2line`, `compiler_builtins`,
  # `dlmalloc`, …) are in rust-src's lockfile, not the project's, so they are
  # not vendored by the scripts that build this directory. The same script
  # repairs crates copied from GitHub checkouts, whose checksum file has no
  # `package` checksum, which the `std` build requires.
  VENDOR_DIR="$(sed -n 's/^directory *= *"\(.*\)"/\1/p' .cargo/config.toml | head -1)"
  if [ -n "$VENDOR_DIR" ] && [ -d "$VENDOR_DIR" ]; then
    python3 scripts/fetch_std_crates.py "$VENDOR_DIR" "$NIGHTLY" || threads_failed=1
  fi
  if [ "$threads_failed" -eq 0 ]; then
    mkdir -p "$THREAD_TARGET_DIR"
    if build_threaded; then
      echo "published the threaded module: public/horizon-client-threaded.js and _bg.wasm ($NIGHTLY)"
    else
      echo "error: the threaded module failed to build; cargo's output above says why" >&2
      echo "       the flags need a nightly with rust-src; this project builds with" >&2
      echo "       $NIGHTLY, which this script uses by default ($NIGHTLY_HINT)" >&2
      threads_failed=1
    fi
  fi
else
  echo "warning: no '$NIGHTLY' toolchain (needs rustup, rust-src): skipping the threaded module" >&2
  echo "         install it with: $NIGHTLY_HINT" >&2
fi

echo "published public/horizon-client.js and public/horizon-client_bg.wasm"
echo "serve with: python3 scripts/serve_web.py 5173"

# A half-done build used to look like a success here, and the page then fell
# back to the single-threaded module without anyone noticing. Say so.
if [ "$threads_failed" -ne 0 ]; then
  echo "error: only the single-threaded module is published; the page will run" >&2
  echo "       single-threaded until the threaded build succeeds" >&2
  exit 1
fi
