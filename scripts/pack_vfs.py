#!/usr/bin/env python3
"""
Packs `/tmp/horizon-resources` -> `/resources/...` and `/tmp/horizon-cache` -> `/game/.horizon-cache/...`
into a single `HZVFS001` virtual filesystem archive (`public/horizon-vfs.bin`).
"""
import gzip
import os
import shutil
import struct
import subprocess
import sys
import tempfile

MAGIC = b"HZVFS001"
FLAG_RAW = 0
FLAG_GZIP = 1
FLAG_DIR = 2
FIXED_MTIME = 1_700_000_000

SKIP_GZIP_EXTS = {".png", ".ogg", ".zip", ".gz"}


def collect_tree(src_root, vfs_prefix):
    dirs = []
    files = []
    for dp, dns, fns in os.walk(src_root):
        dns.sort()
        fns.sort()
        rel_dir = os.path.relpath(dp, src_root).replace("\\", "/")
        vdir = vfs_prefix if rel_dir == "." else f"{vfs_prefix}/{rel_dir}"
        dirs.append(vdir)
        for fn in fns:
            if fn.endswith(".tmp"):
                continue
            full = os.path.join(dp, fn)
            vpath = f"{vdir}/{fn}"
            files.append((vpath, full))
    return dirs, files


def fail(message):
    print(f"pack_vfs.py: {message}", file=sys.stderr)
    raise SystemExit(1)


def main():
    args = sys.argv[1:]
    audio_bitrate = None
    audio_long_bitrate = None
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--audio-bitrate" and i + 1 < len(args):
            i += 1
            audio_bitrate = args[i]
        elif args[i] == "--audio-long-bitrate" and i + 1 < len(args):
            i += 1
            audio_long_bitrate = args[i]
        else:
            positional.append(args[i])
        i += 1
    if len(positional) != 3:
        fail(
            "usage: pack_vfs.py RESOURCES_DIR CACHE_DIR OUT_PATH "
            "[--audio-bitrate 64k] [--audio-long-bitrate 96k]"
        )
    res_dir, cache_dir, out_path = positional

    # An empty pack "succeeds" and then the page cannot load anything, which is
    # much harder to diagnose than a refusal here. The game itself requires the
    # prepared install's manifest at the root of the pack (`assets.rs`,
    # `game.rs::preflight`), so a folder without it is the wrong folder.
    if not os.path.isdir(res_dir):
        fail(
            f"{res_dir} is not a directory.\n"
            "  Pass the prepared resource folder: the one holding\n"
            "  horizon-resources.json (.local/horizon-client/<version> of a\n"
            "  launcher install or of `horizon-prepare --prepare`)."
        )
    if not os.path.isfile(os.path.join(res_dir, "horizon-resources.json")):
        fail(
            f"{res_dir} has no horizon-resources.json, so it is not a prepared\n"
            "  resource folder. The game reads that manifest from the root of the\n"
            "  pack, so packing this folder would produce a pack that cannot load.\n"
            "  Find the real one with: find ~ -name horizon-resources.json"
        )
    if not os.path.isdir(cache_dir):
        print(f"pack_vfs.py: {cache_dir} is not a directory; packing no cache", file=sys.stderr)

    # The sound library dominates the pack, and Vorbis at 64 kbit/s is
    # transparent for one-shot effects. Re-encode into a scratch copy so the
    # prepared install the launcher uses keeps its full-quality audio.
    audio_scratch = None
    if audio_bitrate:
        audio_scratch = tempfile.mkdtemp(prefix="horizon-audio-")
        cmd = [
            sys.executable,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "compress_audio.py"),
            res_dir,
            audio_scratch,
            "--bitrate",
            audio_bitrate,
        ]
        if audio_long_bitrate:
            cmd += ["--long-bitrate", audio_long_bitrate]
        print("pack_vfs.py: " + " ".join(cmd), file=sys.stderr)
        subprocess.run(cmd, check=True)
        res_dir = audio_scratch

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    dirs1, files1 = collect_tree(res_dir, "/resources")
    dirs2, files2 = collect_tree(cache_dir, "/game/.horizon-cache")
    if not files1:
        fail(f"{res_dir} contains no files to pack")
    all_dirs = sorted(set(dirs1 + dirs2))
    all_files = sorted(files1 + files2, key=lambda x: x[0])

    prepared_entries = []
    for d in all_dirs:
        prepared_entries.append((d.encode("utf-8"), FLAG_DIR, 0, FIXED_MTIME, b""))

    raw_total = 0
    for vpath, full in all_files:
        raw = open(full, "rb").read()
        raw_len = len(raw)
        raw_total += raw_len
        ext = os.path.splitext(vpath)[1].lower()
        if raw_len >= 128 and ext not in SKIP_GZIP_EXTS:
            comp = gzip.compress(raw, compresslevel=6, mtime=0)
            if len(comp) < raw_len * 0.90:
                payload = comp
                flag = FLAG_GZIP
            else:
                payload = raw
                flag = FLAG_RAW
        else:
            payload = raw
            flag = FLAG_RAW
        prepared_entries.append((vpath.encode("utf-8"), flag, raw_len, FIXED_MTIME, payload))

    header_size = 12
    for path_bytes, _, _, _, _ in prepared_entries:
        header_size += 2 + len(path_bytes) + 1 + 8 * 4

    with open(out_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack("<I", len(prepared_entries)))
        cur_offset = header_size
        for path_bytes, flag, raw_len, mtime, payload in prepared_entries:
            data_len = len(payload)
            f.write(struct.pack("<H", len(path_bytes)))
            f.write(path_bytes)
            f.write(struct.pack("<BQQQQ", flag, raw_len, mtime, cur_offset, data_len))
            cur_offset += data_len
        for _, _, _, _, payload in prepared_entries:
            if payload:
                f.write(payload)

    if audio_scratch:
        shutil.rmtree(audio_scratch, ignore_errors=True)

    total_out = os.path.getsize(out_path)
    print(
        f"Packed {len(all_files)} files + {len(all_dirs)} dirs: "
        f"{raw_total / (1024 * 1024):.1f} MB raw -> {total_out / (1024 * 1024):.1f} MB in {out_path}"
    )


if __name__ == "__main__":
    main()
