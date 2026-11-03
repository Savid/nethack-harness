//! The daemon: one thread blocked in `accept` on `<dir>/sock`, answering one JSON request line with one JSON reply
//! line per connection, in order, so play is serialised. It holds all game memory in RAM, has no timers, and writes
//! only its log, on errors.
//!
//! Request: `{"cmd": "look"|"send"|"go"|"rest"|"stop", "keys"?: str, "target"?: str, "count"?: u32}`; `keys` uses
//! the escape syntax `\e \r \n \t \\ \xHH`. Reply: `{"code": int, "text": str, "facts": {…}}`.

use std::io::{BufRead, Write};
use std::os::unix::net::UnixListener;
use std::path::Path;

use serde::Deserialize;

use crate::link::Term;
use crate::play::Play;
use crate::types::Reply;

/// A request's command.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Cmd {
    /// Render the current view.
    Look,
    /// Send keys.
    Send,
    /// Walk to a target.
    Go,
    /// Search in place.
    Rest,
    /// Exit the daemon.
    Stop,
}

/// One request line.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    /// The command.
    pub cmd: Cmd,
    /// Keys for `send`, in escape syntax.
    #[serde(default)]
    pub keys: Option<String>,
    /// Target for `go`.
    #[serde(default)]
    pub target: Option<String>,
    /// Searches for `rest`.
    #[serde(default)]
    pub count: Option<u32>,
}

/// What to do after answering a request.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum After {
    /// Keep serving.
    Serve,
    /// Exit: a `stop`, or the terminal closed and a client has been told.
    Exit,
}

/// Serves requests on the socket at `socket` for the game behind the terminal socket `terminal`, until a `stop`
/// or a closed terminal. Writes `ready` and a newline to `ready` once listening.
pub fn serve(
    socket: &Path,
    terminal: &Path,
    max_steps: u32,
    ready: &mut dyn Write,
) -> std::io::Result<()> {
    todo!()
}

/// Binds the listening socket, replacing a stale socket file no daemon answers on.
pub fn bind(socket: &Path) -> std::io::Result<UnixListener> {
    todo!()
}

/// Answers one request.
pub fn handle(play: &mut Play<Term>, request: &Request) -> (Reply, After) {
    todo!()
}

/// Reads one request line from `input` into `line`, bounded in length, and decodes it; the error is the message a
/// malformed request is answered with, under code 64.
pub fn read_request(input: &mut dyn BufRead, line: &mut String) -> Result<Request, String> {
    todo!()
}
