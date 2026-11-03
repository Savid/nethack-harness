//! A fake terminal server: `POST /terminal` over a Unix socket, HTTP/1.1 keep-alive, replaying scripted output and
//! implementing `wait` and `waiting`.
//!
//! Each request with non-empty input takes the next script step. Output steps append their bytes to the output
//! buffer and advance the cursor; the reply carries the bytes after the request's `after`, `truncated` when
//! `after` is older than the buffer start, and `waiting` as the step says. A request with empty input is a pure
//! read and never takes a step. A request with input while the script is exhausted is answered like a game that
//! printed nothing and waits.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

/// What the fake game does with the next input.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Step {
    /// Prints these bytes and waits for a key: the reply says `waiting: true`.
    Output(Vec<u8>),
    /// Prints these bytes but stays busy until the request's `wait` deadline: the reply says `waiting: false`; the
    /// next pure read with `wait` reports `waiting: true` with no new output.
    Busy(Vec<u8>),
    /// Refuses the input with this HTTP status and body, such as 410 `waiting to start`, 410 `time is up` or 503
    /// `terminal busy`; the input is not consumed.
    Refuse(u16, String),
    /// Drops the buffer: the next reply's cursor goes back to zero with the given output.
    Restart(Vec<u8>),
}

/// One request the server received.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Received {
    /// Decoded input bytes.
    pub input: Vec<u8>,
    /// The `after` cursor.
    pub after: u64,
    /// The `wait` milliseconds.
    pub wait: u32,
}

/// A running fake server; dropping it stops the server and removes the socket.
pub struct FakeTerminalServer {
    path: PathBuf,
    received: Arc<Mutex<Vec<Received>>>,
    connections: Arc<Mutex<u32>>,
    thread: Option<JoinHandle<()>>,
}

impl FakeTerminalServer {
    /// Starts a server on `<dir>/terminal.sock` whose screen already holds `initial` (cursor at its length), then
    /// plays `script`.
    pub fn start(dir: &Path, initial: &[u8], script: Vec<Step>) -> FakeTerminalServer {
        todo!()
    }

    /// The socket path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Every request received so far.
    pub fn received(&self) -> Vec<Received> {
        todo!()
    }

    /// The non-empty inputs received so far, in order.
    pub fn inputs(&self) -> Vec<Vec<u8>> {
        self.received()
            .into_iter()
            .filter(|r| !r.input.is_empty())
            .map(|r| r.input)
            .collect()
    }

    /// How many connections clients opened.
    pub fn connections(&self) -> u32 {
        todo!()
    }
}

impl Drop for FakeTerminalServer {
    /// Stops the server thread and removes the socket.
    fn drop(&mut self) {
        todo!()
    }
}
