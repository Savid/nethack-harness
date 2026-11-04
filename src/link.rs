//! The terminal link: one persistent HTTP/1.1 keep-alive connection over a Unix socket to `POST /terminal`, and
//! the screen kept in step with it. A request is `{"input": base64, "after": cursor, "wait": ms}`; a 200 reply is
//! `{"output": base64, "cursor": n, "truncated": bool, "waiting": bool}`, where `waiting` means the game is blocked
//! reading a key, has consumed all input sent so far, and all its output is in `output`. A frozen, stopping or
//! ended game answers at once with `waiting` false.

use std::borrow::Cow;
use std::io::{ErrorKind, Read, Write};
use std::ops::Range;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::screen::{PromptContext, View};
use crate::types::ExitCode;
use crate::vt::Vt;

/// Longest the server is asked to wait for the game to settle, in milliseconds.
pub const WAIT_MS: u32 = 3000;
pub const INPUT_MAX: usize = 4096;
/// Re-reads with `wait` while the game stays busy, before reporting it still busy.
pub const BUSY_READS: u32 = 4;
/// A run of 503 answers is retried for at most `BUSY_BUDGET`, after pauses that start at `BUSY_PAUSE` and double
/// up to `BUSY_PAUSE_MAX`.
const BUSY_BUDGET: Duration = Duration::from_secs(5);
const BUSY_PAUSE: Duration = Duration::from_millis(10);
const BUSY_PAUSE_MAX: Duration = Duration::from_secs(1);

/// Largest response body accepted: the server keeps at most 64 KiB of output, which base64 and JSON enlarge.
const BODY_MAX: usize = 512 * 1024;
const HEADER_MAX: usize = 8192;
const CHUNK_LINE_MAX: usize = 1024;
/// Capacity the response and output buffers return to after a reply larger than this, such as a full replay.
const KEEP: usize = 32 * 1024;
/// A connection idle this long is replaced before use, because the server closes idle connections after 30 s.
const IDLE: Duration = Duration::from_secs(20);
/// How much longer than the requested wait a reply may take before the server counts as unresponsive.
const READ_MARGIN_MS: u64 = 5000;
const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
/// Most characters of an error body quoted in a message.
const QUOTE_MAX: usize = 200;

/// Why an exchange with the terminal failed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LinkError {
    /// 410 `waiting to start`: the game takes no input yet; this request's input was not sent.
    Held(String),
    /// Any other 410, or the socket went away after answering before: the terminal is closed.
    Closed(String),
    /// The socket could not be reached at all.
    Unavailable(String),
    /// The server answered something this client does not understand, kept answering 503, or the connection
    /// failed partway through an exchange.
    Protocol(String),
    /// The game was still busy after the re-reads, or the command ran out of time; later keys were not sent.
    Busy(String),
}

impl LinkError {
    pub fn code(&self) -> ExitCode {
        match self {
            Self::Held(_) => ExitCode::Held,
            Self::Closed(_) | Self::Unavailable(_) => ExitCode::Closed,
            Self::Protocol(_) => ExitCode::Failed,
            Self::Busy(_) => ExitCode::Short,
        }
    }
}

impl std::fmt::Display for LinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Held(why) => write!(f, "the game has not started yet: {why}"),
            Self::Closed(why) => write!(f, "the terminal is closed: {why}"),
            Self::Unavailable(why) | Self::Protocol(why) | Self::Busy(why) => f.write_str(why),
        }
    }
}

impl std::error::Error for LinkError {}

/// What one exchange returned, its output bytes aside: whether the output does not continue the previous one
/// (truncated, or the cursor went back), so the screen is reset first, and whether the game is blocked reading a
/// key with all its output delivered.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Exchange {
    pub reset: bool,
    pub waiting: bool,
}

/// The HTTP client: one connection, reconnected on EOF, with request and response buffers reused across calls.
pub struct Link {
    path: PathBuf,
    stream: Option<UnixStream>,
    cursor: u64,
    request: Vec<u8>,
    /// The request body while it is built.
    json: Vec<u8>,
    /// The last response; its body is `response[body]`, dechunked in place.
    response: Vec<u8>,
    body: Range<usize>,
    output: Vec<u8>,
    /// When the server last answered; once it has, a vanished socket means a closed terminal.
    used: Option<Instant>,
}

/// The 200 reply body. `output` is null when the server has no output at all.
#[derive(Deserialize)]
struct Answer<'a> {
    #[serde(borrow, default)]
    output: Option<Cow<'a, str>>,
    cursor: u64,
    #[serde(default)]
    truncated: bool,
    #[serde(default)]
    waiting: bool,
}

/// How one HTTP round trip failed.
enum Fail {
    /// The request could not be written, or the connection closed before any byte of a response arrived.
    Stale { written: bool },
    /// The socket failed partway through the response, or the response took past its deadline.
    Io(std::io::Error),
    /// The response is not HTTP this client understands; says what the server answered.
    Bad(&'static str),
}

/// The error for a reply this client cannot use, saying what the server answered.
fn answered(what: impl std::fmt::Display) -> LinkError {
    LinkError::Protocol(format!("the terminal socket answered {what}"))
}

struct Head {
    status: u16,
    length: Option<usize>,
    chunked: bool,
    close: bool,
}

impl Link {
    /// A link to the terminal socket at `path`; it connects on first use.
    pub fn new(path: &Path) -> Link {
        Link {
            path: path.to_path_buf(),
            stream: None,
            cursor: 0,
            request: Vec::with_capacity(1024),
            json: Vec::with_capacity(1024),
            response: Vec::with_capacity(16 * 1024),
            body: 0..0,
            output: Vec::with_capacity(16 * 1024),
            used: None,
        }
    }

    /// Sends `input` (possibly empty, a pure read) and returns what the server answered. With `wait_ms` > 0 the
    /// server holds the reply until the game waits for a key or the deadline passes. 503 answers are retried with
    /// growing pauses for at most `BUSY_BUDGET`.
    pub fn exchange(&mut self, mut input: &[u8], wait_ms: u32) -> Result<Exchange, LinkError> {
        if input.len() > INPUT_MAX {
            return Err(LinkError::Protocol(format!(
                "at most {INPUT_MAX} input bytes go in one request"
            )));
        }
        let mut wait = wait_ms.min(WAIT_MS);
        let mut reset = false;
        let busy_until = Instant::now() + BUSY_BUDGET;
        let mut pause = BUSY_PAUSE;
        loop {
            match self.post(input, wait)? {
                200 => {}
                503 if Instant::now() + pause < busy_until => {
                    std::thread::sleep(pause);
                    pause = (pause * 2).min(BUSY_PAUSE_MAX);
                    continue;
                }
                503 => {
                    return Err(LinkError::Protocol(format!(
                        "the terminal stayed busy (HTTP 503) for {} s",
                        BUSY_BUDGET.as_secs()
                    )));
                }
                410 if self.quote().contains("waiting") => {
                    return Err(LinkError::Held(self.quote()));
                }
                410 => return Err(LinkError::Closed(self.quote())),
                status => return Err(answered(format!("HTTP {status}: {}", self.quote()))),
            }
            let answer: Answer = serde_json::from_slice(&self.response[self.body.clone()])
                .map_err(|e| answered(format!("malformed JSON: {e}")))?;
            self.output.clear();
            self.output.shrink_to(KEEP);
            let text = answer.output.as_deref().unwrap_or_default();
            if !base64_decode(text.as_bytes(), &mut self.output) {
                self.output.clear();
                return Err(answered("malformed base64 output"));
            }
            if answer.cursor < self.cursor {
                // The server started over and clamped `after` to its new cursor, so this reply holds none of its
                // output: read everything it has from the start.
                self.cursor = 0;
                reset = true;
                input = &[];
                wait = 0;
                continue;
            }
            self.cursor = answer.cursor;
            return Ok(Exchange {
                reset: reset || answer.truncated,
                waiting: answer.waiting,
            });
        }
    }

    /// The output bytes of the last exchange.
    pub fn output(&self) -> &[u8] {
        &self.output
    }

    /// The server's output cursor after the last exchange.
    pub fn cursor(&self) -> u64 {
        self.cursor
    }

    /// The body of the last response as text, trimmed and cut short for a message.
    fn quote(&self) -> String {
        let text = String::from_utf8_lossy(&self.response[self.body.clone()]);
        text.trim().chars().take(QUOTE_MAX).collect()
    }

    /// Posts one request and returns the response status, with its body in `response[body]`. Input is never sent
    /// twice: a kept connection is checked before input goes out on it, and one the server closed is replaced once,
    /// unless it may have taken the input. One idle long enough for the server to close it is replaced before use.
    fn post(&mut self, input: &[u8], wait: u32) -> Result<u16, LinkError> {
        self.json.clear();
        self.json.extend_from_slice(b"{\"input\":\"");
        base64_encode(input, &mut self.json);
        // Writes into a Vec cannot fail.
        let _ = write!(self.json, "\",\"after\":{},\"wait\":{wait}}}", self.cursor);
        self.request.clear();
        let _ = write!(
            self.request,
            "POST /terminal HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\n\r\n",
            self.json.len()
        );
        self.request.extend_from_slice(&self.json);
        let has_input = !input.is_empty();
        if self.used.is_some_and(|at| at.elapsed() >= IDLE)
            || (has_input && self.stream.as_ref().is_some_and(|stream| !open(stream)))
        {
            self.stream = None;
        }
        let limit = u64::from(wait) + READ_MARGIN_MS;
        // A failed connection is not put back, so a retry connects afresh and is never retried itself.
        loop {
            let reused = self.stream.is_some();
            let mut stream = match self.stream.take() {
                Some(stream) => stream,
                None => self.connect()?,
            };
            let deadline = Instant::now() + Duration::from_millis(limit);
            let error = match self.round_trip(&mut stream, deadline) {
                Ok(head) => {
                    self.used = Some(Instant::now());
                    if !head.close {
                        self.stream = Some(stream);
                    }
                    return Ok(head.status);
                }
                Err(Fail::Stale { written }) if reused && !(written && has_input) => continue,
                Err(Fail::Stale { written: true }) if has_input => LinkError::Protocol(
                    "the terminal connection closed before answering; whether the keys reached the game is \
                     unknown"
                        .to_owned(),
                ),
                Err(Fail::Stale { .. }) => self.gone(&"the connection closed without an answer"),
                Err(Fail::Io(e)) => LinkError::Protocol(match e.kind() {
                    ErrorKind::WouldBlock | ErrorKind::TimedOut => {
                        format!("the terminal socket did not answer within {limit} ms")
                    }
                    _ => format!("the terminal connection failed partway through an exchange ({e})"),
                }),
                Err(Fail::Bad(what)) => answered(what),
            };
            return Err(error);
        }
    }

    fn connect(&self) -> Result<UnixStream, LinkError> {
        let read_timeout = Duration::from_millis(u64::from(WAIT_MS) + READ_MARGIN_MS);
        UnixStream::connect(&self.path)
            .and_then(|stream| {
                stream.set_write_timeout(Some(WRITE_TIMEOUT))?;
                stream.set_read_timeout(Some(read_timeout))?;
                Ok(stream)
            })
            .map_err(|e| self.gone(&e))
    }

    /// The error for a socket that failed: closed if it answered before, otherwise unreachable.
    fn gone(&self, why: &dyn std::fmt::Display) -> LinkError {
        let path = self.path.display();
        if self.used.is_some() {
            LinkError::Closed(format!("the terminal socket went away ({why})"))
        } else {
            LinkError::Unavailable(format!("cannot reach the terminal socket {path}: {why}"))
        }
    }

    /// Writes the request and reads one response into `response` by `deadline`, its body dechunked in place.
    fn round_trip(&mut self, stream: &mut UnixStream, deadline: Instant) -> Result<Head, Fail> {
        match stream.write_all(&self.request) {
            Ok(()) => {}
            Err(e) if dropped(&e) => return Err(Fail::Stale { written: false }),
            Err(e) => return Err(Fail::Io(e)),
        }
        self.response.clear();
        self.response.shrink_to(KEEP);
        let mut reply = Reply {
            stream,
            buf: &mut self.response,
            deadline,
        };
        let end = match reply.find(0, b"\r\n\r\n", HEADER_MAX, "oversized headers") {
            Err(Fail::Io(e)) if reply.buf.is_empty() && dropped(&e) => {
                return Err(Fail::Stale { written: true });
            }
            found => found? + 4,
        };
        let mut head = parse_head(&reply.buf[..end])?;
        let length = if head.chunked {
            reply.dechunk(end)?
        } else if let Some(length) = head.length {
            if length > BODY_MAX {
                return Err(Fail::Bad("an oversized body"));
            }
            while reply.buf.len() < end + length {
                reply.more()?;
            }
            length
        } else {
            head.close = true;
            loop {
                match reply.more() {
                    Err(Fail::Io(e)) if e.kind() == ErrorKind::UnexpectedEof => break,
                    read => read?,
                }
            }
            reply.buf.len() - end
        };
        self.body = end..end + length;
        Ok(head)
    }
}

/// Whether a kept connection is still open: no end of file or error is waiting to be read on it.
fn open(stream: &UnixStream) -> bool {
    if stream.set_nonblocking(true).is_err() {
        return false;
    }
    let pending = (&*stream).read(&mut [0u8; 1]);
    stream.set_nonblocking(false).is_ok()
        && matches!(pending, Err(e) if e.kind() == ErrorKind::WouldBlock)
}

/// Whether a socket error means the peer closed the connection.
fn dropped(e: &std::io::Error) -> bool {
    matches!(
        e.kind(),
        ErrorKind::BrokenPipe
            | ErrorKind::ConnectionReset
            | ErrorKind::ConnectionAborted
            | ErrorKind::NotConnected
            | ErrorKind::UnexpectedEof
    )
}

/// A response being read from `stream` into `buf`, all of it by `deadline`, so a server sending its reply a byte at
/// a time cannot hold the link.
struct Reply<'a> {
    stream: &'a mut UnixStream,
    buf: &'a mut Vec<u8>,
    deadline: Instant,
}

impl Reply<'_> {
    /// Reads at least one more byte; end of file is `UnexpectedEof`, and a read once the deadline has passed is
    /// `TimedOut`.
    fn more(&mut self) -> Result<(), Fail> {
        if self.buf.len() > HEADER_MAX + BODY_MAX {
            return Err(Fail::Bad("an oversized response"));
        }
        if Instant::now() >= self.deadline {
            return Err(Fail::Io(ErrorKind::TimedOut.into()));
        }
        let len = self.buf.len();
        self.buf.resize(len + 16 * 1024, 0);
        let read = loop {
            match self.stream.read(&mut self.buf[len..]) {
                Err(e) if e.kind() == ErrorKind::Interrupted => {}
                read => break read,
            }
        };
        self.buf.truncate(len + read.as_ref().copied().unwrap_or(0));
        match read {
            Ok(0) => Err(Fail::Io(ErrorKind::UnexpectedEof.into())),
            Ok(_) => Ok(()),
            Err(e) => Err(Fail::Io(e)),
        }
    }

    /// The offset of the first `pat` at or after `buf[at..]`, reading more as needed; more than `max` bytes from
    /// `at` without one is `Bad(why)`.
    fn find(
        &mut self,
        at: usize,
        pat: &[u8],
        max: usize,
        why: &'static str,
    ) -> Result<usize, Fail> {
        let mut from = at;
        loop {
            if let Some(i) = self.buf[from..].windows(pat.len()).position(|w| w == pat) {
                return Ok(from + i);
            }
            if self.buf.len() - at > max {
                return Err(Fail::Bad(why));
            }
            from = self.buf.len().saturating_sub(pat.len() - 1).max(at);
            self.more()?;
        }
    }

    /// Decodes in place a chunked body that starts at `buf[start..]`, reading more as needed; returns the body's
    /// length, which then lies at `buf[start..]`.
    fn dechunk(&mut self, start: usize) -> Result<usize, Fail> {
        let bad = || Fail::Bad("a malformed chunked body");
        let mut at = start;
        let mut end = start;
        loop {
            let line = self.find(at, b"\r\n", CHUNK_LINE_MAX, "an overlong chunk line")?;
            let size_text = std::str::from_utf8(&self.buf[at..line]).map_err(|_| bad())?;
            let size_text = size_text.split(';').next().unwrap_or("").trim();
            let size = usize::from_str_radix(size_text, 16).map_err(|_| bad())?;
            at = line + 2;
            if size == 0 {
                // Trailers, if any, end with an empty line.
                loop {
                    let line = self.find(at, b"\r\n", CHUNK_LINE_MAX, "an overlong chunk line")?;
                    if line == at {
                        return Ok(end - start);
                    }
                    at = line + 2;
                }
            }
            if end - start + size > BODY_MAX {
                return Err(Fail::Bad("an oversized body"));
            }
            while self.buf.len() < at + size + 2 {
                self.more()?;
            }
            if &self.buf[at + size..at + size + 2] != b"\r\n" {
                return Err(bad());
            }
            self.buf.copy_within(at..at + size, end);
            end += size;
            at += size + 2;
        }
    }
}

fn parse_head(head: &[u8]) -> Result<Head, Fail> {
    let bad = || Fail::Bad("malformed HTTP");
    let text = std::str::from_utf8(head).map_err(|_| bad())?;
    let mut lines = text.split("\r\n");
    let mut status_line = lines.next().ok_or_else(bad)?.split(' ');
    let version = status_line.next().ok_or_else(bad)?;
    if !version.starts_with("HTTP/1.") {
        return Err(bad());
    }
    let status = status_line.next().unwrap_or_default();
    let mut parsed = Head {
        status: status.parse().map_err(|_| bad())?,
        length: None,
        chunked: false,
        close: version == "HTTP/1.0",
    };
    for (name, value) in lines.filter_map(|line| line.split_once(':')) {
        let name = name.trim();
        let mut tokens = value.split(',').map(str::trim);
        if name.eq_ignore_ascii_case("content-length") {
            parsed.length = Some(value.trim().parse().map_err(|_| bad())?);
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            parsed.chunked = tokens.any(|token| token.eq_ignore_ascii_case("chunked"));
        } else if name.eq_ignore_ascii_case("connection") {
            for token in tokens {
                if token.eq_ignore_ascii_case("close") {
                    parsed.close = true;
                } else if token.eq_ignore_ascii_case("keep-alive") {
                    parsed.close = false;
                }
            }
        }
    }
    Ok(parsed)
}

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/// Standard base64 encoding of `bytes`, appended to `out`.
pub fn base64_encode(bytes: &[u8], out: &mut Vec<u8>) {
    out.reserve(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk.iter().fold(0, |n, &byte| n << 8 | u32::from(byte));
        // A chunk of k bytes, at the top of 24 bits, fills k + 1 characters; `=` pads the rest.
        let n = n << (8 * (3 - chunk.len()));
        for i in 0..=chunk.len() {
            out.push(ALPHABET[(n >> (18 - 6 * i)) as usize & 63]);
        }
        out.resize(out.len() + 3 - chunk.len(), b'=');
    }
}

/// The six bits a base64 character stands for.
fn sextet(c: u8) -> Option<u32> {
    let v = match c {
        b'A'..=b'Z' => c - b'A',
        b'a'..=b'z' => c - b'a' + 26,
        b'0'..=b'9' => c - b'0' + 52,
        b'+' => 62,
        b'/' => 63,
        _ => return None,
    };
    Some(u32::from(v))
}

/// Standard base64 decoding of `text`, appended to `out`; false on malformed input. Padding may be left off.
pub fn base64_decode(text: &[u8], out: &mut Vec<u8>) -> bool {
    let pad = text.iter().rev().take_while(|&&c| c == b'=').count().min(2);
    let data = &text[..text.len() - pad];
    if (pad > 0 && !text.len().is_multiple_of(4)) || data.len() % 4 == 1 {
        return false;
    }
    out.reserve(data.len() / 4 * 3 + 2);
    for group in data.chunks(4) {
        let Some(n) = group.iter().try_fold(0, |n, &c| Some(n << 6 | sextet(c)?)) else {
            return false;
        };
        // A group of k characters holds k - 1 bytes, at the top of its 24 bits.
        let n: u32 = n << (6 * (4 - group.len()));
        out.extend_from_slice(&n.to_be_bytes()[1..group.len()]);
    }
    true
}

/// A terminal the executor drives: the real `Term`, or a scripted one in tests.
pub trait Terminal {
    /// Reads any pending output without sending input.
    fn poll(&mut self) -> Result<(), LinkError>;
    /// Sends `keys` as one batch and returns once the game waits for a key, re-reading while it stays busy; a
    /// game still busy after `BUSY_READS` re-reads is `LinkError::Busy`.
    fn send(&mut self, keys: &[u8]) -> Result<(), LinkError>;
    fn view(&self) -> &View;
    /// Whether the game wrote to the message rows of the current view since the last `send`.
    fn message_written(&self) -> bool;
    fn prompts(&self) -> &PromptContext;
}

/// The real terminal: a `Link`, the emulator it feeds and the parsed view of its screen.
pub struct Term {
    link: Link,
    vt: Vt,
    prompts: PromptContext,
    view: View,
    deadline: Option<Instant>,
}

impl Term {
    /// A terminal over the socket at `path`; nothing is read until `sync`.
    pub fn new(path: &Path) -> Term {
        let vt = Vt::new();
        let view = View::parse(vt.grid(), vt.cursor());
        Term {
            link: Link::new(path),
            vt,
            prompts: PromptContext::default(),
            view,
            deadline: None,
        }
    }

    /// Sets the time by which the current command must stop sending: past it, `send` refuses with
    /// `LinkError::Busy`, so a command against a slow game ends while its client still waits.
    pub fn deadline(&mut self, at: Instant) {
        self.deadline = Some(at);
    }

    /// Reads the screen once the game waits for a key, so a game still starting up has drawn its first screen, and,
    /// if the emulator has never seen a full clear, asks the game to redraw (`^R`).
    pub fn sync(&mut self) -> Result<(), LinkError> {
        self.step(None, WAIT_MS)?;
        if !self.vt.complete() {
            self.send(b"\x12")?;
        }
        if !self.vt.complete() {
            return Err(LinkError::Protocol(
                "the terminal has not supplied a complete screen".to_owned(),
            ));
        }
        Ok(())
    }

    /// Whether the emulator has seen a full clear since its last reset, so every cell is current.
    pub fn complete(&self) -> bool {
        self.vt.complete()
    }

    /// Sends `keys`, or none for a pure read, and applies the reply: resets the screen if the output does not
    /// continue, notes `keys` against the screen they were typed at, feeds the output and re-parses the view.
    /// Returns whether the game waits for a key.
    fn step(&mut self, keys: Option<&[u8]>, wait: u32) -> Result<bool, LinkError> {
        let exchange = self.link.exchange(keys.unwrap_or_default(), wait)?;
        if exchange.reset {
            self.vt.reset();
            self.prompts.reset();
        }
        if let Some(keys) = keys {
            self.prompts.before_input(&self.view, keys);
        }
        if exchange.reset || keys.is_some() || !self.link.output().is_empty() {
            self.vt.feed(self.link.output());
            self.view.reparse(self.vt.grid(), self.vt.cursor());
            self.prompts.observe(&mut self.view);
        }
        Ok(exchange.waiting)
    }
}

impl Terminal for Term {
    fn poll(&mut self) -> Result<(), LinkError> {
        self.step(None, 0)?;
        Ok(())
    }

    fn send(&mut self, keys: &[u8]) -> Result<(), LinkError> {
        self.vt.clear_touched();
        for read in 0..=BUSY_READS {
            if self.deadline.is_some_and(|at| Instant::now() >= at) {
                return Err(LinkError::Busy(
                    "the command ran out of time; the game may still be busy".to_owned(),
                ));
            }
            if self.step((read == 0).then_some(keys), WAIT_MS)? {
                return Ok(());
            }
        }
        Err(LinkError::Busy(format!(
            "the game is still busy after {} reads; later keys were not sent",
            BUSY_READS + 1
        )))
    }

    fn view(&self) -> &View {
        &self.view
    }

    fn message_written(&self) -> bool {
        self.vt.message_written(usize::from(self.view.message_rows))
    }

    fn prompts(&self) -> &PromptContext {
        &self.prompts
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn encode(bytes: &[u8]) -> String {
        let mut out = Vec::new();
        base64_encode(bytes, &mut out);
        String::from_utf8(out).unwrap()
    }

    fn decode(text: &str) -> Option<Vec<u8>> {
        let mut out = Vec::new();
        base64_decode(text.as_bytes(), &mut out).then_some(out)
    }

    #[test]
    fn base64_matches_the_standard_vectors() {
        for (plain, coded) in [
            ("", ""),
            ("f", "Zg=="),
            ("fo", "Zm8="),
            ("foo", "Zm9v"),
            ("foob", "Zm9vYg=="),
            ("fooba", "Zm9vYmE="),
            ("foobar", "Zm9vYmFy"),
        ] {
            assert_eq!(encode(plain.as_bytes()), coded);
            assert_eq!(decode(coded).unwrap(), plain.as_bytes());
        }
        assert_eq!(encode(&[0xfb, 0xff, 0xf0]), "+//w");
        assert_eq!(decode("Zm8").unwrap(), b"fo");
    }

    #[test]
    fn base64_round_trips_every_byte() {
        let bytes: Vec<u8> = (0..=255u8).chain((0..=255u8).rev()).collect();
        for len in 0..bytes.len() {
            assert_eq!(decode(&encode(&bytes[..len])).unwrap(), &bytes[..len]);
        }
    }

    #[test]
    fn base64_rejects_malformed_input() {
        for bad in ["Z", "Zm9v!", "Zg=", "Zg===", "Z===", "Zm9v\n"] {
            assert!(decode(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn response_heads_parse_framing() {
        let head = parse_head(
            b"HTTP/1.1 410 Gone\r\nContent-Type: text/plain\r\ncontent-length: 18\r\n\r\n",
        )
        .ok()
        .unwrap();
        assert_eq!(
            (head.status, head.length, head.chunked, head.close),
            (410, Some(18), false, false)
        );
        let head = parse_head(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
        )
        .ok()
        .unwrap();
        assert!(head.chunked && head.close && head.length.is_none());
        assert!(parse_head(b"HTTP/1.0 200 OK\r\n\r\n").ok().unwrap().close);
        assert!(parse_head(b"SPDY 200\r\n\r\n").is_err());
        assert!(parse_head(b"HTTP/1.1 abc\r\n\r\n").is_err());
    }

    #[test]
    fn errors_say_what_failed() {
        assert_eq!(
            LinkError::Held("waiting to start".to_owned()).to_string(),
            "the game has not started yet: waiting to start"
        );
        assert_eq!(
            LinkError::Closed("time is up".to_owned()).to_string(),
            "the terminal is closed: time is up"
        );
    }

    #[test]
    fn errors_map_to_exit_codes() {
        let code = |e: LinkError| e.code();
        assert_eq!(code(LinkError::Held(String::new())), ExitCode::Held);
        assert_eq!(code(LinkError::Closed(String::new())), ExitCode::Closed);
        assert_eq!(
            code(LinkError::Unavailable(String::new())),
            ExitCode::Closed
        );
        assert_eq!(code(LinkError::Protocol(String::new())), ExitCode::Failed);
        assert_eq!(code(LinkError::Busy(String::new())), ExitCode::Short);
    }
}
