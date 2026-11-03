//! The command line: `nethack-harness [--dir D] [--json] [--socket PATH] [--max-steps N] COMMAND`, where COMMAND is
//! `look`, `send KEYS`, `go TARGET`, `rest N`, `stop` or `serve`.
//!
//! A client command connects to `<dir>/sock`, sends one request line, prints the reply's text (or, with `--json`,
//! the reply object) and exits with its code. When nothing answers it takes `flock(<dir>/lock)`, checks again, and
//! starts `serve` detached in its own process group with output to `<dir>/log`, waiting at most 10 s for the
//! daemon's `ready` line. `serve` runs the daemon in the foreground.

use std::fs::{DirBuilder, OpenOptions};
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command as Process, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::daemon::{COMMAND_TIME, Cmd, Request, check, serve};
use crate::play::STEPS_MAX;
use crate::types::ExitCode;

/// The session directory when `--dir` is not given.
pub const DEFAULT_DIR: &str = ".nethack-harness";
/// The terminal socket when `--socket` is not given.
pub const DEFAULT_SOCKET: &str = "/run/nethack/terminal.sock";
/// Longest a client waits for a daemon it started, or for a busy one to accept it.
pub const START_TIMEOUT_MS: u64 = 10_000;
/// Longest a client waits for its reply: the daemon's own limit on a command, with room for one slow exchange.
const REPLY_TIMEOUT: Duration = Duration::from_secs(COMMAND_TIME.as_secs() + 30);
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

/// The parts of a reply line the client acts on.
#[derive(Deserialize)]
struct Answer {
    code: i32,
    text: String,
}

/// Parses the arguments after the program name. `send` keys may start with `-`; they are checked against the escape
/// syntax and the 1–256 byte limit here.
pub fn parse(args: &[String]) -> Result<Args, String> {
    let mut parsed = Args {
        dir: PathBuf::from(DEFAULT_DIR),
        json: false,
        socket: PathBuf::from(DEFAULT_SOCKET),
        max_steps: STEPS_MAX,
        command: Command::Serve,
    };
    let mut at = 0;
    loop {
        let Some(arg) = args.get(at) else {
            return Err("missing command".to_owned());
        };
        let value = || {
            args.get(at + 1)
                .ok_or_else(|| format!("{arg} takes a value"))
        };
        match arg.as_str() {
            "--json" => {
                parsed.json = true;
                at += 1;
                continue;
            }
            "--dir" => parsed.dir = PathBuf::from(value()?),
            "--socket" => parsed.socket = PathBuf::from(value()?),
            "--max-steps" => {
                parsed.max_steps = value()?
                    .parse()
                    .ok()
                    .filter(|n| (1..=STEPS_MAX).contains(n))
                    .ok_or_else(|| format!("--max-steps takes 1 to {STEPS_MAX}"))?;
            }
            _ => break,
        }
        at += 2;
    }
    let name = args[at].as_str();
    let operands = &args[at + 1..];
    let arity = match name {
        "look" | "stop" | "serve" => 0,
        "send" | "go" | "rest" => 1,
        _ => return Err(format!("unknown command {name}")),
    };
    if operands.len() != arity {
        return Err(match arity {
            0 => format!("{name} takes no arguments"),
            _ => format!("{name} takes one argument"),
        });
    }
    let mut request = Request {
        cmd: Cmd::Look,
        keys: None,
        target: None,
        count: None,
    };
    match name {
        "serve" => return Ok(parsed),
        "look" => {}
        "stop" => request.cmd = Cmd::Stop,
        "send" => {
            request.cmd = Cmd::Send;
            request.keys = Some(operands[0].clone());
        }
        "go" => {
            request.cmd = Cmd::Go;
            request.target = Some(operands[0].clone());
        }
        _ => {
            request.cmd = Cmd::Rest;
            request.count = Some(0);
            if let Ok(count) = operands[0].parse() {
                request.count = Some(count);
            }
        }
    }
    check(&request)?;
    parsed.command = Command::Client(request);
    Ok(parsed)
}

/// Runs a parsed command and returns the exit code.
pub fn run(args: &Args) -> i32 {
    match &args.command {
        Command::Serve => {
            let (socket, _, _) = paths(&args.dir);
            let started = private_dir(&args.dir).and_then(|()| {
                let mut stdout = std::io::stdout().lock();
                serve(&socket, &args.socket, args.max_steps, &mut stdout)
            });
            match started {
                Ok(()) => ExitCode::Done.code(),
                Err(e) => fail(format_args!("serve: {e}")),
            }
        }
        Command::Client(request) => client(args, request),
    }
}

/// Prints an error to standard error and returns the failure code.
fn fail(message: std::fmt::Arguments) -> i32 {
    let _ = writeln!(std::io::stderr(), "nethack-harness: {message}");
    ExitCode::Failed.code()
}

/// Whether a connect error means no daemon is listening.
fn absent(e: &std::io::Error) -> bool {
    matches!(e.kind(), ErrorKind::NotFound | ErrorKind::ConnectionRefused)
}

/// Sends one request and prints its reply.
fn client(args: &Args, request: &Request) -> i32 {
    let stream = if request.cmd == Cmd::Stop {
        match UnixStream::connect(paths(&args.dir).0) {
            Ok(stream) => stream,
            Err(e) if absent(&e) => {
                println!("no daemon is running");
                return ExitCode::Done.code();
            }
            Err(e) => return fail(format_args!("cannot reach the daemon: {e}")),
        }
    } else {
        match connect(args) {
            Ok(stream) => stream,
            Err(e) => return fail(format_args!("{e}")),
        }
    };
    let mut line = match serde_json::to_string(request) {
        Ok(line) => line,
        Err(e) => return fail(format_args!("cannot encode the request: {e}")),
    };
    line.push('\n');
    let mut writer = &stream;
    if let Err(e) = writer.write_all(line.as_bytes()) {
        return fail(format_args!("cannot send the request: {e}"));
    }
    line.clear();
    let log = paths(&args.dir).2;
    let read = stream
        .set_read_timeout(Some(REPLY_TIMEOUT))
        .and_then(|()| BufReader::new(&stream).read_line(&mut line));
    match read {
        Ok(0) => {
            return fail(format_args!(
                "the daemon closed the connection without a reply; see {}",
                log.display()
            ));
        }
        Ok(_) => {}
        Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
            return fail(format_args!(
                "the daemon did not answer within {} s; see {}",
                REPLY_TIMEOUT.as_secs(),
                log.display()
            ));
        }
        Err(e) => return fail(format_args!("cannot read the reply: {e}")),
    }
    let answer: Answer = match serde_json::from_str(line.trim_end()) {
        Ok(answer) => answer,
        Err(e) => return fail(format_args!("the daemon sent a malformed reply: {e}")),
    };
    let mut stdout = std::io::stdout().lock();
    let printed = if args.json {
        stdout.write_all(line.as_bytes())
    } else if answer.text.is_empty() || answer.text.ends_with('\n') {
        stdout.write_all(answer.text.as_bytes())
    } else {
        writeln!(stdout, "{}", answer.text)
    };
    let _ = printed.and_then(|()| stdout.flush());
    answer.code
}

/// Connects to the daemon in `dir`, starting one under the directory lock if nothing answers. A refused connect
/// while a daemon holds the socket's lock means its queue of waiting clients is full, so the client retries with
/// growing pauses instead of starting another.
pub fn connect(args: &Args) -> std::io::Result<UnixStream> {
    let (socket, lock, _) = paths(&args.dir);
    match UnixStream::connect(&socket) {
        Err(e) if absent(&e) => {}
        other => return other,
    }
    private_dir(&args.dir)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(lock)?;
    lock.lock()?;
    let deadline = Instant::now() + Duration::from_millis(START_TIMEOUT_MS);
    let mut pause = Duration::from_millis(10);
    loop {
        match UnixStream::connect(&socket) {
            Err(e) if e.kind() == ErrorKind::ConnectionRefused && serving(&socket) => {
                if Instant::now() + pause >= deadline {
                    return Err(e);
                }
                std::thread::sleep(pause);
                pause = (pause * 2).min(Duration::from_millis(500));
            }
            Err(e) if absent(&e) => break,
            other => return other,
        }
    }
    start(args)?;
    UnixStream::connect(&socket)
}

/// Whether a daemon holds the lock on `<socket>.lock`, so the socket is live.
fn serving(socket: &Path) -> bool {
    let mut path = socket.as_os_str().to_owned();
    path.push(".lock");
    OpenOptions::new()
        .write(true)
        .open(path)
        .is_ok_and(|file| file.try_lock().is_err())
}

/// Creates `dir` and any missing parents, a new directory readable by its owner only.
fn private_dir(dir: &Path) -> std::io::Result<()> {
    DirBuilder::new().recursive(true).mode(0o700).create(dir)
}

/// Starts `serve` detached and waits for its `ready` line.
pub fn start(args: &Args) -> std::io::Result<()> {
    let (_, _, log_path) = paths(&args.dir);
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)?;
    let mut child = Process::new(std::env::current_exe()?)
        .arg("--dir")
        .arg(&args.dir)
        .arg("--socket")
        .arg(&args.socket)
        .arg("--max-steps")
        .arg(args.max_steps.to_string())
        .arg("serve")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(log)
        .process_group(0)
        .spawn()?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| std::io::Error::other("the daemon has no output pipe"))?;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut line = String::new();
        let read = BufReader::new(stdout).read_line(&mut line);
        let _ = tx.send(read.map(|_| line));
    });
    let failed =
        |why: &str| std::io::Error::other(format!("the daemon {why}; see {}", log_path.display()));
    match rx.recv_timeout(Duration::from_millis(START_TIMEOUT_MS)) {
        Ok(Ok(line)) if line.trim_end() == "ready" => Ok(()),
        Ok(_) => {
            let _ = child.wait();
            Err(failed("did not start"))
        }
        Err(_) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(failed(&format!(
                "did not start within {} s",
                START_TIMEOUT_MS / 1000
            )))
        }
    }
}

/// The paths of the socket, lock and log in `dir`.
pub fn paths(dir: &Path) -> (PathBuf, PathBuf, PathBuf) {
    (dir.join("sock"), dir.join("lock"), dir.join("log"))
}

/// The whole program: parses `std::env::args`, runs, and returns the exit code.
pub fn main() -> i32 {
    let mut args = Vec::new();
    for arg in std::env::args_os().skip(1) {
        match arg.into_string() {
            Ok(arg) => args.push(arg),
            Err(_) => return usage("arguments must be UTF-8; write other key bytes as \\xHH"),
        }
    }
    match parse(&args) {
        Ok(args) => run(&args),
        Err(why) => usage(&why),
    }
}

/// Prints `why` and the usage line to standard error and returns the usage code.
fn usage(why: &str) -> i32 {
    let _ = writeln!(std::io::stderr(), "nethack-harness: {why}\n{USAGE}");
    ExitCode::Usage.code()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(line: &[&str]) -> Result<Args, String> {
        parse(&line.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>())
    }

    fn request(line: &[&str]) -> Request {
        match args(line).unwrap().command {
            Command::Client(request) => request,
            Command::Serve => panic!("not a client command"),
        }
    }

    #[test]
    fn global_options_come_before_the_command() {
        let parsed = args(&[
            "--dir",
            "d",
            "--json",
            "--socket",
            "t.sock",
            "--max-steps",
            "8",
            "look",
        ])
        .unwrap();
        assert_eq!(parsed.dir, PathBuf::from("d"));
        assert!(parsed.json);
        assert_eq!(parsed.socket, PathBuf::from("t.sock"));
        assert_eq!(parsed.max_steps, 8);
        assert_eq!(request(&["look"]).cmd, Cmd::Look);
        let defaults = args(&["serve"]).unwrap();
        assert_eq!(defaults.command, Command::Serve);
        assert_eq!(defaults.dir, PathBuf::from(DEFAULT_DIR));
        assert_eq!(defaults.socket, PathBuf::from(DEFAULT_SOCKET));
        assert_eq!(defaults.max_steps, STEPS_MAX);
    }

    #[test]
    fn send_keys_may_start_with_a_dash_and_stay_escaped() {
        let send = request(&["send", "-\\e"]);
        assert_eq!((send.cmd, send.keys.as_deref()), (Cmd::Send, Some("-\\e")));
        assert_eq!(request(&["send", "--"]).keys.as_deref(), Some("--"));
        assert_eq!(request(&["send", "--json"]).keys.as_deref(), Some("--json"));
    }

    #[test]
    fn operands_are_checked() {
        assert_eq!(request(&["go", "23,3"]).target.as_deref(), Some("23,3"));
        assert_eq!(request(&["rest", "20"]).count, Some(20));
        for bad in [
            &[][..],
            &["--json"],
            &["dance"],
            &["look", "x"],
            &["send"],
            &["send", ""],
            &["send", "a", "b"],
            &["go", ""],
            &["rest"],
            &["rest", "0"],
            &["rest", "201"],
            &["rest", "x"],
            &["--max-steps", "0", "look"],
            &["--max-steps", "65", "look"],
            &["--dir"],
            &["serve", "--socket", "x"],
        ] {
            assert!(args(bad).is_err(), "{bad:?}");
        }
        let long = "a".repeat(257);
        assert!(args(&["send", &long]).unwrap_err().contains("1 to 256"));
        assert!(args(&["send", &long[..256]]).is_ok());
        assert!(args(&["send", "\\x41\\e\\r\\n\\t\\\\"]).is_ok());
    }
}
