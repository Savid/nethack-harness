//! The daemon: one thread blocked in `accept` on `<dir>/sock`, answering one JSON request line with one JSON reply
//! line per connection, in order, so play is serialised. It holds all game memory in RAM, has no timers, and writes
//! only its log, on errors.
//!
//! Request: `{"cmd": "look"|"send"|"go"|"rest"|"stop", "keys"?: str, "target"?: str, "count"?: u32}`; `keys` uses
//! the escape syntax `\e \r \n \t \\ \xHH`. Reply: `{"code": int, "text": str, "facts": {…}}`.

use std::fs::{File, OpenOptions, Permissions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::link::Term;
use crate::play::{KEYS_MAX, Play, REST_MAX};
use crate::types::{ExitCode, Reply, unescape};

/// Longest request line accepted, in bytes.
pub const REQUEST_MAX: usize = 4096;
/// Longest a `go` target, in bytes.
pub const TARGET_MAX: usize = 64;
/// Longest the daemon waits on one client to send its whole request, and again to take its whole reply.
const CLIENT_TIMEOUT: Duration = Duration::from_secs(2);
/// Longest one command keeps sending to the game; past it the command stops short.
pub const COMMAND_TIME: Duration = Duration::from_secs(60);

/// A request's command.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
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

impl Cmd {
    /// The command's name.
    pub fn name(self) -> &'static str {
        match self {
            Cmd::Look => "look",
            Cmd::Send => "send",
            Cmd::Go => "go",
            Cmd::Rest => "rest",
            Cmd::Stop => "stop",
        }
    }
}

/// One request line.
#[derive(Clone, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    /// The command.
    pub cmd: Cmd,
    /// Keys for `send`, in escape syntax.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keys: Option<String>,
    /// Target for `go`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
    /// Searches for `rest`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
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

/// A checked request.
enum Checked<'a> {
    Look,
    Send(Vec<u8>),
    Go(&'a str),
    Rest(u32),
    Stop,
}

/// Checks that a request carries exactly the arguments its command takes, within their limits; the error is the
/// message it is refused with under code 64.
pub fn check(request: &Request) -> Result<(), String> {
    checked(request).map(|_| ())
}

fn checked(request: &Request) -> Result<Checked<'_>, String> {
    let name = request.cmd.name();
    let only = |keys: bool, target: bool, count: bool| -> Result<(), String> {
        if (request.keys.is_some() && !keys)
            || (request.target.is_some() && !target)
            || (request.count.is_some() && !count)
        {
            Err(match request.cmd {
                Cmd::Look | Cmd::Stop => format!("{name} takes no keys, target or count"),
                Cmd::Send => "send takes only keys".to_owned(),
                Cmd::Go => "go takes only a target".to_owned(),
                Cmd::Rest => "rest takes only a count".to_owned(),
            })
        } else {
            Ok(())
        }
    };
    match request.cmd {
        Cmd::Look => only(false, false, false).map(|()| Checked::Look),
        Cmd::Stop => only(false, false, false).map(|()| Checked::Stop),
        Cmd::Send => {
            only(true, false, false)?;
            let keys = unescape(request.keys.as_deref().unwrap_or(""))?;
            if keys.is_empty() || keys.len() > KEYS_MAX {
                return Err(format!("send takes 1 to {KEYS_MAX} keys"));
            }
            Ok(Checked::Send(keys))
        }
        Cmd::Go => {
            only(false, true, false)?;
            match request.target.as_deref().map(str::trim) {
                Some(target) if !target.is_empty() && target.len() <= TARGET_MAX => {
                    Ok(Checked::Go(target))
                }
                _ => Err(format!(
                    "go takes a target of 1 to {TARGET_MAX} bytes: x,y, a map glyph, a feature or frontier"
                )),
            }
        }
        Cmd::Rest => {
            only(false, false, true)?;
            match request.count {
                Some(count) if (1..=REST_MAX).contains(&count) => Ok(Checked::Rest(count)),
                _ => Err(format!("rest takes 1 to {REST_MAX} searches")),
            }
        }
    }
}

/// Serves requests on the socket at `socket` for the game behind the terminal socket `terminal`, until a `stop`
/// or a closed terminal. Writes `ready` and a newline to `ready` once listening.
pub fn serve(
    socket: &Path,
    terminal: &Path,
    max_steps: u32,
    ready: &mut dyn Write,
) -> std::io::Result<()> {
    let (listener, _held) = bind(socket)?;
    let mut play = Play::new(Term::new(terminal), max_steps);
    if let Err(e) = ready.write_all(b"ready\n").and_then(|()| ready.flush()) {
        let _ = std::fs::remove_file(socket);
        return Err(e);
    }
    let mut line = Vec::with_capacity(256);
    let mut out = Vec::with_capacity(16 * 1024);
    let result = loop {
        let stream = match listener.accept() {
            Ok((stream, _)) => stream,
            Err(e) if e.kind() == ErrorKind::Interrupted => continue,
            Err(e) => break Err(e),
        };
        if answer(&stream, &mut play, &mut line, &mut out) == After::Exit {
            break Ok(());
        }
    };
    let _ = std::fs::remove_file(socket);
    result
}

/// Answers the one request on `stream`. A reply that tells of a closed terminal ends the daemon only once it has
/// reached the client.
fn answer(
    stream: &UnixStream,
    play: &mut Play<Term>,
    line: &mut Vec<u8>,
    out: &mut Vec<u8>,
) -> After {
    let (reply, after) = match read_line(stream, line) {
        // A connection that sends nothing, such as a liveness probe, gets no reply.
        Ok(0) => return After::Serve,
        Ok(_) => match parse_request(line) {
            Ok(request) => handle(play, &request),
            Err(why) => (Reply::message(ExitCode::Usage, why), After::Serve),
        },
        Err(e) => (
            Reply::message(ExitCode::Usage, format!("cannot read the request: {e}")),
            After::Serve,
        ),
    };
    out.clear();
    if let Err(e) = serde_json::to_writer(&mut *out, &reply) {
        log(format_args!("cannot encode a reply: {e}"));
        return After::Serve;
    }
    out.push(b'\n');
    match write_all(stream, out) {
        Ok(()) => {
            if after == After::Exit && reply.code == ExitCode::Closed {
                log(format_args!("exiting: {}", reply.text));
            }
            after
        }
        Err(e) => {
            log(format_args!("cannot write a reply: {e}"));
            if reply.code == ExitCode::Closed {
                After::Serve
            } else {
                after
            }
        }
    }
}

/// The time left before `deadline`, or a timeout error once it has passed.
fn left(deadline: Instant) -> std::io::Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|left| !left.is_zero())
        .ok_or_else(|| ErrorKind::TimedOut.into())
}

/// Reads one request line from `stream` into `line` within `CLIENT_TIMEOUT` in all, however the client paces it;
/// returns the bytes read, 0 for a connection that sent nothing. A line over `REQUEST_MAX` bytes is cut there.
fn read_line(mut stream: &UnixStream, line: &mut Vec<u8>) -> std::io::Result<usize> {
    let deadline = Instant::now() + CLIENT_TIMEOUT;
    line.clear();
    let mut chunk = [0u8; 512];
    while !line.contains(&b'\n') && line.len() <= REQUEST_MAX {
        stream.set_read_timeout(Some(left(deadline)?))?;
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => line.extend_from_slice(&chunk[..n]),
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) if e.kind() == ErrorKind::WouldBlock => return Err(ErrorKind::TimedOut.into()),
            Err(e) => return Err(e),
        }
    }
    Ok(line.len())
}

/// Writes all of `out` to `stream` within `CLIENT_TIMEOUT` in all.
fn write_all(mut stream: &UnixStream, mut out: &[u8]) -> std::io::Result<()> {
    let deadline = Instant::now() + CLIENT_TIMEOUT;
    while !out.is_empty() {
        stream.set_write_timeout(Some(left(deadline)?))?;
        match stream.write(out) {
            Ok(0) => return Err(ErrorKind::WriteZero.into()),
            Ok(n) => out = &out[n..],
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    Ok(())
}

/// Writes one line to the log, which is standard error.
fn log(message: std::fmt::Arguments) {
    let _ = writeln!(std::io::stderr(), "nethack-harness: {message}");
}

/// Binds the listening socket, readable and writable by its owner only, and returns it with the lock on
/// `<socket>.lock` that the daemon holds for as long as it serves. A socket file nobody holds the lock for is stale
/// and replaced; while a daemon holds it, binding fails with `AddrInUse`.
pub fn bind(socket: &Path) -> std::io::Result<(UnixListener, File)> {
    let mut path = socket.as_os_str().to_owned();
    path.push(".lock");
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(path)?;
    if lock.try_lock().is_err() {
        return Err(std::io::Error::new(
            ErrorKind::AddrInUse,
            format!("a daemon already answers on {}", socket.display()),
        ));
    }
    match std::fs::remove_file(socket) {
        Err(e) if e.kind() != ErrorKind::NotFound => return Err(e),
        _ => {}
    }
    let listener = UnixListener::bind(socket)?;
    std::fs::set_permissions(socket, Permissions::from_mode(0o600))?;
    Ok((listener, lock))
}

/// Answers one request.
pub fn handle(play: &mut Play<Term>, request: &Request) -> (Reply, After) {
    let command = match checked(request) {
        Ok(command) => command,
        Err(why) => return (Reply::message(ExitCode::Usage, why), After::Serve),
    };
    if let Checked::Stop = command {
        return (Reply::message(ExitCode::Done, "stopped"), After::Exit);
    }
    play.term_mut().deadline(Instant::now() + COMMAND_TIME);
    let reply = match (play.sync(), command) {
        (Err(e), _) => Reply::message(e.code(), e.to_string()),
        (Ok(()), Checked::Look) => play.look(),
        (Ok(()), Checked::Send(keys)) => play.send(&keys),
        (Ok(()), Checked::Go(target)) => play.go(target),
        (Ok(()), Checked::Rest(count)) => play.rest(count),
        (Ok(()), Checked::Stop) => Reply::message(ExitCode::Done, "stopped"),
    };
    let after = if reply.code == ExitCode::Closed {
        After::Exit
    } else {
        After::Serve
    };
    (reply, after)
}

/// Decodes one request line; the error is the message a malformed request is answered with, under code 64.
pub fn parse_request(line: &[u8]) -> Result<Request, String> {
    let end = line.iter().position(|&b| b == b'\n').unwrap_or(line.len());
    if end >= REQUEST_MAX {
        return Err(format!("a request line takes at most {REQUEST_MAX} bytes"));
    }
    serde_json::from_slice(line[..end].trim_ascii_end())
        .map_err(|e| format!("invalid request: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(text: &str) -> Result<Request, String> {
        parse_request(text.as_bytes())
    }

    fn request(cmd: Cmd) -> Request {
        Request {
            cmd,
            keys: None,
            target: None,
            count: None,
        }
    }

    #[test]
    fn requests_decode_from_one_line() {
        assert_eq!(read("{\"cmd\":\"look\"}\n").unwrap(), request(Cmd::Look));
        let send = read("{\"cmd\":\"send\",\"keys\":\"-\\\\e\"}\n{\"cmd\":\"stop\"}\n").unwrap();
        assert_eq!(send.keys.as_deref(), Some("-\\e"));
        assert_eq!(
            read("{\"cmd\":\"rest\",\"count\":5}").unwrap().count,
            Some(5)
        );
    }

    #[test]
    fn malformed_requests_are_refused() {
        assert!(read("").unwrap_err().contains("invalid request"));
        assert!(
            parse_request(b"{\"cmd\":\"go\",\"target\":\"\xff\"}\n")
                .unwrap_err()
                .contains("invalid request")
        );
        assert!(
            read("{\"cmd\":\"dance\"}\n")
                .unwrap_err()
                .contains("invalid request")
        );
        assert!(read("{\"cmd\":\"look\",\"extra\":1}\n").is_err());
        assert!(read("not json\n").is_err());
        let long = format!(
            "{{\"cmd\":\"go\",\"target\":\"{}\"}}\n",
            "x".repeat(REQUEST_MAX)
        );
        assert!(read(&long).unwrap_err().contains("at most"));
    }

    #[test]
    fn requests_serialise_without_absent_fields() {
        let mut send = request(Cmd::Send);
        send.keys = Some("\\e".to_owned());
        assert_eq!(
            serde_json::to_string(&send).unwrap(),
            "{\"cmd\":\"send\",\"keys\":\"\\\\e\"}"
        );
        assert_eq!(
            serde_json::to_string(&request(Cmd::Stop)).unwrap(),
            "{\"cmd\":\"stop\"}"
        );
    }

    #[test]
    fn arguments_are_checked_per_command() {
        assert!(check(&request(Cmd::Look)).is_ok());
        assert!(check(&request(Cmd::Send)).is_err());
        let mut send = request(Cmd::Send);
        send.keys = Some("-\\e".to_owned());
        assert!(check(&send).is_ok());
        send.keys = Some("x".repeat(KEYS_MAX + 1));
        assert!(check(&send).unwrap_err().contains("1 to 256"));
        send.keys = Some("☃".to_owned());
        assert!(check(&send).is_err());
        let mut go = request(Cmd::Go);
        assert!(check(&go).is_err());
        go.target = Some("  ".to_owned());
        assert!(check(&go).is_err());
        go.target = Some("23,3".to_owned());
        assert!(check(&go).is_ok());
        go.target = Some("x".repeat(TARGET_MAX + 1));
        assert!(check(&go).is_err());
        go.count = Some(1);
        assert_eq!(check(&go).unwrap_err(), "go takes only a target");
        let mut rest = request(Cmd::Rest);
        for (count, ok) in [
            (None, false),
            (Some(0), false),
            (Some(1), true),
            (Some(200), true),
            (Some(201), false),
        ] {
            rest.count = count;
            assert_eq!(check(&rest).is_ok(), ok, "{count:?}");
        }
        let mut look = request(Cmd::Look);
        look.keys = Some("x".to_owned());
        assert_eq!(
            check(&look).unwrap_err(),
            "look takes no keys, target or count"
        );
    }
}
