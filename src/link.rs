//! The terminal link: one persistent HTTP/1.1 keep-alive connection over a Unix socket to `POST /terminal`, and
//! the screen kept in step with it.
//!
//! Request: `{"input": base64, "after": cursor, "wait": ms}`. Reply 200: `{"output": base64, "cursor": n,
//! "truncated": bool, "waiting": bool}`; `waiting` is true when the game is blocked reading a key, has consumed
//! all input sent so far, and all its output is in `output`. With `wait` > 0 and input written, the server
//! replies as soon as `waiting` is true, or at the deadline. 503 is busy; 410 `waiting to start` means input is
//! held; any other 410 means the terminal is closed.

use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};

use crate::screen::{PromptContext, View};
use crate::vt::Vt;

/// Longest the server is asked to wait for the game to settle, in milliseconds.
pub const WAIT_MS: u32 = 3000;
/// Most input bytes one request carries.
pub const INPUT_MAX: usize = 4096;
/// Retries of a 503 before giving up.
pub const BUSY_RETRIES: u32 = 100;
/// Pause between 503 retries, in milliseconds.
pub const BUSY_PAUSE_MS: u64 = 50;
/// Re-reads with `wait` while the game stays busy, before reporting it still busy.
pub const BUSY_READS: u32 = 4;

/// Why an exchange with the terminal failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LinkError {
    /// 410 `waiting to start`: the game takes no input yet; nothing was sent.
    Held(String),
    /// Any other 410, or the socket went away after answering before: the terminal is closed.
    Closed(String),
    /// The socket could not be reached at all.
    Unavailable(String),
    /// The server answered something this client does not understand, or kept answering 503.
    Protocol(String),
}

impl std::fmt::Display for LinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        todo!()
    }
}

/// What one exchange returned; the output bytes are in `Link::output`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Exchange {
    /// The output does not continue the previous one (truncated, or the cursor went back): reset the screen first.
    pub reset: bool,
    /// The game is blocked reading a key with all its output delivered.
    pub waiting: bool,
}

/// The HTTP client: one connection, reconnected on EOF, with request and response buffers reused across calls.
pub struct Link {
    path: PathBuf,
    stream: Option<UnixStream>,
    cursor: u64,
    answered: bool,
    request: Vec<u8>,
    response: Vec<u8>,
    output: Vec<u8>,
}

impl Link {
    /// A link to the terminal socket at `path`; it connects on first use.
    pub fn new(path: &Path) -> Link {
        todo!()
    }

    /// Sends `input` (possibly empty, a pure read) and returns what the server answered. With `wait_ms` > 0 and
    /// input given, the server holds the reply until the game waits for a key or the deadline passes. A 503 is
    /// retried `BUSY_RETRIES` times.
    pub fn exchange(&mut self, input: &[u8], wait_ms: u32) -> Result<Exchange, LinkError> {
        todo!()
    }

    /// The output bytes of the last exchange.
    pub fn output(&self) -> &[u8] {
        &self.output
    }

    /// The server's output cursor after the last exchange.
    pub fn cursor(&self) -> u64 {
        self.cursor
    }
}

/// Standard base64 encoding of `bytes`, appended to `out`.
pub fn base64_encode(bytes: &[u8], out: &mut Vec<u8>) {
    todo!()
}

/// Standard base64 decoding of `text`, appended to `out`; false on malformed input.
pub fn base64_decode(text: &[u8], out: &mut Vec<u8>) -> bool {
    todo!()
}

/// A terminal the executor drives: the real `Term`, or a scripted one in tests.
pub trait Terminal {
    /// Reads any pending output without sending input.
    fn poll(&mut self) -> Result<(), LinkError>;
    /// Sends `keys` as one batch and returns once the game waits for a key, re-reading while it stays busy.
    fn send(&mut self, keys: &[u8]) -> Result<(), LinkError>;
    /// The current screen.
    fn view(&self) -> &View;
    /// Whether the game wrote to the message rows of the current view since the last `send`.
    fn message_written(&self) -> bool;
    /// The positioning-prompt tracker.
    fn prompts(&self) -> &PromptContext;
}

/// The real terminal: a `Link`, the emulator it feeds and the parsed view of its screen.
pub struct Term {
    link: Link,
    vt: Vt,
    prompts: PromptContext,
    view: View,
}

impl Term {
    /// A terminal over the socket at `path`; nothing is read until `sync`.
    pub fn new(path: &Path) -> Term {
        todo!()
    }

    /// Reads the screen and, if the emulator has never seen a full clear, asks the game to redraw (`^R`).
    pub fn sync(&mut self) -> Result<(), LinkError> {
        todo!()
    }
}

impl Terminal for Term {
    fn poll(&mut self) -> Result<(), LinkError> {
        todo!()
    }

    fn send(&mut self, keys: &[u8]) -> Result<(), LinkError> {
        todo!()
    }

    fn view(&self) -> &View {
        &self.view
    }

    fn message_written(&self) -> bool {
        todo!()
    }

    fn prompts(&self) -> &PromptContext {
        &self.prompts
    }
}
