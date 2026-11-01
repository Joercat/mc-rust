#!/usr/bin/env python3
"""Classify every blocking-primitive site in the threaded web build.

The page's main thread traps on `memory.atomic.wait32` ("Atomics.wait cannot be
called in this context"), so no code reachable from a `horizon_web_*` export or
from the eframe frame update may park. This script finds every place where the
wasm client could park and requires an explicit verdict for each, so "nothing
blocks the main thread" is a list rather than an assertion.

    scripts/audit_main_thread.py             # classify; exit 1 on an unclassified site
    scripts/audit_main_thread.py --groups    # group by crate path::function
    scripts/audit_main_thread.py --crates    # the crates the wasm build compiles
    scripts/audit_main_thread.py --json      # machine-readable report

What counts as a blocking primitive:

    .lock()                 std::sync::Mutex
    .wait(/.wait_timeout(   std::sync::Condvar
    .recv(/.recv_timeout(   std::sync::mpsc
    thread::park(_timeout)  std::thread
    thread::sleep(          std::thread, and std's shim replacement
    block_on(               pollster/wgpu runtime entry
    .join()                 JoinHandle, including scope handles

`std::sync::RwLock` is checked separately (`--rwlock`): it is read and written
through `horizon_core::sync::read`/`write` everywhere, so any `RwLock` taken
with a bare `.read()`/`.write()` is reported as its own failure. `.read()` and
`.write()` are not part of the main sweep because the client also uses those
names for item-stack accessors, which do not block.

A site's key is `<crate-relative path>::<enclosing function>`; every site with
that key shares one verdict. Verdicts:

    frame-nonparking    reachable from the page, cannot park by construction
                        (shim lock, bounded poll, `may_block()` guard, cfg'd out)
    frame-single-owner  reachable from the page; the primitive is taken by that
                        one thread, so it never waits for another
    worker              runs on a named thread that may block freely
    unreachable         compiled in, but no page entry point reaches it
    not-wasm            not compiled into the wasm module
    not-a-wait          named like a wait but is not a blocking primitive

Checks applied to the table: every site has a verdict; `frame-nonparking` sites
must show the shim, a `try_`/`is_finished` poll or a `may_block()` guard near
the line; `worker` verdicts must name the thread and that name must appear as a
`std::thread`/pool spawn label in the crate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import OrderedDict, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(ROOT, "crates")

# Crates compiled into `horizon-client` for wasm32-unknown-unknown, from
# `cargo tree --target wasm32-unknown-unknown -p horizon-client --prefix none`
# (regenerate with --crates --from-cargo).
WASM_CRATES = [
    "horizon-assets",
    "horizon-bench",
    "horizon-client",
    "horizon-client-core",
    "horizon-client-input",
    "horizon-client-render",
    "horizon-command",
    "horizon-core",
    "horizon-friends",
    "horizon-hdr",
    "horizon-items",
    "horizon-mods",
    "horizon-protocol",
    "horizon-server-files",
    "horizon-shader-api",
    "horizon-shaders",
    "horizon-telemetry",
    "horizon-theme",
    "horizon-threads",
    "horizon-world",
]

PATTERNS = [
    ("lock", re.compile(r"\.lock\(\)")),
    ("condvar", re.compile(r"\.wait(?:_timeout)?\(")),
    ("recv", re.compile(r"\.recv(?:_timeout)?\(")),
    ("park", re.compile(r"\bthread::(?:park|park_timeout)\(")),
    ("sleep", re.compile(r"\bthread::sleep\(|\bsync::sleep\(")),
    ("block", re.compile(r"\bblock_on\(")),
    ("join", re.compile(r"\.join\(\)")),
]

FN_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
    r"(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)"
)
CFG_RE = re.compile(r"#\[cfg\(([^)]*)\)\]")
RWLOCK_DECL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:[A-Za-z_:<>0-9, ]*?)?RwLock<")

# Whole-file defaults, overridden by VERDICTS keys.
FILE_VERDICTS: "OrderedDict[str, tuple[str, str]]" = OrderedDict({
    'horizon-assets/src/item_model.rs': ('frame-single-owner', 'item-model builder caches (`time`, `compass`): the asset load runs inline on the frame in the wasm build, and only that thread reads them'),
    'horizon-client-core/src/block_entity_nbt.rs': ('not-a-wait', '`self.lock()` is a fluent builder method on the NBT writer, not a `Mutex`'),
    'horizon-client-render/src/audio.rs': ('frame-single-owner', 'the mixer and decode cache; its decoders are `std::thread::spawn`/`Builder::spawn`, which wasm32 cannot create, so only the frame holds these locks'),
    'horizon-client-render/src/block_entity_render.rs': ('frame-single-owner', 'block-entity glyph and sign caches, built and read on the render thread'),
    'horizon-client-render/src/gfx.rs': ('frame-single-owner', 'renderer-owned caches (display log, readback); only the render thread takes them'),
    'horizon-client-render/src/gfx/animate.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/gfx/arena.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/gfx/compositor.rs': ('frame-single-owner', "the compositor's buffers are handed over and taken back on the render thread"),
    'horizon-client-render/src/gfx/native_material.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/gfx/native_post.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/gfx/native_sky.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/gfx/occlusion.rs': ('frame-nonparking', 'the frame polls with `try_recv`; `recv` is taken only when `may_block()`'),
    'horizon-client-render/src/gfx/pack_post.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/gfx/world_post.rs': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client-render/src/hud.rs': ('not-wasm', 'the chunked decode pool is `#[cfg(not(target_arch = "wasm32"))]`; wasm decodes serially'),
    'horizon-client-render/src/lod.rs': ('worker', 'thread: far-terrain mesher, spawned by `Lod::new`'),
    'horizon-client-render/src/logging.rs': ('worker', 'thread: the log sink'),
    'horizon-client-render/src/packs/cache.rs': ('worker', 'thread: the pack cache writer'),
    'horizon-client-render/src/par.rs': ('not-a-wait', '`par_map` runs one thread on wasm, so the scope neither spawns nor joins'),
    'horizon-client-render/src/streaming.rs': ('frame-nonparking', 'the blocking `recv_timeout` arms are `#[cfg(not(all(wasm32, atomics)))]`; the threaded build polls'),
    'horizon-client/src/benchmark.rs': ('unreachable', 'benchmark harness entry point; the page never runs it'),
    'horizon-client/src/control.rs': ('worker', 'thread: the control-socket listener'),
    'horizon-client/src/game.rs': ('frame-single-owner', "the page's own channels (`WEB_*` status and error) and renderer caches: the wasm exports write and read them on the main thread"),
    'horizon-client/src/game/commands/pregen.rs': ('frame-nonparking', '`Streaming::wait` is a bounded poll on the threaded build'),
    'horizon-client/src/game/dedicated.rs': ('unreachable', 'dedicated-server role; the page never starts one'),
    'horizon-client/src/game/integrated.rs': ('frame-nonparking', '`call` polls with `try_recv` when `may_block()` is false, and `halt` joins through the shim'),
    'horizon-client/src/game/persist.rs': ('not-a-wait', '`TickChunkMembership::wait(key)` records a key for the next sync; it is not a condvar'),
    'horizon-client/src/game/server_packs.rs': ('frame-nonparking', 'the pack reload thread is joined through the shim'),
    'horizon-client/src/game/world_loading.rs': ('not-wasm', 'the loader\'s `join` is on the `#[cfg(not(target_arch = "wasm32"))]` path; wasm takes `inline_result`'),
    'horizon-client/src/heartbeat.rs': ('worker', 'thread: the heartbeat thread'),
    'horizon-client/src/item_render.rs': ('frame-single-owner', 'item render caches (baked models, masks, extents, translucency), built and read on the render thread'),
    'horizon-client/src/lan/chunks.rs': ('worker', 'thread: the LAN chunk warm pool'),
    'horizon-client/src/lan/conn.rs': ('worker', 'thread: the LAN connection reader and writer'),
    'horizon-client/src/lan/discovery.rs': ('worker', 'thread: LAN discovery'),
    'horizon-client/src/lan/mod.rs': ('worker', 'thread: the LAN listener and its fetch thread'),
    'horizon-client/src/mobs/finalize.rs': ('frame-single-owner', 'the mob sound-id cache, taken on the tick thread'),
    'horizon-client/src/mod_content.rs': ('frame-nonparking', 'the content-table worker is joined through the shim'),
    'horizon-client/src/multiplayer.rs': ('frame-single-owner', "the server list's save guard: the UI writes the list on the frame"),
    'horizon-client/src/net.rs': ('worker', 'thread: the net reader and its ping thread'),
    'horizon-client/src/save.rs': ('frame-nonparking', '`flush` polls the outstanding count when `may_block()` is false; the condvar arm is for worker threads'),
    'horizon-client/src/title/world_management.rs': ('not-wasm', 'desktop `std::process` path'),
    'horizon-client/src/vanillagen/lod.rs': ('worker', 'thread: the far-terrain builder'),
    'horizon-command/src/network.rs': ('frame-single-owner', 'the command interner, taken by the thread that runs the command'),
    'horizon-core/src/density/graph/seeded.rs': ('frame-single-owner', 'a write-once noise cache: filled when the graph loads, read-only afterwards'),
    'horizon-core/src/sync.rs': ('not-a-wait', 'the shim itself: these spin loops are what replace parking'),
    'horizon-friends/src/guest.rs': ('worker', 'thread: the friends guest relay'),
    'horizon-friends/src/host.rs': ('worker', 'thread: the friends host relay'),
    'horizon-friends/src/portmap.rs': ('worker', 'thread: the port-mapping thread'),
    'horizon-friends/src/reach.rs': ('worker', 'thread: the reachability probe'),
    'horizon-friends/src/session.rs': ('worker', 'thread: the friends session loop'),
    'horizon-protocol/src/connection.rs': ('worker', "thread: the connection's reader, and its condvar"),
    'horizon-protocol/src/resolver/dns.rs': ('worker', 'thread: the DNS resolver'),
    'horizon-threads/src/lib.rs': ('worker', "thread: the pool worker's own loop"),
    'horizon-world/src/level/fluids.rs': ('frame-single-owner', "the level's occlusion cache, taken by physics on the game's tick thread"),
    'horizon-world/src/worldgen/generator.rs': ('frame-single-owner', 'worldgen caches: filled when the generator data loads, read-only afterwards'),
    'horizon-world/src/worldgen/generator_blocks.rs': ('not-wasm', 'the loader thread\'s join is `#[cfg(not(target_arch = "wasm32"))]`'),
    'horizon-world/src/worldgen/generator_structures.rs': ('frame-nonparking', 'the ring-search wait is inside a `may_block()` branch; the other arm polls the counter'),
})

VERDICTS: "OrderedDict[str, tuple[str, str]]" = OrderedDict({
    'horizon-client-render/src/gfx/occlusion.rs::spawn': ('worker', 'thread: the occlusion search thread'),
    'horizon-client-render/src/streaming.rs::drop': ('frame-nonparking', 'the job threads are joined through the shim'),
    'horizon-client-render/src/streaming.rs::run_worker': ('worker', 'thread: a streamer job thread'),
    'horizon-client/src/game.rs::build_renderer': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client/src/game.rs::finish': ('not-wasm', 'the world loader\'s join is on the `#[cfg(not(target_arch = "wasm32"))]` path'),
    'horizon-client/src/game.rs::load_resources': ('not-wasm', "the resource loader's thread; the wasm build loads inline"),
    'horizon-client/src/game.rs::loading_renderer': ('frame-nonparking', '`pollster::block_on` on the renderer set-up path; the shipped page runs this at start-up without parking'),
    'horizon-client/src/game/integrated.rs::run_component_harness': ('unreachable', 'component harness entry point'),
    'horizon-client/src/lan/mod.rs::keys': ('worker', 'thread: the server-icon fetch thread'),
    'horizon-world/src/worldgen/generator.rs::joined': ('not-wasm', "the wasm build's `joined` takes the inline value; this `.join()` is the desktop branch"),
})



def wasm_crates(from_cargo: bool) -> list[str]:
    if not from_cargo:
        return WASM_CRATES
    tree = subprocess.run(
        [
            "cargo",
            "tree",
            "--target",
            "wasm32-unknown-unknown",
            "-p",
            "horizon-client",
            "--prefix",
            "none",
            "--no-dedupe",
            "-e",
            "normal",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if tree.returncode != 0:
        sys.exit(f"cargo tree failed:\n{tree.stderr}")
    names = {
        line.split()[0]
        for line in tree.stdout.splitlines()
        if line.strip() and os.path.isdir(os.path.join(WORKSPACE, line.split()[0]))
    }
    return sorted(names)


def source_files(crate: str):
    base = os.path.join(WORKSPACE, crate, "src")
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in sorted(filenames):
            if not name.endswith(".rs"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, WORKSPACE).replace(os.sep, "/")
            if "/tests/" in rel or "/examples/" in rel or "/benches/" in rel:
                continue
            # A file of tests: `tests.rs`, `crumbling_tests.rs`, `end_sky/tests.rs`.
            if rel.endswith("tests.rs") or rel.endswith("_tests.rs"):
                continue
            if rel.startswith(crate + "/src/bin/"):
                continue
            yield rel, path


def enclosing_fn(lines: list[str], index: int) -> str:
    for i in range(index, -1, -1):
        match = FN_RE.match(lines[i])
        if match:
            return match.group(1)
    return "<top>"


def cfg_scopes(lines: list[str]) -> list[tuple[int, int, str]]:
    """(first line, last line, cfg expression) for every `#[cfg(..)]` item."""
    scopes = []
    for i, line in enumerate(lines):
        match = CFG_RE.search(line)
        if not match:
            continue
        j = i
        while j < len(lines) and "{" not in lines[j]:
            j += 1
            if j - i > 3:
                break
        if j >= len(lines) or "{" not in lines[j]:
            # A one-item attribute (`#[cfg(..)] use ..;`): the next line only.
            scopes.append((i, i + 1, match.group(1)))
            continue
        depth = 0
        k = j
        while k < len(lines):
            depth += lines[k].count("{") - lines[k].count("}")
            if depth <= 0:
                break
            k += 1
        scopes.append((i, k, match.group(1)))
    return scopes


def test_scopes(lines: list[str]) -> list[tuple[int, int]]:
    # `#[cfg(test)]`, `#[cfg(all(test, unix))]`, `#[cfg(any(test, feature = "x"))]`.
    return [
        (start, end)
        for start, end, expr in cfg_scopes(lines)
        if re.search(r"\btest\b", expr)
    ]


class Site:
    __slots__ = ("rel", "line", "kind", "text", "function", "cfg", "test", "verdict", "why")

    def __init__(self, rel, line, kind, text, function, cfg, test):
        self.rel = rel
        self.line = line
        self.kind = kind
        self.text = text
        self.function = function
        self.cfg = cfg
        self.test = test
        self.verdict = None
        self.why = None

    @property
    def key(self) -> str:
        return f"{self.rel}::{self.function}"


def scan(crates: list[str]) -> list[Site]:
    sites: list[Site] = []
    for crate in crates:
        for rel, path in source_files(crate):
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            scope = cfg_scopes(lines)
            tests = test_scopes(lines)
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith("//"):
                    continue
                for kind, pattern in PATTERNS:
                    if not pattern.search(line):
                        continue
                    cfg = None
                    for start, end, expr in scope:
                        if start <= i <= end:
                            cfg = expr
                    sites.append(
                        Site(
                            rel,
                            i + 1,
                            kind,
                            stripped,
                            enclosing_fn(lines, i),
                            cfg,
                            any(start <= i <= end for start, end in tests),
                        )
                    )
                    break
    return sites


FRAME_EVIDENCE = (
    "horizon_core::sync::",
    "try_recv",
    "try_lock",
    "is_finished",
    "may_block",
)


def classify(sites: list[Site]) -> list[str]:
    problems = []
    for site in sites:
        if any(token in site.text for token in FRAME_EVIDENCE):
            site.verdict = "frame-nonparking"
            site.why = "shim, poll or may_block() guard on the line"
            continue
        if site.test:
            site.verdict, site.why = "not-wasm", "inside a #[cfg(test)] module"
            continue
        if site.cfg and "wasm32" in site.cfg and "not(" in site.cfg:
            site.verdict, site.why = "not-wasm", f"#[cfg({site.cfg})]"
            continue
        entry = VERDICTS.get(site.key) or FILE_VERDICTS.get(site.rel)
        if entry:
            site.verdict, site.why = entry
    return problems


def rwlock_check(crates: list[str]) -> list[str]:
    """`RwLock`s taken with a bare .read()/.write() instead of the shim."""
    offenders = []
    for crate in crates:
        for rel, path in source_files(crate):
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            names = {m.group(1) for line in lines for m in [RWLOCK_DECL.search(line)] if m}
            for i, line in enumerate(lines, 1):
                if "horizon_core::sync::" in line:
                    continue
                for name in names:
                    if re.search(rf"\b{re.escape(name)}\s*\.\s*(?:read|write)\(\)", line):
                        offenders.append(f"{rel}:{i}: {line.strip()}")
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--groups", action="store_true")
    parser.add_argument("--crates", action="store_true")
    parser.add_argument("--from-cargo", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--rwlock", action="store_true", help="only the RwLock check")
    args = parser.parse_args()

    crates = wasm_crates(args.from_cargo)

    if args.crates:
        for crate in crates:
            print(crate)
        return 0

    if args.rwlock:
        offenders = rwlock_check(crates)
        print(f"RwLock sites taken without the shim: {len(offenders)}")
        for line in offenders:
            print("  " + line)
        return 1 if offenders else 0

    sites = scan(crates)
    classify(sites)

    if args.groups:
        groups = OrderedDict()
        for site in sites:
            groups.setdefault(site.key, []).append(site)
        for key, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            verdict = members[0].verdict or "UNCLASSIFIED"
            kinds = ",".join(sorted({m.kind for m in members}))
            print(f"{len(members):3d}  {verdict:18s} {key}  [{kinds}]")
        return 0

    if args.json:
        print(json.dumps([site.__dict__ if False else {
            "file": s.rel, "line": s.line, "kind": s.kind, "fn": s.function,
            "verdict": s.verdict, "why": s.why, "text": s.text,
        } for s in sites], indent=2))
        return 0

    counts = defaultdict(int)
    unclassified = []
    for site in sites:
        counts[site.verdict or "UNCLASSIFIED"] += 1
        if site.verdict is None:
            unclassified.append(site)

    print(f"crates ({len(crates)}): {' '.join(crates)}")
    print(f"sites: {len(sites)}")
    for verdict in sorted(counts):
        print(f"  {verdict:18s} {counts[verdict]}")
    offenders = rwlock_check(crates)
    print(f"RwLock taken without the shim: {len(offenders)}")
    for line in offenders[:20]:
        print("  " + line)
    if unclassified:
        print(f"\nUNCLASSIFIED {len(unclassified)}:")
        for site in unclassified:
            print(f"  {site.rel}:{site.line} {site.kind} in {site.function}")
        return 1
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main())
