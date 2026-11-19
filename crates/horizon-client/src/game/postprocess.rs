//! LevelChunk.postProcessGeneration: positions that worldgen marked for
//! postprocessing (aquifer fluids next to open air, features' shape-dependent
//! blocks) are updated once the chunk is live, which starts cave waterfalls
//! and lava falls, raises bubble columns over magma and soul sand and fixes
//! block shapes against their neighbours.
use super::Game;
use horizon_client::{Result, blocks::Fluid};
use horizon_core::position::BlockPos;
use std::sync::Arc;

/// Block.UPDATE_INVISIBLE | UPDATE_KNOWN_SHAPE | UPDATE_SKIP_BLOCK_ENTITY_SIDEEFFECTS.
const POSTPROCESS_FLAGS: i32 = 276;

impl Game<'_> {
    pub(super) fn postprocess_chunks(&mut self) -> Result<()> {
        let ready: Vec<(i32, i32)> = horizon_client::spans::time("postprocess_scan", || self
            .world
            .chunks
            .iter()
            .filter(|(_, c)| c.lit && !c.postprocess.is_empty())
            .map(|(k, _)| *k)
            .filter(|&(x, z)| {
                (-1..=1)
                    .all(|dz| (-1..=1).all(|dx| self.world.chunks.contains_key(&(x + dx, z + dz))))
            })
            .collect());
        if ready.is_empty() {
            return Ok(());
        }
        // The ticks the marks schedule go to the chunks' containers, which a
        // chunk that arrived since the last tick doesn't have yet
        // (`LevelChunk.registerTickContainerInLevel` comes before
        // `postProcessGeneration`).
        horizon_client::spans::time("postprocess_register", || self.sync_tick_chunks());
        // Post-processing a whole view's columns in one tick was 23 ms of the
        // tick a world opened on. The chunks the budget does not reach keep
        // their marks (`postprocess` stays non-empty) and come back next tick.
        const POST_BUDGET: std::time::Duration = std::time::Duration::from_millis(2);
        let deadline = horizon_core::time::Instant::now() + POST_BUDGET;
        // The marks are taken off their columns first, then run in one level
        // view: building a view per column was 16.7 ms of a world-opening
        // tick (117 columns at about 0.14 ms each), more than the work the
        // marks themselves do.
        let mut work: Vec<((i32, i32), Vec<[i32; 3]>)> = Vec::new();
        for key in ready {
            let Some(chunk) = self.world.chunks.get_mut(&key) else {
                continue;
            };
            // `take` needs the column to itself: while the mesher still holds
            // this one (a world that just opened meshes its whole view),
            // `Arc::make_mut` would copy the column to take its marks, and 117
            // of those in one tick was 14 ms of it. A column another thread is
            // still holding keeps its marks and comes back when it is done.
            let Some(chunk) = Arc::get_mut(chunk) else {
                continue;
            };
            work.push((key, std::mem::take(&mut chunk.postprocess)));
        }
        // A single column can carry thousands of marks (a cave system's
        // fluids), which took 152 ms of one tick on its own: the marks are run
        // in column then mark order up to the budget too, and the rest go back
        // to the front of their column's queue for the next tick.
        let mut rest: Vec<((i32, i32), Vec<[i32; 3]>)> = Vec::new();
        self.with_level(|b, level, ticks| {
            for (key, marks) in work {
                let mut done = 0;
                for [x, y, z] in marks.iter().copied() {
                    if horizon_core::time::Instant::now() >= deadline {
                        break;
                    }
                    done += 1;
                    let mark = horizon_core::time::Instant::now();
                    let pos = BlockPos::new(x, y, z);
                    let state = level.world.block(x, y, z);
                    let facts = level.facts.get(state);
                    let (fluid, liquid_block) = (
                        facts.fluid != Fluid::None,
                        matches!(&*facts.name, "minecraft:water" | "minecraft:lava"),
                    );
                    if fluid {
                        b.scheduled_fluid_tick(level, ticks, pos);
                    }
                    if liquid_block {
                        // LiquidBlock.tick: bubble columns over soul sand and magma.
                        b.scheduled_block_tick(level, ticks, pos);
                    } else {
                        let updated = b.update_from_neighbour_shapes(level, ticks, pos, state);
                        if updated != state {
                            b.set_block(level, ticks, pos, updated, POSTPROCESS_FLAGS);
                        }
                    }
                    // A mark that is one block's shape check is far inside the
                    // budget; one whose `set_block` sets off a fluid or bubble
                    // column cascade is not, and is named apart.
                    let took = mark.elapsed();
                    if took >= std::time::Duration::from_millis(3) {
                        horizon_client::spans::add("postprocess_mark_slow", took);
                    }
                }
                if done < marks.len() {
                    rest.push((key, marks[done..].to_vec()));
                }
            }
        });
        for (key, marks) in rest {
            if let Some(chunk) = self.world.chunks.get_mut(&key).and_then(Arc::get_mut) {
                chunk.postprocess.splice(0..0, marks);
            }
        }
        self.process_level_events()
    }
}
