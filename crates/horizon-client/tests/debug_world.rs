//! The debug world end to end, headlessly: the generator's grid, and a round
//! trip through the region writer the autosave, pause save and close all use.
//! Skipped when the prepared resources are not in `.local/horizon-client`.
use horizon_client::{
    assets::Assets,
    biome::Biomes,
    chunk::Chunk,
    debuggen::{BARRIER_Y, DebugTerrain, LAYER_Y},
    save::{SaveDir, dimension_dir},
    streaming::ChunkGenerator,
};
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

fn resources() -> Option<PathBuf> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../.local/horizon-client")
        .join(horizon_core::VANILLA_VERSION);
    root.join("items.json").exists().then_some(root)
}

fn chunk(terrain: &DebugTerrain, x: i32, z: i32) -> Chunk {
    terrain.finish(terrain.proto(&mut (), x, z))
}

#[test]
fn the_grid_holds_every_registered_state_once() {
    let Some(root) = resources() else {
        return;
    };
    let assets = Assets::load(&root).unwrap();
    let terrain = DebugTerrain::new(assets.registry);
    let grid = terrain.grid();
    assert_eq!(grid.count(), assets.registry.len());
    // Every column of the grid is a different state, in registry order.
    for (x, z) in [(0, 0), (1, 0), (0, 1), (7, 3), (grid.width() - 1, 0)] {
        assert_eq!(
            terrain.state_at(x, z),
            Some(z as u32 * grid.width() as u32 + x as u32)
        );
    }
    assert_eq!(terrain.state_at(grid.width(), 0), None);
    // The barrier floor is the registry's barrier.
    assert_eq!(
        terrain.barrier(),
        assets.registry.default_state("minecraft:barrier").unwrap()
    );
}

#[test]
fn a_debug_world_saves_and_reloads_with_the_grid_intact() {
    let Some(root) = resources() else {
        return;
    };
    let assets = Assets::load(&root).unwrap();
    let biomes = Arc::new(Biomes::load(&root).unwrap());
    let terrain = DebugTerrain::new(assets.registry);
    let dir = horizon_core::vfs::temp_dir().join(format!(
        "horizon-debug-world-{}-{:?}",
        horizon_core::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    let save = SaveDir::new(dimension_dir(&dir, "minecraft:overworld"));
    // The chunks an autosave, a pause save or a close queue.
    let chunks: Vec<Arc<Chunk>> = [(0, 0), (1, 0), (0, 1)]
        .into_iter()
        .map(|(x, z)| Arc::new(chunk(&terrain, x, z)))
        .collect();
    save.save_later(&chunks, assets.registry, &biomes).unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while !save.writes_finished().unwrap() {
        assert!(std::time::Instant::now() < deadline, "the writer never finished");
        std::thread::sleep(std::time::Duration::from_millis(1));
    }
    for saved in &chunks {
        let loaded = save
            .load(saved.x, saved.z, assets.registry, &biomes)
            .unwrap_or_else(|| panic!("({}, {}) did not load back", saved.x, saved.z));
        assert_eq!((loaded.x, loaded.z), (saved.x, saved.z));
        for z in 0..16 {
            for x in 0..16 {
                let (wx, wz) = (saved.x * 16 + x, saved.z * 16 + z);
                assert_eq!(
                    loaded.get(x, LAYER_Y, z),
                    saved.get(x, LAYER_Y, z),
                    "state at {wx},{wz}"
                );
                assert_eq!(
                    loaded.get(x, BARRIER_Y, z),
                    saved.get(x, BARRIER_Y, z),
                    "platform at {wx},{wz}"
                );
            }
        }
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_debug_world_asks_for_its_own_generator() {
    // The dimension a create-screen debug world writes names the generator, and
    // the generator is found from that alone: no other code path is involved.
    let settings = horizon_world::nbt::Tag::compound([
        (
            "dimensions",
            horizon_world::nbt::Tag::compound([(
                "minecraft:overworld",
                horizon_world::nbt::Tag::compound([
                    ("type", horizon_world::nbt::Tag::String("minecraft:overworld".into())),
                    (
                        "generator",
                        horizon_world::nbt::Tag::compound([(
                            "type",
                            horizon_world::nbt::Tag::String("minecraft:debug".into()),
                        )]),
                    ),
                ]),
            )]),
        ),
    ]);
    let Some(root) = resources() else {
        return;
    };
    let assets = Assets::load(&root).unwrap();
    let terrain = DebugTerrain::from_settings(&settings, assets.registry).expect("a debug world");
    assert_eq!(terrain.grid().count(), assets.registry.len());
    // Any other generator is not this one.
    let normal = horizon_world::nbt::Tag::compound([(
        "dimensions",
        horizon_world::nbt::Tag::compound([(
            "minecraft:overworld",
            horizon_world::nbt::Tag::compound([
                ("type", horizon_world::nbt::Tag::String("minecraft:overworld".into())),
                (
                    "generator",
                    horizon_world::nbt::Tag::compound([(
                        "type",
                        horizon_world::nbt::Tag::String("minecraft:noise".into()),
                    )]),
                ),
            ]),
        )]),
    )]);
    assert!(DebugTerrain::from_settings(&normal, assets.registry).is_none());
}
