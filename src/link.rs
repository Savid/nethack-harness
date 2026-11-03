//! The terminal link: one persistent HTTP/1.1 keep-alive connection over a Unix socket to `POST /terminal`, and
//! the screen kept in step with it.
//!
//! Request: `{"input": base64, "after": cursor, "wait": ms}`. Reply 200: `{"output": base64, "cursor": n,
//! "truncated": bool, "waiting": bool}`; `waiting` is true when the game is blocked reading a key, has consumed
//! all input sent so far, and all its output is in `output`. With `wait` > 0 and input written, the server
//! replies as soon as `waiting` is true, or at the deadline. 503 is busy; 410 `waiting to start` means input is
//! held; any other 410 means the terminal is closed.

use std::borrow::Cow;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::Deserialize;

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

/// Largest response body accepted: the server keeps at most 64 KiB of output, which base64 and JSON enlarge.
const BODY_MAX: usize = 512 * 1024;
/// Largest response header block accepted.
const HEADER_MAX: usize = 8192;
/// A connection idle this long is replaced before use, because the server closes idle connections after 30 s.
const IDLE: Duration = Duration::from_secs(20);
/// How much longer than the requested wait a reply may take before the server counts as unresponsive.
const READ_MARGIN_MS: u64 = 5000;
/// Longest a request write may block.
const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
/// Most bytes of an error body quoted in a message.
const QUOTE_MAX: usize = 200;
/// Base64 alphabet.
const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

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
        match self {
            LinkError::Held(why) => write!(f, "the game has not started yet: {why}"),
            LinkError::Closed(why) => write!(f, "the terminal is closed: {why}"),
            LinkError::Unavailable(why) | LinkError::Protocol(why) => f.write_str(why),
        }
    }
}

impl std::error::Error for LinkError {}

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
    body: Vec<u8>,
    used: Option<Instant>,
    read_timeout_ms: u64,
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
    /// The connection was closed before any byte of a response arrived.
    Stale,
    /// The socket failed.
    Io(std::io::Error),
    /// The response is not HTTP this client understands.
    Bad(String),
}

impl From<std::io::Error> for Fail {
    fn from(e: std::io::Error) -> Fail {
        Fail::Io(e)
    }
}

/// The parts of a response head the client acts on.
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
            answered: false,
            request: Vec::with_capacity(1024),
            response: Vec::with_capacity(16 * 1024),
            output: Vec::with_capacity(16 * 1024),
            body: Vec::with_capacity(16 * 1024),
            used: None,
            read_timeout_ms: 0,
        }
    }

    /// Sends `input` (possibly empty, a pure read) and returns what the server answered. With `wait_ms` > 0 and
    /// input given, the server holds the reply until the game waits for a key or the deadline passes. A 503 is
    /// retried `BUSY_RETRIES` times.
    pub fn exchange(&mut self, input: &[u8], wait_ms: u32) -> Result<Exchange, LinkError> {
        if input.len() > INPUT_MAX {
            return Err(LinkError::Protocol(format!(
                "at most {INPUT_MAX} input bytes go in one request"
            )));
        }
        let mut input = input;
        let mut wait = wait_ms.min(WAIT_MS);
        let mut busy = 0;
        let mut reset = false;
        loop {
            let status = self.post(input, wait)?;
            match status {
                200 => {
                    let (cursor, truncated, waiting) = self.decode()?;
                    if cursor < self.cursor {
                        // The server started over and clamped `after` to its new cursor, so this reply holds
                        // none of its output: read everything it has from the start.
                        self.cursor = 0;
                        reset = true;
                        input = &[];
                        wait = 0;
                        continue;
                    }
                    self.cursor = cursor;
                    return Ok(Exchange {
                        reset: reset || truncated,
                        waiting,
                    });
                }
                503 if busy < BUSY_RETRIES => {
                    busy += 1;
                    std::thread::sleep(Duration::from_millis(BUSY_PAUSE_MS));
                }
                410 => {
                    let text = self.quote();
                    return Err(if text.contains("waiting") {
                        LinkError::Held(text)
                    } else {
                        LinkError::Closed(text)
                    });
                }
                _ => {
                    return Err(LinkError::Protocol(format!(
                        "the terminal socket answered HTTP {status}: {}",
                        self.quote()
                    )));
                }
            }
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
        let text = String::from_utf8_lossy(&self.body);
        let text = text.trim();
        match text.char_indices().nth(QUOTE_MAX) {
            Some((at, _)) => text[..at].to_owned(),
            None => text.to_owned(),
        }
    }

    /// Decodes a 200 body into `output`; returns the cursor, `truncated` and `waiting`.
    fn decode(&mut self) -> Result<(u64, bool, bool), LinkError> {
        let answer: Answer = serde_json::from_slice(&self.body).map_err(|e| {
            LinkError::Protocol(format!("the terminal socket answered malformed JSON: {e}"))
        })?;
        self.output.clear();
        if let Some(text) = &answer.output
            && !base64_decode(text.as_bytes(), &mut self.output)
        {
            self.output.clear();
            return Err(LinkError::Protocol(
                "the terminal socket answered malformed base64 output".to_owned(),
            ));
        }
        Ok((answer.cursor, answer.truncated, answer.waiting))
    }

    /// Posts one request and returns the response status, with its body in `body`. A reused connection that the
    /// server closed is replaced once; one idle long enough for the server to close it is replaced before use.
    fn post(&mut self, input: &[u8], wait: u32) -> Result<u16, LinkError> {
        self.body.clear();
        self.body.extend_from_slice(b"{\"input\":\"");
        base64_encode(input, &mut self.body);
        // Writes into a Vec cannot fail.
        let _ = write!(self.body, "\",\"after\":{},\"wait\":{wait}}}", self.cursor);
        self.request.clear();
        let _ = write!(
            self.request,
            "POST /terminal HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n\
             Content-Length: {}\r\n\r\n",
            self.body.len()
        );
        self.request.extend_from_slice(&self.body);
        if self.used.is_some_and(|at| at.elapsed() >= IDLE) {
            self.stream = None;
        }
        let read_timeout_ms = u64::from(wait) + READ_MARGIN_MS;
        let mut retried = false;
        loop {
            let reused = self.stream.is_some();
            let mut stream = match self.stream.take() {
                Some(stream) => stream,
                None => self.connect()?,
            };
            if self.read_timeout_ms != read_timeout_ms {
                stream
                    .set_read_timeout(Some(Duration::from_millis(read_timeout_ms)))
                    .map_err(|e| self.gone(&e))?;
                self.read_timeout_ms = read_timeout_ms;
            }
            match self.round_trip(&mut stream) {
                Ok(head) => {
                    self.answered = true;
                    self.used = Some(Instant::now());
                    if !head.close {
                        self.stream = Some(stream);
                    }
                    return Ok(head.status);
                }
                Err(Fail::Stale) if reused && !retried => retried = true,
                Err(Fail::Stale) => {
                    return Err(self.gone(&"the connection closed without an answer"));
                }
                Err(Fail::Io(e))
                    if matches!(e.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) =>
                {
                    return Err(LinkError::Protocol(format!(
                        "the terminal socket did not answer within {read_timeout_ms} ms"
                    )));
                }
                Err(Fail::Io(e)) => return Err(self.gone(&e)),
                Err(Fail::Bad(why)) => return Err(LinkError::Protocol(why)),
            }
        }
    }

    /// A new connection.
    fn connect(&mut self) -> Result<UnixStream, LinkError> {
        let stream = UnixStream::connect(&self.path).map_err(|e| self.gone(&e))?;
        stream
            .set_write_timeout(Some(WRITE_TIMEOUT))
            .map_err(|e| self.gone(&e))?;
        self.read_timeout_ms = 0;
        Ok(stream)
    }

    /// The error for a socket that failed: closed if it answered before, otherwise unreachable.
    fn gone(&self, why: &dyn std::fmt::Display) -> LinkError {
        if self.answered {
            LinkError::Closed(format!("the terminal socket went away ({why})"))
        } else {
            LinkError::Unavailable(format!(
                "cannot reach the terminal socket {}: {why}",
                self.path.display()
            ))
        }
    }

    /// Writes the request and reads one response into `response`, its body into `body`.
    fn round_trip(&mut self, stream: &mut UnixStream) -> Result<Head, Fail> {
        if let Err(e) = stream.write_all(&self.request) {
            return Err(if dropped(&e) {
                Fail::Stale
            } else {
                Fail::Io(e)
            });
        }
        self.response.clear();
        self.body.clear();
        let mut from = 0;
        let end = loop {
            if let Some(at) = find(&self.response[from..], b"\r\n\r\n") {
                break from + at + 4;
            }
            if self.response.len() > HEADER_MAX {
                return Err(Fail::Bad(
                    "the terminal socket answered oversized headers".to_owned(),
                ));
            }
            from = self.response.len().saturating_sub(3);
            match more(stream, &mut self.response) {
                Err(Fail::Io(e)) if self.response.is_empty() && dropped(&e) => {
                    return Err(Fail::Stale);
                }
                other => other?,
            }
        };
        let mut head = parse_head(&self.response[..end])?;
        if head.chunked {
            dechunk(stream, &mut self.response, end, &mut self.body)?;
        } else if let Some(length) = head.length {
            if length > BODY_MAX {
                return Err(Fail::Bad(
                    "the terminal socket answered an oversized body".to_owned(),
                ));
            }
            while self.response.len() < end + length {
                more(stream, &mut self.response)?;
            }
            self.body
                .extend_from_slice(&self.response[end..end + length]);
        } else {
            head.close = true;
            loop {
                match more(stream, &mut self.response) {
                    Ok(()) => {}
                    Err(Fail::Io(e)) if e.kind() == ErrorKind::UnexpectedEof => break,
                    Err(other) => return Err(other),
                }
            }
            self.body.extend_from_slice(&self.response[end..]);
        }
        Ok(head)
    }
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

/// The offset of `needle` in `hay`.
fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// Reads at least one more byte into `buf`; EOF is `UnexpectedEof`.
fn more(stream: &mut UnixStream, buf: &mut Vec<u8>) -> Result<(), Fail> {
    if buf.len() > HEADER_MAX + BODY_MAX {
        return Err(Fail::Bad(
            "the terminal socket answered an oversized response".to_owned(),
        ));
    }
    let mut chunk = [0u8; 16 * 1024];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => return Err(Fail::Io(ErrorKind::UnexpectedEof.into())),
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                return Ok(());
            }
            Err(e) if e.kind() == ErrorKind::Interrupted => {}
            Err(e) => return Err(Fail::Io(e)),
        }
    }
}

/// Parses the status line and the headers that frame the body.
fn parse_head(head: &[u8]) -> Result<Head, Fail> {
    let bad = || Fail::Bad("the terminal socket answered malformed HTTP".to_owned());
    let text = std::str::from_utf8(head).map_err(|_| bad())?;
    let mut lines = text.split("\r\n");
    let mut status_line = lines.next().ok_or_else(bad)?.split(' ');
    let version = status_line.next().ok_or_else(bad)?;
    if !version.starts_with("HTTP/1.") {
        return Err(bad());
    }
    let status = status_line
        .next()
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(bad)?;
    let mut parsed = Head {
        status,
        length: None,
        chunked: false,
        close: version == "HTTP/1.0",
    };
    for line in lines {
        let Some((name, value)) = line.split_once(':') else {
            continue;
        };
        let name = name.trim();
        let value = value.trim();
        if name.eq_ignore_ascii_case("content-length") {
            parsed.length = Some(value.parse().map_err(|_| bad())?);
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            parsed.chunked = value
                .split(',')
                .any(|v| v.trim().eq_ignore_ascii_case("chunked"));
        } else if name.eq_ignore_ascii_case("connection") {
            for token in value.split(',') {
                let token = token.trim();
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

/// Decodes a chunked body that starts at `buf[at..]`, reading more as needed, into `body`.
fn dechunk(
    stream: &mut UnixStream,
    buf: &mut Vec<u8>,
    mut at: usize,
    body: &mut Vec<u8>,
) -> Result<(), Fail> {
    let bad = || Fail::Bad("the terminal socket answered a malformed chunked body".to_owned());
    loop {
        let line = line_end(stream, buf, at)?;
        let size_text = std::str::from_utf8(&buf[at..line]).map_err(|_| bad())?;
        let size_text = size_text.split(';').next().unwrap_or("").trim();
        let size = usize::from_str_radix(size_text, 16).map_err(|_| bad())?;
        at = line + 2;
        if size == 0 {
            // Trailers, if any, end with an empty line.
            loop {
                let line = line_end(stream, buf, at)?;
                let empty = line == at;
                at = line + 2;
                if empty {
                    return Ok(());
                }
            }
        }
        if body.len() + size > BODY_MAX {
            return Err(Fail::Bad(
                "the terminal socket answered an oversized body".to_owned(),
            ));
        }
        while buf.len() < at + size + 2 {
            more(stream, buf)?;
        }
        body.extend_from_slice(&buf[at..at + size]);
        if &buf[at + size..at + size + 2] != b"\r\n" {
            return Err(bad());
        }
        at += size + 2;
    }
}

/// The offset of the CRLF ending the line that starts at `buf[at..]`, reading more as needed.
fn line_end(stream: &mut UnixStream, buf: &mut Vec<u8>, at: usize) -> Result<usize, Fail> {
    let mut from = at;
    loop {
        if let Some(end) = find(&buf[from..], b"\r\n") {
            return Ok(from + end);
        }
        if buf.len() - at > 1024 {
            return Err(Fail::Bad(
                "the terminal socket answered an overlong chunk line".to_owned(),
            ));
        }
        from = buf.len().saturating_sub(1).max(at);
        more(stream, buf)?;
    }
}

/// Standard base64 encoding of `bytes`, appended to `out`.
pub fn base64_encode(bytes: &[u8], out: &mut Vec<u8>) {
    out.reserve(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = chunk.get(1).copied().map_or(0, u32::from);
        let b2 = chunk.get(2).copied().map_or(0, u32::from);
        let n = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[(n >> 18) as usize & 63]);
        out.push(ALPHABET[(n >> 12) as usize & 63]);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63]
        } else {
            b'='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63]
        } else {
            b'='
        });
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
    let pad = text
        .iter()
        .rev()
        .take(2)
        .take_while(|&&c| c == b'=')
        .count();
    if pad > 0 && !text.len().is_multiple_of(4) {
        return false;
    }
    let data = &text[..text.len() - pad];
    if data.len() % 4 == 1 {
        return false;
    }
    out.reserve(data.len() / 4 * 3 + 2);
    for group in data.chunks(4) {
        let mut n = 0u32;
        for &c in group {
            let Some(v) = sextet(c) else {
                return false;
            };
            n = (n << 6) | v;
        }
        n <<= 6 * (4 - group.len() as u32);
        out.push((n >> 16) as u8);
        if group.len() > 2 {
            out.push((n >> 8) as u8);
        }
        if group.len() > 3 {
            out.push(n as u8);
        }
    }
    true
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
        let vt = Vt::new();
        let view = View::parse(vt.grid(), vt.cursor());
        Term {
            link: Link::new(path),
            vt,
            prompts: PromptContext::new(),
            view,
        }
    }

    /// Reads the screen and, if the emulator has never seen a full clear, asks the game to redraw (`^R`).
    pub fn sync(&mut self) -> Result<(), LinkError> {
        self.poll()?;
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

    /// Applies an exchange: resets the screen if the output does not continue, notes `keys` against the screen
    /// they were typed at, feeds the output and re-parses the view.
    fn apply(&mut self, exchange: Exchange, keys: Option<&[u8]>) {
        if exchange.reset {
            self.vt.reset();
            self.prompts.reset();
        }
        if let Some(keys) = keys {
            self.prompts.before_input(&self.view, keys);
        }
        if self.link.output().is_empty() && !exchange.reset && keys.is_none() {
            return;
        }
        self.vt.feed(self.link.output());
        self.view.reparse(self.vt.grid(), self.vt.cursor());
        self.prompts.observe(&mut self.view);
    }
}

impl Terminal for Term {
    fn poll(&mut self) -> Result<(), LinkError> {
        let exchange = self.link.exchange(&[], 0)?;
        self.apply(exchange, None);
        Ok(())
    }

    fn send(&mut self, keys: &[u8]) -> Result<(), LinkError> {
        self.vt.clear_touched();
        let mut exchange = self.link.exchange(keys, WAIT_MS)?;
        self.apply(exchange, Some(keys));
        let mut reads = 0;
        while !exchange.waiting && reads < BUSY_READS {
            exchange = self.link.exchange(&[], WAIT_MS)?;
            self.apply(exchange, None);
            reads += 1;
        }
        Ok(())
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
}
