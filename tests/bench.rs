//! Measurements against the performance budget, printed rather than asserted because they depend on the machine.
//! Run with `cargo test --release --test bench -- --ignored --nocapture --test-threads 1`.

mod support;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use nethack_harness::link::{LinkError, Terminal};
use nethack_harness::play::Play;
use nethack_harness::screen::{PromptContext, View};
use nethack_harness::types::{COLS, Colour, ROWS};
use support::server::FakeTerminalServer;
use support::{Fixture, scratch};

const BIN: &str = env!("CARGO_BIN_EXE_nethack-harness");

/// A map with many remembered items, doors and two monsters.
const BUSY: &str = "seed4101-0120";
/// Requests sent to the daemon before its resident memory is read.
const LOOKS: usize = 1000;
/// Client invocations timed.
const CLIENTS: usize = 200;

/// The SGR parameter for a colour.
fn sgr(colour: Colour) -> u8 {
    match colour {
        Colour::Default => 39,
        Colour::Black => 30,
        Colour::Red => 31,
        Colour::Green => 32,
        Colour::Brown => 33,
        Colour::Blue => 34,
        Colour::Magenta => 35,
        Colour::Cyan => 36,
        Colour::White => 37,
        Colour::BrightBlack => 90,
        Colour::BrightRed => 91,
        Colour::BrightGreen => 92,
        Colour::BrightBrown => 93,
        Colour::BrightBlue => 94,
        Colour::BrightMagenta => 95,
        Colour::BrightCyan => 96,
        Colour::BrightWhite => 97,
    }
}

/// The terminal output that draws `fixture` on a cleared screen.
fn ansi(fixture: &Fixture) -> Vec<u8> {
    let grid = fixture.grid();
    let mut out = String::from("\x1b[H\x1b[2J");
    for r in 0..ROWS {
        out.push_str(&format!("\x1b[{};1H", r + 1));
        for c in 0..COLS {
            let cell = grid.cells[r][c];
            out.push_str("\x1b[0");
            if cell.bold {
                out.push_str(";1");
            }
            if cell.rev {
                out.push_str(";7");
            }
            out.push_str(&format!(";{}m", sgr(cell.fg)));
            out.push(cell.ch);
        }
    }
    let cursor = fixture.cursor();
    out.push_str(&format!("\x1b[0m\x1b[{};{}H", cursor.y + 1, cursor.x + 1));
    out.into_bytes()
}

/// A terminal that shows one screen and takes keys without changing it.
struct StillTerm {
    view: View,
    prompts: PromptContext,
}

impl Terminal for StillTerm {
    fn poll(&mut self) -> Result<(), LinkError> {
        Ok(())
    }

    fn send(&mut self, _keys: &[u8]) -> Result<(), LinkError> {
        Ok(())
    }

    fn view(&self) -> &View {
        &self.view
    }

    fn message_written(&self) -> bool {
        false
    }

    fn prompts(&self) -> &PromptContext {
        &self.prompts
    }
}

/// Microseconds per call of `f`, as (median, 99th percentile).
fn time_us(runs: usize, mut f: impl FnMut()) -> (f64, f64) {
    let mut samples: Vec<f64> = (0..runs)
        .map(|_| {
            let start = Instant::now();
            f();
            start.elapsed().as_secs_f64() * 1e6
        })
        .collect();
    samples.sort_by(f64::total_cmp);
    (samples[runs / 2], samples[runs * 99 / 100])
}

/// Resident memory of process `pid` in KiB.
fn rss_kib(pid: u32) -> u64 {
    let output = Command::new("ps")
        .args(["-o", "rss=", "-p", &pid.to_string()])
        .output()
        .expect("run ps");
    String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .expect("an RSS figure")
}

/// One request over the socket API; returns the reply line.
fn ask(socket: &Path, line: &str) -> String {
    let mut stream = UnixStream::connect(socket).expect("connect to the daemon");
    stream.write_all(line.as_bytes()).expect("send the request");
    let mut reply = String::new();
    BufReader::new(&stream)
        .read_line(&mut reply)
        .expect("read the reply");
    reply
}

/// Starts `serve` for the terminal at `terminal` and waits for it to listen.
fn serve(dir: &Path, terminal: &Path) -> Child {
    let mut child = Command::new(BIN)
        .arg("--dir")
        .arg(dir)
        .arg("--socket")
        .arg(terminal)
        .arg("serve")
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .expect("start the daemon");
    let mut ready = String::new();
    BufReader::new(child.stdout.as_mut().expect("the daemon's output"))
        .read_line(&mut ready)
        .expect("read ready");
    assert_eq!(ready, "ready\n");
    child
}

#[test]
#[ignore = "measurement; run in release"]
fn look_in_process() {
    for name in [BUSY, "seed1001-start", "seed7-0240"] {
        let view = Fixture::load(name).view();
        let mut play = Play::new(
            StillTerm {
                view: view.clone(),
                prompts: PromptContext::default(),
            },
            64,
        );
        let (show, show_p99) = time_us(2000, || {
            play.show();
        });
        let (look, look_p99) = time_us(2000, || {
            play.look();
        });
        println!(
            "{name}: show {show:.1} us (p99 {show_p99:.1}), look with farlooks and : on a still screen \
             {look:.1} us (p99 {look_p99:.1})"
        );
    }
}

#[test]
#[ignore = "measurement; run in release"]
fn daemon_memory_and_latency() {
    let dir = scratch("bench-daemon");
    let fixture = Fixture::load(BUSY);
    let server = FakeTerminalServer::start(&dir, &ansi(&fixture), vec![]);
    let state = dir.join("s");
    let mut daemon = serve(&state, server.path());
    let pid = daemon.id();
    let socket = state.join("sock");
    let started = rss_kib(pid);

    let first = ask(&socket, "{\"cmd\":\"look\"}\n");
    assert!(first.contains("you: 42,17"), "{first}");
    let mut rss = Vec::new();
    let mut latencies = Vec::with_capacity(LOOKS);
    for i in 1..=LOOKS {
        let start = Instant::now();
        let reply = ask(&socket, "{\"cmd\":\"look\"}\n");
        latencies.push(start.elapsed().as_secs_f64() * 1e6);
        assert!(reply.starts_with("{\"code\":0"), "{reply}");
        if [10, 100, 500, 1000].contains(&i) {
            rss.push((i, rss_kib(pid)));
        }
    }
    latencies.sort_by(f64::total_cmp);
    let requests = server.received().len();
    println!(
        "daemon RSS: {started} KiB at start; {}",
        rss.iter()
            .map(|(i, kib)| format!("{kib} KiB after {i} looks"))
            .collect::<Vec<_>>()
            .join(", ")
    );
    println!(
        "look over the socket API with the fake server: median {:.0} us, p99 {:.0} us; {} terminal requests \
         for {} looks",
        latencies[LOOKS / 2],
        latencies[LOOKS * 99 / 100],
        requests,
        LOOKS + 1
    );

    let (direct, _) = time_us(CLIENTS, || {
        ask(&socket, "{\"cmd\":\"look\"}\n");
    });
    let (client, client_p99) = time_us(CLIENTS, || {
        let output = Command::new(BIN)
            .arg("--dir")
            .arg(&state)
            .arg("look")
            .stdout(Stdio::null())
            .output()
            .expect("run the client");
        assert!(output.status.success());
    });
    println!(
        "client look: median {client:.0} us wall (p99 {client_p99:.0}); the same request over the socket \
         {direct:.0} us; client overhead {:.0} us",
        client - direct
    );

    let before = server.received().len();
    std::thread::sleep(Duration::from_secs(2));
    println!(
        "idle 2 s: {} terminal requests, RSS {} KiB",
        server.received().len() - before,
        rss_kib(pid)
    );
    assert!(ask(&socket, "{\"cmd\":\"stop\"}\n").contains("stopped"));
    let _ = daemon.wait();
}

#[test]
#[ignore = "measurement; run in release"]
fn send_latency() {
    let dir = scratch("bench-send");
    let fixture = Fixture::load(BUSY);
    let screen = ansi(&fixture);
    let server = FakeTerminalServer::start(&dir, &screen, vec![]);
    let mut turn = 438;
    server.respond(Box::new(move |input: &[u8]| {
        if input != b"s" {
            return Vec::new();
        }
        turn += 1;
        String::from_utf8_lossy(&screen)
            .replace("T:438", &format!("T:{turn}"))
            .into_bytes()
    }));
    let state = dir.join("s");
    let mut daemon = serve(&state, server.path());
    let socket = state.join("sock");
    ask(&socket, "{\"cmd\":\"look\"}\n");
    let before = server.received().len();
    let (median, p99) = time_us(CLIENTS, || {
        let reply = ask(&socket, "{\"cmd\":\"send\",\"keys\":\"s\"}\n");
        assert!(reply.starts_with("{\"code\":0"), "{reply}");
    });
    println!(
        "send of one key that redraws the screen, with the look around after it: median {median:.0} us, \
         p99 {p99:.0} us; {} terminal requests per send",
        (server.received().len() - before) / CLIENTS
    );
    ask(&socket, "{\"cmd\":\"stop\"}\n");
    let _ = daemon.wait();
}
