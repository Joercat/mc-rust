# Horizon (web build)

Horizon is a Minecraft client written in Rust. This branch builds it for the
browser: the same client, the same renderer and the same world generator,
compiled to WebAssembly and served by a small static server. Nothing is
installed — open the page and play.

```sh
python3 scripts/serve_web.py 5173
# http://localhost:5173/            title screen
# http://localhost:5173/?world=demo straight into a world
```

The build is offline-only: singleplayer worlds run as an integrated server in
the page, and the multiplayer, LAN and profile features are removed.

Building from source, the query parameters, how the pieces fit together and
the known limitations are in [`docs/WEB.md`](docs/WEB.md).
