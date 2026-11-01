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
# The key covers the compiler *and* the sysroot `core` it ships: reinstalling or
# repairing a toolchain keeps its version string, and artefacts built against
# the old sysroot must not be reachable from the repaired one.
thread_key_for() {
  local sysroot core
  sysroot="$(rustc +"$1" --print sysroot 2>/dev/null || true)"
  core="$(ls "$sysroot"/lib/rustlib/wasm32-unknown-unknown/lib/libcore-*.rmeta 2>/dev/null | head -1)"
  printf '%s %s' "$2" "$(sha256sum "$core" 2>/dev/null | cut -c1-16)" | sha256sum | cut -c1-8
}

THREAD_VERSION="$(rustc +"$NIGHTLY" --version 2>/dev/null || echo unknown)"
THREAD_KEY="$(thread_key_for "$NIGHTLY" "$THREAD_VERSION")"
THREAD_TARGET_DIR="$THREADS_DIR_ROOT/$THREAD_KEY"
mkdir -p "$THREADS_DIR_ROOT"

threaded_cargo() {
  RUSTFLAGS="$THREAD_RUSTFLAGS" CARGO_TARGET_DIR="$THREAD_TARGET_DIR" \
    cargo +"$NIGHTLY" build -Z build-std=std,panic_abort --offline \
      --profile "$PROFILE" --target wasm32-unknown-unknown \
      -p horizon-client --bin horizon-client 2>&1 | tee "$LOG"
}

# `-Z build-std` links std's own dependencies against the `core` it rebuilt.
# When `dlmalloc` or `cfg_if` is handed two `core` crates (`E0152 duplicate lang
# item`) the compile command says which one: print its flags from a verbose
# re-run, which only rebuilds the units that failed, so it is quick.
diagnose_lang_item() {
  echo "       build environment:" >&2
  local name value line unit src verbose="$TARGET_DIR/threaded-verbose.log"
  for name in RUSTC RUSTC_WRAPPER RUSTC_WORKSPACE_WRAPPER CARGO_ENCODED_RUSTFLAGS \
    RUSTFLAGS CARGO_BUILD_TARGET RUSTUP_TOOLCHAIN CARGO_HOME SCCACHE_DIR; do
    value="${!name:-}"
    if [ -n "$value" ]; then echo "         $name=$value" >&2; fi
  done
  echo "       patches cargo skipped, if any:" >&2
  grep -i 'was not used in the crate graph' "$LOG" | sed 's/^/         /' >&2 || true
  # The unit that just failed is rebuilt by this run, so `-v` prints its command
  # line. Keep the whole thing: it is also the only place the cfg-if unit id of
  # *this* build appears, and its source path names the version.
  RUSTFLAGS="$THREAD_RUSTFLAGS" CARGO_TARGET_DIR="$THREAD_TARGET_DIR" \
    cargo +"$NIGHTLY" build -Z build-std=std,panic_abort --offline \
      --profile "$PROFILE" --target wasm32-unknown-unknown \
      -p horizon-client --bin horizon-client -v >"$verbose" 2>&1 || true
  line="$(grep -m1 -- '--crate-name dlmalloc' "$verbose" || true)"
  if [ -z "$line" ]; then
    echo "       dlmalloc was not recompiled; the full run is in $verbose" >&2
    return
  fi
  unit="$(printf '%s' "$line" | grep -o 'cfg-if/[a-f0-9]*/out' | head -1)"
  if [ -n "$unit" ]; then
    src="$(grep -ho 'cfg-if-[0-9][0-9.]*' "$THREAD_TARGET_DIR/wasm32-unknown-unknown/$PROFILE/build/$unit"/*.d 2>/dev/null | head -1)"
    echo "       dlmalloc's cfg-if: ${src:-unknown} ($unit)" >&2
  fi
  echo "       dlmalloc was compiled with:" >&2
  printf '%s\n' "$line" \
    | grep -o -e '--extern [^ ]*' -e '-Z [^ ]*' -e '--sysroot [^ ]*' -e '-C target-feature=[^ ]*' \
    | sed 's/^/         /' >&2
}

build_threaded() {
  local wasm="$THREAD_TARGET_DIR/wasm32-unknown-unknown/$PROFILE/horizon-client.wasm"
  local out="$TARGET_DIR/wasm-bindgen-web-threaded"  # Drop the previous module first: a failed build leaves it in place, and
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
      diagnose_lang_item
      if [ "$THREAD_DIR_FRESH" -eq 1 ]; then
        echo "error: the build failed with nothing cached to clear, so this is not" >&2
        echo "       a stale directory: the std build is picking up the wrong \`core\`" >&2
        echo "       on this machine. the block above names the one dlmalloc got." >&2
        return 1
      fi
      # If this directory already holds a rebuilt core, clearing cannot change
      # the outcome, and a retry spends minutes rebuilding std to fail the same
      # way. The compiler-keyed path already rules out the cross-compiler
      # staleness this retry was written for, so report and stop.
      if ls "$THREAD_TARGET_DIR/wasm32-unknown-unknown/$PROFILE"/build/core/*/out/libcore-*.rmeta >/dev/null 2>&1 \
        || grep -q 'Compiling core v' "$LOG"; then
        echo "       (std is complete in this directory, so clearing it cannot" >&2
        echo "       change anything; not retrying)" >&2
        return 1
      fi
      echo "note: the std cache in $THREAD_TARGET_DIR cannot be used" >&2
      echo "      clearing it and building again ($THREAD_VERSION)" >&2
      rm -rf "$THREAD_TARGET_DIR"
      if ! threaded_cargo; then
        diagnose_lang_item
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
    # `-Z build-std` rewrites Cargo.lock: std's own dependencies get entries.
    # A lock left rewritten by an earlier run is a different build graph, and
    # the one input a fresh target directory does not reset. Start every
    # threaded build from the committed lock (keeping the old one beside the
    # log) and put it back afterwards.
    if git rev-parse --git-dir >/dev/null 2>&1 && [ -f Cargo.lock ] \
      && ! git diff --quiet -- Cargo.lock; then
      echo "note: Cargo.lock differs from the commit; building from the committed one" >&2
      echo "      (the working copy is saved as $TARGET_DIR/Cargo.lock.before)" >&2
      cp Cargo.lock "$TARGET_DIR/Cargo.lock.before"
      git checkout -- Cargo.lock
    fi
    mkdir -p "$THREAD_TARGET_DIR"
    if [ -n "$(ls -A "$THREAD_TARGET_DIR" 2>/dev/null)" ]; then THREAD_DIR_FRESH=0; else THREAD_DIR_FRESH=1; fi
    if build_threaded; then
      echo "published the threaded module: public/horizon-client-threaded.js and _bg.wasm ($NIGHTLY)"
    else
      echo "error: the threaded module failed to build; cargo's output above says why" >&2
      echo "       the flags need a nightly with rust-src; this project builds with" >&2
      echo "       $NIGHTLY, which this script uses by default ($NIGHTLY_HINT)" >&2
      threads_failed=1
    fi
    # Undo the lock rewrite this build made, so the next run starts from the
    # same graph (and a failed one does not poison the following attempt).
    git checkout -- Cargo.lock 2>/dev/null || true
  fi
else
  echo "warning: no '$NIGHTLY' toolchain (needs rustup, rust-src): skipping the threaded module" >&2
  echo "         install it with: $NIGHTLY_HINT" >&2
fi

# One more compiler is worth a try before giving up. The failure above is in how
# this machine assembles the std graph for the pinned nightly (same unit hashes,
# same flags, same sysroot as a build that works elsewhere), and another nightly
# builds its own std from its own rust-src into its own directory. Only when the
# pinned one actually failed with `E0152`, only when the user did not pick a
# nightly themselves, and only when the other one is installed.
if [ "$threads_failed" -ne 0 ] && [ -z "${HORIZON_NIGHTLY:-}" ] \
  && grep -q 'duplicate lang item' "$LOG" 2>/dev/null; then
  for ALT in nightly-2026-10-29 nightly-2026-10-31; do
    [ "$ALT" = "$NIGHTLY" ] && continue
    cargo +"$ALT" --version >/dev/null 2>&1 || continue
    echo "note: $NIGHTLY cannot build std here; trying $ALT instead" >&2
    NIGHTLY="$ALT"
    NIGHTLY_HINT="rustup toolchain install $ALT -c rust-src -t wasm32-unknown-unknown"
    THREAD_VERSION="$(rustc +"$ALT" --version 2>/dev/null || echo unknown)"
    THREAD_KEY="$(thread_key_for "$ALT" "$THREAD_VERSION")"
    THREAD_TARGET_DIR="$THREADS_DIR_ROOT/$THREAD_KEY"
    python3 scripts/fetch_std_crates.py "$VENDOR_DIR" "$ALT" || continue
    mkdir -p "$THREAD_TARGET_DIR"
    THREAD_DIR_FRESH=1
    if build_threaded; then
      echo "published the threaded module: public/horizon-client-threaded.js and _bg.wasm ($ALT)"
      threads_failed=0
      break
    fi
  done
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
