//! Measurements against the performance budget. Times are printed, since they depend on the machine; resident
//! memory, its growth, terminal requests per command and idle activity are asserted against the budget. Run with
//! `cargo test --release --test bench -- --ignored --nocapture --test-threads 1`.

mod support;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use nethack_harness::play::Play;
use nethack_harness::types::{COLS, Colour, ROWS};
use support::server::FakeTerminalServer;
use support::{Fixture, FixtureTerm, scratch};

const BIN: &str = env!("CARGO_BIN_EXE_nethack-harness");

/// A map with many remembered items, doors and two monsters.
const BUSY: &str = "seed4101-0120";
/// Commands sent to the daemon before its resident memory is read.
const COMMANDS: usize = 1000;
/// The daemon's resident memory budget, in KiB.
const RSS_MAX_KIB: u64 = 4096;
/// Most the daemon's resident memory may grow between its 500th and 1000th command, in KiB.
const RSS_GROWTH_KIB: u64 = 64;
/// The output the terminal server keeps, which the daemon's first read replays whole.
const RING: usize = 64 * 1024;
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
        let mut play = Play::new(FixtureTerm::still(view), 64);
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

/// The game behind the fake server for the resource runs: `s` passes a turn, `<` moves to the other of two levels
/// with no stair between them, and any other key leaves the screen as it is.
fn responder(screen: Vec<u8>) -> support::server::Responder {
    let mut turn = 438;
    let mut deeper = false;
    Box::new(move |input: &[u8]| {
        match input {
            b"s" => turn += 1,
            b"<" => deeper = !deeper,
            _ => return Vec::new(),
        }
        String::from_utf8_lossy(&screen)
            .replace("T:438", &format!("T:{turn}"))
            .replace("Dlvl:2", if deeper { "Dlvl:3" } else { "Dlvl:2" })
            .into_bytes()
    })
}

/// Context switches of process `pid`, where the system reports them.
fn switches(pid: u32) -> Option<u64> {
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    status
        .lines()
        .filter(|line| line.contains("ctxt_switches:"))
        .map(|line| line.split_whitespace().last()?.parse::<u64>().ok())
        .sum()
}

#[test]
#[ignore = "measurement; run in release"]
fn daemon_memory_and_latency() {
    let dir = scratch("bench-daemon");
    let screen = ansi(&Fixture::load(BUSY));
    let mut initial = Vec::with_capacity(RING + screen.len());
    while initial.len() < RING {
        initial.extend_from_slice(&screen);
    }
    let server = FakeTerminalServer::start(&dir, &initial, vec![]);
    server.respond(responder(screen));
    let state = dir.join("s");
    let mut daemon = serve(&state, server.path());
    let pid = daemon.id();
    let socket = state.join("sock");
    let started = rss_kib(pid);

    let first = ask(&socket, "{\"cmd\":\"look\"}\n");
    assert!(first.contains("you: 42,17"), "{first}");
    let commands = [
        "{\"cmd\":\"look\"}\n",
        "{\"cmd\":\"send\",\"keys\":\"s\"}\n",
        "{\"cmd\":\"rest\",\"count\":2}\n",
        "{\"cmd\":\"go\",\"target\":\"frontier\"}\n",
        "{\"cmd\":\"look\"}\n",
        "{\"cmd\":\"send\",\"keys\":\"i\"}\n",
        "{\"cmd\":\"go\",\"target\":\">\"}\n",
        "{\"cmd\":\"send\",\"keys\":\"<\"}\n",
    ];
    let mut rss = Vec::new();
    let mut latencies = Vec::with_capacity(COMMANDS);
    for i in 1..=COMMANDS {
        let start = Instant::now();
        let reply = ask(&socket, commands[i % commands.len()]);
        latencies.push(start.elapsed().as_secs_f64() * 1e6);
        assert!(!reply.starts_with("{\"code\":4"), "{reply}");
        if [10, 100, 500, 1000].contains(&i) {
            rss.push((i, rss_kib(pid)));
        }
    }
    latencies.sort_by(f64::total_cmp);
    println!(
        "daemon RSS: {started} KiB at start; {}",
        rss.iter()
            .map(|(i, kib)| format!("{kib} KiB after {i} commands"))
            .collect::<Vec<_>>()
            .join(", ")
    );
    println!(
        "look, send, rest and go over the socket API with the fake server: median {:.0} us, p99 {:.0} us",
        latencies[COMMANDS / 2],
        latencies[COMMANDS * 99 / 100],
    );
    let at = |n: usize| {
        rss.iter()
            .find(|&&(i, _)| i == n)
            .map_or(0, |&(_, kib)| kib)
    };
    assert!(at(1000) <= RSS_MAX_KIB, "{} KiB resident", at(1000));
    assert!(
        at(1000) <= at(500) + RSS_GROWTH_KIB,
        "resident memory grew from {} KiB to {} KiB",
        at(500),
        at(1000)
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

    let before = (server.received().len(), switches(pid));
    std::thread::sleep(Duration::from_secs(2));
    let after = (server.received().len(), switches(pid));
    println!(
        "idle 2 s: {} terminal requests, {:?} context switches, RSS {} KiB",
        after.0 - before.0,
        after.1.zip(before.1).map(|(a, b)| a - b),
        rss_kib(pid)
    );
    assert_eq!(after, before, "the idle daemon did something");
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
    server.respond(responder(screen));
    let state = dir.join("s");
    let mut daemon = serve(&state, server.path());
    let socket = state.join("sock");
    ask(&socket, "{\"cmd\":\"look\"}\n");
    let before = server.received().len();
    let (median, p99) = time_us(CLIENTS, || {
        let reply = ask(&socket, "{\"cmd\":\"send\",\"keys\":\"s\"}\n");
        assert!(reply.starts_with("{\"code\":0"), "{reply}");
    });
    let requests = (server.received().len() - before) / CLIENTS;
    println!(
        "send of one key that redraws the screen, with the look around after it: median {median:.0} us, \
         p99 {p99:.0} us; {requests} terminal requests per send"
    );
    // One read of the screen, the key, and the look underfoot and farlooks the new turn calls for.
    assert!(requests <= 5, "{requests} terminal requests per send");
    ask(&socket, "{\"cmd\":\"stop\"}\n");
    let _ = daemon.wait();
}
