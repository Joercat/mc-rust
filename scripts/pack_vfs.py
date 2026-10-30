#!/usr/bin/env python3
"""
Packs `/tmp/horizon-resources` -> `/resources/...` and `/tmp/horizon-cache` -> `/game/.horizon-cache/...`
into a single `HZVFS001` virtual filesystem archive (`public/horizon-vfs.bin`).
"""
import gzip
import os
import struct
import sys

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


def main():
    res_dir = sys.argv[1]
    cache_dir = sys.argv[2]
    out_path = sys.argv[3]

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    dirs1, files1 = collect_tree(res_dir, "/resources")
    dirs2, files2 = collect_tree(cache_dir, "/game/.horizon-cache")
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

    total_out = os.path.getsize(out_path)
    print(
        f"Packed {len(all_files)} files + {len(all_dirs)} dirs: "
        f"{raw_total / (1024 * 1024):.1f} MB raw -> {total_out / (1024 * 1024):.1f} MB in {out_path}"
    )


if __name__ == "__main__":
    main()
