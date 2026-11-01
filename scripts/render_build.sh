#!/usr/bin/env bash
# Render's build command for the web client (see render.yaml).
#
# `public/` on this branch has the page and both engines, but not the resource
# pack: `public/horizon-vfs.bin` is hundreds of megabytes and is a build output,
# so it is uploaded once (a GitHub release asset does the job) and downloaded
# here from HORIZON_VFS_URL, the environment variable the Blueprint declares.
#
# Repacking after changing the game's assets means uploading a new asset and
# pointing HORIZON_VFS_URL at it; nothing else here changes.
set -euo pipefail
cd "$(dirname "$0")/.."

# Both engines and their glue are part of this branch. A checkout without them
# would deploy a page that cannot start, so stop with the missing name.
for file in \
  public/horizon-client.js \
  public/horizon-client_bg.wasm \
  public/horizon-client-threaded.js \
  public/horizon-client-threaded_bg.wasm; do
  if [ ! -s "$file" ]; then
    echo "render_build: $file is missing from the checkout" >&2
    exit 1
  fi
done

pack=public/horizon-vfs.bin
if [ -s "$pack" ]; then
  echo "render_build: using the resource pack in the checkout"
else
  url="${HORIZON_VFS_URL:-}"
  if [ -z "$url" ]; then
    echo "render_build: no resource pack, and HORIZON_VFS_URL is not set." >&2
    echo "  Upload public/horizon-vfs.bin as a GitHub release asset and set" >&2
    echo "  HORIZON_VFS_URL to its download URL in the Render Dashboard." >&2
    exit 1
  fi
  echo "render_build: downloading the resource pack from $url"
  # The download is renamed into place only when it finished, so an interrupted
  # one cannot leave a short file for the checks below to pass.
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 2 --connect-timeout 30 -o "$pack.part" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --tries=3 -O "$pack.part" "$url"
  else
    echo "render_build: neither curl nor wget is available to download the pack" >&2
    exit 1
  fi
  mv "$pack.part" "$pack"
fi

# The page mounts the pack as a whole archive, so a placeholder (an HTML error
# page, an LFS pointer, a truncated transfer) has to fail the deploy here rather
# than in the browser: check the magic and a size no real pack is under.
magic="$(head -c 8 "$pack" || true)"
if [ "$magic" != "HZVFS001" ]; then
  echo "render_build: $pack does not start with the HZVFS001 magic" >&2
  exit 1
fi
bytes="$(wc -c < "$pack" | tr -d ' ')"
if [ "$bytes" -lt 50000000 ]; then
  echo "render_build: $pack is only $bytes bytes; the real pack is hundreds of MB" >&2
  exit 1
fi
echo "render_build: resource pack $((bytes / 1000000)) MB"
