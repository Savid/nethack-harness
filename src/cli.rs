//! The command line: `nethack-harness [--dir D] [--json] [--socket PATH] [--max-steps N] COMMAND`, where COMMAND is
//! `look`, `send KEYS`, `go TARGET`, `rest N`, `stop` or `serve`.
//!
//! A client command connects to `<dir>/sock`, sends one request line, prints the reply's text (or, with `--json`,
//! the reply object) and exits with its code. When nothing answers it takes `flock(<dir>/lock)`, checks again, and
//! starts `serve` detached in its own process group with output to `<dir>/log`, waiting at most 10 s for the
//! daemon's `ready` line. `serve` runs the daemon in the foreground.

use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};

use crate::daemon::Request;

/// The session directory when `--dir` is not given.
pub const DEFAULT_DIR: &str = ".nethack-harness";
/// The terminal socket when `--socket` is not given.
pub const DEFAULT_SOCKET: &str = "/run/nethack/terminal.sock";
/// Longest a client waits for a daemon it started.
pub const START_TIMEOUT_MS: u64 = 10_000;
/// The usage line printed with exit code 64.
pub const USAGE: &str = "usage: nethack-harness [--dir D] [--json] [--socket PATH] [--max-steps N] \
                         look | send KEYS | go TARGET | rest N | stop | serve";

/// A parsed command line.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Args {
    /// The session directory.
    pub dir: PathBuf,
    /// Print the reply object instead of its text.
    pub json: bool,
    /// The terminal socket a started daemon connects to.
    pub socket: PathBuf,
    /// Most steps one `go` takes, 1 to 64.
    pub max_steps: u32,
    /// What to do.
    pub command: Command,
}

/// A command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Command {
    /// Ask the daemon for a request's reply.
    Client(Request),
    /// Run the daemon in the foreground.
    Serve,
}

/// Parses the arguments after the program name. `send` keys may start with `-`; they are checked against the escape
/// syntax and the 1–256 byte limit here.
pub fn parse(args: &[String]) -> Result<Args, String> {
    todo!()
}

/// Runs a parsed command and returns the exit code.
pub fn run(args: &Args) -> i32 {
    todo!()
}

/// Connects to the daemon in `dir`, starting one under the directory lock if nothing answers.
pub fn connect(args: &Args) -> std::io::Result<UnixStream> {
    todo!()
}

/// Starts `serve` detached and waits for its `ready` line.
pub fn start(args: &Args) -> std::io::Result<()> {
    todo!()
}

/// The paths of the socket, lock and log in `dir`.
pub fn paths(dir: &Path) -> (PathBuf, PathBuf, PathBuf) {
    (dir.join("sock"), dir.join("lock"), dir.join("log"))
}

/// The whole program: parses `std::env::args`, runs, and returns the exit code.
pub fn main() -> i32 {
    todo!()
}
