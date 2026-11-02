#!/usr/bin/env bash
# Three consecutive autosaves and chest opens, under the page's no-park rules.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p target
rustc --edition 2024 -O -o target/no_park_main_thread scripts/no_park_main_thread.rs
exec target/no_park_main_thread
