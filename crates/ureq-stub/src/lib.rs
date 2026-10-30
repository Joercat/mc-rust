use std::fmt;
use std::io::{self, Read};
use std::time::Duration;

#[derive(Debug)]
pub struct Transport(String);
impl fmt::Display for Transport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "{}", self.0) }
}

#[derive(Debug)]
pub enum Error {
    Status(u16, Response),
    Transport(Transport),
}
impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Status(code, _) => write!(f, "HTTP status {code}"),
            Self::Transport(t) => write!(f, "{t}"),
        }
    }
}
impl std::error::Error for Error {}

#[derive(Debug, Default)]
pub struct AgentBuilder;
impl AgentBuilder {
    pub fn new() -> Self { Self }
    pub fn timeout(self, _: Duration) -> Self { self }
    pub fn timeout_connect(self, _: Duration) -> Self { self }
    pub fn timeout_read(self, _: Duration) -> Self { self }
    pub fn timeout_write(self, _: Duration) -> Self { self }
    pub fn redirects(self, _: u32) -> Self { self }
    pub fn user_agent(self, _: &str) -> Self { self }
    pub fn build(self) -> Agent { Agent }
}

#[derive(Clone, Debug, Default)]
pub struct Agent;
impl Agent {
    pub fn get(&self, _url: &str) -> Request { Request }
    pub fn post(&self, _url: &str) -> Request { Request }
}

pub fn get(_url: &str) -> Request { Request }
pub fn post(_url: &str) -> Request { Request }

#[derive(Clone, Debug, Default)]
pub struct Request;
impl Request {
    pub fn set(self, _header: &str, _value: &str) -> Self { self }
    pub fn query(self, _param: &str, _value: &str) -> Self { self }
    pub fn timeout(self, _: Duration) -> Self { self }
    pub fn call(self) -> Result<Response, Error> {
        Err(Error::Transport(Transport("Online features disabled in web build".into())))
    }
    pub fn send_bytes(self, _data: &[u8]) -> Result<Response, Error> {
        Err(Error::Transport(Transport("Online features disabled in web build".into())))
    }
    pub fn send_string(self, _data: &str) -> Result<Response, Error> {
        Err(Error::Transport(Transport("Online features disabled in web build".into())))
    }
}

#[derive(Debug, Default)]
pub struct Response;
impl Response {
    pub fn status(&self) -> u16 { 503 }
    pub fn header(&self, _name: &str) -> Option<&str> { None }
    pub fn into_reader(self) -> Box<dyn Read + Send + Sync + 'static> { Box::new(io::empty()) }
    pub fn into_string(self) -> io::Result<String> { Ok(String::new()) }
}
