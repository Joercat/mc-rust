#!/usr/bin/env python3
"""Re-encode a prepared resource pack's PNG textures smaller.

The block, item, GUI and entity atlases are thousands of small PNGs. Most of
them were saved by a paint program at a larger size than the pixels need, so
the same image fits in fewer bytes: `--mode lossless` re-packs them with
oxipng (identical pixels, typically 5-20% off), and `--mode quantize` also
reduces them to a palette (`--colors 256`, `--quality` 0-100 for pngquant's
minimum acceptable quality) for a much larger cut on soft or shaded art at the
cost of a little banding.

Neither mode changes a pixel's colour where the file is kept as it was: a
re-encoded file that did not get smaller is replaced by the original. The
pack itself is not touched -- the tree is written to OUT_DIR, with every file
that is not a texture linked in from RESOURCES_DIR -- and `pack_vfs.py`
packs that instead:

  python3 scripts/pack_vfs.py RESOURCES_DIR /tmp/empty-cache public/horizon-vfs.bin \\
      --audio-bitrate 64k --texture-mode lossless

Usage:
  python3 scripts/compress_textures.py RESOURCES_DIR OUT_DIR [options]

Options:
  --mode lossless|quantize  what to do to each PNG (default lossless)
  --quality 0..100          pngquant's minimum quality for quantize (default 90)
  --colors 2..256           palette size for the Pillow fallback (default 256)
  --oxipng PATH             oxipng binary (default: oxipng on PATH, else Pillow)
  --pngquant PATH           pngquant binary (default: pngquant on PATH, else Pillow)
  --jobs N                  parallel encoders (default: CPU count)
  --skip-existing           keep textures already in OUT_DIR (resume)
"""
import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from overlay_tree import link_tree, replace_with

KIB = 1024
MIB = 1024 * KIB


def locate(name, explicit):
    """An encoder binary, or None when it is not installed."""
    if explicit:
        return explicit if os.path.isfile(explicit) else shutil.which(explicit)
    return shutil.which(name)


def pillow_recompress(src, tmp, mode, quality, colors):
    """The fallback when neither tool is installed: Pillow does both modes."""
    from PIL import Image

    with Image.open(src) as image:
        image.load()
        if mode == "quantize":
            # A palette keeps straight alpha exactly where it is opaque, which
            # is what the atlases are; a soft alpha edge costs some dithering.
            largest = max(8, min(colors, 2 + int(254 * quality / 100.0)))
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")
            # Only the octree quantizers accept an alpha channel; the median cut
            # is the better one otherwise.
            method = (
                Image.Quantize.FASTOCTREE
                if image.mode == "RGBA"
                else Image.Quantize.MEDIANCUT
            )
            image = image.quantize(colors=largest, method=method)
        image.save(tmp, format="PNG", optimize=True, compress_level=9)


def recompress(src, dst, mode, quality, colors, oxipng, pngquant):
    """Re-encode `src` into `dst`; returns (kind, source bytes, output bytes)."""
    tmp = dst + ".tmp.png"
    kind = "copied"
    try:
        if mode == "quantize" and pngquant:
            subprocess.run(
                [
                    pngquant, f"--quality={quality}-100", "--speed", "3",
                    "--strip", "--force", "--output", tmp, src,
                ],
                check=True, capture_output=True,
            )
            kind = "quantized"
        elif mode == "quantize":
            # No pngquant: Pillow still palettes the image, and oxipng (when it
            # is there) packs the smaller palette afterwards.
            pillow_recompress(src, tmp, mode, quality, colors)
            kind = "quantized"
            if oxipng:
                requantized = tmp + ".png"
                subprocess.run(
                    [oxipng, "-o", "2", "--strip", "safe", "--quiet", tmp],
                    check=True, capture_output=True,
                )
                if os.path.exists(requantized):
                    os.remove(requantized)
        elif oxipng:
            shutil.copy2(src, tmp)
            subprocess.run(
                [oxipng, "-o", "2", "--strip", "safe", "--quiet", tmp],
                check=True, capture_output=True,
            )
            kind = "optimized"
        else:
            pillow_recompress(src, tmp, mode, quality, colors)
            kind = "optimized"
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        # Nothing useful from the encoder: keep the original bytes.
        if os.path.exists(tmp):
            os.remove(tmp)
        shutil.copy2(src, tmp)
        kind = "copied"
        print(f"compress_textures.py: {src}: {error}", file=sys.stderr)

    if os.path.getsize(tmp) >= os.path.getsize(src):
        os.remove(tmp)
        shutil.copy2(src, tmp)
        kind = "copied"
    replace_with(tmp, dst)
    return kind, os.path.getsize(src), os.path.getsize(dst)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resources", help="prepared resource folder (has horizon-resources.json)")
    parser.add_argument("out", help="where the re-encoded tree goes")
    parser.add_argument("--mode", choices=("lossless", "quantize"), default="lossless")
    parser.add_argument("--quality", type=int, default=90)
    parser.add_argument("--colors", type=int, default=256)
    parser.add_argument("--oxipng", default="oxipng")
    parser.add_argument("--pngquant", default="pngquant")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    if not os.path.isdir(args.resources):
        raise SystemExit(f"compress_textures.py: {args.resources} is not a directory")
    oxipng = locate("oxipng", args.oxipng)
    pngquant = locate("pngquant", args.pngquant) if args.mode == "quantize" else None
    if not oxipng and not pngquant:
        try:
            import PIL  # noqa: F401
        except ImportError:
            raise SystemExit(
                "compress_textures.py: no encoder. Install one of\n"
                "  oxipng   apt install oxipng | brew install oxipng |\n"
                "           https://github.com/shssoichiro/oxipng/releases\n"
                "  pngquant apt install pngquant | brew install pngquant\n"
                "  Pillow   python3 -m pip install pillow"
            )

    textures = []
    for root, dirs, files in os.walk(args.resources):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(".png"):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, args.resources)
                textures.append((full, os.path.join(args.out, rel)))
    if not textures:
        print("compress_textures.py: no .png files found", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)
    # Every other file (models, sounds, the manifest) is still part of the pack.
    linked = link_tree(args.resources, args.out)
    if linked:
        print(f"compress_textures.py: linked {linked} other files into {args.out}")

    state = {"optimized": 0, "quantized": 0, "copied": 0, "skipped": 0, "failed": 0}
    before = after = 0

    def one(pair):
        nonlocal before, after
        src, dst = pair
        if args.skip_existing and os.path.isfile(dst) and not os.path.samefile(src, dst):
            state["skipped"] += 1
            return
        try:
            kind, source_bytes, output_bytes = recompress(
                src, dst, args.mode, args.quality, args.colors, oxipng, pngquant
            )
        except OSError as error:
            state["failed"] += 1
            print(f"compress_textures.py: {src}: {error}", file=sys.stderr)
            return
        state[kind] += 1
        before += source_bytes
        after += output_bytes

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        list(pool.map(one, textures))

    if before:
        saved = 100.0 * (before - after) / before
        print(
            f"Re-encoded {len(textures)} textures ({args.mode}): "
            f"{before / MIB:.1f} MB -> {after / MIB:.1f} MB ({saved:.1f}% smaller): "
            f"{state['optimized']} optimized, {state['quantized']} quantized, "
            f"{state['copied']} kept, {state['skipped']} skipped, {state['failed']} failed"
        )
    return 0 if state["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
