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
            return Step::Output(Vec::new());
        }
        turn += 1;
        Step::Output(
            format!(
                "\x1b[H\x1b[K\x1b[24;1HDlvl:1 $:0 HP:12(16) Pw:2(2) AC:6 Xp:1 T:{turn}\x1b[K\x1b[4;22H"
            )
            .into_bytes(),
        )
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
        Step::Output(out)
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
    assert_eq!(
        bad["text"],
        "look takes no keys, target, wet, count or near"
    );
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
    /// The `--More--` page after the end-of-game window.
    Closing,
}

/// How the mini game's hero dies: the reason as the tombstone carves it on two lines, and the game's entry in the
/// score list, two rows, the second ending with the hit point column.
#[derive(Clone, Copy)]
struct Death {
    carved: [&'static str; 2],
    entry: [&'static str; 2],
}

const JACKAL: Death = Death {
    carved: ["killed by a", "jackal"],
    entry: [
        "             0  Hero-Val-Hum-Fem-Law died in The Dungeons of Doom on",
        "                level 1.  Killed by a jackal.                           -  [16]",
    ],
};

/// A death while helpless: the tombstone carves the reason without `, while praying`, the score list prints it
/// whole.
const PRAYING: Death = Death {
    carved: ["killed by a gray", "unicorn"],
    entry: [
        "             0  Hero-Val-Hum-Fem-Law died in The Dungeons of Doom on",
        "                level 1.  Killed by a gray unicorn, while praying.      -  [16]",
    ],
};

/// What the game answers a search with while a hostile monster stands next to the hero, taking no time.
const ALREADY_FOUND: &str = "You already found a monster.  Use 'm' prefix to force another search.";

/// A small game behind the fake terminal: one lit room, a hero that moves and searches, a jackal that turns up
/// after a set number of searches, farlook, look-here, and a death with its end-of-game pages, after which the game
/// prints the score list and exits. A room may hold water and down stairs: a step into water is taken only after
/// the `m` prefix, as the game takes a deliberate move into known water, and never when the water refuses the hero.
///
/// Searches take no time in two ways. A hero that acts twice in some turns (`acts_twice`, every Nth search)
/// searches with the turn counter left where it was while the jackal paces a square. With the jackal next to the
/// hero, a search is refused with `ALREADY_FOUND`, which the game does not repeat while it is still its last
/// message: a repeat clears the message line and shows nothing.
struct MiniGame {
    hero: (u8, u8),
    turn: u32,
    searches: u32,
    arrives_after: Option<u32>,
    arrives_at: (u8, u8),
    jackal: Option<(u8, u8)>,
    stage: Stage,
    pick: (u8, u8),
    message: String,
    water: Vec<(u8, u8)>,
    stairs: Option<(u8, u8)>,
    refuses_water: bool,
    prefixed: bool,
    death: Death,
    acts_twice: Option<u32>,
    paces: bool,
    /// The last message the game printed.
    said: String,
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
            arrives_at: (27, 4),
            jackal: None,
            stage: Stage::Map,
            pick: (22, 4),
            message: String::new(),
            water: Vec::new(),
            stairs: None,
            refuses_water: false,
            prefixed: false,
            death: JACKAL,
            acts_twice: None,
            paces: false,
            said: String::new(),
        }
    }

    /// The jackal pacing between 26,4 and 27,4, and a hero that acts twice in a turn every `every`th search.
    fn quick(every: u32) -> MiniGame {
        let mut game = MiniGame::new(None);
        game.jackal = Some((27, 4));
        game.paces = true;
        game.acts_twice = Some(every);
        game
    }

    /// The jackal next to the hero, at 23,4.
    fn cornered() -> MiniGame {
        let mut game = MiniGame::new(None);
        game.jackal = Some((23, 4));
        game
    }

    /// Water filling columns 24 and 25 of the room, between the hero and down stairs at 27,4.
    fn moat(refuses_water: bool) -> MiniGame {
        let mut game = MiniGame::new(None);
        game.water = (24..=25)
            .flat_map(|x| (Self::TOP + 1..Self::BOTTOM).map(move |y| (x, y)))
            .collect();
        game.stairs = Some((27, 4));
        game.refuses_water = refuses_water;
        game
    }

    fn responder(mut self) -> (Vec<u8>, Responder) {
        let first = self.screen();
        (
            first,
            Box::new(move |input: &[u8]| {
                let closing = self.stage == Stage::Closing;
                for &key in input {
                    self.key(key);
                    if !self.message.is_empty() {
                        self.said.clone_from(&self.message);
                    }
                }
                if closing {
                    return Step::Exit(self.scores());
                }
                Step::Output(self.screen())
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
            Stage::Tomb => self.stage = Stage::Closing,
            Stage::Closing => {}
        }
    }

    fn command(&mut self, key: u8) {
        self.message.clear();
        let prefixed = std::mem::replace(&mut self.prefixed, key == b'm');
        match key {
            b'm' | 0x12 | 0x1b => {}
            b's' if self.jackal.is_some_and(|(x, y)| {
                x.abs_diff(self.hero.0) <= 1 && y.abs_diff(self.hero.1) <= 1
            }) =>
            {
                if self.said != ALREADY_FOUND {
                    self.message = ALREADY_FOUND.to_owned();
                }
            }
            b's' => {
                self.searches += 1;
                if self
                    .acts_twice
                    .is_none_or(|n| !self.searches.is_multiple_of(n))
                {
                    self.turn += 1;
                }
                if Some(self.searches) == self.arrives_after {
                    self.jackal = Some(self.arrives_at);
                }
                if self.paces
                    && let Some((x, _)) = &mut self.jackal
                {
                    *x = if *x == 27 { 26 } else { 27 };
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
                    if self.water.contains(&to) && (!prefixed || self.refuses_water) {
                        self.message = "You avoid stepping into the water.".to_owned();
                    } else if Self::floor(to) && Some(to) != self.jackal {
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
                &format!("                  |{:^18}|", self.death.carved[0]),
                &format!("                  |{:^18}|", self.death.carved[1]),
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
        if self.stage == Stage::Closing {
            return b"\x1b[H\x1b[2J\x1b[24;1H--More--".to_vec();
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
        for &(x, y) in &self.water {
            at(&mut out, x, y);
            out.push_str("\x1b[34m}\x1b[0m");
        }
        if let Some((x, y)) = self.stairs {
            at(&mut out, x, y);
            out.push('>');
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

    /// The score list the game prints as it exits, after leaving the alternate screen: its own entry in standout,
    /// padded to the last column but one.
    fn scores(&self) -> Vec<u8> {
        let mut out = String::from("\x1b[H\x1b[2J\x1b[H\x1b[?1049l\r\n\r\n");
        out.push_str(
            " No  Points     Name                                                   Hp [max]\r\n",
        );
        for row in self.death.entry {
            out.push_str(&format!("\x1b[7m{row:79}\x1b[0m\r\n"));
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

fn facts_of(output: &Output) -> serde_json::Value {
    facts(output)
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

/// The movement keys among `inputs`, as text.
fn moves(inputs: &[Vec<u8>]) -> Vec<String> {
    inputs
        .iter()
        .filter(|keys| keys.last().is_some_and(|k| b"hjklyubn".contains(k)))
        .map(|keys| String::from_utf8_lossy(keys).into_owned())
        .collect()
}

#[test]
fn go_wet_crosses_water_that_a_dry_go_has_no_route_over() {
    let dir = scratch("e2e-go-wet");
    let server = serve_game(&dir, MiniGame::moat(false));
    let session = Session::new(&dir, server.path());
    let look = stdout(&session.run(&["look"]));
    assert!(
        look.lines().any(|line| line
            == "seen: > down stairs 27,4 (no route); } water 24,3 (no route), 25,3 (no route), \
                24,4 (no route), +3 more"),
        "{look}"
    );
    let dry = session.run(&["go", ">"]);
    assert_eq!(code(&dry), 1, "{}{}", stdout(&dry), stderr(&dry));
    assert_eq!(stdout(&dry), "no known route to >\n");
    assert!(moves(&server.inputs()).is_empty());

    let wet = session.run(&["go", "--wet", ">"]);
    assert_eq!(code(&wet), 0, "{}{}", stdout(&wet), stderr(&wet));
    assert_eq!(
        stdout(&wet).lines().next(),
        Some("go --wet >: 5 steps, 2 wet, 5 turns, now at 27,4; done")
    );
    // Every step went after the `m` prefix, the two into water too, and none was refused.
    assert_eq!(moves(&server.inputs()), ["ml"; 5]);
    let back = session.run(&["--json", "go", "--wet", "22,4"]);
    let facts = facts(&back);
    assert_eq!(facts["pos"], serde_json::json!([22, 4]));
    assert_eq!(facts["report"]["verb"], "go --wet 22,4");
    assert_eq!(facts["report"]["wet"], 2);
    assert_eq!(facts["report"]["steps"], 5);
    // A dry go reports no wet steps at all.
    let dry = facts_of(&session.run(&["--json", "go", "23,4"]));
    assert_eq!(dry["report"]["done"], true);
    assert!(dry["report"].get("wet").is_none(), "{dry}");
}

#[test]
fn a_wet_step_the_game_refuses_stops_short_with_its_message_and_square() {
    let dir = scratch("e2e-go-wet-refused");
    let server = serve_game(&dir, MiniGame::moat(true));
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let mut stream = UnixStream::connect(session.socket()).expect("connect to the daemon");
    stream
        .write_all(b"{\"cmd\":\"go\",\"target\":\">\",\"wet\":true}\n")
        .unwrap();
    let mut line = String::new();
    BufReader::new(&stream).read_line(&mut line).unwrap();
    let reply: serde_json::Value = serde_json::from_str(&line).unwrap();
    assert_eq!(reply["code"], 2, "{line}");
    let text = reply["text"].as_str().unwrap();
    assert_eq!(
        text.lines().next(),
        Some(
            "go --wet >: 2 steps, 1 wet, 1 turn, now at 23,4; stopped: wet step 23,4->24,4 refused"
        ),
        "{text}"
    );
    assert!(
        text.lines()
            .any(|l| l == "msg: You avoid stepping into the water."),
        "{text}"
    );
    let report = &reply["facts"]["report"];
    assert_eq!(
        (report["done"].as_bool(), report["wet"].as_u64()),
        (Some(false), Some(1))
    );
    assert_eq!(reply["facts"]["pos"], serde_json::json!([23, 4]));
    // The refused step was sent once and not tried again.
    assert_eq!(moves(&server.inputs()), ["ml", "ml"]);
    // `wet` belongs to go alone.
    let mut stream = UnixStream::connect(session.socket()).expect("connect to the daemon");
    stream
        .write_all(b"{\"cmd\":\"rest\",\"count\":1,\"wet\":true}\n")
        .unwrap();
    line.clear();
    BufReader::new(&stream).read_line(&mut line).unwrap();
    assert!(line.contains("\"code\":64"), "{line}");
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
fn a_rest_says_which_searches_took_no_time_and_what_the_screen_showed() {
    // Every fourth search leaves the turn counter where it was while the jackal paces.
    let dir = scratch("e2e-rest-quick");
    let server = serve_game(&dir, MiniGame::quick(4));
    let session = Session::new(&dir, server.path());
    // The first search shows the floor the jackal stood on.
    assert_eq!(code(&session.run(&["send", "s"])), 0);
    let rested = session.run(&["--json", "rest", "8"]);
    assert_eq!(code(&rested), 0, "{}{}", stdout(&rested), stderr(&rested));
    let reply: serde_json::Value = serde_json::from_slice(&rested.stdout).unwrap();
    assert_eq!(
        reply["text"].as_str().and_then(|text| text.lines().next()),
        Some(
            "rest 8: 8 searches, 6 turns, 2 took no time (turn counter unchanged, no message or prompt); \
             done"
        )
    );
    let report = &reply["facts"]["report"];
    assert_eq!(report["no_time"], 2);
    assert_eq!(
        report["no_time_why"],
        serde_json::json!([{"searches": 2, "why": "turn counter unchanged"}])
    );
    drop(session);
    drop(server);

    // With the jackal next to the hero the game refuses the search, then shows nothing for a repeat.
    let dir = scratch("e2e-rest-refused");
    let server = serve_game(&dir, MiniGame::cornered());
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let looked = server.inputs().len();
    let refused = session.run(&["rest", "5"]);
    assert_eq!(
        code(&refused),
        1,
        "{}{}",
        stdout(&refused),
        stderr(&refused)
    );
    let text = stdout(&refused);
    assert_eq!(
        text.lines().next(),
        Some(
            "rest 5: 1 search, 0 turns, 1 took no time (the game said \"You already found a monster.  Use \
             'm' prefix to force another search.\"); stopped: no time passed"
        ),
        "{text}"
    );
    let again = session.run(&["--json", "rest", "5"]);
    assert_eq!(code(&again), 1, "{}{}", stdout(&again), stderr(&again));
    let reply: serde_json::Value = serde_json::from_slice(&again.stdout).unwrap();
    assert_eq!(
        reply["text"].as_str().and_then(|text| text.lines().next()),
        Some(
            "rest 5: 4 searches, 0 turns, 4 took no time (1 turn counter unchanged, no message or prompt; \
             3 screen unchanged); stopped: no time passed"
        )
    );
    let report = &reply["facts"]["report"];
    assert_eq!(report["no_time"], 4);
    assert_eq!(
        report["no_time_why"],
        serde_json::json!([
            {"searches": 1, "why": "turn counter unchanged"},
            {"searches": 3, "why": "screen unchanged"}
        ])
    );
    assert_eq!(reply["facts"]["msgs"], serde_json::json!([]));
    // The two rests sent five searches and nothing else.
    assert_eq!(server.inputs()[looked..], vec![b"s".to_vec(); 5]);
}

#[test]
fn a_rest_with_a_near_radius_stops_only_for_a_monster_within_it() {
    // The jackal turns up 5 squares from the hero after the third search: outside a radius of 3.
    let dir = scratch("e2e-rest-near-far");
    let server = serve_game(&dir, MiniGame::new(Some(3)));
    let session = Session::new(&dir, server.path());
    let rested = session.run(&["--json", "rest", "20", "--near", "3"]);
    assert_eq!(code(&rested), 0, "{}{}", stdout(&rested), stderr(&rested));
    let facts = facts(&rested);
    assert_eq!(facts["report"]["verb"], "rest --near 3 20");
    assert_eq!(facts["report"]["done"], true);
    assert_eq!(facts["report"]["steps"], 20);
    assert_eq!(facts["monsters"][0]["dist"], 5);
    let searches = server.inputs().iter().filter(|keys| *keys == b"s").count();
    assert_eq!(searches, 20);
    drop(session);
    drop(server);

    // The jackal turns up 2 squares away: inside the radius, so the rest stops there.
    let dir = scratch("e2e-rest-near-close");
    let mut game = MiniGame::new(Some(3));
    game.arrives_at = (24, 4);
    let server = serve_game(&dir, game);
    let session = Session::new(&dir, server.path());
    let rested = session.run(&["rest", "--near", "3", "20"]);
    assert_eq!(code(&rested), 2, "{}{}", stdout(&rested), stderr(&rested));
    assert_eq!(
        stdout(&rested).lines().next(),
        Some(
            "rest --near 3 20: 3 searches, 3 turns; stopped: a monster came within 3 squares of the hero \
             (d 24,4, 2 away)"
        )
    );
    let searches = server.inputs().iter().filter(|keys| *keys == b"s").count();
    assert_eq!(searches, 3);
}

#[test]
fn a_death_is_paged_to_the_score_list_and_reported_as_code_3() {
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
    // What the end-of-game window shows is end text, not messages.
    assert_eq!(facts["msgs"], serde_json::json!(["You die..."]), "{facts}");
    let sent = server.inputs().len();
    assert_eq!(server.inputs().last().map(Vec::as_slice), Some(&b" "[..]));

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
    let look = session.run(&["look"]);
    assert_eq!(code(&look), 3);
    assert!(
        stdout(&look).contains("screen (game over):\n No  Points     Name"),
        "{}",
        stdout(&look)
    );
    assert_eq!(server.inputs().len(), sent);
}

#[test]
fn a_death_while_helpless_is_reported_whole_from_the_score_list() {
    let dir = scratch("e2e-death-helpless");
    let mut game = MiniGame::new(None);
    game.death = PRAYING;
    let server = serve_game(&dir, game);
    let session = Session::new(&dir, server.path());
    assert_eq!(code(&session.run(&["look"])), 0);
    let died = session.run(&["--json", "send", "Fl"]);
    assert_eq!(code(&died), 3, "{}{}", stdout(&died), stderr(&died));
    let reply: serde_json::Value = serde_json::from_slice(&died.stdout).expect("a JSON reply");
    let death = "killed by a gray unicorn, while praying";
    assert_eq!(reply["facts"]["end"]["death"], death);
    let text = reply["text"].as_str().unwrap_or_default();
    assert!(
        text.lines().any(|line| line == format!("end: {death}")),
        "{text}"
    );
    let over = session.run(&["send", "s"]);
    assert_eq!(code(&over), 3);
    assert!(
        stdout(&over).contains("nothing was sent"),
        "{}",
        stdout(&over)
    );
    assert!(
        stdout(&over)
            .lines()
            .any(|line| line == format!("end: {death}")),
        "{}",
        stdout(&over)
    );
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
