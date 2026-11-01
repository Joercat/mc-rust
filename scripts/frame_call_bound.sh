#!/usr/bin/env bash
# Builds and runs the frame-call measurement against the shipped deferral.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p target
rustc --edition 2024 -O -o target/frame_call_bound scripts/frame_call_bound.rs
exec target/frame_call_bound
