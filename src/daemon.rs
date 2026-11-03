//! The daemon: one thread blocked in `accept` on `<dir>/sock`, answering one JSON request line with one JSON reply
//! line per connection, in order, so play is serialised. It holds all game memory in RAM, has no timers, and writes
//! only its log, on errors.
//!
//! Request: `{"cmd": "look"|"send"|"go"|"rest"|"stop", "keys"?: str, "target"?: str, "count"?: u32}`; `keys` uses
//! the escape syntax `\e \r \n \t \\ \xHH`. Reply: `{"code": int, "text": str, "facts": {…}}`.

use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::link::Term;
use crate::play::{KEYS_MAX, Play, REST_MAX};
use crate::types::{ExitCode, Reply, unescape};

/// Longest request line accepted, in bytes.
pub const REQUEST_MAX: usize = 4096;
/// Longest the daemon waits on one client to send its request or take its reply.
const CLIENT_TIMEOUT: Duration = Duration::from_secs(10);

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
                Some(target) if !target.is_empty() => Ok(Checked::Go(target)),
                _ => Err("go takes a target: x,y, a map glyph, a feature or frontier".to_owned()),
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
    let listener = bind(socket)?;
    let mut play = Play::new(Term::new(terminal), max_steps);
    if let Err(e) = ready.write_all(b"ready\n").and_then(|()| ready.flush()) {
        let _ = std::fs::remove_file(socket);
        return Err(e);
    }
    let mut line = String::with_capacity(256);
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
    line: &mut String,
    out: &mut Vec<u8>,
) -> After {
    let _ = stream.set_read_timeout(Some(CLIENT_TIMEOUT));
    let _ = stream.set_write_timeout(Some(CLIENT_TIMEOUT));
    let mut reader = BufReader::with_capacity(1024, stream);
    let (reply, after) = match read_request(&mut reader, line) {
        Ok(request) => handle(play, &request),
        // A connection that sends nothing, such as a liveness probe, gets no reply.
        Err(_) if line.is_empty() => return After::Serve,
        Err(why) => (Reply::message(ExitCode::Usage, why), After::Serve),
    };
    out.clear();
    if let Err(e) = serde_json::to_writer(&mut *out, &reply) {
        log(format_args!("cannot encode a reply: {e}"));
        return After::Serve;
    }
    out.push(b'\n');
    let mut writer = stream;
    match writer.write_all(out) {
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

/// Writes one line to the log, which is standard error.
fn log(message: std::fmt::Arguments) {
    let _ = writeln!(std::io::stderr(), "nethack-harness: {message}");
}

/// Binds the listening socket, replacing a stale socket file no daemon answers on.
pub fn bind(socket: &Path) -> std::io::Result<UnixListener> {
    match UnixListener::bind(socket) {
        Err(e) if e.kind() == ErrorKind::AddrInUse => {
            if UnixStream::connect(socket).is_ok() {
                return Err(std::io::Error::new(
                    ErrorKind::AddrInUse,
                    format!("a daemon already answers on {}", socket.display()),
                ));
            }
            std::fs::remove_file(socket)?;
            UnixListener::bind(socket)
        }
        other => other,
    }
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
    if !play.term().complete()
        && let Err(e) = play.term_mut().sync()
    {
        let reply = Reply::message(e.code(), e.to_string());
        let after = if reply.code == ExitCode::Closed {
            After::Exit
        } else {
            After::Serve
        };
        return (reply, after);
    }
    let reply = match command {
        Checked::Look => play.look(),
        Checked::Send(keys) => play.send(&keys),
        Checked::Go(target) => play.go(target),
        Checked::Rest(count) => play.rest(count),
        Checked::Stop => Reply::message(ExitCode::Done, "stopped"),
    };
    let after = if reply.code == ExitCode::Closed {
        After::Exit
    } else {
        After::Serve
    };
    (reply, after)
}

/// Reads one request line from `input` into `line`, bounded in length, and decodes it; the error is the message a
/// malformed request is answered with, under code 64.
pub fn read_request(input: &mut dyn BufRead, line: &mut String) -> Result<Request, String> {
    line.clear();
    let read = input
        .take(REQUEST_MAX as u64 + 1)
        .read_line(line)
        .map_err(|e| format!("cannot read the request: {e}"))?;
    if read == 0 {
        return Err("empty request".to_owned());
    }
    if read > REQUEST_MAX {
        return Err(format!("a request line takes at most {REQUEST_MAX} bytes"));
    }
    serde_json::from_str(line.trim_end()).map_err(|e| format!("invalid request: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(text: &str) -> Result<Request, String> {
        let mut line = String::new();
        read_request(&mut text.as_bytes(), &mut line)
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
        assert!(read("").unwrap_err().contains("empty"));
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
