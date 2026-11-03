//! End to end: the real client and daemon binaries against the fake terminal server.

mod support;

use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use support::scratch;
use support::server::{FakeTerminalServer, Step};

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
    let sent = session.run(&["send", "-\\e"]);
    assert_eq!(code(&sent), 0, "{}{}", stdout(&sent), stderr(&sent));
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
