//! The debug world's layout, proven on its own: the `Grid` the generator
//! applies to chunks, taken from its real source file, with the content at
//! chunk boundaries asserted. No world, no registry, no GPU.
#[path = "../crates/horizon-client/src/debuggen/grid.rs"]
mod grid;
use grid::{BARRIER_Y, Grid, LAYER_Y};
use std::collections::HashMap;

fn main() {
    // A 40x40 grid: wider than two chunks, so chunk borders fall inside it.
    let states: Vec<u32> = (1000..1000 + 40 * 40).collect();
    let g = Grid::new(states.len());
    assert_eq!(g.width(), 40);
    let mut blocks = HashMap::new();
    for cz in 0..3 {
        for cx in 0..3 {
            for (x, y, z, state) in g.place(cx, cz, &states, 9) {
                blocks.insert((cx * 16 + x, y, cz * 16 + z), state);
            }
        }
    }
    let at = |x: i32, y: i32, z: i32| blocks.get(&(x, y, z)).copied();
    assert_eq!(at(0, LAYER_Y, 0), Some(1000));
    assert_eq!(at(15, LAYER_Y, 0), Some(1015));
    assert_eq!(at(16, LAYER_Y, 0), Some(1016), "column 16 is chunk 1");
    assert_eq!(at(0, LAYER_Y, 1), Some(1040), "a row is 40 states");
    assert_eq!(at(39, LAYER_Y, 39), Some(2599), "the last state");
    assert_eq!(at(0, BARRIER_Y, 0), Some(9));
    assert_eq!(at(39, BARRIER_Y, 39), Some(9));
    assert_eq!(at(0, LAYER_Y - 2, 0), None, "air below the platform");
    assert_eq!(at(40, LAYER_Y, 0), None, "nothing past the grid");
    assert_eq!(at(40, BARRIER_Y, 0), None, "no platform past the grid");
    println!(
        "debug grid: {} states in a {}x{} grid, {} blocks placed over {} chunks",
        states.len(),
        g.width(),
        g.width(),
        blocks.len(),
        blocks.len() / 512
    );
    println!(
        "debug grid: (0,0)=1000, (15,0)=1015, (16,0)=1016, (0,1)=1040, (39,39)=2599; \
         barrier {BARRIER_Y} under every column; air past x=39"
    );
    println!("DEBUG_GRID_OK");
}
