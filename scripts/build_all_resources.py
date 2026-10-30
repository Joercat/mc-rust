#!/usr/bin/env python3
"""
Builds the complete Horizon 26.3 (`data_version = 5023`, `schema = 7`) `<resources>/`
directory from upstream vanilla 26.3 sources (`/tmp/mcmeta-*`, `/tmp/pumpkin/assets`,
`/tmp/mc-26.3-src`) and `/tmp/horizon-baked/{animations,entity-models}.json`, plus
`/tmp/horizon-nbt-inputs.json` for `horizon-web-prepare` to emit the 6 binary NBT tables.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import shutil
import struct
from pathlib import Path

DATA_VERSION = 5023
PROTOCOL_VERSION = 777
VERSION_STR = "26.3"

MCMETA_ASSETS = Path("/tmp/mcmeta-assets")
MCMETA_DATA = Path("/tmp/mcmeta-data")
MCMETA_SUMMARY = Path("/tmp/mcmeta-summary")
MCMETA_REGISTRIES = Path("/tmp/mcmeta-registries")
PUMPKIN_ASSETS = Path("/tmp/pumpkin/assets")
MC_SRC = Path("/tmp/mc-26.3-src/src/net/minecraft")
BAKED_DIR = Path("/tmp/horizon-baked")

OUT_ROOT = Path("/tmp/horizon-resources")
NBT_INPUTS_PATH = Path("/tmp/horizon-nbt-inputs.json")

# Pumpkin `state_flags` bits (from pumpkin-data/src/block_state.rs)
PF_IS_AIR = 1 << 0
PF_BURNABLE = 1 << 1
PF_TOOL_REQUIRED = 1 << 2
PF_SIDED_TRANSPARENCY = 1 << 3
PF_REPLACEABLE = 1 << 4
PF_IS_LIQUID = 1 << 5
PF_IS_SOLID = 1 << 6
PF_IS_FULL_CUBE = 1 << 7
PF_IS_SOLID_BLOCK = 1 << 8
PF_HAS_RANDOM_TICKS = 1 << 9
PF_IS_SOLID_RENDER = 1 << 10
PF_CAN_OCCLUDE = 1 << 11
PF_HAS_ANALOG_OUTPUT = 1 << 12

# Pumpkin `side_flags` bits (Down=0, Up=1, North=2, South=3, West=4, East=5)
PF_DOWN_CENTER = 1 << 6
PF_UP_CENTER = 1 << 7

WORLDGEN_BUNDLES = [
    ("noises", "worldgen/noise"),
    ("densities", "worldgen/density_function"),
    ("rules", "worldgen/material_rule"),
    ("conditions", "worldgen/material_condition"),
    ("settings", "worldgen/noise_settings"),
    ("carvers", "worldgen/carver"),
    ("biomes", "worldgen/biome"),
    ("world_presets", "worldgen/world_preset"),
    ("features", "worldgen/feature"),
    ("placed_features", "worldgen/placed_feature"),
    ("state_providers", "worldgen/block_state_provider"),
    ("biome_tags", "tags/worldgen/biome"),
    ("feature_tags", "tags/worldgen/feature"),
    ("block_tags", "tags/block"),
    ("fluid_tags", "tags/fluid"),
    ("processor_lists", "worldgen/processor_list"),
    ("template_pools", "worldgen/template_pool"),
    ("structures", "worldgen/structure"),
    ("structure_sets", "worldgen/structure_set"),
]

FLUID_KINDS = [
    "minecraft:empty",
    "minecraft:flowing_water",
    "minecraft:water",
    "minecraft:flowing_lava",
    "minecraft:lava",
]


def f32_val(x: float) -> float:
    return struct.unpack(">f", struct.pack(">f", float(x)))[0]


def f32_hex(x: float) -> str:
    return f"{struct.unpack('>I', struct.pack('>f', float(x)))[0]:08x}"


def f64_hex(x: float) -> str:
    return f"{struct.unpack('>Q', struct.pack('>d', float(x)))[0]:016x}"


def parse_java_num(expr: str, consts: dict[str, float] | None = None) -> float:
    expr = expr.strip()
    if consts and expr in consts:
        return consts[expr]
    # Replace known constants in simple arithmetic expressions
    if consts:
        for k, v in sorted(consts.items(), key=lambda kv: -len(kv[0])):
            expr = re.sub(rf"\b{re.escape(k)}\b", repr(v), expr)
    expr = expr.replace("(double)", "").replace("(float)", "")
    # Convert Java float literals `0.42F` -> `f32_val(0.42)` so IEEE-754 promotion matches JVM
    expr = re.sub(
        r"(-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)[fF]\b",
        r"f32_val(\1)",
        expr,
    )
    expr = re.sub(
        r"(-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)[dD]\b",
        r"\1",
        expr,
    )
    return float(eval(expr, {"__builtins__": {}, "f32_val": f32_val, "Math": math}))


def strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def split_top_args(s: str) -> list[str]:
    out = []
    cur = []
    dp = db = dg = 0
    ins = False
    i = 0
    while i < len(s):
        ch = s[i]
        if ins:
            cur.append(ch)
            if ch == "\\" and i + 1 < len(s):
                i += 1
                cur.append(s[i])
            elif ch == '"':
                ins = False
        else:
            if ch == '"':
                ins = True
                cur.append(ch)
            elif ch == "(":
                dp += 1
                cur.append(ch)
            elif ch == ")":
                dp -= 1
                cur.append(ch)
            elif ch == "{":
                db += 1
                cur.append(ch)
            elif ch == "}":
                db -= 1
                cur.append(ch)
            elif ch == "<":
                dg += 1
                cur.append(ch)
            elif ch == ">":
                dg -= 1
                cur.append(ch)
            elif ch == "," and dp == 0 and db == 0 and dg == 0:
                out.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def write_json(path: Path, obj, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, separators=(",", ":") if indent is None else None, indent=indent)
    path.write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_assets_and_data() -> int:
    print("[1/9] Copying vanilla 26.3 assets/ and data/ ...")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PUMPKIN_ASSETS / "default_icon.png", OUT_ROOT / "pack.png")
    count = 1
    assets_src = MCMETA_ASSETS / "assets"
    for src in assets_src.rglob("*"):
        if not src.is_file():
            continue
        # Skip bulky .ogg streams (SoundRegistry drops missing .ogg files cleanly)
        if src.suffix == ".ogg":
            continue
        if "assets/minecraft/lang/" in src.as_posix() and src.name != "en_us.json":
            continue
        rel = src.relative_to(MCMETA_ASSETS)
        dst = OUT_ROOT / rel
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        count += 1

    data_src = MCMETA_DATA / "data"
    for src in data_src.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(MCMETA_DATA)
        dst = OUT_ROOT / rel
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        count += 1
    return count


def parse_sound_events() -> dict[str, str]:
    """Map SoundEvents.<CONST> -> 'minecraft:<sound.id>'."""
    src = strip_comments((MC_SRC / "sounds/SoundEvents.java").read_text())
    out = {}
    for m in re.finditer(
        r'SoundEvent(?:\.Reference)?\s+([A-Z0-9_]+)\s*=\s*register(?:ForHolder)?\s*\(\s*"([^"]+)"',
        src,
    ):
        val = m.group(2)
        out[m.group(1)] = val if ":" in val else f"minecraft:{val}"
    # Fallback scan for any Holder.Reference<SoundEvent>
    for m in re.finditer(
        r'([A-Z0-9_]+)\s*=\s*register(?:ForHolder)?\s*\(\s*(?:Identifier\.withDefaultNamespace\(\s*)?"([^"]+)"',
        src,
    ):
        val = m.group(2)
        out.setdefault(m.group(1), val if ":" in val else f"minecraft:{val}")
    return out


def parse_set_and_wood_types(sound_events: dict[str, str]):
    src_bs = strip_comments((MC_SRC / "world/level/block/state/properties/BlockSetType.java").read_text())
    set_types: dict[str, dict] = {}
    for m in re.finditer(r"BlockSetType\s+([A-Z0-9_]+)\s*=\s*register\s*\(\s*new\s+BlockSetType\s*\((.*?)\)\s*\)", src_bs, flags=re.S):
        cname = m.group(1)
        args = split_top_args(m.group(2))
        if len(args) == 2:
            sname = args[0].strip('"')
            set_types[cname] = {
                "name": sname,
                "canOpenByHand": True,
                "canOpenByWindCharge": True,
                "canButtonBeActivatedByArrows": True,
                "pressurePlateSensitivity": "EVERYTHING",
                "doorClose": sound_events.get("WOODEN_DOOR_CLOSE", "minecraft:block.wooden_door.close"),
                "doorOpen": sound_events.get("WOODEN_DOOR_OPEN", "minecraft:block.wooden_door.open"),
                "trapdoorClose": sound_events.get("WOODEN_TRAPDOOR_CLOSE", "minecraft:block.wooden_trapdoor.close"),
                "trapdoorOpen": sound_events.get("WOODEN_TRAPDOOR_OPEN", "minecraft:block.wooden_trapdoor.open"),
                "pressurePlateClickOff": sound_events.get("WOODEN_PRESSURE_PLATE_CLICK_OFF", "minecraft:block.wooden_pressure_plate.click_off"),
                "pressurePlateClickOn": sound_events.get("WOODEN_PRESSURE_PLATE_CLICK_ON", "minecraft:block.wooden_pressure_plate.click_on"),
                "buttonClickOff": sound_events.get("WOODEN_BUTTON_CLICK_OFF", "minecraft:block.wooden_button.click_off"),
                "buttonClickOn": sound_events.get("WOODEN_BUTTON_CLICK_ON", "minecraft:block.wooden_button.click_on"),
            }
        elif len(args) >= 14:
            def snd(a: str) -> str:
                k = a.split(".")[-1].strip()
                return sound_events.get(k, f"minecraft:{k.lower()}")
            set_types[cname] = {
                "name": args[0].strip('"'),
                "canOpenByHand": args[1] == "true",
                "canOpenByWindCharge": args[2] == "true",
                "canButtonBeActivatedByArrows": args[3] == "true",
                "pressurePlateSensitivity": args[4].split(".")[-1].strip(),
                "doorClose": snd(args[6]),
                "doorOpen": snd(args[7]),
                "trapdoorClose": snd(args[8]),
                "trapdoorOpen": snd(args[9]),
                "pressurePlateClickOff": snd(args[10]),
                "pressurePlateClickOn": snd(args[11]),
                "buttonClickOff": snd(args[12]),
                "buttonClickOn": snd(args[13]),
            }
        elif len(args) == 1:
            sname = args[0].strip('"')
            set_types[cname] = {
                "name": sname,
                "canOpenByHand": True,
                "canOpenByWindCharge": True,
                "canButtonBeActivatedByArrows": True,
                "pressurePlateSensitivity": "EVERYTHING",
                "doorClose": sound_events.get("WOODEN_DOOR_CLOSE", "minecraft:block.wooden_door.close"),
                "doorOpen": sound_events.get("WOODEN_DOOR_OPEN", "minecraft:block.wooden_door.open"),
                "trapdoorClose": sound_events.get("WOODEN_TRAPDOOR_CLOSE", "minecraft:block.wooden_trapdoor.close"),
                "trapdoorOpen": sound_events.get("WOODEN_TRAPDOOR_OPEN", "minecraft:block.wooden_trapdoor.open"),
                "pressurePlateClickOff": sound_events.get("WOODEN_PRESSURE_PLATE_CLICK_OFF", "minecraft:block.wooden_pressure_plate.click_off"),
                "pressurePlateClickOn": sound_events.get("WOODEN_PRESSURE_PLATE_CLICK_ON", "minecraft:block.wooden_pressure_plate.click_on"),
                "buttonClickOff": sound_events.get("WOODEN_BUTTON_CLICK_OFF", "minecraft:block.wooden_button.click_off"),
                "buttonClickOn": sound_events.get("WOODEN_BUTTON_CLICK_ON", "minecraft:block.wooden_button.click_on"),
            }
    # Also match `register(new BlockSetType("oak"))` single-arg wood set types
    for m in re.finditer(r'BlockSetType\s+([A-Z0-9_]+)\s*=\s*register\s*\(\s*new\s+BlockSetType\s*\(\s*"([^"]+)"\s*\)\s*\)', src_bs):
        cname, sname = m.group(1), m.group(2)
        set_types.setdefault(cname, {
            "name": sname,
            "canOpenByHand": True,
            "canOpenByWindCharge": True,
            "canButtonBeActivatedByArrows": True,
            "pressurePlateSensitivity": "EVERYTHING",
            "doorClose": sound_events.get("WOODEN_DOOR_CLOSE", "minecraft:block.wooden_door.close"),
            "doorOpen": sound_events.get("WOODEN_DOOR_OPEN", "minecraft:block.wooden_door.open"),
            "trapdoorClose": sound_events.get("WOODEN_TRAPDOOR_CLOSE", "minecraft:block.wooden_trapdoor.close"),
            "trapdoorOpen": sound_events.get("WOODEN_TRAPDOOR_OPEN", "minecraft:block.wooden_trapdoor.open"),
            "pressurePlateClickOff": sound_events.get("WOODEN_PRESSURE_PLATE_CLICK_OFF", "minecraft:block.wooden_pressure_plate.click_off"),
            "pressurePlateClickOn": sound_events.get("WOODEN_PRESSURE_PLATE_CLICK_ON", "minecraft:block.wooden_pressure_plate.click_on"),
            "buttonClickOff": sound_events.get("WOODEN_BUTTON_CLICK_OFF", "minecraft:block.wooden_button.click_off"),
            "buttonClickOn": sound_events.get("WOODEN_BUTTON_CLICK_ON", "minecraft:block.wooden_button.click_on"),
        })

    src_wt = strip_comments((MC_SRC / "world/level/block/state/properties/WoodType.java").read_text())
    wood_types: dict[str, dict] = {}
    for m in re.finditer(r"WoodType\s+([A-Z0-9_]+)\s*=\s*register\s*\(\s*new\s+WoodType\s*\((.*?)\)\s*\)", src_wt, flags=re.S):
        cname = m.group(1)
        args = split_top_args(m.group(2))
        sname = args[0].strip('"')
        bs_key = args[1].split(".")[-1].strip()
        st = set_types.get(bs_key, set_types.get("OAK"))
        if len(args) >= 6:
            fc = sound_events.get(args[4].split(".")[-1].strip(), "minecraft:block.fence_gate.close")
            fo = sound_events.get(args[5].split(".")[-1].strip(), "minecraft:block.fence_gate.open")
        else:
            fc = sound_events.get("FENCE_GATE_CLOSE", "minecraft:block.fence_gate.close")
            fo = sound_events.get("FENCE_GATE_OPEN", "minecraft:block.fence_gate.open")
        wood_types[cname] = {
            "name": sname,
            "setType": st,
            "fenceGateClose": fc,
            "fenceGateOpen": fo,
        }
    return set_types, wood_types


def parse_block_and_item_classes(sound_events: dict[str, str], set_types: dict, wood_types: dict):
    """Scan all Java block & item classes + Blocks.java + Items.java."""
    # 1. Scan all class declarations under world/level/block and world/item
    super_of: dict[str, str] = {}
    ifaces_of: dict[str, list[str]] = {}
    methods_of: dict[str, set[str]] = {}
    class_src: dict[str, str] = {}

    for root_sub in [MC_SRC / "world/level/block", MC_SRC / "world/item"]:
        for jf in root_sub.rglob("*.java"):
            s = strip_comments(jf.read_text())
            for cm in re.finditer(
                r"\b(?:public|protected|private)?\s*(?:abstract\s+|final\s+)*(?:class|interface)\s+([A-Za-z_][A-Za-z0-9_]*)(?:<[^>]+>)?(?:\s+extends\s+([A-Za-z0-9_., <>]+?))?(?:\s+implements\s+([A-Za-z0-9_., <>]+?))?\s*\{",
                s,
            ):
                cname = cm.group(1)
                ext = cm.group(2)
                imp = cm.group(3)
                if ext:
                    # For interfaces `extends A, B`, first is super, rest are ifaces
                    parts = [re.sub(r"<.*?>", "", x).strip().split(".")[-1] for x in split_top_args(ext)]
                    super_of[cname] = parts[0]
                    if len(parts) > 1:
                        ifaces_of.setdefault(cname, []).extend(parts[1:])
                if imp:
                    parts = [re.sub(r"<.*?>", "", x).strip().split(".")[-1] for x in split_top_args(imp)]
                    ifaces_of.setdefault(cname, []).extend(parts)
                class_src[cname] = s
                # Find declared method names
                mset = set()
                for mm in re.finditer(
                    r"\b(?:public|protected)\s+(?:[A-Za-z0-9_<>\[\]., ?]+\s+)+([a-z][A-Za-z0-9_]*)\s*\(",
                    s,
                ):
                    mset.add(mm.group(1))
                methods_of[cname] = mset

    def class_chain(cname: str) -> list[str]:
        chain = []
        cur = cname
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = super_of.get(cur)
        if "BlockBehaviour" not in chain and ("Block" in chain or cname.endswith("Block")):
            chain.append("BlockBehaviour")
        return chain

    def all_interfaces(cname: str) -> list[str]:
        res = []
        queue = []
        for c in class_chain(cname):
            queue.extend(ifaces_of.get(c, []))
        seen = set()
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            res.append(cur)
            if cur in super_of:
                queue.append(super_of[cur])
            queue.extend(ifaces_of.get(cur, []))
        return res

    def declaring_class(cname: str, method: str, default: str | None = None) -> str | None:
        for c in class_chain(cname):
            if method in methods_of.get(c, set()):
                return c
        return default

    # Parse BlockTags.<CONST> -> "minecraft:<tag>"
    bt_src = strip_comments((MC_SRC / "tags/BlockTags.java").read_text())
    block_tag_consts = {}
    for m in re.finditer(r'TagKey<Block>\s+([A-Z0-9_]+)\s*=\s*create\s*\(\s*"([^"]+)"\s*\)', bt_src):
        v = m.group(2)
        block_tag_consts[m.group(1)] = v if ":" in v else f"minecraft:{v}"

    # Parse TreeGrower.<CONST> -> "minecraft:..." or name
    tg_path = MC_SRC / "world/level/block/grower/TreeGrower.java"
    tree_growers = {}
    if tg_path.is_file():
        tg_src = strip_comments(tg_path.read_text())
        for m in re.finditer(r'TreeGrower\s+([A-Z0-9_]+)\s*=\s*new\s+TreeGrower\s*\(\s*"([^"]+)"', tg_src):
            tree_growers[m.group(1)] = m.group(2)

    # Parse helper factory methods in Blocks.java (e.g. `stair(Block)`, `woodenButton(BlockSetType)`, `flowerPot(Block)`, etc.)
    blocks_src = strip_comments((MC_SRC / "world/level/block/Blocks.java").read_text())
    helper_cls: dict[str, tuple[str, str]] = {}
    for hm in re.finditer(
        r"private\s+static\s+([A-Za-z0-9_]+)\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)\s*\{",
        blocks_src,
    ):
        ret_c, hname, hparams = hm.group(1), hm.group(2), hm.group(3)
        # Extract body
        j = hm.end()
        db = 1
        while j < len(blocks_src) and db > 0:
            if blocks_src[j] == "{":
                db += 1
            elif blocks_src[j] == "}":
                db -= 1
            j += 1
        body = blocks_src[hm.end():j - 1]
        new_m = re.search(r"new\s+([A-Za-z0-9_]+)\s*\(", body)
        actual_cls = new_m.group(1) if new_m else ret_c
        helper_cls[hname] = (actual_cls, body)

    # Parse BlockItemIds.java + BlockIds.java + ItemIds.java
    ref_ids: dict[str, str] = {}
    for ref_file in ["BlockItemIds.java", "BlockIds.java", "ItemIds.java"]:
        rp = MC_SRC / "references" / ref_file
        if rp.is_file():
            rsrc = strip_comments(rp.read_text())
            for rm in re.finditer(r'\b([A-Z0-9_]+)\s*=\s*(?:BlockItemId\.)?create\s*\(\s*"([^"]+)"\s*\)', rsrc):
                ref_ids[rm.group(1)] = f"minecraft:{rm.group(2)}"

    DYE_COLORS = [
        "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
        "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
    ]

    def scan_stmt_end(s: str, start: int) -> int:
        j = start
        dp = db = 0
        ins = False
        while j < len(s):
            ch = s[j]
            if ins:
                if ch == "\\" and j + 1 < len(s):
                    j += 2
                    continue
                if ch == '"':
                    ins = False
            else:
                if ch == '"':
                    ins = True
                elif ch == "(":
                    dp += 1
                elif ch == ")":
                    dp -= 1
                elif ch == "{":
                    db += 1
                elif ch == "}":
                    db -= 1
                elif ch == ";" and dp == 0 and db == 0:
                    break
            j += 1
        return j

    # First pass over Blocks.java: map constant name -> block id ("minecraft:stone")
    const_to_block_id: dict[str, str] = {}
    block_regs: dict[str, str] = {}
    for m in re.finditer(r"public\s+static\s+final\s+(Block|ColorCollection<Block>|WeatheringCopperCollection<Block>)\s+([A-Z0-9_]+)\s*=\s*", blocks_src):
        bkind, cname = m.group(1), m.group(2)
        j = scan_stmt_end(blocks_src, m.end())
        expr = blocks_src[m.end():j].strip()
        if bkind == "Block":
            id_m = re.search(r"(?:BlockItemIds|BlockIds)\.([A-Z0-9_]+)", expr)
            sm = re.search(r'"([a-z0-9_/]+)"', expr)
            if id_m and id_m.group(1) in ref_ids:
                bid = ref_ids[id_m.group(1)]
            elif sm:
                bid = f"minecraft:{sm.group(1)}"
            else:
                bid = f"minecraft:{cname.lower()}"
            const_to_block_id[cname] = bid
            block_regs[bid] = expr
        elif bkind == "ColorCollection<Block>":
            # Expand 16 dyed block IDs
            suffix = {
                "BED": "bed", "WOOL": "wool", "WOOL_STAIRS": "wool_stairs", "WOOL_SLAB": "wool_slab",
                "STAINED_GLASS": "stained_glass", "DYED_TERRACOTTA": "terracotta",
                "STAINED_GLASS_PANE": "stained_glass_pane", "CARPET": "carpet",
                "BANNER": "banner", "WALL_BANNER": "wall_banner",
                "DYED_SHULKER_BOX": "shulker_box", "GLAZED_TERRACOTTA": "glazed_terracotta",
                "CONCRETE": "concrete", "CONCRETE_STAIRS": "concrete_stairs",
                "CONCRETE_SLAB": "concrete_slab", "CONCRETE_POWDER": "concrete_powder",
                "DYED_CANDLE": "candle", "DYED_CANDLE_CAKE": "candle_cake",
            }.get(cname, cname.lower())
            for col in DYE_COLORS:
                bid = f"minecraft:{col}_{suffix}"
                extra = ""
                if cname == "CONCRETE_POWDER":
                    extra = f" new ConcretePowderBlock(Blocks.{col.upper()}_CONCRETE, p)"
                    const_to_block_id[f"{col.upper()}_CONCRETE"] = f"minecraft:{col}_concrete"
                elif cname in ("WOOL_STAIRS", "CONCRETE_STAIRS"):
                    base_s = "wool" if "WOOL" in cname else "concrete"
                    const_to_block_id[f"{col.upper()}_{base_s.upper()}"] = f"minecraft:{col}_{base_s}"
                    extra = f" new StairBlock(Blocks.{col.upper()}_{base_s.upper()}, p)"
                elif cname in ("WOOL_SLAB", "CONCRETE_SLAB"):
                    extra = " new SlabBlock(p)"
                elif cname == "DYED_CANDLE_CAKE":
                    const_to_block_id[f"{col.upper()}_CANDLE"] = f"minecraft:{col}_candle"
                    extra = f" new CandleCakeBlock(Blocks.{col.upper()}_CANDLE, p)"
                block_regs[bid] = expr + extra
        elif bkind == "WeatheringCopperCollection<Block>":
            base_id = cname.lower()
            # Find waxed class vs weathering class in expr
            # WeatheringCopperCollection.registerBlocks(ids, Blocks::register, waxedFactory, weatheringFactory, props)
            args = split_top_args(expr[expr.find("(") + 1:expr.rfind(")")]) if "(" in expr else []
            waxed_expr = args[2] if len(args) >= 4 else expr
            weath_expr = args[3] if len(args) >= 4 else expr
            for prefix, wstate in [("", "UNAFFECTED"), ("exposed_", "EXPOSED"), ("weathered_", "WEATHERED"), ("oxidized_", "OXIDIZED")]:
                if base_id == "copper_block" and prefix:
                    unwaxed_short = f"{prefix}copper"
                else:
                    unwaxed_short = f"{prefix}{base_id}"
                waxed_short = f"waxed_{unwaxed_short}"
                block_regs[f"minecraft:{unwaxed_short}"] = weath_expr + f" WeatherState.{wstate}"
                block_regs[f"minecraft:{waxed_short}"] = waxed_expr + f" WeatherState.{wstate}" 

    # Second pass: resolve class + constructor fields for each block
    block_info: dict[str, dict] = {}
    for bid, expr in block_regs.items():
        cls = "Block"
        # Check `ClassName::new`
        ref_m = re.search(r"\b([A-Za-z0-9_]+)::new\b", expr)
        new_m = re.search(r"\bnew\s+([A-Za-z0-9_]+)\s*(?:<[^>]*>)?\s*\(", expr)
        if new_m:
            cls = new_m.group(1)
        elif ref_m:
            cls = ref_m.group(1)
        else:
            # Check helper call `register("...", p -> helper(...), ...)` or `register("...", Blocks::helper, ...)`
            for hname, (hcls, hbody) in helper_cls.items():
                if re.search(rf"\b{re.escape(hname)}\s*\(|Blocks::{re.escape(hname)}\b", expr):
                    cls = hcls
                    expr = expr + " /*HELPER*/ " + hbody
                    break

        chain = class_chain(cls)
        ifaces = all_interfaces(cls)
        fields: dict = {}

        # Extract WoodType / BlockSetType
        wt_m = re.search(r"\bWoodType\.([A-Z0-9_]+)\b", expr)
        bst_m = re.search(r"\bBlockSetType\.([A-Z0-9_]+)\b", expr)
        if wt_m and wt_m.group(1) in wood_types:
            fields["type"] = wood_types[wt_m.group(1)]
        elif bst_m and bst_m.group(1) in set_types:
            fields["type"] = set_types[bst_m.group(1)]
        elif any(c in chain for c in ("DoorBlock", "TrapDoorBlock", "ButtonBlock", "PressurePlateBlock", "FenceGateBlock", "StandingSignBlock", "WallSignBlock", "CeilingHangingSignBlock", "WallHangingSignBlock")):
            # Infer from block name if helper passed wood/set type as param
            short = bid.removeprefix("minecraft:")
            matched = False
            for wname, wobj in sorted(wood_types.items(), key=lambda kv: -len(kv[1]["name"])):
                if short.startswith(wobj["name"] + "_"):
                    if "FenceGateBlock" in chain or "SignBlock" in cls:
                        fields["type"] = wobj
                    else:
                        fields["type"] = wobj["setType"]
                    matched = True
                    break
            if not matched:
                for bname, bobj in sorted(set_types.items(), key=lambda kv: -len(kv[1]["name"])):
                    if short.startswith(bobj["name"] + "_") or (bname == "STONE" and short in ("stone_button", "stone_pressure_plate", "polished_blackstone_button", "polished_blackstone_pressure_plate")):
                        fields["type"] = bobj
                        break
                if "type" not in fields:
                    if "iron" in short:
                        fields["type"] = set_types["IRON"]
                    elif "copper" in short or "waxed_" in short:
                        fields["type"] = set_types["COPPER"]
                    elif "heavy_weighted" in short:
                        fields["type"] = set_types["IRON"]
                    elif "light_weighted" in short:
                        fields["type"] = set_types["GOLD"]
                    else:
                        fields["type"] = wood_types["OAK"] if "FenceGateBlock" in chain else set_types["OAK"]

        # Weathering state
        ws_m = re.search(r"WeatherState\.([A-Z0-9_]+)", expr)
        if ws_m:
            fields["weatherState"] = ws_m.group(1)
        elif "WeatheringCopper" in ifaces or "CopperGolemStatueBlock" in chain:
            short = bid.removeprefix("minecraft:").removeprefix("waxed_")
            if short.startswith("oxidized_"):
                fields["weatherState"] = "OXIDIZED"
            elif short.startswith("weathered_"):
                fields["weatherState"] = "WEATHERED"
            elif short.startswith("exposed_"):
                fields["weatherState"] = "EXPOSED"
            else:
                fields["weatherState"] = "UNAFFECTED"

        # Pressure plate / button ticks & maxWeight
        if "ButtonBlock" in chain:
            tm = re.search(r",\s*(\d+)\s*[,)]", expr)
            fields["ticksToStayPressed"] = int(tm.group(1)) if tm else (30 if fields.get("type", {}).get("canButtonBeActivatedByArrows") else 20)
        if "WeightedPressurePlateBlock" in chain:
            fields["maxWeight"] = 15 if "light_" in bid else 150
            fields["ticksToStayPressed"] = 10
        elif "BasePressurePlateBlock" in chain:
            fields["ticksToStayPressed"] = 20

        # Block links (concrete powder, potted, dead coral, stairs, candle cake, stems, nylium)
        ref_blocks = [const_to_block_id[c] for c in re.findall(r"\bBlocks\.([A-Z0-9_]+)\b", expr) if c in const_to_block_id and const_to_block_id[c] != bid]
        short = bid.removeprefix("minecraft:")
        if "ConcretePowderBlock" in chain and ref_blocks:
            fields["concrete"] = ref_blocks[0]
        elif "FlowerPotBlock" in chain:
            fields["potted"] = ref_blocks[0] if ref_blocks else "minecraft:air"
        elif any(x in chain for x in ("CoralBlock", "CoralPlantBlock", "CoralFanBlock", "CoralWallFanBlock")) and ref_blocks:
            fields["deadBlock"] = ref_blocks[0]
        elif "StairBlock" in chain and ref_blocks:
            fields["base"] = ref_blocks[0]
        elif "CandleCakeBlock" in chain and ref_blocks:
            fields["candleBlock"] = ref_blocks[0]
        elif "StemBlock" in chain:
            if len(ref_blocks) >= 2:
                fields["fruit"] = ref_blocks[0]
                fields["attachedStem"] = ref_blocks[1]
            fields["stemSupportBlocks"] = "minecraft:supports_stem_crops" if "minecraft:supports_stem_crops" else "minecraft:farmland"
            fields["fruitSupportBlocks"] = f"minecraft:supports_{short.replace('_stem', '')}"
        elif "AttachedStemBlock" in chain and len(ref_blocks) >= 2:
            fields["stem"] = ref_blocks[0]
            fields["fruit"] = ref_blocks[1]
        elif "FarmlandBlock" in chain or cls == "FarmBlock":
            fields["baseBlock"] = "minecraft:dirt"

        # Support tags from class source or constructor args
        for c in chain:
            csrc = class_src.get(c, "")
            for bt_c in re.findall(r"\bBlockTags\.([A-Z0-9_]+)\b", expr + "\n" + csrc):
                if bt_c in block_tag_consts:
                    tval = block_tag_consts[bt_c]
                    if "STEM" in bt_c and "stemSupportBlocks" not in fields:
                        fields["stemSupportBlocks"] = tval
                    elif "FRUIT" in bt_c or ("MELON" in bt_c or "PUMPKIN" in bt_c):
                        fields.setdefault("fruitSupportBlocks", tval)
                    elif "SUPPORTS_" in bt_c:
                        fields.setdefault("supportBlocks", tval)

        # TreeGrower / Fungus / Mushroom features
        tg_m = re.search(r"\bTreeGrower\.([A-Z0-9_]+)\b", expr)
        if tg_m and tg_m.group(1) in tree_growers:
            fields["treeGrower"] = {"name": tree_growers[tg_m.group(1)]}
        feat_m = re.search(r"(?:TreeFeatures|HugeMushroomFeatures|NetherFeatures|VegetationFeatures)\.([A-Z0-9_]+)", expr)
        if feat_m:
            fields["feature"] = f"minecraft:{feat_m.group(1).lower()}"
        if "NetherFungusBlock" in chain and len(ref_blocks) >= 1:
            fields["requiredBlock"] = ref_blocks[0]

        # GrowingPlantBlock growth direction & probability
        if "GrowingPlantHeadBlock" in chain or "GrowingPlantBodyBlock" in chain or "GrowingPlantBlock" in chain:
            csrc = class_src.get(cls, "")
            if "Direction.DOWN" in csrc or short in ("weeping_vines", "weeping_vines_plant", "cave_vines", "cave_vines_plant", "pale_hanging_moss"):
                fields["growthDirection"] = "DOWN"
            else:
                fields["growthDirection"] = "UP"
            if short == "kelp":
                fields["growPerTickProbability"] = 0.14
            elif short in ("weeping_vines", "twisting_vines"):
                fields["growPerTickProbability"] = 0.1
            elif short == "cave_vines":
                fields["growPerTickProbability"] = 0.11
            if "scheduleFluidTicks" in csrc or short in ("kelp", "kelp_plant", "seagrass", "tall_seagrass"):
                fields["scheduleFluidTicks"] = True

        # Barrel / Shulker open/close sounds
        if short == "barrel":
            fields["openSound"] = sound_events.get("BARREL_OPEN", "minecraft:block.barrel.open")
            fields["closeSound"] = sound_events.get("BARREL_CLOSE", "minecraft:block.barrel.close")

        has_dyn = "dynamicShape" in expr or cls in ("ShulkerBoxBlock", "MovingPistonBlock")
        block_info[bid] = {
            "cls": cls,
            "chain": chain,
            "interfaces": ifaces,
            "fields": fields,
            "dynamic_shape": has_dyn,
            "canSurvive_owner": declaring_class(cls, "canSurvive", "BlockBehaviour"),
            "mayPlaceOn_owner": declaring_class(cls, "mayPlaceOn", None),
            "skipRendering_owner": declaring_class(cls, "skipRendering", "BlockBehaviour"),
            "fallOn_owner": declaring_class(cls, "fallOn", "Block"),
            "bounceOn_owner": declaring_class(cls, "bounceOn", "Block"),
        }

    # Parse Items.java for item->block bindings
    items_src = strip_comments((MC_SRC / "world/item/Items.java").read_text())
    item_bindings: dict[str, dict] = {}
    for m in re.finditer(r"public\s+static\s+final\s+Item\s+([A-Z0-9_]+)\s*=\s*", items_src):
        j = scan_stmt_end(items_src, m.end())
        expr = items_src[m.end():j].strip()
        brefs = [const_to_block_id[c] for c in re.findall(r"\bBlocks\.([A-Z0-9_]+)\b", expr) if c in const_to_block_id]
        if not brefs:
            continue
        id_m = re.search(r"(?:BlockItemIds|ItemIds)\.([A-Z0-9_]+)", expr)
        if id_m and id_m.group(1) in ref_ids:
            iid = ref_ids[id_m.group(1)]
        else:
            iid = f"minecraft:{m.group(1).lower()}" 

        icls = "BlockItem"
        new_m = re.search(r"\bnew\s+([A-Za-z0-9_]+)\s*(?:<[^>]*>)?\s*\(", expr)
        ref_m = re.search(r"\b([A-Za-z0-9_]+)::new\b", expr)
        if new_m and "Properties" not in new_m.group(1):
            icls = new_m.group(1)
        elif ref_m:
            icls = ref_m.group(1)

        ichain = class_chain(icls)
        if "BlockItem" not in ichain and icls != "SolidBucketItem":
            ichain.append("BlockItem")
        ifields: dict = {"block": brefs[0]}
        if len(brefs) >= 2 and ("StandingAndWallBlockItem" in ichain or "SignItem" in ichain or "HangingSignItem" in ichain or "BannerItem" in ichain):
            ifields["wallBlock"] = brefs[1]
            ifields["attachmentDirection"] = "UP" if "HangingSignItem" in ichain else "DOWN"
        item_bindings[iid] = {
            "class": ichain,
            "fields": ifields,
        }

    # Add collection-registered block items (beds, wool, banners, copper doors/chests/statues/etc.)
    for bid_k, bdata_k in block_info.items():
        if bid_k in item_bindings or bid_k == "minecraft:air":
            continue
        sh_k = bid_k.removeprefix("minecraft:")
        if sh_k.endswith("_wall_banner") or sh_k.endswith("_wall_sign") or sh_k.endswith("_wall_hanging_sign") or sh_k.endswith("_wall_torch") or sh_k.endswith("_candle_cake") or sh_k.startswith("potted_"):
            continue
        if sh_k.endswith("_banner"):
            wall_id = f"minecraft:{sh_k[:-7]}_wall_banner"
            item_bindings[bid_k] = {
                "class": ["BannerItem", "StandingAndWallBlockItem", "BlockItem", "Item"],
                "fields": {"block": bid_k, "wallBlock": wall_id, "attachmentDirection": "DOWN"},
            }
        elif "DoorBlock" in bdata_k["chain"]:
            item_bindings[bid_k] = {
                "class": ["DoubleHighBlockItem", "BlockItem", "Item"],
                "fields": {"block": bid_k},
            }
        elif "BedBlock" in bdata_k["chain"]:
            item_bindings[bid_k] = {
                "class": ["BedItem", "BlockItem", "Item"],
                "fields": {"block": bid_k},
            }
        else:
            item_bindings[bid_k] = {
                "class": ["BlockItem", "Item"],
                "fields": {"block": bid_k},
            }

    return block_info, item_bindings


def parse_attributes_and_entities(pumpkin_entities: dict):
    """Build attributes.json + entity-types.json from Attributes.java, DefaultAttributes.java, and entity classes."""
    attr_src = strip_comments((MC_SRC / "world/entity/ai/attributes/Attributes.java").read_text())
    attr_consts = {"DEFAULT_ATTACK_SPEED": 4.0}
    attr_const_to_id: dict[str, str] = {}
    attr_defs: dict[str, dict] = {}

    for m in re.finditer(
        r'Holder<Attribute>\s+([A-Z0-9_]+)\s*=\s*register\s*\(\s*"([^"]+)"\s*,\s*new\s+(RangedAttribute|Attribute)\s*\((.*?)\)((?:\.[a-zA-Z0-9_]+\([^)]*\))*)\s*\)',
        attr_src,
        flags=re.S,
    ):
        cname, aid_short, kind, args_raw, chain_calls = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        aid = f"minecraft:{aid_short}"
        attr_const_to_id[cname] = aid
        args = split_top_args(args_raw)
        default_val = parse_java_num(args[1], attr_consts)
        entry = {
            "id": aid,
            "default": f64_hex(default_val),
            "syncable": ".setSyncable(true)" in chain_calls,
            "_default_f64": default_val,
        }
        if kind == "RangedAttribute" and len(args) >= 4:
            entry["min"] = f64_hex(parse_java_num(args[2], attr_consts))
            entry["max"] = f64_hex(parse_java_num(args[3], attr_consts))
        attr_defs[aid] = entry

    # Order attributes by BuiltInRegistries.ATTRIBUTE order (pumpkin/assets/attributes.json `id`)
    pumpkin_attrs = json.loads((PUMPKIN_ASSETS / "attributes.json").read_text())
    ordered_attr_ids = [
        f"minecraft:{k}"
        for k, _ in sorted(pumpkin_attrs.items(), key=lambda kv: kv[1]["id"])
    ]
    attributes_list = []
    for aid in ordered_attr_ids:
        d = dict(attr_defs[aid])
        d.pop("_default_f64", None)
        attributes_list.append(d)

    # Scan all entity Java classes for static attribute builder methods
    living_base = {
        attr_const_to_id[k]: attr_defs[attr_const_to_id[k]]["_default_f64"]
        for k in [
            "MAX_HEALTH", "KNOCKBACK_RESISTANCE", "MOVEMENT_SPEED", "ARMOR",
            "ARMOR_TOUGHNESS", "MAX_ABSORPTION", "STEP_HEIGHT", "SCALE",
            "GRAVITY", "SAFE_FALL_DISTANCE", "FALL_DAMAGE_MULTIPLIER",
            "JUMP_STRENGTH", "OXYGEN_BONUS", "BURNING_TIME",
            "EXPLOSION_KNOCKBACK_RESISTANCE", "WATER_MOVEMENT_EFFICIENCY",
            "MOVEMENT_EFFICIENCY", "ATTACK_KNOCKBACK", "CAMERA_DISTANCE",
            "WAYPOINT_TRANSMIT_RANGE",
        ]
        if k in attr_const_to_id
    }
    # Add any extra attributes added in LivingEntity.createLivingAttributes()
    le_src = strip_comments((MC_SRC / "world/entity/LivingEntity.java").read_text())
    le_m = re.search(r"createLivingAttributes\s*\(\s*\)\s*\{(.*?)\}", le_src, flags=re.S)
    if le_m:
        living_base = {}
        for am in re.finditer(r"\.add\s*\(\s*Attributes\.([A-Z0-9_]+)(?:\s*,\s*([^)]+))?\)", le_m.group(1)):
            ak = attr_const_to_id[am.group(1)]
            living_base[ak] = parse_java_num(am.group(2), attr_consts) if am.group(2) else attr_defs[ak]["_default_f64"]

    mob_base = dict(living_base)
    mob_src = strip_comments((MC_SRC / "world/entity/Mob.java").read_text())
    mob_m = re.search(r"createMobAttributes\s*\(\s*\)\s*\{(.*?)\}", mob_src, flags=re.S)
    if mob_m:
        for am in re.finditer(r"\.add\s*\(\s*Attributes\.([A-Z0-9_]+)(?:\s*,\s*([^)]+))?\)", mob_m.group(1)):
            ak = attr_const_to_id[am.group(1)]
            mob_base[ak] = parse_java_num(am.group(2), attr_consts) if am.group(2) else attr_defs[ak]["_default_f64"]

    # Index all entity Java files by class name
    ent_files: dict[str, str] = {}
    for jf in (MC_SRC / "world/entity").rglob("*.java"):
        ent_files[jf.stem] = strip_comments(jf.read_text())

    builder_cache: dict[tuple[str, str], dict[str, float]] = {
        ("LivingEntity", "createLivingAttributes"): living_base,
        ("Mob", "createMobAttributes"): mob_base,
    }

    def eval_attr_method(cls_name: str, m_name: str, depth: int = 0) -> dict[str, float]:
        key = (cls_name, m_name)
        if key in builder_cache:
            return dict(builder_cache[key])
        if depth > 8 or cls_name not in ent_files:
            return dict(mob_base)
        s = ent_files[cls_name]
        # Collect numeric constants in this class
        local_consts = dict(attr_consts)
        for cm in re.finditer(r"static\s+final\s+(?:double|float|int)\s+([A-Z0-9_]+)\s*=\s*([^;]+);", s):
            try:
                local_consts[cm.group(1)] = parse_java_num(cm.group(2), local_consts)
            except Exception:
                pass
        mm = re.search(rf"AttributeSupplier\.Builder\s+{re.escape(m_name)}\s*\(\s*\)\s*\{{(.*?)\}}", s, flags=re.S)
        if not mm:
            # Check superclass
            sup_m = re.search(rf"class\s+{re.escape(cls_name)}\b[^{{]*extends\s+([A-Za-z0-9_]+)", s)
            if sup_m:
                return eval_attr_method(sup_m.group(1), m_name, depth + 1)
            return dict(mob_base)
        body = mm.group(1)
        # Base call: `OtherClass.createFooAttributes()` or `createMobAttributes()`
        base_m = re.search(r"(?:([A-Za-z0-9_]+)\.)?(create[A-Za-z0-9_]*Attributes| createAttributes)\s*\(\s*\)", body)
        if base_m:
            bcls = base_m.group(1) or cls_name
            bmeth = base_m.group(2).strip()
            if bcls == cls_name and bmeth == m_name:
                sup_m = re.search(rf"class\s+{re.escape(cls_name)}\b[^{{]*extends\s+([A-Za-z0-9_]+)", s)
                vals = eval_attr_method(sup_m.group(1), bmeth, depth + 1) if sup_m else dict(mob_base)
            else:
                vals = eval_attr_method(bcls, bmeth, depth + 1)
        else:
            vals = dict(living_base)
        for am in re.finditer(r"\.add\s*\(\s*Attributes\.([A-Z0-9_]+)(?:\s*,\s*([^)]+))?\)", body):
            ak = attr_const_to_id.get(am.group(1))
            if ak:
                if am.group(2):
                    try:
                        vals[ak] = parse_java_num(am.group(2), local_consts)
                    except Exception:
                        vals[ak] = attr_defs[ak]["_default_f64"]
                else:
                    vals[ak] = attr_defs[ak]["_default_f64"]
        builder_cache[key] = dict(vals)
        return vals

    # Parse DefaultAttributes.java SUPPLIERS
    da_src = strip_comments((MC_SRC / "world/entity/ai/attributes/DefaultAttributes.java").read_text())
    entity_suppliers_f64: dict[str, dict[str, float]] = {}
    entity_suppliers_hex: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"\.put\s*\(\s*EntityType\.([A-Z0-9_]+)\s*,\s*([A-Za-z0-9_]+)\.([a-zA-Z0-9_]+)\s*\(\s*\)\.build\s*\(\s*\)\s*\)", da_src):
        et_const, cls_name, m_name = m.group(1), m.group(2), m.group(3)
        eid = f"minecraft:{et_const.lower()}"
        vals = eval_attr_method(cls_name, m_name)
        entity_suppliers_f64[eid] = vals
        entity_suppliers_hex[eid] = {k: f64_hex(v) for k, v in sorted(vals.items())}

    attributes_json = {
        "data_version": DATA_VERSION,
        "attributes": attributes_list,
        "entities": dict(sorted(entity_suppliers_hex.items())),
    }

    # Build entity-types.json
    entity_types_json = {}
    for ename, edata in sorted(pumpkin_entities.items()):
        eid = f"minecraft:{ename}"
        dim = edata.get("dimension", [0.6, 1.8])
        entry = {
            "id": edata["id"],
            "category": edata.get("category", "MISC").lower(),
            "width": dim[0],
            "height": dim[1],
            "eye_height": edata.get("eye_height", dim[1] * 0.85),
            "fire_immune": edata.get("fire_immune", False),
            "summonable": edata.get("summonable", True),
            "saveable": edata.get("saveable", True),
        }
        if eid in entity_suppliers_f64:
            entry["attributes"] = entity_suppliers_f64[eid]
        entity_types_json[eid] = entry

    return attributes_json, entity_types_json


def box_edges(shapes_list: list[list[float]]) -> list[list[float]]:
    """Generate 12 wireframe edge segments [x0,y0,z0,x1,y1,z1] per AABB (deduplicated)."""
    edges = []
    seen = set()
    for mn_mx in shapes_list:
        x0, y0, z0, x1, y1, z1 = mn_mx
        segs = [
            (x0, y0, z0, x1, y0, z0), (x0, y1, z0, x1, y1, z0),
            (x0, y0, z1, x1, y0, z1), (x0, y1, z1, x1, y1, z1),
            (x0, y0, z0, x0, y1, z0), (x1, y0, z0, x1, y1, z0),
            (x0, y0, z1, x0, y1, z1), (x1, y0, z1, x1, y1, z1),
            (x0, y0, z0, x0, y0, z1), (x1, y0, z0, x1, y0, z1),
            (x0, y1, z0, x0, y1, z1), (x1, y1, z0, x1, y1, z1),
        ]
        for s in segs:
            if s not in seen:
                seen.add(s)
                edges.append(list(s))
    return edges


def rotate_mirror_prop(prop: str, val: str, mode: str) -> str:
    """Vanilla BlockState.rotate / mirror on standard block properties."""
    cw90 = {"north": "east", "east": "south", "south": "west", "west": "north"}
    cw180 = {"north": "south", "east": "west", "south": "north", "west": "east"}
    ccw90 = {"north": "west", "west": "south", "south": "east", "east": "north"}
    lr = {"north": "south", "south": "north"}  # LEFT_RIGHT mirrors Z (north<->south)
    fb = {"east": "west", "west": "east"}      # FRONT_BACK mirrors X (east<->west)

    if prop in ("facing", "horizontal_facing"):
        table = {"cw90": cw90, "cw180": cw180, "ccw90": ccw90, "lr": lr, "fb": fb}[mode]
        return table.get(val, val)
    if prop == "axis":
        if mode in ("cw90", "ccw90"):
            return {"x": "z", "z": "x"}.get(val, val)
        return val
    if prop == "rotation" and val.isdigit():
        r = int(val)
        if mode == "cw90":
            return str((r + 4) & 15)
        if mode == "cw180":
            return str((r + 8) & 15)
        if mode == "ccw90":
            return str((r + 12) & 15)
        if mode == "lr":
            return str((8 - r) & 15)
        if mode == "fb":
            return str((16 - r) & 15)
    if prop == "orientation" and "_" in val:
        a, b = val.split("_", 1)
        return f"{rotate_mirror_prop('facing', a, mode)}_{rotate_mirror_prop('facing', b, mode)}"
    return val


def rotate_mirror_state_dict(props: dict[str, str], mode: str) -> dict[str, str]:
    res = dict(props)
    # Directional boolean/side properties (`north`, `east`, `south`, `west`)
    dirs = ["north", "east", "south", "west"]
    if all(d in props for d in dirs):
        perm = {
            "cw90": {"east": "north", "south": "east", "west": "south", "north": "west"},
            "cw180": {"south": "north", "west": "east", "north": "south", "east": "west"},
            "ccw90": {"west": "north", "north": "east", "east": "south", "south": "west"},
            "lr": {"north": "south", "south": "north", "east": "east", "west": "west"},
            "fb": {"east": "west", "west": "east", "north": "north", "south": "south"},
        }[mode]
        for dst_d, src_d in perm.items():
            res[dst_d] = props[src_d]
    for k, v in props.items():
        if k in dirs and all(d in props for d in dirs):
            continue
        res[k] = rotate_mirror_prop(k, v, mode)
    if mode in ("lr", "fb") and "hinge" in res:
        # Door hinge flips on mirror when facing along or across mirror axis
        res["hinge"] = "right" if res["hinge"] == "left" else "left"
    return res


def main():
    resource_file_count = copy_assets_and_data()

    print("[2/9] Loading Pumpkin + mcmeta registries & block tables ...")
    pumpkin_blocks_doc = json.loads((PUMPKIN_ASSETS / "blocks.json").read_text())
    pumpkin_shapes = [
        [float(s["min"][0]), float(s["min"][1]), float(s["min"][2]),
         float(s["max"][0]), float(s["max"][1]), float(s["max"][2])]
        for s in pumpkin_blocks_doc["shapes"]
    ]
    pumpkin_sound_types = pumpkin_blocks_doc["sound_types"]
    pumpkin_blocks = pumpkin_blocks_doc["blocks"]
    pumpkin_fluids = json.loads((PUMPKIN_ASSETS / "fluids.json").read_text())
    pumpkin_map_colors = {
        mc["id"]: mc["col"] for mc in json.loads((PUMPKIN_ASSETS / "map_colors.json").read_text())
    }
    pumpkin_items = json.loads((PUMPKIN_ASSETS / "items.json").read_text())
    item_id_to_name = {0: "minecraft:air"}
    for iname, idata in pumpkin_items.items():
        iid_num = idata["id"] if isinstance(idata, dict) else idata
        item_id_to_name[iid_num] = f"minecraft:{iname}"
    pumpkin_entities = json.loads((PUMPKIN_ASSETS / "entities.json").read_text())

    mcmeta_blocks = json.loads((MCMETA_SUMMARY / "blocks/data.json").read_text())
    mcmeta_item_comps = json.loads((MCMETA_SUMMARY / "item_components/data.json").read_text())
    mcmeta_registries = json.loads((MCMETA_SUMMARY / "registries/data.json").read_text())
    mcmeta_commands = json.loads((MCMETA_SUMMARY / "commands/data.json").read_text())

    # Fluid state lookup by block_state_id
    fluid_by_block_state: dict[int, tuple[int, int, bool]] = {}
    fluid_kind_index = {name: idx for idx, name in enumerate(FLUID_KINDS)}
    for fl in pumpkin_fluids:
        fname = f"minecraft:{fl['name']}"
        if fname == "minecraft:empty":
            continue
        fk_idx = fluid_kind_index[fname]
        props_def = fl.get("properties", [])
        prop_combos = list(itertools.product(*[p["values"] for p in props_def])) if props_def else [()]
        prop_names = [p["name"] for p in props_def]
        for idx, fst in enumerate(fl["states"]):
            bs_id = fst["block_state_id"]
            combo = dict(zip(prop_names, prop_combos[idx])) if idx < len(prop_combos) else {}
            falling = combo.get("falling") == "true"
            fluid_by_block_state[bs_id] = (fk_idx, int(fst["level"]), falling)

    print("[3/9] Parsing vanilla 26.3 Java classes, BlockSetTypes, WoodTypes, Attributes ...")
    sound_events = parse_sound_events()
    set_types, wood_types = parse_set_and_wood_types(sound_events)
    block_info, item_bindings = parse_block_and_item_classes(sound_events, set_types, wood_types)
    attributes_json, entity_types_json = parse_attributes_and_entities(pumpkin_entities)

    # Resolve block tags locally so we know `bars_tag`, `causes_suffocation`, `fence_gates`, etc.
    block_tags_bundle: dict[str, str] = {}
    bt_dir = MCMETA_DATA / "data/minecraft/tags/block"
    for p in sorted(bt_dir.rglob("*.json")):
        rel = p.relative_to(bt_dir).with_suffix("").as_posix()
        block_tags_bundle[f"minecraft:{rel}"] = p.read_text(encoding="utf-8")

    raw_tag_entries: dict[str, list[str]] = {}
    for tname, ttext in block_tags_bundle.items():
        raw_tag_entries[tname] = [
            v if isinstance(v, str) else v["id"]
            for v in json.loads(ttext).get("values", [])
        ]

    resolved_block_tags: dict[str, set[str]] = {}
    def resolve_btag(tname: str, seen: set[str] | None = None) -> set[str]:
        if tname in resolved_block_tags:
            return resolved_block_tags[tname]
        seen = (seen or set()) | {tname}
        out = set()
        for entry in raw_tag_entries.get(tname, []):
            if entry.startswith("#"):
                ref = entry[1:]
                if ":" not in ref:
                    ref = f"minecraft:{ref}"
                if ref not in seen:
                    out |= resolve_btag(ref, seen)
            else:
                out.add(entry if ":" in entry else f"minecraft:{entry}")
        resolved_block_tags[tname] = out
        return out

    for tname in list(raw_tag_entries.keys()):
        resolve_btag(tname)

    bars_blocks = resolved_block_tags.get("minecraft:bars", set())
    suffocating_tag_blocks = resolved_block_tags.get("minecraft:causes_suffocation", set())
    fence_gate_blocks = sorted(resolved_block_tags.get("minecraft:fence_gates", set()))

    print("[4/9] Building facts.json, model-states.json, gameplay-facts.json, block-behaviours.json ...")
    facts_states = []
    facts_defaults = {}
    model_states = {}
    gameplay_states = []
    behaviour_blocks = []
    behaviour_states = []
    render_defs = {}
    landing_defs = {}
    fire_ignite_odds = {}
    fire_burn_odds = {}
    survival_classes = {}
    survival_placement = {}
    survival_support_tags = {}
    double_plants = []
    mossy_carpets = []

    # Structure-blocks arrays
    struct_rotate = []
    struct_mirror = []
    struct_full = []
    struct_loot = []
    struct_block_entities: dict[str, list[int]] = {}

    # NBT table per-state arrays
    nbt_collision_shapes: list[list[int] | None] = []
    nbt_occlusion_shapes: list[list[int] | None] = []
    nbt_support_masks: list[int] = []
    nbt_block_prop_flags: list[int] = []
    nbt_movement_rows: list[list] = []

    # Mob pathfinding per-state rows `[PathType, flags_u16, top_f32]`
    mob_state_rows = []

    LOOT_CONTAINER_ENTITY_NAMES = {
        "chest", "trapped_chest", "barrel", "dispenser", "dropper",
        "hopper", "shulker_box", "crafter", "decorated_pot", "brushable_block",
    }
    be_type_names = pumpkin_blocks_doc["block_entity_types"]

    for b in sorted(pumpkin_blocks, key=lambda x: x["id"]):
        bshort = b["name"]
        bid = f"minecraft:{bshort}"
        binfo = block_info.get(bid, {
            "cls": "Block",
            "chain": ["Block", "BlockBehaviour"],
            "interfaces": [],
            "fields": {},
            "dynamic_shape": False,
            "canSurvive_owner": "BlockBehaviour",
            "mayPlaceOn_owner": None,
            "skipRendering_owner": "BlockBehaviour",
            "fallOn_owner": "Block",
            "bounceOn_owner": "Block",
        })
        props_schema_raw, default_props = mcmeta_blocks[bshort]
        prop_names = sorted(props_schema_raw.keys())
        prop_combos = (
            [dict(zip(prop_names, vals)) for vals in itertools.product(*[props_schema_raw[k] for k in prop_names])]
            if prop_names else [{}]
        )
        assert len(prop_combos) == len(b["states"]), f"State count mismatch on {bid}"

        # Map prop tuple -> state_id for structure rotate/mirror
        combo_to_sid = {tuple(c[k] for k in prop_names): b["states"][i]["id"] for i, c in enumerate(prop_combos)}
        if prop_names:
            assert combo_to_sid[tuple(default_props[k] for k in prop_names)] == b["default_state_id"], f"Default state mismatch on {bid}"

        # Model states schema
        m_schema = {}
        for pk, pvals in props_schema_raw.items():
            if all(v.isdigit() for v in pvals):
                nums = [int(v) for v in pvals]
                if nums == list(range(min(nums), max(nums) + 1)):
                    m_schema[pk] = {"integer": [min(nums), max(nums)]}
                else:
                    m_schema[pk] = {"named": pvals}
            else:
                m_schema[pk] = {"named": pvals}
        model_states[bid] = {
            "schema": m_schema,
            "states": prop_combos,
        }

        facts_defaults[bid] = b["default_state_id"]
        b_item_name = item_id_to_name.get(b.get("item_id", 0), "minecraft:air")

        # Flammable odds
        if "flammable" in b:
            fire_ignite_odds[bid] = int(b["flammable"]["spread_chance"])
            fire_burn_odds[bid] = int(b["flammable"]["burn_chance"])

        # Survival & placement metadata
        survival_classes[bid] = binfo["canSurvive_owner"]
        if binfo["mayPlaceOn_owner"]:
            survival_placement[bid] = binfo["mayPlaceOn_owner"]
        stags = {}
        for tk in ("stemSupportBlocks", "supportBlocks", "fruitSupportBlocks"):
            if tk in binfo["fields"]:
                stags[tk] = binfo["fields"][tk]
        if stags:
            survival_support_tags[bid] = stags
        if "DoublePlantBlock" in binfo["chain"]:
            double_plants.append(bid)
        if "MossyCarpetBlock" in binfo["chain"]:
            mossy_carpets.append(bid)

        # Render definition
        sr_owner = binfo["skipRendering_owner"]
        sr_rule = {
            "BlockBehaviour": "never",
            "HalfTransparentBlock": "same_block",
            "PowderSnowBlock": "same_block",
            "IronBarsBlock": "bars",
            "LeavesBlock": "leaves",
            "LiquidBlock": "liquid",
            "MangroveRootsBlock": "mangrove_roots",
        }.get(sr_owner, "never")
        def_state_obj = next(st for st in b["states"] if st["id"] == b["default_state_id"])
        render_defs[bid] = {
            "rule": sr_rule,
            "can_occlude": bool(def_state_obj["state_flags"] & PF_CAN_OCCLUDE),
            "bars_tag": bid in bars_blocks,
        }

        # Landing definition
        fall_owner = binfo["fallOn_owner"]
        if fall_owner not in ("Block", "HayBlock", "HoneyBlock", "SlimeBlock", "PowderSnowBlock", "PointedDripstoneBlock", "FarmlandBlock", "TurtleEggBlock"):
            fall_owner = "FarmlandBlock" if fall_owner == "FarmBlock" else "Block"
        ldef = {
            "fall": fall_owner,
            "bounce": binfo["bounceOn_owner"] == "ShelfMushroomBlock",
        }
        if fall_owner == "FarmlandBlock":
            ldef["base"] = binfo["fields"].get("baseBlock", "minecraft:dirt")
        elif fall_owner == "HoneyBlock":
            st_snd = pumpkin_sound_types[def_state_obj["sound_type"]]
            ldef["volume"] = f32_hex(st_snd["volume"])
            ldef["pitch"] = f32_hex(st_snd["pitch"])
        landing_defs[bid] = ldef

        # Block behaviour entry
        behaviour_blocks.append({
            "name": bid,
            "class": binfo["chain"],
            "interfaces": binfo["interfaces"],
            "fields": binfo["fields"],
            "dynamic_shape": binfo["dynamic_shape"],
            "item": b_item_name,
        })

        # Shape offset kind
        soff = b.get("shape_offset")
        offset_kind = soff["type"].upper() if soff else "NONE"
        is_fence_gate = bid in fence_gate_blocks
        is_signal_source = any(
            x in binfo["chain"]
            for x in (
                "PoweredBlock", "RedstoneTorchBlock", "LeverBlock", "ButtonBlock",
                "BasePressurePlateBlock", "DetectorRailBlock", "DaylightDetectorBlock",
                "TargetBlock", "TrappedChestBlock", "LecternBlock", "ObserverBlock",
                "DiodeBlock", "RedStoneWireBlock", "SculkSensorBlock", "LightningRodBlock",
                "TripWireHookBlock", "JukeboxBlock",
            )
        )
        render_shape = "INVISIBLE" if (
            bshort in ("air", "cave_air", "void_air", "barrier", "light", "structure_void", "moving_piston")
            or "LiquidBlock" in binfo["chain"]
            or ("BaseEntityBlock" in binfo["chain"] and sr_owner == "BlockBehaviour" and bshort in (
                "chest", "trapped_chest", "ender_chest", "copper_chest",
                "exposed_copper_chest", "weathered_copper_chest", "oxidized_copper_chest",
                "waxed_copper_chest", "waxed_exposed_copper_chest", "waxed_weathered_copper_chest", "waxed_oxidized_copper_chest",
                "shulker_box", "white_shulker_box", "orange_shulker_box", "magenta_shulker_box", "light_blue_shulker_box",
                "yellow_shulker_box", "lime_shulker_box", "pink_shulker_box", "gray_shulker_box", "light_gray_shulker_box",
                "cyan_shulker_box", "purple_shulker_box", "blue_shulker_box", "brown_shulker_box", "green_shulker_box",
                "red_shulker_box", "black_shulker_box", "bed", "conduit", "decorated_pot",
            ))
        ) else "MODEL"

        map_rgb = pumpkin_map_colors.get(b.get("map_color", 0), 0)

        for idx, st in enumerate(b["states"]):
            sid = st["id"]
            assert sid == len(facts_states), f"Non-sequential state id {sid} != {len(facts_states)}"
            pdict = prop_combos[idx]
            sf = st["state_flags"]
            side_f = st["side_flags"]

            is_air = bool(sf & PF_IS_AIR)
            is_liquid = bool(sf & PF_IS_LIQUID)
            waterlogged = pdict.get("waterlogged") == "true"
            if sid in fluid_by_block_state:
                fl_kind, fl_lvl, fl_fall = fluid_by_block_state[sid]
            elif waterlogged or bshort in ("kelp", "kelp_plant", "seagrass", "tall_seagrass", "bubble_column"):
                fl_kind, fl_lvl, fl_fall = (2, 8, False)
            else:
                fl_kind, fl_lvl, fl_fall = (0, 0, False)
            has_fluid = fl_kind != 0

            # `state.toString()` + `BlockState.FULL_CODEC`
            if pdict:
                pstr = ",".join(f"{k}={pdict[k]}" for k in prop_names)
                to_str = f"Block{{{bid}}}[{pstr}]"
                codec_obj = {"id": bid, "properties": pdict}
            else:
                to_str = f"Block{{{bid}}}"
                codec_obj = bid
            facts_states.append([bid, is_air, has_fluid, to_str, codec_obj])

            # Collision & outline shapes
            c_ids = st["collision_shapes"]
            o_ids = st["outline_shapes"]
            full_coll = (len(c_ids) == 1 and c_ids[0] == 0 and not binfo["dynamic_shape"])
            empty_coll = (len(c_ids) == 0)
            full_out = (len(o_ids) == 1 and o_ids[0] == 0 and offset_kind == "NONE")
            empty_out = (len(o_ids) == 0)

            be_idx = st.get("block_entity_type", 65535)
            has_be = (be_idx is not None and be_idx < len(be_type_names))
            if has_be:
                be_name = f"minecraft:{be_type_names[be_idx]}"
                struct_block_entities.setdefault(be_name, []).append(sid)
                if be_type_names[be_idx] in LOOT_CONTAINER_ENTITY_NAMES:
                    struct_loot.append(sid)

            snd = pumpkin_sound_types[st["sound_type"]]
            def snd_id(s: str) -> str:
                return s if ":" in s else f"minecraft:{s}"

            can_occ = bool(sf & PF_CAN_OCCLUDE)
            sided_tr = bool(sf & PF_SIDED_TRANSPARENCY)
            opacity = int(st["opacity"])
            skylight_down = (opacity == 0 and not sided_tr)

            gf_entry = {
                "id": sid,
                "emission": int(st["luminance"]),
                "dampening": opacity,
                "skylight_down": skylight_down,
                "shape_occlusion": sided_tr,
                "can_occlude": can_occ,
                "solid_render": bool(sf & PF_IS_SOLID_RENDER),
                "hardness": float(st["hardness"]),
                "requires_tool": bool(sf & PF_TOOL_REQUIRED),
                "replaceable": bool(sf & PF_REPLACEABLE),
                "random_ticks": bool(sf & PF_HAS_RANDOM_TICKS),
                "block_entity": has_be,
                "render_shape": render_shape,
                "liquid": is_liquid,
                "ignited_by_lava": bool(sf & PF_BURNABLE),
                "push": st["piston_behavior"],
                "offset": offset_kind,
                "full_collision": full_coll,
                "map_color": map_rgb,
                "outline_offset": offset_kind != "NONE",
                "friction": float(b["slipperiness"]),
                "speed_factor": float(b["velocity_multiplier"]),
                "jump_factor": float(b["jump_velocity_multiplier"]),
                "explosion_resistance": float(b["blast_resistance"]),
                "item": b_item_name,
                "sound": {
                    "volume": float(snd["volume"]),
                    "pitch": float(snd["pitch"]),
                    "break": {"id": snd_id(snd["break_sound"])},
                    "step": {"id": snd_id(snd["step_sound"])},
                    "place": {"id": snd_id(snd["place_sound"])},
                    "hit": {"id": snd_id(snd["hit_sound"])},
                    "fall": {"id": snd_id(snd["fall_sound"])},
                },
            }
            if empty_out:
                gf_entry["outline"] = []
                gf_entry["outline_edges"] = []
            elif not full_out:
                boxes = [pumpkin_shapes[i] for i in o_ids]
                gf_entry["outline"] = boxes
                gf_entry["outline_edges"] = box_edges(boxes)
            gameplay_states.append(gf_entry)

            # Sturdy bits: FULL (0..5), CENTER (6..11), RIGID (12..17)
            full_6 = side_f & 0x3F
            center_6 = full_6 | (((side_f & PF_DOWN_CENTER) >> 6) << 0) | (((side_f & PF_UP_CENTER) >> 7) << 1)
            rigid_6 = full_6
            sturdy = full_6 | (center_6 << 6) | (rigid_6 << 12)

            is_suffocating = bool(sf & PF_IS_FULL_CUBE) and (bid in suffocating_tag_blocks)
            b_flags = (
                (1 if (sf & PF_IS_SOLID) else 0)
                | (2 if is_air else 0)
                | (4 if (sf & PF_IS_SOLID_BLOCK) else 0)
                | (8 if is_signal_source else 0)
                | (16 if (sf & PF_HAS_ANALOG_OUTPUT) else 0)
                | (32 if empty_coll else 0)
                | (64 if is_suffocating else 0)
            )
            weak_sig = 0xFFFFFF if bid == "minecraft:redstone_block" else 0
            behaviour_states.append([
                sturdy,
                b_flags,
                fl_kind,
                fl_lvl,
                fl_fall,
                weak_sig,
                0,
                False,
                st["instrument"].upper(),
            ])

            # Structure transforms
            if pdict:
                struct_rotate.append([
                    combo_to_sid.get(tuple(rotate_mirror_state_dict(pdict, m)[k] for k in prop_names), sid)
                    for m in ("cw90", "cw180", "ccw90")
                ])
                struct_mirror.append([
                    combo_to_sid.get(tuple(rotate_mirror_state_dict(pdict, m)[k] for k in prop_names), sid)
                    for m in ("lr", "fb")
                ])
            else:
                struct_rotate.append([sid, sid, sid])
                struct_mirror.append([sid, sid])
            struct_full.append(full_coll)

            # NBT table inputs
            nbt_collision_shapes.append(None if binfo["dynamic_shape"] else c_ids)
            if not can_occ or offset_kind != "NONE":
                nbt_occlusion_shapes.append([])
            else:
                nbt_occlusion_shapes.append(o_ids)
            nbt_support_masks.append(sturdy)

            # worldgen block-properties flags:
            # 1=air, 2=replaceable, 4=blocks_motion (solid), 8=liquid, 16=solid_render
            wg_pflags = (
                (1 if is_air else 0)
                | (2 if (sf & PF_REPLACEABLE) else 0)
                | (4 if (sf & PF_IS_SOLID) else 0)
                | (8 if is_liquid else 0)
                | (16 if (sf & PF_IS_SOLID_RENDER) else 0)
            )
            nbt_block_prop_flags.append(wg_pflags)
            nbt_movement_rows.append([
                float(b["slipperiness"]),
                float(b["velocity_multiplier"]),
                float(b["jump_velocity_multiplier"]),
                float(b.get("bounce_restitution", 0.0)),
                float(b.get("fall_distance_reduction", 0.0)),
                is_fence_gate and pdict.get("open") == "true",
            ])

            # Mob pathfinding row `[PathType, flags_u16, top_f32]`
            if is_air or empty_coll:
                ptype = "LAVA" if fl_kind in (3, 4) else ("WATER" if has_fluid else ("DAMAGE_FIRE" if "FireBlock" in binfo["chain"] or bid == "minecraft:magma_block" else "OPEN"))
                top_y = 0.0
            else:
                top_y = max(pumpkin_shapes[i][4] for i in c_ids)
                if "FenceBlock" in binfo["chain"] or "WallBlock" in binfo["chain"] or (is_fence_gate and pdict.get("open") != "true"):
                    ptype = "FENCE"
                elif "DoorBlock" in binfo["chain"]:
                    ptype = "DOOR_OPEN" if pdict.get("open") == "true" else ("DOOR_IRON_CLOSED" if not binfo["fields"].get("type", {}).get("canOpenByHand", True) else "DOOR_WOOD_CLOSED")
                elif "LeavesBlock" in binfo["chain"]:
                    ptype = "LEAVES"
                elif "BaseRailBlock" in binfo["chain"]:
                    ptype = "RAIL"
                elif bid == "minecraft:cactus":
                    ptype = "DAMAGE_OTHER"
                elif bid == "minecraft:sweet_berry_bush":
                    ptype = "DAMAGE_OTHER"
                elif bid == "minecraft:powder_snow":
                    ptype = "POWDER_SNOW"
                else:
                    ptype = "BLOCKED"
            mflags = (1 if (side_f & 2) else 0) | (2 if full_coll else 0)
            mob_state_rows.append([ptype, mflags, top_y])

    # Add item_frame & glow_item_frame to model-states.json (used by item frame renderer)
    for frame_id in ("minecraft:item_frame", "minecraft:glow_item_frame"):
        model_states[frame_id] = {
            "schema": {"map": {"named": ["false", "true"]}},
            "states": [{"map": "false"}, {"map": "true"}],
        }

    assert len(facts_states) == 35723, f"Expected 35723 states, got {len(facts_states)}"

    write_json(OUT_ROOT / "facts.json", {"states": facts_states, "defaults": facts_defaults})
    write_json(OUT_ROOT / "model-states.json", model_states)
    write_json(OUT_ROOT / "block-tags.json", block_tags_bundle)
    write_json(OUT_ROOT / "gameplay-facts.json", {"states": gameplay_states})
    write_json(OUT_ROOT / "block-behaviours.json", {
        "fluids": FLUID_KINDS,
        "fire": {"igniteOdds": fire_ignite_odds, "burnOdds": fire_burn_odds},
        "blocks": behaviour_blocks,
        "states": behaviour_states,
        "items": item_bindings,
    })
    write_json(OUT_ROOT / "render-definitions.json", render_defs)
    write_json(OUT_ROOT / "landing-definitions.json", landing_defs)

    print("[5/9] Writing items.json, registries.json, commands.json, command-registries.json, attributes, damage ...")
    items_out = {f"minecraft:{k}": v for k, v in sorted(mcmeta_item_comps.items())}
    write_json(OUT_ROOT / "items.json", items_out)
    write_json(OUT_ROOT / "commands.json", mcmeta_commands)
    write_json(OUT_ROOT / "attributes.json", attributes_json)
    write_json(OUT_ROOT / "entity-types.json", entity_types_json)
    write_json(OUT_ROOT / "player-float-ids.json", {"health": 9, "absorption": 17})

    # Copy baked animations + entity-models
    animations_baked = json.loads((BAKED_DIR / "animations.json").read_text())
    shutil.copy2(BAKED_DIR / "entity-models.json", OUT_ROOT / "entity-models.json")
    write_json(OUT_ROOT / "mob-data.json", {
        "states": mob_state_rows,
        "placements": {},
        "animations": animations_baked,
    })

    # Damage types, tags, catalog
    dmg_types = []
    dmg_types_map = {}
    dt_dir = MCMETA_DATA / "data/minecraft/damage_type"
    for p in sorted(dt_dir.glob("*.json")):
        dt_id = f"minecraft:{p.stem}"
        raw = json.loads(p.read_text())
        dmg_types_map[dt_id] = raw
        dmg_types.append({
            "id": dt_id,
            "message_id": raw["message_id"],
            "exhaustion": f32_hex(float(raw["exhaustion"])),
            "scaling": raw["scaling"],
            "effects": raw.get("effects", "hurt"),
            "death_message_type": raw.get("death_message_type", "default"),
        })
    dmg_tags = {}
    dtag_dir = MCMETA_DATA / "data/minecraft/tags/damage_type"
    for p in sorted(dtag_dir.rglob("*.json")):
        rel = p.relative_to(dtag_dir).with_suffix("").as_posix()
        dmg_tags[f"minecraft:{rel}"] = p.read_text(encoding="utf-8")
    write_json(OUT_ROOT / "damage-types.json", dmg_types_map)
    write_json(OUT_ROOT / "damage-tags.json", dmg_tags)
    write_json(OUT_ROOT / "damage-catalog.json", {
        "data_version": DATA_VERSION,
        "types": dmg_types,
    })

    # Build registries.json with exact protocol IDs from Pumpkin where available, falling back to mcmeta-summary
    registries_out = {}
    for rname, entries in sorted(mcmeta_registries.items()):
        full_rname = rname if ":" in rname else f"minecraft:{rname}"
        entries_obj = {}
        for idx, ename in enumerate(entries):
            full_ename = ename if ":" in ename else f"minecraft:{ename}"
            entries_obj[full_ename] = {"protocol_id": idx}
        registries_out[full_rname] = {"protocol_id": len(registries_out), "entries": entries_obj}

    # Override exact protocol IDs from Pumpkin assets
    registries_out["minecraft:block"]["entries"] = {
        f"minecraft:{b['name']}": {"protocol_id": b["id"]} for b in pumpkin_blocks
    }
    registries_out["minecraft:block"]["default"] = "minecraft:air"
    registries_out["minecraft:item"]["entries"] = {
        f"minecraft:{k}": {"protocol_id": (v["id"] if isinstance(v, dict) else v)}
        for k, v in pumpkin_items.items()
    }
    registries_out["minecraft:item"]["default"] = "minecraft:air"
    registries_out["minecraft:entity_type"]["entries"] = {
        f"minecraft:{k}": {"protocol_id": v["id"]} for k, v in pumpkin_entities.items()
    }
    registries_out["minecraft:fluid"]["entries"] = {
        f"minecraft:{fl['name']}": {"protocol_id": fl["id"]} for fl in pumpkin_fluids
    }
    registries_out["minecraft:fluid"]["default"] = "minecraft:empty"
    p_attrs = json.loads((PUMPKIN_ASSETS / "attributes.json").read_text())
    registries_out["minecraft:attribute"]["entries"] = {
        f"minecraft:{k}": {"protocol_id": v["id"]} for k, v in p_attrs.items()
    }
    write_json(OUT_ROOT / "registries.json", registries_out)

    # command-registries.json
    by_reg = {}
    for rname, rdata in registries_out.items():
        short_r = rname.removeprefix("minecraft:")
        tdir = MCMETA_DATA / "data/minecraft/tags" / short_r
        tlist = []
        if tdir.is_dir():
            for p in sorted(tdir.rglob("*.json")):
                tlist.append(f"minecraft:{p.relative_to(tdir).with_suffix('').as_posix()}")
        by_reg[rname] = {"ids": sorted(rdata["entries"].keys()), "tags": tlist}

    ench_dir = MCMETA_DATA / "data/minecraft/enchantment"
    enchantments = sorted(f"minecraft:{p.stem}" for p in ench_dir.glob("*.json")) if ench_dir.is_dir() else []
    tmpl_dir = MCMETA_DATA / "data/minecraft/structure"
    templates = sorted(f"minecraft:{p.relative_to(tmpl_dir).with_suffix('').as_posix()}" for p in tmpl_dir.rglob("*.nbt")) if tmpl_dir.is_dir() else []
    adv_dir = MCMETA_DATA / "data/minecraft/advancement"
    adv_criteria = {}
    if adv_dir.is_dir():
        for p in sorted(adv_dir.rglob("*.json")):
            aid = f"minecraft:{p.relative_to(adv_dir).with_suffix('').as_posix()}"
            ad = json.loads(p.read_text())
            if isinstance(ad.get("criteria"), dict):
                adv_criteria[aid] = sorted(ad["criteria"].keys())
    rec_dir = MCMETA_DATA / "data/minecraft/recipe"
    recipes_ids = sorted(f"minecraft:{p.stem}" for p in rec_dir.glob("*.json")) if rec_dir.is_dir() else []

    write_json(OUT_ROOT / "command-registries.json", {
        "effects": sorted(registries_out.get("minecraft:mob_effect", {}).get("entries", {}).keys()),
        "enchantments": enchantments,
        "attributes": [a["id"] for a in attributes_json["attributes"]],
        "damage_types": sorted(dmg_types_map.keys()),
        "templates": templates,
        "persistent_components": sorted(registries_out.get("minecraft:data_component_type", {}).get("entries", {}).keys()),
        "learnable_recipes": recipes_ids,
        "by_registry": by_reg,
        "block_entities": sorted(f"minecraft:{x}" for x in be_type_names),
        "block_tag_members": {k: sorted(v) for k, v in sorted(resolved_block_tags.items())},
        "clock_time_markers": {},
        "advancement_criteria": adv_criteria,
    })

    print("[6/9] Building creative-inventory.json ...")
    en_us = json.loads((MCMETA_ASSETS / "assets/minecraft/lang/en_us.json").read_text())
    # Categorize all items cleanly across standard CreativeModeTabs
    all_item_ids = [
        f"minecraft:{k}"
        for k, _ in sorted(pumpkin_items.items(), key=lambda kv: (kv[1]["id"] if isinstance(kv[1], dict) else kv[1]))
        if k != "air"
    ]
    building_items, natural_items, functional_items, redstone_items = [], [], [], []
    tools_items, combat_items, food_items, ingredients_items, spawn_eggs = [], [], [], [], []

    for iid in all_item_ids:
        sh = iid.removeprefix("minecraft:")
        comp = items_out.get(iid, {})
        if sh.endswith("_spawn_egg"):
            spawn_eggs.append(iid)
        elif "minecraft:food" in comp or "minecraft:consumable" in comp:
            food_items.append(iid)
        elif any(k in sh for k in ("sword", "axe", "bow", "crossbow", "trident", "mace", "spear", "shield", "helmet", "chestplate", "leggings", "boots", "arrow", "horse_armor", "wolf_armor", "totem")):
            combat_items.append(iid)
        elif any(k in sh for k in ("pickaxe", "shovel", "hoe", "shears", "flint_and_steel", "fishing_rod", "brush", "spyglass", "compass", "clock", "lead", "name_tag", "saddle", "boat", "raft", "minecart", "bucket", "goat_horn", "music_disc", "harness")):
            tools_items.append(iid)
        elif any(k in sh for k in ("redstone", "piston", "repeater", "comparator", "observer", "hopper", "dropper", "dispenser", "target", "lever", "button", "pressure_plate", "rail", "tnt", "sculk_sensor", "daylight_detector", "lectern", "crafter", "bulb")):
            redstone_items.append(iid)
        elif iid in facts_defaults:
            if any(k in sh for k in ("ore", "raw_", "dirt", "grass", "podzol", "mycelium", "mud", "clay", "sand", "gravel", "log", "leaves", "sapling", "flower", "tulip", "orchid", "allium", "dandelion", "poppy", "daisy", "cornflower", "lily", "rose", "mushroom", "fungus", "roots", "moss", "vine", "kelp", "seagrass", "coral", "ice", "snow", "cactus", "bamboo", "sugar_cane", "pumpkin", "melon", "dripstone", "amethyst", "sculk", "nylium", "netherrack", "end_stone", "basalt", "tuff", "calcite", "obsidian")):
                natural_items.append(iid)
            elif any(k in sh for k in ("torch", "lantern", "campfire", "table", "furnace", "smoker", "anvil", "grindstone", "stonecutter", "loom", "chest", "barrel", "shulker_box", "bed", "banner", "sign", "shelf", "bookshelf", "candle", "bell", "beacon", "conduit", "lodestone", "respawn_anchor", "jukebox", "note_block", "ladder", "scaffolding", "pot", "head", "skull", "infested", "spawner", "vault")):
                functional_items.append(iid)
            else:
                building_items.append(iid)
        else:
            ingredients_items.append(iid)

    # Item tags for search index
    item_tag_map: dict[str, list[str]] = {}
    it_dir = MCMETA_DATA / "data/minecraft/tags/item"
    if it_dir.is_dir():
        for p in sorted(it_dir.rglob("*.json")):
            tname = f"minecraft:{p.relative_to(it_dir).with_suffix('').as_posix()}"
            for v in json.loads(p.read_text()).get("values", []):
                s = v if isinstance(v, str) else v.get("id", "")
                if s and not s.startswith("#"):
                    full_s = s if ":" in s else f"minecraft:{s}"
                    item_tag_map.setdefault(full_s, []).append(tname)

    def stack_snbt(iid: str) -> str:
        return f'{{count:1,id:"{iid}"}}'

    def item_disp_name(iid: str) -> str:
        sh = iid.removeprefix("minecraft:")
        return en_us.get(f"item.minecraft.{sh}") or en_us.get(f"block.minecraft.{sh}") or sh.replace("_", " ").title()

    search_snbt = [stack_snbt(i) for i in all_item_ids]
    search_lines = [[item_disp_name(i)] for i in all_item_ids]
    search_tags = [item_tag_map.get(i, []) for i in all_item_ids]

    tabs_spec = [
        ("minecraft:building_blocks", "Building Blocks", "minecraft:bricks", "TOP", 0, "CATEGORY", building_items),
        ("minecraft:colored_blocks", "Colored Blocks", "minecraft:cyan_wool", "TOP", 1, "CATEGORY", [i for i in building_items if any(c in i for c in ("wool", "carpet", "terracotta", "concrete", "glass", "candle", "banner", "bed"))]),
        ("minecraft:natural_blocks", "Natural Blocks", "minecraft:grass_block", "TOP", 2, "CATEGORY", natural_items),
        ("minecraft:functional_blocks", "Functional Blocks", "minecraft:oak_sign", "TOP", 3, "CATEGORY", functional_items),
        ("minecraft:redstone_blocks", "Redstone Blocks", "minecraft:redstone", "TOP", 4, "CATEGORY", redstone_items),
        ("minecraft:search", "Search Items", "minecraft:compass", "TOP", 6, "SEARCH", all_item_ids),
        ("minecraft:tools_and_utilities", "Tools & Utilities", "minecraft:diamond_pickaxe", "BOTTOM", 0, "CATEGORY", tools_items),
        ("minecraft:combat", "Combat", "minecraft:netherite_sword", "BOTTOM", 1, "CATEGORY", combat_items),
        ("minecraft:food_and_drinks", "Food & Drinks", "minecraft:golden_apple", "BOTTOM", 2, "CATEGORY", food_items),
        ("minecraft:ingredients", "Ingredients", "minecraft:iron_ingot", "BOTTOM", 3, "CATEGORY", ingredients_items),
        ("minecraft:spawn_eggs", "Spawn Eggs", "minecraft:pig_spawn_egg", "BOTTOM", 4, "CATEGORY", spawn_eggs),
        ("minecraft:inventory", "Survival Inventory", "minecraft:chest", "BOTTOM", 6, "INVENTORY", []),
    ]
    tabs_json = []
    for tid, title, icon, row, col, tkind, titems in tabs_spec:
        t_obj = {
            "id": tid,
            "title": title,
            "icon": stack_snbt(icon),
            "row": row,
            "column": col,
            "type": tkind,
            "visible": True,
            "items": search_snbt if tkind == "SEARCH" else [stack_snbt(x) for x in titems],
        }
        if tkind == "SEARCH":
            t_obj["lines"] = search_lines
            t_obj["tags"] = search_tags
        tabs_json.append(t_obj)
    write_json(OUT_ROOT / "creative-inventory.json", {
        "schema": 2,
        "permissions": False,
        "tabs": tabs_json,
    })

    print("[7/9] Building worldgen bundle, presets, survival rules, structure-blocks ...")
    wg_out = OUT_ROOT / "worldgen"
    wg_out.mkdir(parents=True, exist_ok=True)

    # Copy structure NBTs into worldgen/structure/minecraft/
    struct_src = MCMETA_DATA / "data/minecraft/structure"
    for p in struct_src.rglob("*.nbt"):
        dst = wg_out / "structure/minecraft" / p.relative_to(struct_src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)

    # 19 worldgen JSON bundles
    for bname, bpath in WORLDGEN_BUNDLES:
        bdir = MCMETA_DATA / "data/minecraft" / bpath
        bundle_map = {}
        for p in sorted(bdir.rglob("*.json")):
            rel = p.relative_to(bdir).with_suffix("").as_posix()
            bundle_map[f"minecraft:{rel}"] = p.read_text(encoding="utf-8")
        write_json(wg_out / f"{bname}.json", bundle_map)

    # worldgen/block_flags.json
    write_json(wg_out / "block_flags.json", [[s[1], s[2]] for s in facts_states])

    # Multi-noise biome parameters (`presets.json` + `biome-parameters/*.json` + `worldgen/biome_parameters/*.json`)
    mn_tree = json.loads((PUMPKIN_ASSETS / "multi_noise_biome_tree.json").read_text())
    presets_bundle = {}
    for preset_name, root_node in mn_tree.items():
        leaves = []
        def collect_leaves(node):
            if node.get("_type") == "leaf":
                leaves.append(node)
            else:
                for ch in node.get("subTree", []):
                    collect_leaves(ch)
        collect_leaves(root_node)
        quantized_entries = []
        report_entries = []
        p_keys = ["temperature", "humidity", "continentalness", "erosion", "depth", "weirdness"]
        for lf in leaves:
            ps = lf["parameters"]
            coords = [[int(ps[i]["min"]), int(ps[i]["max"])] for i in range(6)]
            offset_q = int(ps[6]["min"])
            quantized_entries.append({
                "coordinates": coords,
                "offset": offset_q,
                "biome": lf["biome"],
            })
            rep_p = {
                p_keys[i]: [ps[i]["min"] / 10000.0, ps[i]["max"] / 10000.0]
                for i in range(6)
            }
            rep_p["offset"] = offset_q / 10000.0
            report_entries.append({"biome": lf["biome"], "parameters": rep_p})
        presets_bundle[f"minecraft:{preset_name}"] = json.dumps(quantized_entries, separators=(",", ":"))
        rep_doc = {"biomes": report_entries}
        write_json(OUT_ROOT / "biome-parameters" / f"{preset_name}.json", rep_doc)
        write_json(wg_out / "biome_parameters" / f"{preset_name}.json", rep_doc)
    write_json(wg_out / "presets.json", presets_bundle)

    # worldgen/blocks/*.json
    wg_blocks = wg_out / "blocks"
    wg_blocks.mkdir(parents=True, exist_ok=True)
    # Bind survival rules using exact table from horizon-prepare/src/survival.rs
    class_rules = dict([
        ("FireBlock", "Fire"), ("CactusBlock", "Cactus"), ("PathBlock", "Path"),
        ("MushroomBlock", "Mushroom"), ("MultifaceBlock", "Multiface"), ("VineBlock", "Vine"),
        ("HangingMossBlock", "HangingMoss"), ("SeaPickleBlock", "SeaPickle"),
        ("SnowLayerBlock", "SnowLayer"), ("BellBlock", "Bell"), ("ShelfMushroomBlock", "FullBehind"),
        ("GrowingPlantBlock", "GrowingPlant"), ("MangrovePropaguleBlock", "MangrovePropagule"),
        ("SpeleothemBlock", "Speleothem"), ("ScaffoldingBlock", "Scaffolding"),
        ("PistonHeadBlock", "PistonHead"), ("ChorusPlantBlock", "ChorusPlant"),
        ("ChorusFlowerBlock", "ChorusFlower"), ("PitcherCropBlock", "PitcherCrop"),
        ("BigDripleafBlock", "BigDripleaf"), ("BigDripleafStemBlock", "BigDripleafStem"),
        ("FarmlandBlock", "Farmland"), ("FarmBlock", "Farmland"), ("CocoaBlock", "Cocoa"),
        ("SugarCaneBlock", "SugarCane"), ("SporeBlossomBlock", "SporeBlossom"),
        ("FrogspawnBlock", "Frogspawn"), ("SmallDripleafBlock", "SmallDripleaf"),
        ("TallSeagrassBlock", "TallSeagrass"), ("DoorBlock", "Door"),
        ("BaseRailBlock", "RigidBelow"), ("LeafLitterBlock", "FullBelow"),
        ("WallSignBlock", "SolidBehind"), ("GrindstoneBlock", "Always"),
        ("RedstoneWallTorchBlock", "FullBehind"), ("BubbleColumnBlock", "BubbleColumn"),
        ("RedstoneWireBlock", "RedstoneWire"), ("RedStoneWireBlock", "RedstoneWire"),
        ("SoulFireBlock", "TagBelow:minecraft:soul_fire_base_blocks"),
        ("BlockBehaviour", "Always"), ("CakeBlock", "SolidBelow"),
        ("CandleCakeBlock", "SolidBelow"), ("BannerBlock", "SolidBelow"),
        ("StandingSignBlock", "SolidBelow"), ("WallBannerBlock", "SolidBehind"),
        ("CarpetBlock", "NonAirBelow"), ("MossyCarpetBlock", "MossCarpet"),
        ("BaseTorchBlock", "CenterBelow"), ("CandleBlock", "CenterBelow"),
        ("LanternBlock", "Lantern"), ("HangingRootsBlock", "FullAbove"),
        ("LadderBlock", "FullBehind"), ("AmethystClusterBlock", "FullBehind"),
        ("TripWireHookBlock", "FullBehind"), ("BaseCoralWallFanBlock", "FullBehind"),
        ("WallTorchBlock", "FullBehind"), ("BasePressurePlateBlock", "RigidOrCenterBelow"),
        ("DiodeBlock", "RigidBelow"), ("BaseCoralPlantTypeBlock", "FullBelow"),
        ("CeilingHangingSignBlock", "CenterAbove"),
        ("FaceAttachedHorizontalDirectionalBlock", "FaceAttached"),
        ("BambooSaplingBlock", "SupportsBamboo"), ("BambooStalkBlock", "SupportsBamboo"),
    ])
    veg_rules = dict([
        ("LilyPadBlock", "LilyPad"), ("WaterlilyBlock", "LilyPad"),
        ("VegetationBlock", "TagBelow:minecraft:supports_vegetation"),
        ("BushBlock", "TagBelow:minecraft:supports_vegetation"),
        ("AzaleaBlock", "TagBelow:minecraft:supports_azalea"),
        ("DryVegetationBlock", "TagBelow:minecraft:supports_dry_vegetation"),
        ("NetherSproutsBlock", "TagBelow:minecraft:supports_nether_sprouts"),
        ("NetherWartBlock", "TagBelow:minecraft:supports_nether_wart"),
        ("WitherRoseBlock", "TagBelow:minecraft:supports_wither_rose"),
        ("CactusFlowerBlock", "CactusFlower"),
        ("SeagrassBlock", "Seagrass"),
    ])
    survival_rules_out = {}
    for bid, owner in survival_classes.items():
        if owner in class_rules:
            survival_rules_out[bid] = class_rules[owner]
        else:
            # Walk up chain to find matching survival rule owner
            for anc in block_info.get(bid, {}).get("chain", []):
                if anc in class_rules:
                    survival_rules_out[bid] = class_rules[anc]
                    break
        placed = survival_placement.get(bid)
        if owner == "CropBlock" and placed == "CropBlock":
            survival_rules_out[bid] = "Crop"
        elif owner == "DoublePlantBlock":
            survival_rules_out[bid] = "DoublePlant"
        elif owner in ("VegetationBlock", "BushBlock"):
            method = placed or "VegetationBlock"
            if method in veg_rules:
                survival_rules_out[bid] = veg_rules[method]
            elif method in ("AttachedStemBlock", "StemBlock"):
                t = survival_support_tags.get(bid, {}).get("stemSupportBlocks", "minecraft:farmland")
                survival_rules_out[bid] = f"TagBelow:{t}"
            elif method in ("NetherFungusBlock", "NetherRootsBlock"):
                t = survival_support_tags.get(bid, {}).get("supportBlocks", "minecraft:nylium")
                survival_rules_out[bid] = f"TagBelow:{t}"
            else:
                survival_rules_out[bid] = "TagBelow:minecraft:supports_vegetation"
        survival_rules_out.setdefault(bid, "Always")

    write_json(wg_blocks / "survival-rules.json", survival_rules_out)
    write_json(wg_blocks / "survival-extra-definitions.json", {
        "fence_gates": fence_gate_blocks,
        "ignition_odds": fire_ignite_odds,
    })
    write_json(wg_blocks / "placement-definitions.json", {
        "double_plants": sorted(double_plants),
        "mossy_carpets": sorted(mossy_carpets),
    })
    write_json(wg_blocks / "structure-blocks.json", {
        "states": len(facts_states),
        "rotate": struct_rotate,
        "mirror": struct_mirror,
        "full_block": struct_full,
        "loot_containers": struct_loot,
        "block_entities": struct_block_entities,
    })

    print("[8/9] Writing /tmp/horizon-nbt-inputs.json for binary NBT generation ...")
    write_json(NBT_INPUTS_PATH, {
        "data_version": DATA_VERSION,
        "shapes": pumpkin_shapes,
        "collision": nbt_collision_shapes,
        "occlusion": nbt_occlusion_shapes,
        "support": nbt_support_masks,
        "block_properties": nbt_block_prop_flags,
        "movement": nbt_movement_rows,
    })

    print("[9/9] Writing horizon-resources.json manifest ...")
    write_json(OUT_ROOT / "horizon-resources.json", {
        "schema": 7,
        "version": VERSION_STR,
        "data_version": DATA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "facts_sha256": sha256_file(OUT_ROOT / "facts.json"),
        "model_states_sha256": sha256_file(OUT_ROOT / "model-states.json"),
        "block_tags_sha256": sha256_file(OUT_ROOT / "block-tags.json"),
        "resource_files": resource_file_count,
    }, indent=2)
    print("All JSON + asset + worldgen resources written to /tmp/horizon-resources!")


if __name__ == "__main__":
    main()
