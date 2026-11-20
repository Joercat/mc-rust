# Horizon (web build)

Horizon is a Minecraft client written in Rust. This branch builds it for the
browser: the same client, the same renderer and the same world generator,
compiled to WebAssembly and served by a small static server. Nothing is
installed — open the page and play.

**No machine of your own to serve it from?** <https://horizon-web-qi24g0-preview.freebuff.site>
— a top-level tab, the threaded engine, nothing installed.

```sh
python3 scripts/serve_web.py --fetch 5173
# http://localhost:5173/            title screen
# http://localhost:5173/?world=demo straight into a world
```

`--fetch` downloads the 400 MB resource pack once (the page keeps its copy in
the browser afterwards); the original release asset is the source. The hosted
route above serves the pack from object storage in 8 MiB parts, because a site
takes assets of at most 10 MiB — see [docs/WEB.md](docs/WEB.md).

The build is offline-only: singleplayer worlds run as an integrated server in
the page, and the multiplayer, LAN and profile features are removed.

Building from source, the query parameters, how the pieces fit together and
the known limitations are in [`docs/WEB.md`](docs/WEB.md).
