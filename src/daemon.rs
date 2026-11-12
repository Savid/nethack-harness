//! The daemon: one thread blocked in `accept` on `<dir>/sock`, answering one JSON request line with one JSON reply
//! line per connection, in order, so play is serialised. It holds all game memory in RAM, has no timers, and writes
//! only its log, on errors.
//!
//! Request: `{"cmd": "look"|"send"|"go"|"rest"|"learn"|"stop", "keys"?: str, "target"?: str, "wet"?: bool,
//! "count"?: u32, "near"?: int, "rows"?: [str], "colours"?: [str]}`; `keys` uses the escape syntax
//! `\e \r \n \t \\ \xHH`. Reply: `{"code": int, "text": str, "facts": {…}}`.

use std::fs::{File, OpenOptions, Permissions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use crate::link::Term;
use crate::play::{KEYS_MAX, Play, REST_MAX};
use crate::types::{COLS, ExitCode, MAP_ROWS, Reply, unescape};

/// Longest request line accepted, in bytes.
const REQUEST_MAX: usize = 8192;
/// Longest `go` target, in bytes.
const TARGET_MAX: usize = 64;
/// Largest `near` radius, in squares: the map's width.
pub const NEAR_MAX: u8 = 80;
/// Longest the daemon waits on one client to send its whole request, and again to take its whole reply.
const CLIENT_TIMEOUT: Duration = Duration::from_secs(2);
/// Longest one command keeps sending to the game; past it the command stops short.
pub const COMMAND_TIME: Duration = Duration::from_secs(60);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Cmd {
    #[default]
    Look,
    Send,
    Go,
    Rest,
    Learn,
    Stop,
}

/// One request line: `keys` for `send`, `target` and optionally `wet` for `go`, `count` for `rest`, optionally
/// `near` for `go` and `rest`, and `rows` and `colours` for `learn`.
#[derive(Clone, Debug, Default, PartialEq, Eq, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Request {
    pub cmd: Cmd,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub keys: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub target: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wet: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub count: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub near: Option<u8>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rows: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub colours: Option<Vec<String>>,
}

/// A request whose arguments have been checked.
#[derive(Debug)]
pub enum Checked<'a> {
    Look,
    Send(Vec<u8>),
    Go {
        target: &'a str,
        wet: bool,
        near: Option<u8>,
    },
    Rest {
        count: u32,
        near: Option<u8>,
    },
    Learn {
        rows: &'a [String],
        colours: &'a [String],
    },
    Stop,
}

/// Checks that a request carries exactly the arguments its command takes, within their limits; the error is the
/// message it is refused with under code 64.
pub fn check(request: &Request) -> Result<Checked<'_>, String> {
    let (keys, target, count, near, map) = (
        request.keys.is_some(),
        request.target.is_some() || request.wet.is_some(),
        request.count.is_some(),
        request.near,
        request.rows.is_some() || request.colours.is_some(),
    );
    let any = keys || target || count || near.is_some();
    let refuse = |why: &str| Err(why.to_owned());
    match request.cmd {
        Cmd::Look if any || map => {
            refuse("look takes no keys, target, wet, count, near, rows or colours")
        }
        Cmd::Stop if any || map => {
            refuse("stop takes no keys, target, wet, count, near, rows or colours")
        }
        Cmd::Send if target || count || near.is_some() || map => refuse("send takes only keys"),
        Cmd::Go if keys || count || map => refuse("go takes only a target, wet and near"),
        Cmd::Rest if keys || target || map => refuse("rest takes only a count and near"),
        Cmd::Learn if any => refuse("learn takes only rows and colours"),
        Cmd::Go | Cmd::Rest if near.is_some_and(|n| !(1..=NEAR_MAX).contains(&n)) => {
            Err(format!("near takes 1 to {NEAR_MAX} squares"))
        }
        Cmd::Look => Ok(Checked::Look),
        Cmd::Stop => Ok(Checked::Stop),
        Cmd::Send => match unescape(request.keys.as_deref().unwrap_or(""))? {
            keys if (1..=KEYS_MAX).contains(&keys.len()) => Ok(Checked::Send(keys)),
            _ => Err(format!("send takes 1 to {KEYS_MAX} keys")),
        },
        Cmd::Go => match request.target.as_deref().map(str::trim) {
            Some(target) if (1..=TARGET_MAX).contains(&target.len()) => Ok(Checked::Go {
                target,
                wet: request.wet == Some(true),
                near,
            }),
            _ => Err(format!(
                "go takes a target of 1 to {TARGET_MAX} bytes: x,y, a map glyph, a feature or frontier"
            )),
        },
        Cmd::Rest => match request.count {
            Some(count) if (1..=REST_MAX).contains(&count) => Ok(Checked::Rest { count, near }),
            _ => Err(format!("rest takes 1 to {REST_MAX} searches")),
        },
        Cmd::Learn => {
            let rows = request.rows.as_deref().unwrap_or_default();
            let colours = request.colours.as_deref().unwrap_or_default();
            let sized = |lines: &[String]| {
                let printable = |line: &String| line.bytes().all(|b| matches!(b, b' '..=b'~'));
                lines.len() == MAP_ROWS
                    && lines
                        .iter()
                        .all(|line| line.len() == COLS && printable(line))
            };
            if !sized(rows) || !sized(colours) {
                return Err(format!(
                    "learn takes {MAP_ROWS} rows and {MAP_ROWS} colours, each of {COLS} printable ASCII characters"
                ));
            }
            let hex = |(ch, colour)| ch == b' ' || matches!(colour, b'0'..=b'9' | b'a'..=b'f');
            let coloured =
                |(row, colours): (&String, &String)| row.bytes().zip(colours.bytes()).all(hex);
            if !rows.iter().zip(colours).all(coloured) {
                return refuse("learn takes a colour digit, 0 to f, under each square of rows");
            }
            Ok(Checked::Learn { rows, colours })
        }
    }
}

/// Serves requests on the socket at `socket` for the game behind the terminal socket `terminal`, until a `stop`
/// or a closed terminal. Writes `ready` and a newline to `ready` once listening.
pub fn serve(socket: &Path, terminal: &Path, ready: &mut dyn Write) -> std::io::Result<()> {
    let (listener, _held) = bind(socket)?;
    let mut play = Play::new(Term::new(terminal));
    if let Err(e) = ready.write_all(b"ready\n").and_then(|()| ready.flush()) {
        let _ = std::fs::remove_file(socket);
        return Err(e);
    }
    let mut line = Vec::with_capacity(256);
    let mut out = Vec::with_capacity(16 * 1024);
    let result = loop {
        match listener.accept() {
            Ok((stream, _)) if answer(&stream, &mut play, &mut line, &mut out) => break Ok(()),
            Ok(_) => {}
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => break Err(e),
        }
    };
    let _ = std::fs::remove_file(socket);
    result
}

/// Answers the one request on `stream`; true when the daemon is to exit. A reply that tells of a closed terminal
/// ends the daemon only once it has reached the client.
fn answer(
    stream: &UnixStream,
    play: &mut Play<Term>,
    line: &mut Vec<u8>,
    out: &mut Vec<u8>,
) -> bool {
    let (reply, exit) = match read_line(stream, line) {
        // A connection that sends nothing, such as a liveness probe, gets no reply.
        Ok(0) => return false,
        Ok(_) => match parse_request(line) {
            Ok(request) => handle(play, &request),
            Err(why) => (Reply::message(ExitCode::Usage, why), false),
        },
        Err(e) => {
            let why = format!("cannot read the request: {e}");
            (Reply::message(ExitCode::Usage, why), false)
        }
    };
    out.clear();
    if let Err(e) = serde_json::to_writer(&mut *out, &reply) {
        log(format_args!("cannot encode a reply: {e}"));
        return false;
    }
    out.push(b'\n');
    let closed = reply.code == ExitCode::Closed;
    match write_all(stream, out) {
        Ok(()) if exit && closed => {
            log(format_args!("exiting: {}", reply.text));
            true
        }
        Ok(()) => exit,
        Err(e) => {
            log(format_args!("cannot write a reply: {e}"));
            exit && !closed
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

/// The lock a serving daemon holds: `<socket>.lock`.
fn lock_path(socket: &Path) -> PathBuf {
    let mut path = socket.as_os_str().to_owned();
    path.push(".lock");
    path.into()
}

/// Whether a daemon holds the lock on `<socket>.lock`, so the socket is live.
pub fn serving(socket: &Path) -> bool {
    OpenOptions::new()
        .write(true)
        .open(lock_path(socket))
        .is_ok_and(|file| file.try_lock().is_err())
}

/// Binds the listening socket, readable and writable by its owner only, and returns it with the lock on
/// `<socket>.lock` that the daemon holds for as long as it serves. A socket file nobody holds the lock for is stale
/// and replaced; while a daemon holds it, binding fails with `AddrInUse`.
pub fn bind(socket: &Path) -> std::io::Result<(UnixListener, File)> {
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(lock_path(socket))?;
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

/// Answers one request; true when the daemon is to exit once the reply reaches the client: after a `stop`, or
/// when the terminal has closed.
pub fn handle(play: &mut Play<Term>, request: &Request) -> (Reply, bool) {
    let command = match check(request) {
        Ok(Checked::Stop) => return (Reply::message(ExitCode::Done, "stopped"), true),
        Ok(command) => command,
        Err(why) => return (Reply::message(ExitCode::Usage, why), false),
    };
    play.term_mut().deadline(Instant::now() + COMMAND_TIME);
    let reply = match (play.sync(), command) {
        (Err(e), _) => Reply::message(e.code(), e.to_string()),
        (Ok(()), Checked::Look) => play.look(),
        (Ok(()), Checked::Send(keys)) => play.send(&keys),
        (Ok(()), Checked::Go { target, wet, near }) => play.go(target, wet, near),
        (Ok(()), Checked::Rest { count, near }) => play.rest(count, near),
        (Ok(()), Checked::Learn { rows, colours }) => play.learn(rows, colours),
        (Ok(()), Checked::Stop) => Reply::message(ExitCode::Done, "stopped"),
    };
    let exit = reply.code == ExitCode::Closed;
    (reply, exit)
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
            wet: None,
            count: None,
            near: None,
            rows: None,
            colours: None,
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
        let wet = read("{\"cmd\":\"go\",\"target\":\">\",\"wet\":true}").unwrap();
        assert_eq!((wet.target.as_deref(), wet.wet), (Some(">"), Some(true)));
        assert!(read("{\"cmd\":\"go\",\"target\":\">\",\"wet\":1}").is_err());
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
    fn learn_takes_the_whole_map_and_a_colour_under_each_square_told() {
        let rows = |row: &str, count: usize| Some(vec![format!("{row:<80}"); count]);
        let mut learn = request(Cmd::Learn);
        assert!(check(&learn).is_err());
        learn.rows = rows(" |..>", MAP_ROWS);
        learn.colours = Some(vec!["7".repeat(COLS); MAP_ROWS]);
        assert!(matches!(
            check(&learn),
            Ok(Checked::Learn { rows, colours }) if rows.len() == MAP_ROWS && colours.len() == MAP_ROWS
        ));
        // A request that tells of every square fits a request line.
        learn.rows = rows(&"\\".repeat(COLS), MAP_ROWS);
        let line = serde_json::to_string(&learn).unwrap();
        assert_eq!(parse_request(line.as_bytes()).unwrap(), learn);
        // The colour under a square that tells nothing is not read.
        learn.rows = rows(" |..>", MAP_ROWS);
        learn.colours = rows("x3bf0", MAP_ROWS);
        assert!(check(&learn).is_ok());
        for colours in [" g777", " F777", " 7"] {
            learn.colours = rows(colours, MAP_ROWS);
            let refused = check(&learn).unwrap_err();
            assert!(refused.contains("colour digit"), "{colours}: {refused}");
        }
        for (told, colours) in [
            (rows(" |..>", MAP_ROWS - 1), rows("77777", MAP_ROWS)),
            (rows(" |..>", MAP_ROWS), rows("77777", MAP_ROWS + 1)),
            (rows(" |..>", MAP_ROWS), None),
            (
                Some(vec![" |..>".to_owned(); MAP_ROWS]),
                rows("77777", MAP_ROWS),
            ),
            (rows(" |.\u{b7}>", MAP_ROWS), rows("77777", MAP_ROWS)),
        ] {
            learn.rows = told;
            learn.colours = colours;
            let refused = check(&learn).unwrap_err();
            assert!(refused.contains("21 rows and 21 colours"), "{refused}");
        }
        learn.near = Some(3);
        assert_eq!(
            check(&learn).unwrap_err(),
            "learn takes only rows and colours"
        );
        let mut go = request(Cmd::Go);
        go.target = Some(">".to_owned());
        go.rows = rows("", MAP_ROWS);
        assert_eq!(
            check(&go).unwrap_err(),
            "go takes only a target, wet and near"
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
        assert!(matches!(
            check(&go),
            Ok(Checked::Go {
                target: "23,3",
                wet: false,
                near: None,
            })
        ));
        go.wet = Some(true);
        assert!(matches!(check(&go), Ok(Checked::Go { wet: true, .. })));
        go.wet = Some(false);
        assert!(matches!(check(&go), Ok(Checked::Go { wet: false, .. })));
        go.target = None;
        assert!(check(&go).is_err());
        go.target = Some("x".repeat(TARGET_MAX + 1));
        assert!(check(&go).is_err());
        go.count = Some(1);
        assert_eq!(
            check(&go).unwrap_err(),
            "go takes only a target, wet and near"
        );
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
            "look takes no keys, target, wet, count, near, rows or colours"
        );
        let mut look = request(Cmd::Look);
        look.near = Some(3);
        assert!(check(&look).is_err());
        let mut send = request(Cmd::Send);
        send.keys = Some("s".to_owned());
        send.wet = Some(true);
        assert_eq!(check(&send).unwrap_err(), "send takes only keys");
        let mut rest = request(Cmd::Rest);
        rest.count = Some(1);
        rest.wet = Some(false);
        assert_eq!(
            check(&rest).unwrap_err(),
            "rest takes only a count and near"
        );
        rest.wet = None;
        for (near, ok) in [
            (Some(0), false),
            (Some(1), true),
            (Some(80), true),
            (Some(81), false),
        ] {
            rest.near = near;
            assert_eq!(check(&rest).is_ok(), ok, "{near:?}");
        }
        rest.near = Some(3);
        assert!(matches!(
            check(&rest),
            Ok(Checked::Rest {
                count: 1,
                near: Some(3)
            })
        ));
        send.near = Some(3);
        send.wet = None;
        assert_eq!(check(&send).unwrap_err(), "send takes only keys");
    }
}
