#!/usr/bin/env python3
"""
Builds compact, zero-bleed web assets for Horizon 26.3 Web Edition:
1. Block & Item 16x16 Texture Atlas (public/assets/atlas.png + atlas.json)
2. Horizon's signature "RUST EDITION" title subtitle banner (exact port of title_edition.rs)
3. Essential 26.3 GUI, HUD, container, environment, entity, font & Horizon theme assets
4. Panorama skybox faces + core .ogg sound effects from Joercat/mc-rust commit cc86ca309d
"""

from __future__ import annotations

import base64
from collections import Counter, deque
import json
import os
from pathlib import Path
import shutil
import subprocess
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "public" / "assets"
MCMETA_ASSETS = Path("/tmp/mcmeta-assets/assets/minecraft")
HORIZON_SRC = Path("/tmp/horizon-src")

# ---------------------------------------------------------------------------
# 1. Exact port of Horizon's crates/horizon-client-render/src/title_edition.rs
# ---------------------------------------------------------------------------
EDITION_W = 512
EDITION_H = 64
EDITION_START = 204
T_BODY = (335, 376)
FACE_TOP = 4
EXTRUSION = 8
OUTLINE = 3
LETTER_GAP = 6
GLYPH_WIDTH = 42
FIRST_FACE = 4

GLYPH_R = [[0, 0, 11, 39], [0, 0, 35, 10], [31, 7, 42, 19], [0, 15, 35, 25], [31, 25, 42, 39]]
GLYPH_U = [[0, 0, 11, 32], [31, 0, 42, 32], [7, 29, 35, 39]]
GLYPH_S = [[7, 0, 42, 10], [0, 7, 11, 19], [7, 15, 35, 25], [31, 21, 42, 32], [0, 29, 35, 39]]


def lum(p: tuple[int, int, int, int]) -> int:
    return (p[0] + p[1] + p[2]) // 3


def build_rust_edition_banner(src_path: Path, dst_path: Path) -> None:
    im = Image.open(src_path).convert("RGBA")
    assert im.size == (EDITION_W, EDITION_H), f"Unexpected edition.png size: {im.size}"
    src = list(im.getdata())

    def px(buf: list[tuple[int, int, int, int]], x: int, y: int) -> tuple[int, int, int, int]:
        return buf[y * EDITION_W + x]

    # Sample palette bands
    bands: list[Counter[tuple[int, int, int, int]]] = [Counter() for _ in range(7)]
    for p in src:
        a = p[3]
        if a == 0:
            continue
        l = lum(p)
        if a == 255:
            if l >= 215:
                b = 0
            elif l >= 170:
                b = 1
            elif l >= 100:
                b = 2
            elif l >= 45:
                b = 3
            elif l >= 20:
                b = 4
            else:
                b = 5
        else:
            b = 6
        bands[b][p] += 1

    def mode(band: Counter[tuple[int, int, int, int]], fallback: tuple[int, int, int, int]):
        if not band:
            return fallback
        return max(band.items(), key=lambda kv: (kv[1], tuple(-c for c in kv[0])))[0]

    outline = mode(bands[5], (0, 0, 0, 255))
    face_col = mode(bands[1], (200, 200, 200, 255))
    highlight = mode(bands[0], face_col)
    rim = mode(bands[2], face_col)
    extrusion = mode(bands[3], outline)
    edge = mode(bands[4], outline)
    soft = mode(bands[6], outline)

    out = list(src)
    for y in range(EDITION_H):
        base = y * EDITION_W
        for x in range(EDITION_START):
            out[base + x] = (0, 0, 0, 0)

    face = [False] * (EDITION_W * EDITION_H)
    for i, glyph in enumerate([GLYPH_R, GLYPH_U, GLYPH_S]):
        left = FIRST_FACE + i * (GLYPH_WIDTH + LETTER_GAP)
        for x0, y0, x1, y1 in glyph:
            for y in range(FACE_TOP + y0, FACE_TOP + y1):
                for x in range(left + x0, left + x1):
                    face[y * EDITION_W + x] = True

    def at(grid: list[bool], x: int, y: int) -> bool:
        return 0 <= x < EDITION_START and 0 <= y < EDITION_H and grid[y * EDITION_W + x]

    body = list(face)
    for y in range(EDITION_H):
        for x in range(EDITION_START):
            if any(at(face, x, y - k) for k in range(1, EXTRUSION + 1)):
                body[y * EDITION_W + x] = True

    def put(x: int, y: int, p: tuple[int, int, int, int]) -> None:
        old = out[y * EDITION_W + x]
        if (p[3], lum(p)) > (old[3], lum(old)):
            out[y * EDITION_W + x] = p

    reach = OUTLINE + 1
    for y in range(EDITION_H):
        for x in range(EDITION_START):
            if at(face, x, y):
                if not at(face, x, y + 1):
                    col = highlight
                elif not at(face, x - 1, y) or not at(face, x + 1, y):
                    col = rim
                else:
                    col = face_col
            elif at(body, x, y):
                if at(face, x, y - 1):
                    col = rim
                elif not at(body, x - 1, y) or not at(body, x + 1, y) or not at(body, x, y + 1):
                    col = edge
                else:
                    col = extrusion
            else:
                dists = [
                    max(abs(dx), abs(dy))
                    for dy in range(-reach, reach + 1)
                    for dx in range(-reach, reach + 1)
                    if at(body, x + dx, y + dy)
                ]
                if not dists:
                    continue
                col = outline if min(dists) <= OUTLINE else soft
            put(x, y, col)

    # Extract T from EDITION
    owner = [0] * (EDITION_W * EDITION_H)
    q: deque[tuple[int, int]] = deque()
    for y in range(EDITION_H):
        for x in range(EDITION_START, EDITION_W):
            p = px(src, x, y)
            if p[3] == 255 and lum(p) >= 30:
                owner[y * EDITION_W + x] = 1 if T_BODY[0] <= x <= T_BODY[1] else 2
                q.append((x, y))
    while q:
        x, y = q.popleft()
        lbl = owner[y * EDITION_W + x]
        for dx, dy in [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]:
            nx, ny = x + dx, y + dy
            if EDITION_START <= nx < EDITION_W and 0 <= ny < EDITION_H:
                if owner[ny * EDITION_W + nx] == 0 and px(src, nx, ny)[3] > 0:
                    owner[ny * EDITION_W + nx] = lbl
                    q.append((nx, ny))

    t_left = FIRST_FACE + 3 * (GLYPH_WIDTH + LETTER_GAP)
    for y in range(EDITION_H):
        for x in range(EDITION_START, EDITION_W):
            if owner[y * EDITION_W + x] == 1:
                put(x + t_left - T_BODY[0], y, px(src, x, y))

    # Fill closed counters
    seen = [False] * (EDITION_W * EDITION_H)
    cq: deque[tuple[int, int]] = deque()
    for y in range(EDITION_H):
        for x in range(EDITION_START):
            border = x == 0 or y == 0 or x == EDITION_START - 1 or y == EDITION_H - 1
            if border and out[y * EDITION_W + x][3] < 255:
                seen[y * EDITION_W + x] = True
                cq.append((x, y))
    while cq:
        x, y = cq.popleft()
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            nx, ny = x + dx, y + dy
            if 0 <= nx < EDITION_START and 0 <= ny < EDITION_H:
                if not seen[ny * EDITION_W + nx] and out[ny * EDITION_W + nx][3] < 255:
                    seen[ny * EDITION_W + nx] = True
                    cq.append((nx, ny))
    for y in range(EDITION_H):
        for x in range(EDITION_START):
            if not seen[y * EDITION_W + x] and out[y * EDITION_W + x][3] < 255:
                out[y * EDITION_W + x] = outline

    # Soften edges
    rim_pts: list[tuple[int, int]] = []
    for y in range(EDITION_H):
        for x in range(EDITION_START):
            if out[y * EDITION_W + x][3] == 0:
                near = any(
                    0 <= x + dx < EDITION_START
                    and 0 <= y + dy < EDITION_H
                    and out[(y + dy) * EDITION_W + (x + dx)][3] == 255
                    for dy in (-1, 0, 1)
                    for dx in (-1, 0, 1)
                )
                if near:
                    rim_pts.append((x, y))
    for x, y in rim_pts:
        out[y * EDITION_W + x] = soft

    out_im = Image.new("RGBA", (EDITION_W, EDITION_H))
    out_im.putdata(out)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    out_im.save(dst_path)
    print(f"Generated {dst_path} (RUST EDITION banner)")


# ---------------------------------------------------------------------------
# 2. Pack 16x16 Block & Item Texture Atlas
# ---------------------------------------------------------------------------
def tint_image(im: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    im = im.convert("RGBA")
    r_m, g_m, b_m = rgb
    data = [
        ((r * r_m) // 255, (g * g_m) // 255, (b * b_m) // 255, a)
        for (r, g, b, a) in im.getdata()
    ]
    out = Image.new("RGBA", im.size)
    out.putdata(data)
    return out


def crop_16(im: Image.Image, frame: int = 0) -> Image.Image:
    im = im.convert("RGBA")
    if im.size == (16, 16):
        return im
    if im.width == 16 and im.height >= 16 * (frame + 1):
        return im.crop((0, frame * 16, 16, (frame + 1) * 16))
    return im.crop((0, 0, 16, 16)).resize((16, 16), Image.Resampling.NEAREST)


def make_chest_tiles(entity_chest_png: Path) -> dict[str, Image.Image]:
    """Slice 16x16 chest_top, chest_side, chest_front from entity/chest/normal.png (64x64)."""
    im = Image.open(entity_chest_png).convert("RGBA")
    # Standard 64x64 chest UV layout (vanilla flips Y on entity chest):
    # Top of lid: (28..42, 0..14) -> 14x14 padded/scaled to 16x16
    top_14 = im.crop((28, 0, 42, 14)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    top = top_14.resize((16, 16), Image.Resampling.NEAREST)

    # Build a clean 14x14 side & front by combining lid (y=14..19) + base (y=33..43)
    side_14 = Image.new("RGBA", (14, 14))
    lid_side = im.crop((0, 14, 14, 19)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    base_side = im.crop((0, 33, 14, 43)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    side_14.paste(lid_side, (0, 0))
    side_14.paste(base_side, (0, 4))
    side = side_14.resize((16, 16), Image.Resampling.NEAREST)

    front_14 = Image.new("RGBA", (14, 14))
    lid_front = im.crop((42, 14, 56, 19)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    base_front = im.crop((42, 33, 56, 43)).transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    front_14.paste(lid_front, (0, 0))
    front_14.paste(base_front, (0, 4))
    # Latch (x=1..3, y=1..5)
    latch = im.crop((1, 1, 3, 5))
    front_14.paste(latch, (6, 3), latch)
    front = front_14.resize((16, 16), Image.Resampling.NEAREST)

    return {"chest_top": top, "chest_side": side, "chest_front": front}


def build_atlas() -> None:
    bdir = MCMETA_ASSETS / "textures" / "block"
    idir = MCMETA_ASSETS / "textures" / "item"

    tiles: list[tuple[str, Image.Image]] = []

    def add_tile(name: str, im: Image.Image) -> None:
        tiles.append((name, im.convert("RGBA")))

    # Special composited grass block sides (so brown dirt stays brown while grass fringe matches biome!)
    grass_side_base = crop_16(Image.open(bdir / "grass_block_side.png"))
    grass_overlay = crop_16(Image.open(bdir / "grass_block_side_overlay.png"))
    for suffix, col in [
        ("grass_block_side_plains", (121, 192, 90)),
        ("grass_block_side_forest", (89, 178, 69)),
        ("grass_block_side_taiga", (118, 168, 114)),
        ("grass_block_side_savanna", (175, 172, 82)),
        ("grass_block_side_cherry", (166, 209, 110)),
    ]:
        comp = grass_side_base.copy()
        ov = tint_image(grass_overlay, col)
        comp.alpha_composite(ov)
        add_tile(suffix, comp)

    # Pre-tinted biome grass tops & foliage for crisp zero-cost rendering + inventory icons
    grass_top_raw = crop_16(Image.open(bdir / "grass_block_top.png"))
    add_tile("grass_block_top_plains", tint_image(grass_top_raw, (121, 192, 90)))
    add_tile("grass_block_top_forest", tint_image(grass_top_raw, (89, 178, 69)))
    add_tile("grass_block_top_taiga", tint_image(grass_top_raw, (118, 168, 114)))
    add_tile("grass_block_top_savanna", tint_image(grass_top_raw, (175, 172, 82)))
    add_tile("grass_block_top_cherry", tint_image(grass_top_raw, (166, 209, 110)))

    # Leaves pre-tinted cleanly with opaque/cutout preservation
    leaves_tints = {
        "oak_leaves_tinted": ("oak_leaves", (96, 168, 62)),
        "spruce_leaves_tinted": ("spruce_leaves", (97, 153, 97)),
        "birch_leaves_tinted": ("birch_leaves", (128, 167, 85)),
        "jungle_leaves_tinted": ("jungle_leaves", (68, 184, 38)),
        "acacia_leaves_tinted": ("acacia_leaves", (158, 164, 52)),
        "dark_oak_leaves_tinted": ("dark_oak_leaves", (76, 148, 48)),
    }
    for name, (src_name, col) in leaves_tints.items():
        add_tile(name, tint_image(crop_16(Image.open(bdir / f"{src_name}.png")), col))

    # Tinted flora & water
    add_tile("short_grass_tinted", tint_image(crop_16(Image.open(bdir / "short_grass.png")), (116, 186, 84)))
    add_tile("fern_tinted", tint_image(crop_16(Image.open(bdir / "fern.png")), (102, 172, 74)))
    add_tile("vine_tinted", tint_image(crop_16(Image.open(bdir / "vine.png")), (96, 168, 62)))

    # Animated water (4 frames) & lava (4 frames)
    water_raw = Image.open(bdir / "water_still.png").convert("RGBA")
    for f in range(4):
        wf = tint_image(crop_16(water_raw, f * 4), (63, 118, 228))
        # Ensure nice translucent water alpha (~195)
        wdata = [(r, g, b, min(205, int(a * 0.82))) for (r, g, b, a) in wf.getdata()]
        wim = Image.new("RGBA", (16, 16))
        wim.putdata(wdata)
        add_tile(f"water_frame_{f}", wim)

    lava_raw = Image.open(bdir / "lava_still.png").convert("RGBA")
    for f in range(4):
        add_tile(f"lava_frame_{f}", crop_16(lava_raw, f * 3))

    # Chest faces
    for cname, cim in make_chest_tiles(MCMETA_ASSETS / "textures/entity/chest/normal.png").items():
        add_tile(cname, cim)

    block_names = [
        "bedrock", "stone", "cobblestone", "mossy_cobblestone", "smooth_stone", "smooth_stone_slab_side",
        "stone_bricks", "mossy_stone_bricks", "cracked_stone_bricks", "chiseled_stone_bricks",
        "granite", "polished_granite", "diorite", "polished_diorite", "andesite", "polished_andesite",
        "deepslate", "deepslate_top", "cobbled_deepslate", "polished_deepslate", "deepslate_bricks", "deepslate_tiles",
        "tuff", "calcite", "dripstone_block",
        "dirt", "coarse_dirt", "rooted_dirt", "mud", "clay", "gravel",
        "grass_block_snow",
        "dirt_path_top", "dirt_path_side", "podzol_top", "podzol_side", "mycelium_top", "mycelium_side",
        "farmland", "farmland_moist",
        "sand", "sandstone", "sandstone_top", "sandstone_bottom", "cut_sandstone", "chiseled_sandstone",
        "red_sand", "red_sandstone", "red_sandstone_top", "red_sandstone_bottom",
        "snow", "ice", "packed_ice", "blue_ice",
        "coal_ore", "deepslate_coal_ore", "iron_ore", "deepslate_iron_ore",
        "copper_ore", "deepslate_copper_ore", "gold_ore", "deepslate_gold_ore",
        "redstone_ore", "deepslate_redstone_ore", "emerald_ore", "deepslate_emerald_ore",
        "lapis_ore", "deepslate_lapis_ore", "diamond_ore", "deepslate_diamond_ore",
        "coal_block", "iron_block", "copper_block", "gold_block", "redstone_block", "emerald_block", "lapis_block", "diamond_block", "amethyst_block",
        "oak_log", "oak_log_top", "oak_planks", "oak_sapling",
        "spruce_log", "spruce_log_top", "spruce_planks", "spruce_sapling",
        "birch_log", "birch_log_top", "birch_planks", "birch_sapling",
        "jungle_log", "jungle_log_top", "jungle_planks", "jungle_sapling",
        "acacia_log", "acacia_log_top", "acacia_planks", "acacia_sapling",
        "dark_oak_log", "dark_oak_log_top", "dark_oak_planks", "dark_oak_sapling",
        "cherry_log", "cherry_log_top", "cherry_planks", "cherry_leaves", "cherry_sapling",
        "pale_oak_log", "pale_oak_log_top", "pale_oak_planks", "pale_oak_leaves", "pale_oak_sapling",
        "bricks", "bookshelf", "obsidian", "crying_obsidian", "glowstone", "sea_lantern",
        "glass", "tinted_glass", "white_stained_glass", "light_blue_stained_glass", "red_stained_glass",
        "white_wool", "light_gray_wool", "gray_wool", "black_wool", "red_wool", "orange_wool", "yellow_wool", "lime_wool", "green_wool", "cyan_wool", "light_blue_wool", "blue_wool", "purple_wool", "magenta_wool", "pink_wool", "brown_wool",
        "white_concrete", "gray_concrete", "black_concrete", "red_concrete", "orange_concrete", "yellow_concrete", "lime_concrete", "cyan_concrete", "blue_concrete", "purple_concrete", "pink_concrete",
        "terracotta", "red_terracotta", "orange_terracotta", "yellow_terracotta", "brown_terracotta", "white_terracotta",
        "crafting_table_top", "crafting_table_front", "crafting_table_side",
        "furnace_top", "furnace_side", "furnace_front", "furnace_front_on",
        "barrel_top", "barrel_side", "barrel_bottom",
        "tnt_top", "tnt_side", "tnt_bottom",
        "sponge", "hay_block_side", "hay_block_top", "melon_side", "melon_top", "pumpkin_side", "pumpkin_top", "carved_pumpkin", "jack_o_lantern",
        "netherrack", "soul_sand", "nether_bricks", "quartz_block_side", "quartz_block_top", "quartz_block_bottom", "end_stone", "end_stone_bricks",
        "torch", "soul_torch", "lantern", "redstone_lamp", "redstone_lamp_on", "ladder",
        "cactus_top", "cactus_side", "cactus_bottom", "sugar_cane", "bamboo_stalk",
        "dead_bush", "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "red_tulip", "oxeye_daisy", "cornflower", "lily_of_the_valley", "pink_petals", "brown_mushroom", "red_mushroom",
        "oak_door_top", "oak_door_bottom", "oak_trapdoor",
        "destroy_stage_0", "destroy_stage_1", "destroy_stage_2", "destroy_stage_3", "destroy_stage_4",
        "destroy_stage_5", "destroy_stage_6", "destroy_stage_7", "destroy_stage_8", "destroy_stage_9",
    ]
    for b in block_names:
        add_tile(b, crop_16(Image.open(bdir / f"{b}.png")))

    item_names = [
        "stick", "coal", "charcoal", "raw_iron", "raw_copper", "raw_gold",
        "iron_ingot", "copper_ingot", "gold_ingot", "diamond", "emerald", "lapis_lazuli", "redstone", "amethyst_shard", "quartz",
        "flint", "flint_and_steel", "shears", "bucket", "water_bucket", "lava_bucket", "compass_16", "clock_00", "spyglass",
        "wooden_pickaxe", "wooden_axe", "wooden_shovel", "wooden_sword", "wooden_hoe",
        "stone_pickaxe", "stone_axe", "stone_shovel", "stone_sword", "stone_hoe",
        "copper_pickaxe", "copper_axe", "copper_shovel", "copper_sword", "copper_hoe",
        "iron_pickaxe", "iron_axe", "iron_shovel", "iron_sword", "iron_hoe",
        "golden_pickaxe", "golden_axe", "golden_shovel", "golden_sword", "golden_hoe",
        "diamond_pickaxe", "diamond_axe", "diamond_shovel", "diamond_sword", "diamond_hoe",
        "netherite_pickaxe", "netherite_axe", "netherite_shovel", "netherite_sword",
        "copper_helmet", "copper_chestplate", "copper_leggings", "copper_boots",
        "iron_helmet", "iron_chestplate", "iron_leggings", "iron_boots",
        "diamond_helmet", "diamond_chestplate", "diamond_leggings", "diamond_boots",
        "apple", "golden_apple", "bread", "beef", "cooked_beef", "porkchop", "cooked_porkchop",
        "chicken", "cooked_chicken", "mutton", "cooked_mutton", "carrot", "baked_potato", "sweet_berries", "rotten_flesh",
        "bone", "string", "feather", "leather", "gunpowder", "ender_pearl", "wheat", "wheat_seeds", "brick", "clay_ball",
        "oak_door", "lantern", "soul_lantern",
    ]
    for it in item_names:
        key = f"item:{it}" if any(t[0] == it for t in tiles) else it
        add_tile(key, crop_16(Image.open(idir / f"{it}.png")))

    cols = 16
    rows = (len(tiles) + cols - 1) // cols
    atlas = Image.new("RGBA", (cols * 16, rows * 16), (0, 0, 0, 0))
    index_map: dict[str, int] = {}
    avg_colors: dict[str, list[int]] = {}

    for idx, (name, tile) in enumerate(tiles):
        c = idx % cols
        r = idx // cols
        atlas.paste(tile, (c * 16, r * 16))
        index_map[name] = idx
        # Average opaque color for minimap
        opaque = [(pr, pg, pb) for (pr, pg, pb, pa) in tile.getdata() if pa >= 64]
        if opaque:
            avg_colors[name] = [
                sum(p[0] for p in opaque) // len(opaque),
                sum(p[1] for p in opaque) // len(opaque),
                sum(p[2] for p in opaque) // len(opaque),
            ]
        else:
            avg_colors[name] = [120, 120, 120]

    OUT.mkdir(parents=True, exist_ok=True)
    atlas.save(OUT / "atlas.png", optimize=True)
    with open(OUT / "atlas.json", "w") as f:
        json.dump(
            {
                "tileSize": 16,
                "columns": cols,
                "rows": rows,
                "count": len(tiles),
                "tiles": index_map,
                "colors": avg_colors,
            },
            f,
            separators=(",", ":"),
        )
    print(f"Packed {len(tiles)} 16x16 tiles into atlas.png ({cols * 16}x{rows * 16})")


# ---------------------------------------------------------------------------
# 3. Copy GUI, Environment, Entity, Font & Horizon Theme Assets
# ---------------------------------------------------------------------------
def copy_ui_and_theme_assets() -> None:
    gui_out = OUT / "gui"
    gui_out.mkdir(parents=True, exist_ok=True)

    # Title logo + Horizon RUST EDITION banner
    shutil.copy2(MCMETA_ASSETS / "textures/gui/title/minecraft.png", gui_out / "minecraft.png")
    build_rust_edition_banner(
        MCMETA_ASSETS / "textures/gui/title/edition.png",
        gui_out / "edition_rust.png",
    )

    # Menu backgrounds & separators
    for g in [
        "menu_background.png",
        "menu_list_background.png",
        "header_separator.png",
        "footer_separator.png",
        "inworld_menu_background.png",
        "inworld_header_separator.png",
        "inworld_footer_separator.png",
    ]:
        shutil.copy2(MCMETA_ASSETS / "textures/gui" / g, gui_out / g)

    # Widgets & HUD sprites
    sprites = [
        ("sprites/widget/button.png", "button.png"),
        ("sprites/widget/button_highlighted.png", "button_highlighted.png"),
        ("sprites/widget/button_disabled.png", "button_disabled.png"),
        ("sprites/widget/slider.png", "slider.png"),
        ("sprites/widget/slider_highlighted.png", "slider_highlighted.png"),
        ("sprites/widget/slider_handle.png", "slider_handle.png"),
        ("sprites/widget/slider_handle_highlighted.png", "slider_handle_highlighted.png"),
        ("sprites/hud/hotbar.png", "hotbar.png"),
        ("sprites/hud/hotbar_selection.png", "hotbar_selection.png"),
        ("sprites/hud/hotbar_offhand_left.png", "hotbar_offhand_left.png"),
        ("sprites/hud/crosshair.png", "crosshair.png"),
        ("sprites/hud/heart/container.png", "heart_container.png"),
        ("sprites/hud/heart/full.png", "heart_full.png"),
        ("sprites/hud/heart/half.png", "heart_half.png"),
        ("sprites/hud/food_empty.png", "food_empty.png"),
        ("sprites/hud/food_full.png", "food_full.png"),
        ("sprites/hud/food_half.png", "food_half.png"),
        ("sprites/hud/armor_empty.png", "armor_empty.png"),
        ("sprites/hud/armor_full.png", "armor_full.png"),
        ("sprites/hud/armor_half.png", "armor_half.png"),
        ("sprites/hud/air.png", "air.png"),
        ("sprites/hud/air_empty.png", "air_empty.png"),
        ("sprites/hud/experience_bar_background.png", "xp_bg.png"),
        ("sprites/hud/experience_bar_progress.png", "xp_fg.png"),
        ("container/inventory.png", "inventory.png"),
        ("container/crafting_table.png", "crafting_table.png"),
        ("container/furnace.png", "furnace.png"),
        ("container/generic_54.png", "generic_54.png"),
        ("container/gamemode_switcher.png", "gamemode_switcher.png"),
    ]
    for rel, dst_name in sprites:
        src = MCMETA_ASSETS / "textures/gui" / rel
        if src.exists():
            shutil.copy2(src, gui_out / dst_name)

    # Environment textures (26.3 layout under environment/celestial/)
    env_out = OUT / "environment"
    env_out.mkdir(parents=True, exist_ok=True)
    for rel, dst_name in [
        ("celestial/sun.png", "sun.png"),
        ("celestial/moon/full_moon.png", "moon.png"),
        ("clouds.png", "clouds.png"),
        ("rain.png", "rain.png"),
        ("snow.png", "snow.png"),
    ]:
        shutil.copy2(MCMETA_ASSETS / "textures/environment" / rel, env_out / dst_name)

    # Entity skins
    ent_out = OUT / "entity"
    ent_out.mkdir(parents=True, exist_ok=True)
    entities = [
        ("pig/pig_temperate.png", "pig.png"),
        ("cow/cow_temperate.png", "cow.png"),
        ("chicken/chicken_temperate.png", "chicken.png"),
        ("sheep/sheep.png", "sheep.png"),
        ("zombie/zombie.png", "zombie.png"),
        ("creeper/creeper.png", "creeper.png"),
        ("skeleton/skeleton.png", "skeleton.png"),
        ("player/wide/steve.png", "steve.png"),
    ]
    for rel, dst_name in entities:
        shutil.copy2(MCMETA_ASSETS / "textures/entity" / rel, ent_out / dst_name)

    # Horizon fonts & brand icon
    fonts_out = OUT / "fonts"
    fonts_out.mkdir(parents=True, exist_ok=True)
    for fname in [
        "Geist-Regular.ttf",
        "Geist-Medium.ttf",
        "Geist-SemiBold.ttf",
        "GeistMono-Regular.ttf",
        "PixelifySans-SemiBold.ttf",
        "PixelifySans-Bold.ttf",
    ]:
        shutil.copy2(HORIZON_SRC / "crates/horizon-theme/fonts" / fname, fonts_out / fname)
    shutil.copy2(MCMETA_ASSETS / "textures/font/ascii.png", fonts_out / "ascii.png")
    shutil.copy2(HORIZON_SRC / "crates/horizon-theme/icons/horizon-64.png", OUT / "horizon-64.png")

    # Splashes (vanilla 26.3 + Horizon Rust splashes)
    raw_splashes = (MCMETA_ASSETS / "texts/splashes.txt").read_text(encoding="utf-8").splitlines()
    horizon_splashes = [
        "Rewritten in Rust!",
        "Zero-install Web Edition!",
        "281 differential harnesses!",
        "100% Offline Singleplayer!",
        "26.3 Parity!",
        "Powered by WebGL2 + WASM!",
        "No JVM pauses!",
        "Smooth AO Lighting!",
    ]
    splashes = horizon_splashes + [
        s.strip() for s in raw_splashes if s.strip() and len(s.strip()) <= 34 and "%" not in s
    ][:80]
    with open(OUT / "splashes.json", "w") as f:
        json.dump(splashes, f)


# ---------------------------------------------------------------------------
# 4. Fetch Panorama & Core .ogg Sounds from Joercat/mc-rust commit cc86ca309d
# ---------------------------------------------------------------------------
def gh_json(endpoint: str) -> dict:
    raw = subprocess.check_output(["gh", "api", endpoint])
    return json.loads(raw)


def fetch_prism_objects() -> None:
    print("Fetching panorama & sound blobs from Joercat/mc-rust cc86ca309d...")
    idx_blob = gh_json("repos/Joercat/mc-rust/git/blobs/86a90bcd32e632533d6479016a8f0e6bba5bbe20")
    objects_index: dict[str, dict] = json.loads(base64.b64decode(idx_blob["content"]))["objects"]

    # Map top-level objects/ tree -> 2-hex prefix tree sha
    root_objects_tree = gh_json("repos/Joercat/mc-rust/git/trees/d9f965119716b92fce82b1b5b82f3bb1f1169cd9")
    prefix_trees = {t["path"]: t["sha"] for t in root_objects_tree["tree"]}
    cached_subtrees: dict[str, dict[str, str]] = {}

    def download_by_sha1(sha1: str) -> bytes:
        pfx = sha1[:2]
        if pfx not in cached_subtrees:
            st = gh_json(f"repos/Joercat/mc-rust/git/trees/{prefix_trees[pfx]}")
            cached_subtrees[pfx] = {x["path"]: x["sha"] for x in st["tree"]}
        blob_sha = cached_subtrees[pfx][sha1]
        blob = gh_json(f"repos/Joercat/mc-rust/git/blobs/{blob_sha}")
        return base64.b64decode(blob["content"])

    # 6 Panorama skybox faces -> crisp 512x512 JPEG
    pano_out = OUT / "panorama"
    pano_out.mkdir(parents=True, exist_ok=True)
    from io import BytesIO
    for i in range(6):
        key = f"minecraft/textures/gui/title/background/panorama_{i}.png"
        raw_bytes = download_by_sha1(objects_index[key]["hash"])
        im = Image.open(BytesIO(raw_bytes)).convert("RGB")
        if im.width > 512:
            im = im.resize((512, 512), Image.Resampling.LANCZOS)
        im.save(pano_out / f"panorama_{i}.jpg", quality=88, optimize=True)
    print("Saved 6 26.3 panorama skybox faces")

    # Core sound effects
    sound_out = OUT / "sounds"
    sound_out.mkdir(parents=True, exist_ok=True)
    wanted_sounds = {
        "click.ogg": "minecraft/sounds/random/click.ogg",
        "pop.ogg": "minecraft/sounds/random/pop.ogg",
        "eat.ogg": "minecraft/sounds/random/eat1.ogg",
        "burp.ogg": "minecraft/sounds/random/burp.ogg",
        "explode.ogg": "minecraft/sounds/random/explode1.ogg",
        "splash.ogg": "minecraft/sounds/random/splash.ogg",
        "swim.ogg": "minecraft/sounds/liquid/swim1.ogg",
        "chest_open.ogg": "minecraft/sounds/random/chestopen.ogg",
        "chest_close.ogg": "minecraft/sounds/random/chestclosed.ogg",
        "hurt.ogg": "minecraft/sounds/damage/hit1.ogg",
        "fall_small.ogg": "minecraft/sounds/damage/fallsmall.ogg",
        "fall_big.ogg": "minecraft/sounds/damage/fallbig.ogg",
        "glass_break.ogg": "minecraft/sounds/random/glass1.ogg",
        "step_grass1.ogg": "minecraft/sounds/step/grass1.ogg",
        "step_grass2.ogg": "minecraft/sounds/step/grass2.ogg",
        "step_stone1.ogg": "minecraft/sounds/step/stone1.ogg",
        "step_stone2.ogg": "minecraft/sounds/step/stone2.ogg",
        "step_wood1.ogg": "minecraft/sounds/step/wood1.ogg",
        "step_wood2.ogg": "minecraft/sounds/step/wood2.ogg",
        "step_sand1.ogg": "minecraft/sounds/step/sand1.ogg",
        "step_gravel1.ogg": "minecraft/sounds/step/gravel1.ogg",
        "step_snow1.ogg": "minecraft/sounds/step/snow1.ogg",
        "step_cloth1.ogg": "minecraft/sounds/step/cloth1.ogg",
        "dig_grass.ogg": "minecraft/sounds/dig/grass1.ogg",
        "dig_stone.ogg": "minecraft/sounds/dig/stone1.ogg",
        "dig_wood.ogg": "minecraft/sounds/dig/wood1.ogg",
        "dig_sand.ogg": "minecraft/sounds/dig/sand1.ogg",
        "dig_gravel.ogg": "minecraft/sounds/dig/gravel1.ogg",
        "dig_snow.ogg": "minecraft/sounds/dig/snow1.ogg",
        "dig_cloth.ogg": "minecraft/sounds/dig/cloth1.ogg",
    }
    saved_count = 0
    for local_name, mc_key in wanted_sounds.items():
        if mc_key in objects_index:
            data = download_by_sha1(objects_index[mc_key]["hash"])
            (sound_out / local_name).write_bytes(data)
            saved_count += 1
    print(f"Saved {saved_count} .ogg sound effects to {sound_out}")


if __name__ == "__main__":
    build_atlas()
    copy_ui_and_theme_assets()
    fetch_prism_objects()
    print("Asset build complete!")
