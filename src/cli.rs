//! The command line: `nethack-harness [--dir D] [--json] [--socket PATH] COMMAND`, where COMMAND is
//! `look`, `send KEYS`, `go [--wet] [--near N] TARGET`, `rest [--near N] COUNT`, `learn ROW… COLOURS…`, `stop` or
//! `serve`.
//!
//! A client command connects to `<dir>/sock`, sends one request line, prints the reply's text (or, with `--json`,
//! the reply object) and exits with its code. When nothing answers it takes `flock(<dir>/lock)`, checks again, and
//! starts `serve` detached in its own process group with output to `<dir>/log`, waiting at most 10 s for the
//! daemon's `ready` line. `serve` runs the daemon in the foreground.

use std::fs::{DirBuilder, OpenOptions};
use std::io::{BufRead, BufReader, ErrorKind, Write};
use std::os::fd::OwnedFd;
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command as Process, Stdio};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::daemon::{COMMAND_TIME, Cmd, NEAR_MAX, Request, check, serve, serving};
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
pub const USAGE: &str = "usage: nethack-harness [--dir D] [--json] [--socket PATH] \
                         look | send KEYS | go [--wet] [--near N] TARGET | rest [--near N] COUNT | \
                         learn ROW... COLOURS... | stop | serve";

/// A parsed command line: the options, then the request for the daemon or `serve` (`None`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Args {
    pub dir: PathBuf,
    pub json: bool,
    pub socket: PathBuf,
    pub request: Option<Request>,
}

/// The parts of a reply line the client acts on.
#[derive(Deserialize)]
struct Answer {
    code: i32,
    text: String,
}

/// Parses the arguments after the program name. `send` keys may start with `-`; they are checked against the escape
/// syntax and the 1–256 byte limit here. `learn` takes the map's rows and then their colours, one operand each.
pub fn parse(args: &[String]) -> Result<Args, String> {
    let mut parsed = Args {
        dir: PathBuf::from(DEFAULT_DIR),
        json: false,
        socket: PathBuf::from(DEFAULT_SOCKET),
        request: None,
    };
    let mut rest = args;
    let (name, operands) = loop {
        let Some((arg, after)) = rest.split_first() else {
            return Err("missing command".to_owned());
        };
        rest = after;
        if arg == "--json" {
            parsed.json = true;
            continue;
        }
        let path = match arg.as_str() {
            "--dir" => &mut parsed.dir,
            "--socket" => &mut parsed.socket,
            _ => break (arg.as_str(), rest),
        };
        let Some((value, after)) = rest.split_first() else {
            return Err(format!("{arg} takes a value"));
        };
        rest = after;
        *path = PathBuf::from(value);
    };
    let operand = operands.first().cloned();
    let request = |cmd| Request {
        cmd,
        ..Request::default()
    };
    let request = match (name, operands.len()) {
        ("serve", 0) => return Ok(parsed),
        ("look", 0) => request(Cmd::Look),
        ("stop", 0) => request(Cmd::Stop),
        ("send", 1) => Request {
            keys: operand,
            ..request(Cmd::Send)
        },
        ("go", _) => {
            let mut go = request(Cmd::Go);
            match options(name, operands, &mut go)?.as_slice() {
                [target] => go.target = Some(target.clone()),
                _ => return Err("go takes [--wet] [--near N] TARGET".to_owned()),
            }
            go
        }
        ("rest", _) => {
            let mut rest = request(Cmd::Rest);
            match options(name, operands, &mut rest)?.as_slice() {
                [count] => rest.count = Some(count.parse().unwrap_or(0)),
                _ => return Err("rest takes [--near N] COUNT".to_owned()),
            }
            rest
        }
        ("learn", _) => {
            // The map's rows, then as many rows of colours; the request check counts and measures them.
            let (rows, colours) = operands.split_at(operands.len() / 2);
            Request {
                rows: Some(rows.to_vec()),
                colours: Some(colours.to_vec()),
                ..request(Cmd::Learn)
            }
        }
        ("look" | "stop" | "serve", _) => return Err(format!("{name} takes no arguments")),
        ("send", _) => return Err("send takes one argument".to_owned()),
        _ => return Err(format!("unknown command {name}")),
    };
    check(&request)?;
    parsed.request = Some(request);
    Ok(parsed)
}

/// The operands of `go` or `rest` left once their options, anywhere among them, are set on `request`: `--near N`,
/// and for `go` `--wet`.
fn options(name: &str, operands: &[String], request: &mut Request) -> Result<Vec<String>, String> {
    let mut left = Vec::new();
    let mut operands = operands.iter();
    while let Some(operand) = operands.next() {
        match operand.as_str() {
            "--wet" if name == "go" => request.wet = Some(true),
            "--near" => {
                let near = operands.next().and_then(|n| n.parse().ok());
                request.near = Some(near.ok_or_else(|| format!("--near takes 1 to {NEAR_MAX}"))?);
            }
            _ => left.push(operand.clone()),
        }
    }
    Ok(left)
}

/// Runs a parsed command and returns the exit code.
pub fn run(args: &Args) -> i32 {
    match &args.request {
        None => {
            let (socket, _, _) = paths(&args.dir);
            let started = private_dir(&args.dir).and_then(|()| {
                let mut stdout = std::io::stdout().lock();
                serve(&socket, &args.socket, &mut stdout)
            });
            match started {
                Ok(()) => ExitCode::Done.code(),
                Err(e) => fail(format_args!("serve: {e}")),
            }
        }
        Some(request) => client(args, request),
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
    let stop = request.cmd == Cmd::Stop;
    let connected = if stop {
        UnixStream::connect(paths(&args.dir).0)
    } else {
        connect(args)
    };
    let stream = match connected {
        Ok(stream) => stream,
        Err(e) if stop && absent(&e) => {
            println!("no daemon is running");
            return ExitCode::Done.code();
        }
        Err(e) if stop => return fail(format_args!("cannot reach the daemon: {e}")),
        Err(e) => return fail(format_args!("{e}")),
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
    // The daemon's output is a socket, so its `ready` line can be awaited with a timeout.
    let (ready, output) = UnixStream::pair()?;
    ready.set_read_timeout(Some(Duration::from_millis(START_TIMEOUT_MS)))?;
    let mut child = Process::new(std::env::current_exe()?)
        .arg("--dir")
        .arg(&args.dir)
        .arg("--socket")
        .arg(&args.socket)
        .arg("serve")
        .stdin(Stdio::null())
        .stdout(OwnedFd::from(output))
        .stderr(log)
        .process_group(0)
        .spawn()?;
    let failed =
        |why: &str| std::io::Error::other(format!("the daemon {why}; see {}", log_path.display()));
    let mut line = String::new();
    match BufReader::new(&ready).read_line(&mut line) {
        Ok(_) if line.trim_end() == "ready" => Ok(()),
        Err(e) if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {
            let _ = child.kill();
            let _ = child.wait();
            Err(failed(&format!(
                "did not start within {} s",
                START_TIMEOUT_MS / 1000
            )))
        }
        _ => {
            let _ = child.wait();
            Err(failed("did not start"))
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
        args(line).unwrap().request.expect("a client command")
    }

    #[test]
    fn global_options_come_before_the_command() {
        let parsed = args(&["--dir", "d", "--json", "--socket", "t.sock", "look"]).unwrap();
        assert_eq!(parsed.dir, PathBuf::from("d"));
        assert!(parsed.json);
        assert_eq!(parsed.socket, PathBuf::from("t.sock"));
        assert_eq!(request(&["look"]).cmd, Cmd::Look);
        let defaults = args(&["serve"]).unwrap();
        assert_eq!(defaults.request, None);
        assert_eq!(defaults.dir, PathBuf::from(DEFAULT_DIR));
        assert_eq!(defaults.socket, PathBuf::from(DEFAULT_SOCKET));
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
        assert_eq!(request(&["go", "23,3"]).wet, None);
        let wet = request(&["go", "--wet", ">"]);
        assert_eq!((wet.target.as_deref(), wet.wet), (Some(">"), Some(true)));
        assert_eq!(request(&["go", ">", "--wet"]), wet);
        assert!(args(&["go", "--wet"]).unwrap_err().contains("[--wet]"));
        let near = request(&["go", "--near", "3", "--wet", "<"]);
        assert_eq!((near.near, near.wet), (Some(3), Some(true)));
        let rest = request(&["rest", "20", "--near", "3"]);
        assert_eq!((rest.count, rest.near), (Some(20), Some(3)));
        assert_eq!(request(&["rest", "--near", "3", "20"]), rest);
        assert_eq!(request(&["rest", "20"]).near, None);
        for bad in [
            &["rest", "20", "--near"][..],
            &["rest", "20", "--near", "0"],
            &["rest", "20", "--near", "x"],
            &["rest", "20", "--near", "300"],
            &["rest", "--wet", "20"],
            &["go", "--near", "81", ">"],
            &["rest", "--near", "3"],
            &["send", "--near", "3"],
        ] {
            assert!(args(bad).is_err(), "{bad:?}");
        }
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
            &["go", "--wet", ""],
            &["go", "--wet", ">", "x"],
            &["--wet", "go", ">"],
            &["rest"],
            &["rest", "0"],
            &["rest", "201"],
            &["rest", "x"],
            &["--dir"],
            &["serve", "--socket", "x"],
        ] {
            assert!(args(bad).is_err(), "{bad:?}");
        }
        let (row, colours) = (format!("{:<80}", " |..>"), "7".repeat(80));
        let mut learn = vec!["learn"];
        learn.extend([row.as_str(); 21]);
        learn.extend([colours.as_str(); 21]);
        let told = request(&learn);
        assert_eq!(told.cmd, Cmd::Learn);
        assert_eq!(told.rows, Some(vec![row.clone(); 21]));
        assert_eq!(told.colours, Some(vec![colours.clone(); 21]));
        assert!(args(&learn[..42]).unwrap_err().contains("21 rows"));
        assert!(args(&["learn"]).is_err());
        let long = "a".repeat(257);
        assert!(args(&["send", &long]).unwrap_err().contains("1 to 256"));
        assert!(args(&["send", &long[..256]]).is_ok());
        assert!(args(&["send", "\\x41\\e\\r\\n\\t\\\\"]).is_ok());
    }
}
