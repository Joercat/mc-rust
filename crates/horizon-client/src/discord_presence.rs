use crate::command::args::GameMode;

pub const DISCORD_APP_ID: &str = "1563857609738690560";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dimension {
    Overworld,
    Nether,
    End,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Play {
    Singleplayer,
    Multiplayer,
    Hosting { players: usize },
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Off,
    Menus,
    World {
        play: Play,
        dimension: Dimension,
        mode: GameMode,
    },
}

pub fn start(_enabled: bool, _automated: bool) {}
pub fn set(_status: Status) {}
pub fn stop() {}
