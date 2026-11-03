//! End to end: the real client and daemon binaries against the fake terminal server.

mod support;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use support::scratch;
use support::server::{FakeTerminalServer, Responder, Step};

const BIN: &str = env!("CARGO_BIN_EXE_nethack-harness");

const SCREEN: &[u8] = b"\x1b[H\x1b[2J\x1b[3;20H------\x1b[4;20H|.@..|\x1b[5;20H------\
\x1b[23;1HHero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful\
\x1b[24;1HDlvl:1 $:0 HP:12(16) Pw:2(2) AC:6 Xp:1 T:10\x1b[4;22H";

const ENDED: &[u8] = b"\x1b[H\x1b[2JDo you want your possessions identified? [ynq] (n)\
\x1b[23;1HHero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful\
\x1b[24;1HDlvl:1 $:0 HP:0(16) Pw:2(2) AC:6 Xp:1 T:10\x1b[1;52H";

/// A session directory played through the binary; dropping it stops its daemon.
struct Session {
    dir: PathBuf,
    terminal: PathBuf,
}

impl Session {
    fn new(dir: &Path, terminal: &Path) -> Session {
        Session {
            dir: dir.join("s"),
            terminal: terminal.to_path_buf(),
        }
    }

    fn command(&self, args: &[&str]) -> Command {
        let mut command = Command::new(BIN);
        command
            .arg("--dir")
            .arg(&self.dir)
            .arg("--socket")
            .arg(&self.terminal)
            .args(args);
        command
    }

    fn run(&self, args: &[&str]) -> Output {
        self.command(args).output().expect("run the client")
    }

    fn socket(&self) -> PathBuf {
        self.dir.join("sock")
    }

    /// Waits until the daemon has removed its socket.
    fn wait_gone(&self) -> bool {
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if !self.socket().exists() {
                return true;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        false
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        let _ = self.run(&["stop"]);
    }
}

fn code(output: &Output) -> i32 {
    output.status.code().unwrap_or(-1)
}

fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).into_owned()
}

fn stderr(output: &Output) -> String {
    String::from_utf8_lossy(&output.stderr).into_owned()
}

fn client(args: &[&str]) -> Output {
    Command::new(BIN)
        .args(args)
        .output()
        .expect("run the client")
}

#[test]
fn usage_errors_exit_64_and_touch_nothing() {
    let dir = scratch("e2e-usage");
    let state = dir.join("s");
    let state_arg = state.to_str().unwrap();
    let long = "a".repeat(257);
    for args in [
        vec![],
        vec!["dance"],
        vec!["rest", "0"],
        vec!["rest", "x"],
        vec!["send"],
        vec!["send", long.as_str()],
        vec!["look", "extra"],
        vec!["--max-steps", "0", "look"],
        vec!["send", "☃"],
    ] {
        let mut line = vec!["--dir", state_arg];
        line.extend(args.iter());
        let output = client(&line);
        assert_eq!(code(&output), 64, "{args:?}: {}", stderr(&output));
        assert!(stderr(&output).contains("usage: nethack-harness"));
        assert!(stdout(&output).is_empty());
    }
    assert!(!state.exists());
}

#[test]
fn stop_without_a_daemon_starts_nothing() {
    let dir = scratch("e2e-stop");
    let state = dir.join("s");
    let output = client(&["--dir", state.to_str().unwrap(), "stop"]);
    assert_eq!(code(&output), 0);
    assert_eq!(stdout(&output), "no daemon is running\n");
    assert!(!state.exists());
}

#[test]
fn an_unreachable_terminal_is_code_4_and_the_daemon_exits() {
    let dir = scratch("e2e-unreachable");
    let session = Session::new(&dir, &dir.join("absent.sock"));
    let output = session.run(&["look"]);
    assert_eq!(code(&output), 4, "{}", stderr(&output));
    assert!(
        stdout(&output).contains("absent.sock"),
        "{}",
        stdout(&output)
    );
    assert!(session.wait_gone());
    assert_eq!(code(&session.run(&["look"])), 4);
}

#[test]
fn direct_play_through_the_binary() {
    let dir = scratch("e2e-play");
    let server = FakeTerminalServer::start(&dir, b"", vec![Step::Output(SCREEN.to_vec())]);
    let session = Session::new(&dir, server.path());
    let look = session.run(&["look"]);
    assert_eq!(code(&look), 0, "{}{}", stdout(&look), stderr(&look));
    let text = stdout(&look);
    assert!(text.contains("|.@..|"), "{text}");
    assert!(text.contains("you: 21,3"), "{text}");
    assert_eq!(server.inputs().first(), Some(&b"\x12".to_vec()));

    server.push((0..200).map(|_| Step::Output(SCREEN.to_vec())));
    let before = server.received().len();
    let sent = session.run(&["send", "-\\e"]);
    assert_eq!(code(&sent), 0, "{}{}", stdout(&sent), stderr(&sent));
    let reads = server.received()[before..]
        .iter()
        .filter(|r| r.input.is_empty())
        .count();
    assert_eq!(reads, 1, "one read of the screen per command");
    let inputs = server.inputs();
    assert_eq!(
        inputs[inputs.len() - 2..],
        [b"-".to_vec(), b"\x1b".to_vec()]
    );

    let _ = session.run(&["go", "23,3"]);
    assert!(server.inputs().contains(&b"ml".to_vec()));

    let rested = session.run(&["rest", "2"]);
    assert_eq!(code(&rested), 1, "a search that passes no time was refused");

    assert_eq!(code(&session.run(&["go", "altar"])), 1);
    assert_eq!(code(&session.run(&["rest", "0"])), 64);

    let json = session.run(&["--json", "look"]);
    let reply: serde_json::Value = serde_json::from_slice(&json.stdout).unwrap();
    assert_eq!(reply["code"], 0);
    assert!(reply["text"].as_str().unwrap().contains("|.@..|"));
    assert!(reply["facts"].is_object());
    assert_eq!(server.connections(), 1);
}

/// The game's start-up splash, drawn on a cleared screen while the game is still busy starting.
const SPLASH: &[u8] = b"\x1b[?1049h\x1b[H\x1b[2J\x1b[5;1HNetHack, Copyright 1985-2026\r\
\x1b[6;1H         By Stichting Mathematisch Centrum and M. Stephenson.\r\x1b[12;1H";

/// The game's turn counter after `send s` on `SCREEN` or the welcome screen.
fn searches(turn: u32) -> Responder {
    let mut turn = turn;
    Box::new(move |input: &[u8]| {
        if input != b"s" {
            return Vec::new();
        }
        turn += 1;
        format!(
            "\x1b[H\x1b[K\x1b[24;1HDlvl:1 $:0 HP:12(16) Pw:2(2) AC:6 Xp:1 T:{turn}\x1b[K\x1b[4;22H"
        )
        .into_bytes()
    })
}

#[test]
fn the_first_look_waits_for_a_starting_game_and_pages_its_welcome() {
    let dir = scratch("e2e-starting");
    let welcome = String::from_utf8_lossy(SCREEN).replace("T:10", "T:1")
        + "\x1b[HHello Hero, welcome to NetHack!--More--";
    let server = FakeTerminalServer::starting(
        &dir,
        SPLASH,
        welcome.as_bytes(),
        vec![Step::Output(
            b"\x1b[H\x1b[KYou are lucky!  Full moon tonight.\x1b[4;22H".to_vec(),
        )],
    );
    server.respond(searches(1));
    let session = Session::new(&dir, server.path());
    let look = session.run(&["look"]);
    let text = stdout(&look);
    assert_eq!(code(&look), 0, "{text}{}", stderr(&look));
    assert!(!text.contains("screen ("), "{text}");
    assert!(text.contains("|.@..|"), "{text}");
    assert!(
        text.contains("msg: Hello Hero, welcome to NetHack!\n"),
        "{text}"
    );
    assert!(
        text.contains("msg: You are lucky!  Full moon tonight."),
        "{text}"
    );
    let first = &server.received()[0];
    assert!(first.input.is_empty() && first.wait > 0, "{first:?}");
    assert_eq!(server.inputs().first(), Some(&b" ".to_vec()));
    assert!(!server.inputs().contains(&b"\x12".to_vec()));

    let sent = session.run(&["send", "s"]);
    assert_eq!(code(&sent), 0, "{}{}", stdout(&sent), stderr(&sent));
    assert!(stdout(&sent).contains("T:2"), "{}", stdout(&sent));
    assert_eq!(server.inputs().last(), Some(&b"s".to_vec()));
}

#[test]
fn a_stale_splash_is_redrawn_before_the_first_look() {
    let dir = scratch("e2e-splash");
    let stale = b"\x1b[5;1HNetHack, Copyright 1985-2026\x1b[12;1H";
    let server = FakeTerminalServer::start(&dir, stale, vec![Step::Output(SCREEN.to_vec())]);
    server.respond(searches(10));
    let session = Session::new(&dir, server.path());
    let look = session.run(&["look"]);
    let text = stdout(&look);
    assert_eq!(code(&look), 0, "{text}{}", stderr(&look));
    assert!(!text.contains("Copyright"), "{text}");
    assert!(text.contains("|.@..|") && text.contains("T:10"), "{text}");
    assert_eq!(server.inputs().first(), Some(&b"\x12".to_vec()));

    let sent = session.run(&["send", "s"]);
    assert!(stdout(&sent).contains("T:11"), "{}", stdout(&sent));
    assert_eq!(server.inputs().last(), Some(&b"s".to_vec()));
}

#[test]
fn game_over_is_code_3() {
    let dir = scratch("e2e-over");
    let server = FakeTerminalServer::start(&dir, ENDED, vec![]);
    let session = Session::new(&dir, server.path());
    let look = session.run(&["look"]);
    assert_eq!(code(&look), 3, "{}{}", stdout(&look), stderr(&look));
    assert_eq!(code(&session.run(&["go", ">"])), 3);
    assert_eq!(code(&session.run(&["send", "y"])), 3);
}

#[test]
fn a_closed_terminal_is_code_4_and_ends_the_daemon() {
    let dir = scratch("e2e-gone");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    server.push([Step::Refuse(410, "game is stopping".to_owned())]);
    let closed = session.run(&["send", "s"]);
    assert_eq!(code(&closed), 4, "{}{}", stdout(&closed), stderr(&closed));
    assert!(stdout(&closed).contains("game is stopping"));
    assert!(session.wait_gone());
    assert_eq!(code(&session.run(&["look"])), 0);
    assert_eq!(server.connections(), 2);
}

#[test]
fn a_held_game_is_code_5_and_the_daemon_stays() {
    let dir = scratch("e2e-held");
    let server = FakeTerminalServer::start(
        &dir,
        b"\x1b[1;1Hpartial",
        vec![
            Step::Refuse(410, "waiting to start".to_owned()),
            Step::Refuse(503, "terminal busy".to_owned()),
            Step::Output(SCREEN.to_vec()),
        ],
    );
    let session = Session::new(&dir, server.path());
    let held = session.run(&["look"]);
    assert_eq!(code(&held), 5, "{}{}", stdout(&held), stderr(&held));
    assert!(stdout(&held).contains("waiting to start"));
    let look = session.run(&["look"]);
    assert_eq!(code(&look), 0, "{}{}", stdout(&look), stderr(&look));
    assert_eq!(server.inputs().first(), Some(&b"\x12".to_vec()));
    assert_eq!(server.connections(), 1);
}

#[test]
fn a_closed_terminal_during_sync_ends_the_daemon() {
    let dir = scratch("e2e-closed");
    let server = FakeTerminalServer::start(
        &dir,
        b"\x1b[1;1Hpartial",
        vec![Step::Refuse(410, "time is up".to_owned())],
    );
    let session = Session::new(&dir, server.path());
    let closed = session.run(&["look"]);
    assert_eq!(code(&closed), 4, "{}{}", stdout(&closed), stderr(&closed));
    assert!(stdout(&closed).contains("time is up"));
    assert!(session.wait_gone());
}

#[test]
fn a_game_that_never_settles_stops_the_keys_short() {
    let dir = scratch("e2e-stuck");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    server.push([Step::Stuck(Vec::new())]);
    let sent = session.run(&["send", "ab"]);
    assert_eq!(code(&sent), 2, "{}{}", stdout(&sent), stderr(&sent));
    assert!(stdout(&sent).contains("still busy"), "{}", stdout(&sent));
    let inputs = server.inputs();
    assert_eq!(inputs.last(), Some(&b"a".to_vec()), "{inputs:?}");
    assert!(!inputs.contains(&b"b".to_vec()), "{inputs:?}");
}

#[test]
fn a_slow_client_cannot_hold_the_daemon() {
    let dir = scratch("e2e-slow-client");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let socket = session.socket();
    let slow = std::thread::spawn(move || {
        let mut stream = UnixStream::connect(socket).unwrap();
        let _ = stream.write_all(b"{");
        for _ in 0..12 {
            std::thread::sleep(Duration::from_millis(500));
            if stream.write_all(b" ").is_err() {
                break;
            }
        }
    });
    std::thread::sleep(Duration::from_millis(100));
    let started = Instant::now();
    let look = session.run(&["look"]);
    assert_eq!(code(&look), 0, "{}", stderr(&look));
    assert!(
        started.elapsed() < Duration::from_secs(4),
        "{:?}",
        started.elapsed()
    );
    let _ = slow.join();
}

#[test]
fn the_session_is_private_to_its_owner() {
    let dir = scratch("e2e-private");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let session = Session::new(&dir, server.path());
    let look = Command::new("sh")
        .arg("-c")
        .arg("umask 000; exec \"$0\" \"$@\"")
        .arg(BIN)
        .arg("--dir")
        .arg(&session.dir)
        .arg("--socket")
        .arg(server.path())
        .arg("look")
        .output()
        .unwrap();
    assert_eq!(code(&look), 0, "{}", stderr(&look));
    let mode = |path: &Path| std::fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&session.dir), 0o700);
    assert_eq!(mode(&session.socket()), 0o600);
}

#[test]
fn an_oversized_target_is_a_usage_error() {
    let dir = scratch("e2e-target");
    let session = Session::new(&dir, &dir.join("absent.sock"));
    let go = session.run(&["go", &"x".repeat(100_000)]);
    assert_eq!(code(&go), 64, "{}", stderr(&go));
    assert!(!session.dir.exists());
}

#[test]
fn a_reply_stays_small_however_many_pages_the_keys_pass() {
    let dir = scratch("e2e-pages");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let mut page = 0u32;
    server.respond(Box::new(move |_| {
        page += 1;
        let mut out = b"\x1b[H\x1b[2J".to_vec();
        for line in 1..=22 {
            out.extend_from_slice(
                format!("\x1b[{line};1HPage {page} line {line}: the quick brown fox jumps over the lazy dog")
                    .as_bytes(),
            );
        }
        out.extend_from_slice(b"\x1b[23;1H--More--");
        out
    }));
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let sent = session.run(&["--json", "send", &" ".repeat(256)]);
    assert!(sent.stdout.len() < 32 * 1024, "{} bytes", sent.stdout.len());
    let text = stdout(&sent);
    assert!(text.contains("earlier lines left out"), "{text}");
    assert!(text.contains("earlier prompts left out"), "{text}");
}

#[test]
fn concurrent_clients_start_one_daemon() {
    let dir = scratch("e2e-concurrent");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let session = Session::new(&dir, server.path());
    let children: Vec<_> = (0..4)
        .map(|_| {
            session
                .command(&["look"])
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .spawn()
                .expect("spawn a client")
        })
        .collect();
    for child in children {
        let output = child.wait_with_output().expect("wait for a client");
        assert_eq!(code(&output), 0, "{}", stderr(&output));
    }
    assert_eq!(server.connections(), 1);
}

#[test]
fn the_daemon_survives_its_starting_clients_process_group() {
    let dir = scratch("e2e-survive");
    let server = FakeTerminalServer::start(
        &dir,
        b"\x1b[1;1Hpartial",
        vec![Step::Slow(500, SCREEN.to_vec())],
    );
    let session = Session::new(&dir, server.path());
    let mut child = session
        .command(&["look"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .expect("spawn a client");
    let deadline = Instant::now() + Duration::from_secs(10);
    while !session.socket().exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    let killed = Command::new("kill")
        .args(["-TERM", "--", &format!("-{}", child.id())])
        .status()
        .expect("run kill");
    assert!(killed.success());
    assert!(!child.wait().expect("wait for the client").success());
    let look = session.run(&["look"]);
    assert_eq!(code(&look), 0, "{}", stderr(&look));
    assert_eq!(server.connections(), 1);
}

#[test]
fn the_socket_api_answers_one_line_per_connection() {
    let dir = scratch("e2e-socket");
    let server = FakeTerminalServer::start(&dir, SCREEN, vec![]);
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let ask = |line: &str| -> serde_json::Value {
        let mut stream = UnixStream::connect(session.socket()).expect("connect to the daemon");
        stream.write_all(line.as_bytes()).unwrap();
        let mut reply = String::new();
        BufReader::new(&stream).read_line(&mut reply).unwrap();
        serde_json::from_str(&reply).unwrap()
    };
    let mut stream = UnixStream::connect(session.socket()).expect("connect to the daemon");
    stream
        .write_all(b"{\"cmd\":\"go\",\"target\":\"\xff\"}\n")
        .unwrap();
    let mut reply = String::new();
    BufReader::new(&stream).read_line(&mut reply).unwrap();
    assert!(reply.contains("\"code\":64"), "{reply}");
    let bad = ask("{\"cmd\":\"look\",\"keys\":\"x\"}\n");
    assert_eq!(bad["code"], 64);
    assert_eq!(bad["text"], "look takes no keys, target or count");
    assert_eq!(ask("not json\n")["code"], 64);
    let look = ask("{\"cmd\":\"look\"}\n");
    assert_eq!(look["code"], 0);
    assert!(look["text"].as_str().unwrap().contains("|.@..|"));
    let stopped = ask("{\"cmd\":\"stop\"}\n");
    assert_eq!(
        (stopped["code"].as_i64(), stopped["text"].as_str()),
        (Some(0), Some("stopped"))
    );
    assert!(session.wait_gone());
}

/// What the mini game is waiting for.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
enum Stage {
    /// A command on the map.
    Map,
    /// A position for a farlook.
    Pick,
    /// A direction to fight in.
    Direction,
    /// The `You die...--More--` page.
    Dying,
    /// An end-of-game disclosure question.
    Disclose,
    /// The tombstone.
    Tomb,
}

/// A small game behind the fake terminal: one lit room, a hero that moves and searches, a jackal that turns up
/// after a set number of searches, farlook, look-here, and a death with its end-of-game pages.
struct MiniGame {
    hero: (u8, u8),
    turn: u32,
    searches: u32,
    arrives_after: Option<u32>,
    jackal: Option<(u8, u8)>,
    stage: Stage,
    pick: (u8, u8),
    message: String,
}

impl MiniGame {
    const LEFT: u8 = 20;
    const RIGHT: u8 = 29;
    const TOP: u8 = 2;
    const BOTTOM: u8 = 6;

    fn new(arrives_after: Option<u32>) -> MiniGame {
        MiniGame {
            hero: (22, 4),
            turn: 10,
            searches: 0,
            arrives_after,
            jackal: None,
            stage: Stage::Map,
            pick: (22, 4),
            message: String::new(),
        }
    }

    fn responder(mut self) -> (Vec<u8>, Responder) {
        let first = self.screen();
        (
            first,
            Box::new(move |input: &[u8]| {
                for &key in input {
                    self.key(key);
                }
                self.screen()
            }),
        )
    }

    fn delta(key: u8) -> Option<(i16, i16)> {
        Some(match key {
            b'h' => (-1, 0),
            b'l' => (1, 0),
            b'k' => (0, -1),
            b'j' => (0, 1),
            b'y' => (-1, -1),
            b'u' => (1, -1),
            b'b' => (-1, 1),
            b'n' => (1, 1),
            _ => return None,
        })
    }

    fn shifted(at: (u8, u8), (dx, dy): (i16, i16)) -> (u8, u8) {
        (
            (i16::from(at.0) + dx).clamp(0, 79) as u8,
            (i16::from(at.1) + dy).clamp(1, 21) as u8,
        )
    }

    fn floor(at: (u8, u8)) -> bool {
        (Self::LEFT + 1..Self::RIGHT).contains(&at.0)
            && (Self::TOP + 1..Self::BOTTOM).contains(&at.1)
    }

    fn key(&mut self, key: u8) {
        match self.stage {
            Stage::Map => self.command(key),
            Stage::Pick => match key {
                b'@' => self.pick = self.hero,
                b'.' | b',' | b';' | b':' => {
                    self.message = if Some(self.pick) == self.jackal {
                        "d        a dog or other canine (jackal)".to_owned()
                    } else if self.pick == self.hero {
                        "@        human or elf (peaceful Hero)".to_owned()
                    } else {
                        ".        floor of a room".to_owned()
                    };
                    self.stage = Stage::Map;
                }
                0x1b => {
                    self.message.clear();
                    self.stage = Stage::Map;
                }
                other => {
                    if let Some(delta) = Self::delta(other) {
                        self.pick = Self::shifted(self.pick, delta);
                    }
                }
            },
            Stage::Direction => {
                self.stage = if Self::delta(key).is_some() {
                    Stage::Dying
                } else {
                    Stage::Map
                };
            }
            Stage::Dying => self.stage = Stage::Disclose,
            Stage::Disclose => self.stage = Stage::Tomb,
            Stage::Tomb => {}
        }
    }

    fn command(&mut self, key: u8) {
        self.message.clear();
        match key {
            b'm' | 0x12 | 0x1b => {}
            b's' => {
                self.turn += 1;
                self.searches += 1;
                if Some(self.searches) == self.arrives_after {
                    self.jackal = Some((27, 4));
                }
            }
            b':' => self.message = "You see no objects here.".to_owned(),
            b';' => {
                self.pick = self.hero;
                self.message = "Pick a monster, object or location.".to_owned();
                self.stage = Stage::Pick;
            }
            b'F' => self.stage = Stage::Direction,
            other => match Self::delta(other) {
                Some(delta) => {
                    let to = Self::shifted(self.hero, delta);
                    if Self::floor(to) && Some(to) != self.jackal {
                        self.hero = to;
                        self.turn += 1;
                    } else {
                        self.message = "You can't move there.".to_owned();
                    }
                }
                None => self.message = format!("Unknown command '{}'.", other as char),
            },
        }
    }

    /// The whole screen, redrawn.
    fn screen(&self) -> Vec<u8> {
        let mut out = String::from("\x1b[H\x1b[2J");
        let at =
            |out: &mut String, x: u8, y: u8| out.push_str(&format!("\x1b[{};{}H", y + 1, x + 1));
        if self.stage == Stage::Tomb {
            let tomb = [
                "                       ----------",
                "                      /          \\",
                "                     /    REST    \\",
                "                    /      IN      \\",
                "                   /     PEACE      \\",
                "                  /                  \\",
                "                  |       Hero       |",
                "                  |      0 Au        |",
                "                  |   killed by a    |",
                "                  |      jackal      |",
                "                  |                  |",
                "                  |                  |",
                "                  |       2026       |",
                "                 *|     *  *  *      | *",
                "        _________)/\\\\_//(\\/(/\\)/\\//\\/|_)_______",
            ];
            for (row, line) in tomb.iter().enumerate() {
                at(&mut out, 0, row as u8 + 1);
                out.push_str(line);
            }
            at(&mut out, 0, 17);
            out.push_str("--More--");
            at(&mut out, 8, 17);
            return out.into_bytes();
        }
        for y in Self::TOP..=Self::BOTTOM {
            at(&mut out, Self::LEFT, y);
            for x in Self::LEFT..=Self::RIGHT {
                out.push(if y == Self::TOP || y == Self::BOTTOM {
                    '-'
                } else if x == Self::LEFT || x == Self::RIGHT {
                    '|'
                } else {
                    '.'
                });
            }
        }
        if let Some((x, y)) = self.jackal {
            at(&mut out, x, y);
            out.push_str("\x1b[33md\x1b[0m");
        }
        at(&mut out, self.hero.0, self.hero.1);
        out.push('@');
        let hp = if self.stage >= Stage::Dying { 0 } else { 12 };
        at(&mut out, 0, 22);
        out.push_str("Hero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful");
        at(&mut out, 0, 23);
        out.push_str(&format!(
            "Dlvl:1 $:0 HP:{hp}(16) Pw:2(2) AC:6 Xp:1 T:{}",
            self.turn
        ));
        let message = match self.stage {
            Stage::Dying => "You die...--More--",
            Stage::Disclose => "Do you want your possessions identified? [ynq] (n)",
            _ => self.message.as_str(),
        };
        at(&mut out, 0, 0);
        out.push_str(message);
        match self.stage {
            Stage::Dying | Stage::Disclose => at(&mut out, message.len() as u8 + 1, 0),
            Stage::Pick => at(&mut out, self.pick.0, self.pick.1),
            _ => at(&mut out, self.hero.0, self.hero.1),
        }
        out.into_bytes()
    }
}

/// A fake terminal server running `game`.
fn serve_game(dir: &Path, game: MiniGame) -> FakeTerminalServer {
    let (first, responder) = game.responder();
    let server = FakeTerminalServer::start(dir, &first, vec![]);
    server.respond(responder);
    server
}

fn facts(output: &Output) -> serde_json::Value {
    let reply: serde_json::Value = serde_json::from_slice(&output.stdout).expect("a JSON reply");
    reply["facts"].clone()
}

#[test]
fn go_walks_to_a_square_and_reports_it() {
    let dir = scratch("e2e-go");
    let server = serve_game(&dir, MiniGame::new(None));
    let session = Session::new(&dir, server.path());
    let look = session.run(&["--json", "look"]);
    assert_eq!(code(&look), 0, "{}{}", stdout(&look), stderr(&look));
    assert_eq!(facts(&look)["pos"], serde_json::json!([22, 4]));
    let went = session.run(&["--json", "go", "27,4"]);
    assert_eq!(code(&went), 0, "{}{}", stdout(&went), stderr(&went));
    let facts = facts(&went);
    assert_eq!(facts["pos"], serde_json::json!([27, 4]));
    assert_eq!(facts["turn"], 15);
    assert_eq!(facts["report"]["done"], true);
    assert_eq!(facts["report"]["steps"], 5);
    let walked: Vec<Vec<u8>> = server
        .inputs()
        .into_iter()
        .filter(|keys| keys.as_slice() == b"ml")
        .collect();
    assert_eq!(walked.len(), 5);
    let text = stdout(&session.run(&["look"]));
    assert!(text.contains("you: 27,4"), "{text}");
}

#[test]
fn rest_searches_and_stops_short_when_a_monster_arrives() {
    let dir = scratch("e2e-rest");
    let server = serve_game(&dir, MiniGame::new(Some(3)));
    let session = Session::new(&dir, server.path());
    let rested = session.run(&["--json", "rest", "2"]);
    assert_eq!(code(&rested), 0, "{}{}", stdout(&rested), stderr(&rested));
    assert_eq!(facts(&rested)["report"]["steps"], 2);
    assert_eq!(facts(&rested)["turn"], 12);

    let interrupted = session.run(&["--json", "rest", "20"]);
    assert_eq!(
        code(&interrupted),
        2,
        "{}{}",
        stdout(&interrupted),
        stderr(&interrupted)
    );
    let facts = facts(&interrupted);
    assert_eq!(facts["report"]["done"], false);
    assert_eq!(facts["report"]["steps"], 1);
    let stopped = facts["report"]["stopped"].to_string();
    assert!(stopped.contains("27,4"), "{stopped}");
    assert_eq!(facts["monsters"][0]["glyph"], "d");
    assert_eq!(facts["monsters"][0]["name"], "jackal");
    let searches = server.inputs().iter().filter(|keys| *keys == b"s").count();
    assert_eq!(searches, 3);
}

#[test]
fn a_death_is_paged_to_the_tombstone_and_reported_as_code_3() {
    let dir = scratch("e2e-death");
    let server = serve_game(&dir, MiniGame::new(None));
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let died = session.run(&["--json", "send", "Fl"]);
    assert_eq!(code(&died), 3, "{}{}", stdout(&died), stderr(&died));
    let facts = facts(&died);
    assert_eq!(facts["end"]["death"], "killed by a jackal");
    let answered = facts["answered"].to_string();
    assert!(answered.contains("possessions identified"), "{answered}");
    let sent = server.inputs().len();

    let over = session.run(&["send", "s"]);
    assert_eq!(code(&over), 3);
    assert!(
        stdout(&over).contains("nothing was sent"),
        "{}",
        stdout(&over)
    );
    assert!(
        stdout(&over).contains("end: killed by a jackal"),
        "{}",
        stdout(&over)
    );
    assert_eq!(code(&session.run(&["look"])), 3);
    assert_eq!(server.inputs().len(), sent);
}

#[test]
fn stop_ends_the_daemon() {
    let dir = scratch("e2e-stop-running");
    let server = serve_game(&dir, MiniGame::new(None));
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    assert!(session.socket().exists());
    let stopped = session.run(&["stop"]);
    assert_eq!(code(&stopped), 0, "{}", stderr(&stopped));
    assert!(session.wait_gone());
    assert_eq!(stdout(&session.run(&["stop"])), "no daemon is running\n");
}
