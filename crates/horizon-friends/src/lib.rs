use std::fmt;

pub const ADDRESS_PREFIX: &str = "friend:";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathMode { Auto, RelayOnly, DirectOnly }
impl PathMode { pub fn from_env() -> Self { Self::Auto } }

pub fn relay_url() -> Option<String> { None }

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Code { pub nameplate: String }
impl fmt::Display for Code {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "{}", self.nameplate) }
}

pub mod stun {
    pub fn servers_from_env() -> Vec<String> { Vec::new() }
    pub fn parse_servers(_s: &str) -> Vec<String> { Vec::new() }
}

pub mod reach {
    pub const HELP_URL: &str = "";
    #[derive(Clone, Debug, Default)]
    pub struct Options {
        pub map_port: bool,
        pub stun: Vec<String>,
        pub loopback: bool,
    }
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub enum Verdict { Public, Mapped(u16), LocalOnly }
}

pub mod invite {
    use super::Code;
    use std::net::SocketAddr;
    pub const SECRET: usize = 10;
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub struct RelayRef { pub address: String, pub code: Code }
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub struct Invite {
        pub secret: [u8; SECRET],
        pub port: u16,
        pub endpoints: Vec<SocketAddr>,
        pub relay: Option<RelayRef>,
    }
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub enum Parsed { Short(Code), Long(Invite) }
    pub fn parse(_code: &str) -> Option<Parsed> { None }
}

pub mod host {
    use super::{Code, PathMode, reach};
    use std::{io, net::SocketAddr};
    pub const MAX_WRONG: u32 = 5;
    pub const DEFAULT_PORT: u16 = 25580;
    #[derive(Clone)]
    pub struct HostConfig {
        pub lan: SocketAddr,
        pub protocol: i32,
        pub game: String,
        pub relay: Option<String>,
        pub path: PathMode,
        pub port: u16,
        pub reach: reach::Options,
    }
    #[derive(Clone, Debug, Default)]
    pub struct HostState {
        pub invite: Option<String>,
        pub short: Option<Code>,
        pub summary: String,
        pub verdict: Option<reach::Verdict>,
        pub relay_problem: Option<String>,
        pub port: u16,
    }
    #[derive(Debug)]
    pub enum HostEvent {
        Code(String),
        WrongCode { count: u32 },
        Connected { direct: bool, peer: SocketAddr },
        Disconnected { peer: SocketAddr },
    }
    pub struct Host;
    impl Host {
        pub fn start(_cfg: HostConfig) -> io::Result<Self> {
            Err(io::Error::new(io::ErrorKind::Unsupported, "Online sharing is disabled in web build"))
        }
        pub fn stop(&mut self) {}
        pub fn poll(&mut self) -> Vec<HostEvent> { Vec::new() }
        pub fn state(&self) -> HostState { HostState::default() }
        pub fn sessions(&self) -> usize { 0 }
    }
}

pub mod guest {
    use super::PathMode;
    use std::io;
    pub struct GuestConfig {
        pub code: String,
        pub relay: Option<String>,
        pub protocol: i32,
        pub game: String,
        pub path: PathMode,
    }
    #[derive(Debug)]
    pub struct JoinError(pub String);
    impl std::fmt::Display for JoinError {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f, "{}", self.0) }
    }
    pub struct Tunnel { pub direct: bool }
    impl Tunnel {
        pub fn serve_loopback(self) -> io::Result<(u16, ())> {
            Err(io::Error::new(io::ErrorKind::Unsupported, "Online features disabled"))
        }
    }
    pub fn join(_cfg: &GuestConfig) -> Result<Tunnel, JoinError> {
        Err(JoinError("Online features are disabled in this web build".into()))
    }
}

pub use guest::{JoinError, Tunnel};
pub use host::{Host, HostConfig, HostEvent, HostState};
