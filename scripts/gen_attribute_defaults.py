#!/usr/bin/env python3
"""Extract vanilla per-entity attribute suppliers into `scripts/attribute-defaults.json`.

`attributes.json` in the game resources carries a `entities` map: the base value
of every attribute an entity type's `DefaultAttributes` supplier supplies
(`minecraft:player` -> `minecraft:movement_speed` = 0.1, ...).  Without it
`LiveAttributes::new` answers `UnknownEntity` and the client cannot spawn its
player (or a mob).

The values come from Pumpkin's generated `EntityType` table, which is itself
generated from Mojang's `DefaultAttributes`/entity classes and tracks the same
Minecraft version this client targets (`DATA_VERSION` in
`crates/horizon-core/src/version.rs`).  Usage:

    python3 scripts/gen_attribute_defaults.py [PUMPKIN_SRC] [--fetch]

`PUMPKIN_SRC` defaults to `/tmp/pumpkin/Pumpkin-master`.  `--fetch` downloads
the master tarball with codeload first (the only GitHub host this sandbox can
reach).
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import struct
import subprocess
import sys
import tarfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "attribute-defaults.json"
TARBALL = "https://codeload.github.com/Pumpkin-MC/Pumpkin/tar.gz/refs/heads/master"
DEFAULT_SRC = pathlib.Path("/tmp/pumpkin/Pumpkin-master")

ATTR_CONST = re.compile(
    r"pub const ([A-Z0-9_]+): Self = Self \{\s*id: \d+,\s*default_value: [^,]+,\s*"
    r"name: \"([a-z0-9_:]+)\",",
    re.S,
)
ENTITY = re.compile(
    r"pub const ([A-Z0-9_]+): EntityType = EntityType \{(.*?)\n    \};\n",
    re.S,
)
SUPPLIER = re.compile(r"\(Attributes::([A-Z0-9_]+), ([0-9.eE+-]+)f64\)")
RESOURCE = re.compile(r"resource_name: \"([a-z0-9_]+)\",")


def fetch(dest: pathlib.Path) -> pathlib.Path:
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"fetching {TARBALL}")
    subprocess.run(["curl", "-sSL", "--max-time", "300", "-o", "/tmp/pumpkin-src.tar.gz", TARBALL], check=True)
    with tarfile.open("/tmp/pumpkin-src.tar.gz") as tar:
        prefix = tar.getnames()[0].split("/")[0]
        members = [m for m in tar.getmembers() if m.name.startswith(prefix + "/crates/pumpkin-data/src/generated/")]
        extracted = pathlib.Path("/tmp/pumpkin-extracted") / prefix
        tar.extractall("/tmp/pumpkin-extracted", members=members)
    return extracted


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    src = pathlib.Path(args[0]) if args else DEFAULT_SRC
    if "--fetch" in sys.argv or not (src / "crates/pumpkin-data/src/generated/attributes.rs").exists():
        src = fetch(src)

    generated = src / "crates/pumpkin-data/src/generated"
    attributes = dict(ATTR_CONST.findall((generated / "attributes.rs").read_text()))
    print(f"{len(attributes)} attributes")

    entities = {}
    for _const, body in ENTITY.findall((generated / "entity_type.rs").read_text()):
        resource = RESOURCE.search(body)
        if not resource:
            continue
        suppliers = {}
        for const, value in SUPPLIER.findall(body):
            name = attributes.get(const)
            if name is None:
                raise SystemExit(f"unknown attribute constant {const}")
            suppliers[name] = f"{struct.unpack('<Q', struct.pack('<d', float(value)))[0]:016x}"
        entities[f"minecraft:{resource.group(1)}"] = dict(sorted(suppliers.items()))

    entities = dict(sorted(entities.items()))
    supplied = sum(1 for v in entities.values() if v)
    print(f"{len(entities)} entity types, {supplied} with suppliers")
    OUT.write_text(json.dumps(entities, separators=(",", ":"), sort_keys=False) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    if "minecraft:player" not in entities or not entities["minecraft:player"]:
        raise SystemExit("player supplier missing")
    print("player:", len(entities["minecraft:player"]), "attributes")


if __name__ == "__main__":
    main()
