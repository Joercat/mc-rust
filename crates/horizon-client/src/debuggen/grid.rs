//! The debug world's layout, as arithmetic: `DebugLevelSource` places every
//! block state once, in a square grid in the x/z plane at y 70, over a barrier
//! platform at y 69. The grid is what makes chunk borders visible: a chunk is
//! 16 columns of it, side by side, with no terrain under the edges.
//!
//! Nothing here touches a `Chunk`, so the layout can be tested on its own and
//! the generator only has to apply what it hands back.

/// `DebugLevelSource`: the y every block state sits at.
pub const LAYER_Y: i32 = 70;
/// The barrier platform's y, one below the states.
pub const BARRIER_Y: i32 = LAYER_Y - 1;

/// A square grid holding `count` states, `width` blocks a side.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Grid {
    width: i32,
    count: usize,
}
impl Grid {
    /// `Mth.ceil(Mth.sqrt(count))` blocks a side.
    pub fn new(count: usize) -> Self {
        let mut width = 1i32;
        while (width as usize) * (width as usize) < count {
            width += 1;
        }
        Self { width, count }
    }
    pub fn width(&self) -> i32 {
        self.width
    }
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn count(&self) -> usize {
        self.count
    }
    /// The state index at a world column, if the grid covers it.
    pub fn index(&self, x: i32, z: i32) -> Option<usize> {
        if x < 0 || z < 0 || x >= self.width || z >= self.width {
            return None;
        }
        let index = z as usize * self.width as usize + x as usize;
        (index < self.count).then_some(index)
    }
    /// The blocks a chunk holds: `(x, y, z)` inside the chunk and the state
    /// there. Columns outside the grid stay air, which is what draws the border.
    pub fn place<'a>(
        &'a self,
        chunk_x: i32,
        chunk_z: i32,
        states: &'a [u32],
        barrier: u32,
    ) -> impl Iterator<Item = (i32, i32, i32, u32)> + 'a {
        (0..16).flat_map(move |z| {
            (0..16).flat_map(move |x| {
                let column = self.index(chunk_x * 16 + x, chunk_z * 16 + z);
                column
                    .into_iter()
                    .flat_map(move |index| {
                        [
                            (x, LAYER_Y, z, states[index]),
                            (x, BARRIER_Y, z, barrier),
                        ]
                    })
                    .collect::<Vec<_>>()
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_grid_is_a_square_of_the_state_count() {
        assert_eq!(Grid::new(0).width(), 1);
        assert_eq!(Grid::new(1).width(), 1);
        assert_eq!(Grid::new(2).width(), 2);
        assert_eq!(Grid::new(256).width(), 16);
        assert_eq!(Grid::new(257).width(), 17);
        assert_eq!(Grid::new(26_000).width(), 162);
    }

    #[test]
    fn columns_map_row_by_row_in_z() {
        let grid = Grid::new(26_000);
        assert_eq!(grid.index(0, 0), Some(0));
        assert_eq!(grid.index(15, 0), Some(15));
        assert_eq!(grid.index(0, 1), Some(162));
        assert_eq!(grid.index(161, 161), Some(161 * 162 + 161));
        assert_eq!(grid.index(162, 0), None);
        assert_eq!(grid.index(-1, 0), None);
    }

    #[test]
    fn a_chunk_holds_the_grids_columns_and_a_barrier_under_them() {
        let states: Vec<u32> = (1000..1000 + 300).collect();
        let grid = Grid::new(states.len());
        let blocks: Vec<_> = grid.place(0, 0, &states, 9).collect();
        // 256 columns, each a state over a barrier.
        assert_eq!(blocks.len(), 512);
        let at = |x: i32, y: i32, z: i32| {
            blocks
                .iter()
                .find(|b| (b.0, b.1, b.2) == (x, y, z))
                .map(|b| b.3)
        };
        assert_eq!(at(0, LAYER_Y, 0), Some(1000));
        assert_eq!(at(15, LAYER_Y, 0), Some(1015));
        assert_eq!(at(0, LAYER_Y, 1), Some(1018)); // width 18: z = 1 starts at 18
        assert_eq!(at(0, BARRIER_Y, 0), Some(9));
        assert_eq!(at(15, BARRIER_Y, 15), Some(9));
        // The chunk next door carries the next columns of the grid.
        let next: Vec<_> = grid.place(1, 0, &states, 9).collect();
        assert_eq!(next.len(), 512);
        assert!(next.iter().any(|b| (b.0, b.1, b.2) == (0, LAYER_Y, 0) && b.3 == 1016));
        // Past the grid's edge there is nothing at all: air, no platform.
        let outside: Vec<_> = grid.place(2, 0, &states, 9).collect();
        assert!(outside.is_empty(), "{:?}", &outside[..outside.len().min(4)]);
    }
}
