//! DebugLevelSource: the debug world's chunk generator. Every block state the
//! registry holds appears once, in a flat grid over a barrier platform, so the
//! block models, block states and textures can be looked at in the world.
mod grid;
pub use grid::{BARRIER_Y, Grid, LAYER_Y};

use crate::{chunk::Chunk, streaming::ChunkGenerator};
use horizon_world::{
    nbt::Tag,
    registry::{BlockRegistry, BlockStateId},
};

/// The generator of a `minecraft:debug` overworld.
pub struct DebugTerrain {
    /// The states in registry order, the order the grid lays them out.
    states: Vec<u32>,
    grid: Grid,
    barrier: u32,
    biome: u32,
}
impl DebugTerrain {
    pub fn new(registry: &BlockRegistry) -> Self {
        Self::with_states(
            (0..registry.len() as u32).collect(),
            registry.default_state("minecraft:barrier").map_or(0, |b| b.0),
        )
    }
    /// The layout for an explicit list of states: the registry's order is the
    /// grid's order, and tests can name their own.
    pub fn with_states(states: Vec<u32>, barrier: u32) -> Self {
        Self {
            grid: Grid::new(states.len()),
            states,
            barrier,
            biome: 0,
        }
    }
    /// The debug generator of a saved world whose overworld asks for one.
    pub fn from_settings(settings: &Tag, registry: &BlockRegistry) -> Option<Self> {
        let generator = settings
            .get("dimensions")?
            .get("minecraft:overworld")?
            .get("generator")?;
        let kind = crate::save::text(generator.get("type"))?;
        (kind.trim_start_matches("minecraft:") == "debug").then(|| Self::new(registry))
    }
    pub fn grid(&self) -> Grid {
        self.grid
    }
    /// `DebugLevelSource.getSpawnHeight`: on top of the platform.
    pub fn spawn_height(&self) -> i32 {
        LAYER_Y + 1
    }
    /// The state the grid puts at a world column, if it covers it.
    pub fn state_at(&self, x: i32, z: i32) -> Option<u32> {
        self.grid.index(x, z).map(|i| self.states[i])
    }
    /// Whether the grid holds a state at this column.
    pub fn covers(&self, x: i32, z: i32) -> bool {
        self.grid.index(x, z).is_some()
    }
    pub fn barrier(&self) -> BlockStateId {
        BlockStateId(self.barrier)
    }
}
impl ChunkGenerator for DebugTerrain {
    type Worker = ();
    fn worker(&self) {}
    fn lod_surface(&self, _: &mut (), _: i32, _: i32) -> Option<crate::lod::Surface> {
        // Far terrain reads the platform as the surface, so the debug world does
        // not fade into hills it does not have.
        Some(crate::lod::Surface {
            height: self.spawn_height(),
            state: BlockStateId(self.barrier),
            water: None,
            biome: self.biome as u16,
        })
    }
    fn proto(&self, _: &mut (), cx: i32, cz: i32) -> crate::streaming::Proto {
        let mut chunk = Chunk::empty(cx, cz);
        for section in &mut chunk.sections {
            std::sync::Arc::make_mut(section).biomes =
                horizon_core::paletted::PalettedContainer::new(crate::chunk::BIOME_STRATEGY, self.biome);
        }
        for (x, y, z, state) in self.grid.place(cx, cz, &self.states, self.barrier) {
            chunk.set(x, y, z, state);
        }
        for section in &mut chunk.sections {
            std::sync::Arc::make_mut(section).compact();
        }
        Box::new(chunk)
    }
    /// DebugLevelSource has no decoration and no structures.
    fn finish(&self, proto: crate::streaming::Proto) -> Chunk {
        *proto.downcast::<Chunk>().expect("debug terrain proto")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn terrain() -> DebugTerrain {
        // A grid wider than two chunks, so the chunk boundary is inside it.
        DebugTerrain::with_states((500..500 + 40 * 40).collect(), 7)
    }

    fn chunk(terrain: &DebugTerrain, cx: i32, cz: i32) -> Chunk {
        terrain.finish(terrain.proto(&mut (), cx, cz))
    }

    #[test]
    fn a_debug_chunk_holds_the_grids_states_over_a_barrier_floor() {
        let terrain = terrain();
        let grid = terrain.grid();
        assert_eq!(grid.width(), 40);
        for (cx, cz) in [(0, 0), (1, 0), (0, 2), (2, 2)] {
            let chunk = chunk(&terrain, cx, cz);
            for z in 0..16 {
                for x in 0..16 {
                    let (wx, wz) = (cx * 16 + x, cz * 16 + z);
                    let expected = terrain.state_at(wx, wz);
                    assert_eq!(
                        chunk.get(x, LAYER_Y, z),
                        expected.unwrap_or(0),
                        "state at {wx},{wz}"
                    );
                    if expected.is_some() {
                        assert_eq!(chunk.get(x, BARRIER_Y, z), 7, "barrier under {wx},{wz}");
                        assert_eq!(chunk.get(x, LAYER_Y - 2, z), 0, "air below {wx},{wz}");
                    } else {
                        assert_eq!(chunk.get(x, BARRIER_Y, z), 0, "no floor at {wx},{wz}");
                    }
                }
            }
        }
    }

    #[test]
    fn the_border_of_the_grid_is_the_border_of_a_chunk() {
        let terrain = terrain();
        // 40 columns: chunks 0 and 1 are inside it, chunk 2 starts at column 32
        // and stops at 39, and chunk 3 is air.
        assert!(terrain.covers(39, 39));
        assert!(!terrain.covers(40, 0));
        let edge = chunk(&terrain, 2, 0);
        assert_ne!(edge.get(7, LAYER_Y, 0), 0, "column 39 is the last state");
        assert_eq!(edge.get(8, LAYER_Y, 0), 0, "column 40 is past the grid");
        let beyond = chunk(&terrain, 3, 0);
        for z in 0..16 {
            for x in 0..16 {
                assert_eq!(beyond.get(x, LAYER_Y, z), 0);
                assert_eq!(beyond.get(x, BARRIER_Y, z), 0);
            }
        }
    }

    #[test]
    fn the_grid_is_the_registrys_states_in_order() {
        let terrain = terrain();
        assert_eq!(terrain.grid().count(), 1600);
        assert_eq!(terrain.state_at(0, 0), Some(500));
        assert_eq!(terrain.state_at(39, 0), Some(539));
        assert_eq!(terrain.state_at(0, 1), Some(540));
        assert_eq!(terrain.state_at(1, 39), Some(1599));
        assert_eq!(terrain.spawn_height(), LAYER_Y + 1);
    }
}
