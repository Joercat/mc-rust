#!/usr/bin/env python3
"""Re-encode a prepared resource pack's Ogg Vorbis audio at a lower bitrate.

The sound library is the bulk of the web build's `horizon-vfs.bin`: thousands
of short effects stored at 128-320 kbit/s. Vorbis at 64 kbit/s (96 kbit/s for
music and long ambience) is transparent enough for sounds that play once, and
cuts the pack by well over half.

The pack itself is not touched: the re-encoded tree is written to OUT_DIR with
the same layout, and `pack_vfs.py --audio-bitrate` packs that instead. Files
already at or below the target are copied, not re-encoded, so nothing is
degraded twice.

Usage:
  python3 scripts/compress_audio.py RESOURCES_DIR OUT_DIR [options]

Options:
  --bitrate 64k        target for files up to --long-after (default 64k)
  --long-bitrate 96k   target for longer files (default 96k)
  --long-after 30      seconds that count as long (default 30)
  --ffmpeg PATH        ffmpeg binary (default: ffmpeg on PATH)
  --jobs N             parallel encoders (default: CPU count)
  --skip-existing      keep files already present in OUT_DIR (resume)
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

KIB = 1024
MIB = 1024 * KIB


def parse_bitrate(text):
    """`64k`, `96K`, `64000` -> bits per second."""
    value = text.strip().lower()
    scale = 1
    if value.endswith("k"):
        value, scale = value[:-1], 1000
    elif value.endswith("m"):
        value, scale = value[:-1], 1_000_000
    try:
        return int(float(value) * scale)
    except ValueError:
        raise SystemExit(f"compress_audio.py: not a bitrate: {text!r}")


def tools(ffmpeg):
    ffmpeg = shutil.which(ffmpeg) or ffmpeg
    if not os.path.isfile(ffmpeg):
        raise SystemExit(
            f"compress_audio.py: no ffmpeg at {ffmpeg!r}.\n"
            "  Install it (apt install ffmpeg, brew install ffmpeg) or pass\n"
            "  --ffmpeg PATH. The static builds from johnvansickle.com work."
        )
    ffprobe = os.path.join(os.path.dirname(os.path.abspath(ffmpeg)), "ffprobe")
    if not os.path.isfile(ffprobe):
        ffprobe = shutil.which("ffprobe") or "ffprobe"
    return ffmpeg, ffprobe


def probe(ffprobe, path):
    """(seconds, bitrate, channels) of the first audio stream."""
    out = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=bit_rate,channels:format=duration,bit_rate",
            "-of", "json", path,
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    stream = (data.get("streams") or [{}])[0]
    container = data.get("format") or {}
    duration = float(container.get("duration") or 0.0)
    bitrate = int(stream.get("bit_rate") or container.get("bit_rate") or 0)
    return duration, bitrate, int(stream.get("channels") or 0)


def encode(ffmpeg, src, dst, target):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-map", "0:a:0", "-vn", "-map_metadata", "-1",
            "-c:a", "libvorbis", "-b:a", str(target), dst,
        ],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resources", help="prepared resource folder (has horizon-resources.json)")
    parser.add_argument("out", help="where the re-encoded tree goes")
    parser.add_argument("--bitrate", default="64k")
    parser.add_argument("--long-bitrate", default="96k")
    parser.add_argument("--long-after", type=float, default=30.0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()

    bitrate = parse_bitrate(args.bitrate)
    long_bitrate = parse_bitrate(args.long_bitrate)
    ffmpeg, ffprobe = tools(args.ffmpeg)
    if not os.path.isdir(args.resources):
        raise SystemExit(f"compress_audio.py: {args.resources} is not a directory")

    sounds = []
    for root, dirs, files in os.walk(args.resources):
        dirs.sort()
        for name in sorted(files):
            if name.lower().endswith(".ogg"):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, args.resources)
                sounds.append((full, os.path.join(args.out, rel)))
    if not sounds:
        print("compress_audio.py: no .ogg files found", file=sys.stderr)
        return 1
    os.makedirs(args.out, exist_ok=True)

    before = sum(os.path.getsize(src) for src, _ in sounds)
    state = {"copied": 0, "encoded": 0, "skipped": 0, "failed": 0}

    def one(pair):
        src, dst = pair
        if args.skip_existing and os.path.isfile(dst):
            state["skipped"] += 1
            return
        try:
            duration, source_rate, _ = probe(ffprobe, src)
            target = long_bitrate if duration > args.long_after else bitrate
            # Already at or under the target: copying keeps it from losing a
            # second generation to re-encoding.
            if (source_rate and source_rate <= target * 1.05) or duration <= 0.0:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                state["copied"] += 1
                return
            encode(ffmpeg, src, dst, target)
            if os.path.getsize(dst) >= os.path.getsize(src):
                shutil.copy2(src, dst)
                state["copied"] += 1
            else:
                state["encoded"] += 1
        except (subprocess.CalledProcessError, ValueError, OSError) as error:
            state["failed"] += 1
            print(f"compress_audio.py: {src}: {error}", file=sys.stderr)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        list(pool.map(one, sounds))

    after = sum(
        os.path.getsize(dst) for _, dst in sounds if os.path.isfile(dst)
    )
    print(
        f"Compressed {len(sounds)} sounds: {before / MIB:.1f} MB -> {after / MIB:.1f} MB "
        f"({state['encoded']} re-encoded at {args.bitrate}/{args.long_bitrate}, "
        f"{state['copied']} already small, {state['failed']} failed)"
    )
    return 0 if state["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
