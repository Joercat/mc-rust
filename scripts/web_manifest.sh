#!/usr/bin/env bash
# Write `public/assets.json`: the SHA-256 of every published build output.
#
# `public/asset-cache.js` keys its IndexedDB copies on these hashes, so a load
# that already has an asset's bytes makes no request for it, and a rebuild only
# downloads the files whose hash changed. The file also records the build stamp
# the page's own code carries, which is what tells the page its stored manifest
# is still the one it was built against.
#
# Run it after publishing anything into `public/`. `scripts/build_web.sh` does,
# and so does `scripts/render_build.sh`: Render downloads the resource pack into
# the tree at deploy time, so its hash is only known once it is there.
set -euo pipefail
cd "$(dirname "$0")/.."

build="$(sed -n 's/^const BUILD = "\(.*\)";$/\1/p' public/app.js | head -1)"
if [ -z "$build" ]; then
  echo "web_manifest: no BUILD constant in public/app.js" >&2
  exit 1
fi
if [ ! -s public/horizon-client_bg.wasm ]; then
  echo "web_manifest: public/horizon-client_bg.wasm is missing" >&2
  exit 1
fi

hash_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

out=public/assets.json
tmp="$out.tmp"
{
  printf '{\n  "build": "%s",\n  "files": {\n' "$build"
  first=1
  for name in \
    index.html app.js threads.js thread-worker.js asset-cache.js \
    horizon-client.js horizon-client_bg.wasm \
    horizon-client-threaded.js horizon-client-threaded_bg.wasm \
    horizon-vfs.bin; do
    [ -f "public/$name" ] || continue
    if [ "$first" -eq 1 ]; then
      first=0
    else
      printf ',\n'
    fi
    printf '    "%s": "%s"' "$name" "$(hash_of "public/$name")"
  done
  printf '\n  }\n}\n'
} > "$tmp"
mv "$tmp" "$out"
echo "web_manifest: wrote $out for build $build"
