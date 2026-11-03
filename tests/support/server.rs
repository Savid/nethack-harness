//! A fake terminal server: `POST /terminal` over a Unix socket, HTTP/1.1 keep-alive, replaying scripted output and
//! implementing `wait` and `waiting`.
//!
//! Each request with non-empty input takes the next script step. Output steps append their bytes to the output
//! buffer and advance the cursor; the reply carries the bytes after the request's `after`, `truncated` when
//! `after` is older than the buffer start, and `waiting` as the step says. A request with empty input is a pure
//! read and never takes a step. A request with input while the script is exhausted goes to the responder, if one is
//! set, which returns what the game prints; without one it is answered like a game that printed nothing and waits. Like the real server, the buffer keeps the last 64 KiB, and a reply body over
//! 2048 bytes is sent chunked.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::JoinHandle;

use nethack_harness::link::{base64_decode, base64_encode};
use serde::Deserialize;

/// Output bytes the server keeps.
const RING: usize = 64 * 1024;
/// Reply bodies longer than this are sent chunked.
const CHUNK_OVER: usize = 2048;

/// What the fake game does with the next input.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Step {
    /// Prints these bytes and waits for a key: the reply says `waiting: true`.
    Output(Vec<u8>),
    /// Prints these bytes but is still busy when the reply goes out, as at the request's `wait` deadline: the reply
    /// says `waiting: false`; the game settles while the next read with `wait` is held, which reports
    /// `waiting: true` with no new output.
    Busy(Vec<u8>),
    /// Prints these bytes and never settles, like a frozen game: this and every later reply comes at once with
    /// `waiting: false`.
    Stuck(Vec<u8>),
    /// Refuses the input with this HTTP status and body, such as 410 `waiting to start`, 410 `time is up` or 503
    /// `terminal busy`; the input is not consumed.
    Refuse(u16, String),
    /// Drops the buffer: the next reply's cursor goes back to zero with the given output.
    Restart(Vec<u8>),
    /// Takes this many milliseconds, then prints these bytes and waits for a key.
    Slow(u64, Vec<u8>),
}

/// A game that answers each input the script leaves to it with the bytes it prints.
pub type Responder = Box<dyn FnMut(&[u8]) -> Vec<u8> + Send>;

/// One request the server received.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Received {
    /// Decoded input bytes.
    pub input: Vec<u8>,
    /// The `after` cursor.
    pub after: u64,
    /// The `wait` milliseconds.
    pub wait: u32,
    /// The HTTP status it was answered with.
    pub status: u16,
}

/// The game's side of the server.
#[derive(Default)]
struct Game {
    buf: Vec<u8>,
    start: u64,
    script: VecDeque<Step>,
    busy: bool,
    stuck: bool,
    received: Vec<Received>,
    connections: u32,
    streams: Vec<UnixStream>,
    responder: Option<Responder>,
}

impl Game {
    fn cursor(&self) -> u64 {
        self.start + self.buf.len() as u64
    }

    fn print(&mut self, bytes: &[u8]) {
        self.buf.extend_from_slice(bytes);
        if self.buf.len() > RING {
            let excess = self.buf.len() - RING;
            self.buf.drain(..excess);
            self.start += excess as u64;
        }
    }
}

/// A request body; unknown fields are refused, as the real server does.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Body {
    input: String,
    after: u64,
    #[serde(default)]
    wait: u32,
}

/// A running fake server; dropping it stops the server and removes the socket.
pub struct FakeTerminalServer {
    path: PathBuf,
    game: Arc<Mutex<Game>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

fn lock(game: &Mutex<Game>) -> MutexGuard<'_, Game> {
    game.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

impl FakeTerminalServer {
    /// Starts a server on `<dir>/term.sock` whose screen already holds `initial` (cursor at its length), then
    /// plays `script`.
    pub fn start(dir: &Path, initial: &[u8], script: Vec<Step>) -> FakeTerminalServer {
        let path = dir.join("term.sock");
        let _ = std::fs::remove_file(&path);
        let listener = UnixListener::bind(&path).expect("bind the fake terminal socket");
        let mut game = Game {
            script: script.into(),
            ..Game::default()
        };
        game.print(initial);
        let game = Arc::new(Mutex::new(game));
        let stop = Arc::new(AtomicBool::new(false));
        let thread = {
            let game = Arc::clone(&game);
            let stop = Arc::clone(&stop);
            std::thread::spawn(move || {
                for stream in listener.incoming() {
                    if stop.load(Ordering::SeqCst) {
                        break;
                    }
                    let Ok(stream) = stream else { continue };
                    {
                        let mut g = lock(&game);
                        g.connections += 1;
                        if let Ok(clone) = stream.try_clone() {
                            g.streams.push(clone);
                        }
                    }
                    let game = Arc::clone(&game);
                    std::thread::spawn(move || serve(stream, &game));
                }
            })
        };
        FakeTerminalServer {
            path,
            game,
            stop,
            thread: Some(thread),
        }
    }

    /// The socket path.
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Every request received so far.
    pub fn received(&self) -> Vec<Received> {
        lock(&self.game).received.clone()
    }

    /// The non-empty inputs the game accepted so far, in order.
    pub fn inputs(&self) -> Vec<Vec<u8>> {
        self.received()
            .into_iter()
            .filter(|r| !r.input.is_empty() && r.status == 200)
            .map(|r| r.input)
            .collect()
    }

    /// How many connections clients opened.
    pub fn connections(&self) -> u32 {
        lock(&self.game).connections
    }

    /// Hands every input the script does not take to `responder`.
    pub fn respond(&self, responder: Responder) {
        lock(&self.game).responder = Some(responder);
    }

    /// Appends steps to the script.
    pub fn push(&self, steps: impl IntoIterator<Item = Step>) {
        lock(&self.game).script.extend(steps);
    }

    /// Closes every open client connection, as a server dropping idle connections does.
    pub fn drop_connections(&self) {
        for stream in lock(&self.game).streams.drain(..) {
            let _ = stream.shutdown(std::net::Shutdown::Both);
        }
    }
}

impl Drop for FakeTerminalServer {
    /// Stops the server thread and removes the socket.
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        let _ = UnixStream::connect(&self.path);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        self.drop_connections();
        let _ = std::fs::remove_file(&self.path);
    }
}

/// Serves requests on one connection until the client closes it.
fn serve(stream: UnixStream, game: &Mutex<Game>) {
    let Ok(write) = stream.try_clone() else {
        return;
    };
    let mut reader = BufReader::new(stream);
    let mut writer = write;
    let mut line = String::new();
    loop {
        line.clear();
        if reader.read_line(&mut line).unwrap_or(0) == 0 {
            return;
        }
        let request_line = line.trim_end().to_owned();
        let mut length = 0usize;
        loop {
            line.clear();
            if reader.read_line(&mut line).unwrap_or(0) == 0 {
                return;
            }
            let header = line.trim_end();
            if header.is_empty() {
                break;
            }
            if let Some((name, value)) = header.split_once(':')
                && name.trim().eq_ignore_ascii_case("content-length")
            {
                length = value.trim().parse().unwrap_or(0);
            }
        }
        let mut body = vec![0u8; length];
        if reader.read_exact(&mut body).is_err() {
            return;
        }
        let (status, reply) = if request_line.starts_with("POST /terminal ") {
            answer(&body, game)
        } else {
            (404, "not found\n".to_owned())
        };
        if respond(&mut writer, status, &reply).is_err() {
            return;
        }
    }
}

/// Answers one request body.
fn answer(body: &[u8], game: &Mutex<Game>) -> (u16, String) {
    let mut input = Vec::new();
    let Ok(request) = serde_json::from_slice::<Body>(body) else {
        return (400, "invalid terminal request\n".to_owned());
    };
    if !base64_decode(request.input.as_bytes(), &mut input) || input.len() > 4096 {
        return (400, "invalid terminal request\n".to_owned());
    }
    let mut g = lock(game);
    let mut received = Received {
        input,
        after: request.after,
        wait: request.wait,
        status: 200,
    };
    if received.input.is_empty() {
        if request.wait > 0 {
            g.busy = false;
        }
    } else {
        match g.script.pop_front() {
            Some(Step::Refuse(status, text)) => {
                received.status = status;
                g.received.push(received);
                return (status, format!("{text}\n"));
            }
            Some(Step::Output(bytes)) => {
                g.print(&bytes);
                g.busy = false;
            }
            Some(Step::Busy(bytes)) => {
                g.print(&bytes);
                g.busy = true;
            }
            Some(Step::Stuck(bytes)) => {
                g.print(&bytes);
                g.stuck = true;
            }
            Some(Step::Slow(ms, bytes)) => {
                drop(g);
                std::thread::sleep(std::time::Duration::from_millis(ms));
                g = lock(game);
                g.print(&bytes);
                g.busy = false;
            }
            Some(Step::Restart(bytes)) => {
                g.buf.clear();
                g.start = 0;
                g.print(&bytes);
                g.busy = false;
            }
            None => {
                let printed = g.responder.as_mut().map(|respond| respond(&received.input));
                if let Some(bytes) = printed {
                    g.print(&bytes);
                }
                g.busy = false;
            }
        }
    }
    g.received.push(received);
    let cursor = g.cursor();
    let from = request.after.clamp(g.start, cursor);
    let mut output = Vec::new();
    base64_encode(&g.buf[(from - g.start) as usize..], &mut output);
    let reply = format!(
        "{{\"output\":\"{}\",\"cursor\":{cursor},\"truncated\":{},\"waiting\":{}}}\n",
        String::from_utf8_lossy(&output),
        request.after < g.start,
        !g.busy && !g.stuck
    );
    (200, reply)
}

/// Writes one response, chunked when the body is long.
fn respond(writer: &mut UnixStream, status: u16, body: &str) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        410 => "Gone",
        503 => "Service Unavailable",
        _ => "Status",
    };
    let kind = if status == 200 {
        "application/json"
    } else {
        "text/plain; charset=utf-8"
    };
    let mut out = format!("HTTP/1.1 {status} {reason}\r\nContent-Type: {kind}\r\n");
    if body.len() > CHUNK_OVER {
        out.push_str("Transfer-Encoding: chunked\r\n\r\n");
        for piece in body.as_bytes().chunks(1500) {
            out.push_str(&format!("{:x}\r\n", piece.len()));
            out.push_str(&String::from_utf8_lossy(piece));
            out.push_str("\r\n");
        }
        out.push_str("0\r\n\r\n");
    } else {
        out.push_str(&format!("Content-Length: {}\r\n\r\n{body}", body.len()));
    }
    writer.write_all(out.as_bytes())
}
