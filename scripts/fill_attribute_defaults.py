#!/usr/bin/env python3
"""Add the vanilla per-entity attribute suppliers to the packed web resources.

`pack_vfs.py` packs an already-baked `attributes.json` / `entity-types.json`.
Both files need the `DefaultAttributes` suppliers:

* `attributes.json.entities[entity][attribute]` (16-digit big-endian f64 bits)
  is what `horizon-world`'s `attributes::Catalog` needs before it can hand out
  an entity's attribute map at all: without an entry `LiveAttributes::new`
  fails with `UnknownEntity`, and the player cannot spawn.
* `entity-types.json[entity].attributes` (plain numbers) is the mob table
  `horizon-client`'s `mobs.rs` reads.

`scripts/gen_attribute_defaults.py` extracts the suppliers from Pumpkin's
generated entity table.  This script merges them into an existing
`HZVFS001` archive (`public/horizon-vfs.bin`) and rewrites it, so a bake made
without the decompiled Minecraft sources can be repaired without re-running
the whole resource pipeline.

    python3 scripts/fill_attribute_defaults.py [VFS] [DEFAULTS]

Both arguments default to `public/horizon-vfs.bin` and
`scripts/attribute-defaults.json`.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import struct
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VFS = ROOT / "public" / "horizon-vfs.bin"
DEFAULTS = ROOT / "scripts" / "attribute-defaults.json"

MAGIC = b"HZVFS001"
FLAG_RAW = 0
FLAG_GZIP = 1
FLAG_DIR = 2
FIXED_MTIME = 1_700_000_000
SKIP_GZIP_EXTS = {".png", ".ogg", ".zip", ".gz"}

HEADER = struct.Struct("<BQQQQ")


def read_archive(path: pathlib.Path):
    data = path.read_bytes()
    if data[:8] != MAGIC:
        raise SystemExit(f"{path}: not a HZVFS001 archive")
    count, = struct.unpack_from("<I", data, 8)
    off = 12
    entries = []
    for _ in range(count):
        plen, = struct.unpack_from("<H", data, off)
        off += 2
        name = data[off:off + plen].decode()
        off += plen
        flag, raw_len, mtime, data_off, data_len = HEADER.unpack_from(data, off)
        off += HEADER.size
        payload = data[data_off:data_off + data_len]
        entries.append([name, flag, raw_len, mtime, payload])
    return entries


def encode(raw_len: int, raw: bytes, name: str) -> tuple[int, bytes]:
    ext = pathlib.PurePosixPath(name).suffix.lower()
    if raw_len >= 128 and ext not in SKIP_GZIP_EXTS:
        comp = gzip.compress(raw, compresslevel=6, mtime=0)
        if len(comp) < raw_len * 0.90:
            return FLAG_GZIP, comp
    return FLAG_RAW, raw


def write_archive(path: pathlib.Path, entries) -> None:
    header_size = 12 + sum(2 + len(name.encode()) + HEADER.size for name, *_ in entries)
    out = bytearray()
    out += MAGIC
    out += struct.pack("<I", len(entries))
    cur = header_size
    for name, flag, raw_len, mtime, payload in entries:
        name_bytes = name.encode()
        out += struct.pack("<H", len(name_bytes))
        out += name_bytes
        out += HEADER.pack(flag, raw_len, mtime, cur, len(payload))
        cur += len(payload)
    for _name, _flag, _raw_len, _mtime, payload in entries:
        out += payload
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(out)
    tmp.replace(path)


def main() -> None:
    vfs = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else VFS
    defaults_path = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULTS
    suppliers: dict[str, dict[str, str]] = json.loads(defaults_path.read_text())

    entries = read_archive(vfs)
    by_name = {name: entry for entry, name in ((e, e[0]) for e in entries)}
    for wanted in ("/resources/attributes.json", "/resources/entity-types.json"):
        if wanted not in by_name:
            raise SystemExit(f"{vfs}: {wanted} missing")

    # attributes.json: the catalog's per-entity supplier bases.
    entry = by_name["/resources/attributes.json"]
    doc = json.loads(deflate(entry))
    known = {a["id"] for a in doc["attributes"]}
    entities = {}
    for entity, values in suppliers.items():
        if not values:
            continue
        unknown = set(values) - known
        if unknown:
            raise SystemExit(f"{entity}: attributes missing from the catalog: {sorted(unknown)}")
        entities[entity] = values
    doc["entities"] = entities
    raw = json.dumps(doc, separators=(",", ":"), sort_keys=False).encode()
    entry[1], entry[4] = encode(len(raw), raw, entry[0])
    entry[2], entry[3] = len(raw), FIXED_MTIME
    print(f"attributes.json: {len(entities)} entity suppliers, {len(known)} attributes")

    # entity-types.json: the same values as plain numbers for the mob table.
    entry = by_name["/resources/entity-types.json"]
    types = json.loads(deflate(entry))
    filled = 0
    for entity, values in suppliers.items():
        if entity in types and values:
            types[entity]["attributes"] = {
                name: struct.unpack(">d", bytes.fromhex(bits))[0] for name, bits in values.items()
            }
            filled += 1
    raw = json.dumps(types, separators=(",", ":"), sort_keys=True).encode()
    entry[1], entry[4] = encode(len(raw), raw, entry[0])
    entry[2], entry[3] = len(raw), FIXED_MTIME
    print(f"entity-types.json: {filled} entity types with attribute defaults")

    write_archive(vfs, entries)
    print(f"wrote {vfs} ({vfs.stat().st_size / (1024 * 1024):.1f} MB)")


def deflate(entry) -> bytes:
    flag, _raw_len, _mtime, payload = entry[1], entry[2], entry[3], entry[4]
    return gzip.decompress(payload) if flag == FLAG_GZIP else payload


if __name__ == "__main__":
    main()
