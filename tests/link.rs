//! The terminal protocol against the fake server: framing, cursors, keep-alive, refusals, busy reads, and the
//! screen kept in step with the game.

mod support;

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use nethack_harness::link::{Exchange, Link, LinkError, Term, Terminal, WAIT_MS, base64_decode};
use support::scratch;
use support::server::{FakeTerminalServer, Step};

/// What a raw server's handler does with its connection after answering a request.
#[derive(PartialEq)]
enum Next {
    Keep,
    Close,
}

type Handler = dyn FnMut(usize, &[u8], &mut UnixStream) -> Next + Send;

/// A terminal server on `path` that hands each request's decoded input, with the connection's index, to `handler`,
/// which writes the response itself.
fn raw_server(path: &Path, handler: Box<Handler>) {
    let listener = UnixListener::bind(path).unwrap();
    let handler = Arc::new(Mutex::new(handler));
    std::thread::spawn(move || {
        for (index, stream) in listener.incoming().enumerate() {
            let Ok(stream) = stream else { continue };
            let handler = Arc::clone(&handler);
            std::thread::spawn(move || {
                let mut writer = stream.try_clone().unwrap();
                let mut reader = BufReader::new(stream);
                loop {
                    let mut line = String::new();
                    let mut length = 0;
                    if reader.read_line(&mut line).unwrap_or(0) == 0 {
                        return;
                    }
                    loop {
                        line.clear();
                        if reader.read_line(&mut line).unwrap_or(0) == 0 {
                            return;
                        }
                        if line.trim_end().is_empty() {
                            break;
                        }
                        if let Some(value) =
                            line.to_ascii_lowercase().strip_prefix("content-length:")
                        {
                            length = value.trim().parse().unwrap();
                        }
                    }
                    let mut body = vec![0; length];
                    if reader.read_exact(&mut body).is_err() {
                        return;
                    }
                    let value: serde_json::Value = serde_json::from_slice(&body).unwrap();
                    let mut input = Vec::new();
                    assert!(base64_decode(
                        value["input"].as_str().unwrap().as_bytes(),
                        &mut input
                    ));
                    if (handler.lock().unwrap())(index, &input, &mut writer) == Next::Close {
                        let _ = writer.shutdown(std::net::Shutdown::Both);
                        return;
                    }
                }
            });
        }
    });
}

/// Writes a 200 reply with no output at cursor 0.
fn empty_reply(stream: &mut UnixStream) {
    let body = "{\"output\":\"\",\"cursor\":0,\"waiting\":true}";
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
}

fn frame(message: &[u8], cursor: &[u8]) -> Vec<u8> {
    [
        b"\x1b[2J\x1b[1;1H".as_slice(),
        message,
        b"\x1b[3;4H@..\x1b[23;1HHero St:16 Lawful\x1b[24;1HDlvl:1 HP:14(14) Pw:3(3) AC:7 Xp:1 T:1",
        cursor,
    ]
    .concat()
}

#[test]
fn reads_output_after_the_cursor_on_one_connection() {
    let dir = scratch("link-cursor");
    let server = FakeTerminalServer::start(
        &dir,
        b"hello",
        vec![
            Step::Output(b" world".to_vec()),
            Step::Output(b"!".to_vec()),
        ],
    );
    let mut link = Link::new(server.path());
    assert_eq!(
        link.exchange(&[], 0).unwrap(),
        Exchange {
            reset: false,
            waiting: true
        }
    );
    assert_eq!(link.output(), b"hello");
    assert_eq!(link.cursor(), 5);
    link.exchange(b"k", WAIT_MS).unwrap();
    assert_eq!(link.output(), b" world");
    link.exchange(&[], 0).unwrap();
    assert_eq!(link.output(), b"");
    link.exchange(b"j", 250).unwrap();
    assert_eq!(link.output(), b"!");
    assert_eq!(link.cursor(), 12);
    let received = server.received();
    assert_eq!(
        received
            .iter()
            .map(|r| (r.after, r.wait))
            .collect::<Vec<_>>(),
        [(0, 0), (5, WAIT_MS), (11, 0), (11, 250)]
    );
    assert_eq!(server.inputs(), [b"k".to_vec(), b"j".to_vec()]);
    assert_eq!(server.connections(), 1);
}

#[test]
fn input_bytes_travel_raw() {
    let dir = scratch("link-raw");
    let server = FakeTerminalServer::start(&dir, b"", vec![]);
    let mut link = Link::new(server.path());
    let every: Vec<u8> = (0..=255u8).collect();
    link.exchange(b"\xf0", WAIT_MS).unwrap();
    link.exchange(&every, WAIT_MS).unwrap();
    assert_eq!(server.inputs(), [b"\xf0".to_vec(), every]);
    let too_long = vec![b'x'; 4097];
    assert!(matches!(
        link.exchange(&too_long, 0),
        Err(LinkError::Protocol(_))
    ));
}

#[test]
fn long_output_arrives_chunked_and_whole() {
    let dir = scratch("link-chunked");
    let big: Vec<u8> = (0..40_000u32).map(|i| (i % 251) as u8).collect();
    let server = FakeTerminalServer::start(&dir, b"", vec![Step::Output(big.clone())]);
    let mut link = Link::new(server.path());
    link.exchange(b"x", WAIT_MS).unwrap();
    assert_eq!(link.output(), big.as_slice());
    link.exchange(b"y", WAIT_MS).unwrap();
    assert_eq!(link.output(), b"");
    assert_eq!(server.connections(), 1);
}

#[test]
fn output_older_than_the_buffer_resets() {
    let dir = scratch("link-truncated");
    let server = FakeTerminalServer::start(&dir, b"start", vec![]);
    let mut link = Link::new(server.path());
    link.exchange(&[], 0).unwrap();
    let flood = vec![b'.'; 70 * 1024];
    server.push([Step::Output(flood)]);
    let exchange = link.exchange(b"x", WAIT_MS).unwrap();
    assert!(exchange.reset);
    assert_eq!(link.output().len(), 64 * 1024);
    assert_eq!(link.cursor(), 5 + 70 * 1024);
}

#[test]
fn a_restarted_server_is_read_from_its_start() {
    let dir = scratch("link-restart");
    let server = FakeTerminalServer::start(
        &dir,
        b"a long first game",
        vec![Step::Restart(b"new".to_vec())],
    );
    let mut link = Link::new(server.path());
    link.exchange(&[], 0).unwrap();
    let exchange = link.exchange(b"x", WAIT_MS).unwrap();
    assert!(exchange.reset);
    assert_eq!(link.output(), b"new");
    assert_eq!(link.cursor(), 3);
    assert_eq!(server.received().last().map(|r| r.after), Some(0));
}

#[test]
fn busy_is_retried_and_refusals_are_classified() {
    let dir = scratch("link-refusals");
    let server = FakeTerminalServer::start(
        &dir,
        b"",
        vec![
            Step::Refuse(503, "terminal busy".to_owned()),
            Step::Refuse(503, "terminal busy".to_owned()),
            Step::Output(b"ok".to_vec()),
            Step::Refuse(410, "waiting to start".to_owned()),
            Step::Refuse(400, "invalid terminal request".to_owned()),
            Step::Refuse(410, "time is up".to_owned()),
        ],
    );
    let mut link = Link::new(server.path());
    link.exchange(b"a", WAIT_MS).unwrap();
    assert_eq!(link.output(), b"ok");
    assert_eq!(server.inputs(), [b"a".to_vec()]);
    assert_eq!(
        link.exchange(b"b", WAIT_MS),
        Err(LinkError::Held("waiting to start".to_owned()))
    );
    match link.exchange(b"c", WAIT_MS) {
        Err(LinkError::Protocol(why)) => {
            assert!(why.contains("400") && why.contains("invalid terminal request"))
        }
        other => panic!("{other:?}"),
    }
    assert_eq!(
        link.exchange(b"d", WAIT_MS),
        Err(LinkError::Closed("time is up".to_owned()))
    );
    assert_eq!(server.inputs(), [b"a".to_vec()]);
    assert_eq!(server.connections(), 1);
}

#[test]
fn busy_game_reports_not_waiting_until_a_read_with_wait() {
    let dir = scratch("link-busy");
    let server = FakeTerminalServer::start(&dir, b"", vec![Step::Busy(b"20s".to_vec())]);
    let mut link = Link::new(server.path());
    assert!(!link.exchange(b"x", WAIT_MS).unwrap().waiting);
    assert!(!link.exchange(&[], 0).unwrap().waiting);
    assert!(link.exchange(&[], WAIT_MS).unwrap().waiting);
}

#[test]
fn a_dropped_connection_is_replaced() {
    let dir = scratch("link-reconnect");
    let server = FakeTerminalServer::start(&dir, b"one", vec![Step::Output(b"two".to_vec())]);
    let mut link = Link::new(server.path());
    link.exchange(&[], 0).unwrap();
    server.drop_connections();
    link.exchange(b"x", WAIT_MS).unwrap();
    assert_eq!(link.output(), b"two");
    assert_eq!(server.connections(), 2);
    assert_eq!(server.inputs(), [b"x".to_vec()]);
}

#[test]
fn an_absent_socket_is_unavailable_and_a_vanished_one_closed() {
    let dir = scratch("link-absent");
    let mut link = Link::new(&dir.join("absent.sock"));
    match link.exchange(&[], 0) {
        Err(LinkError::Unavailable(why)) => assert!(why.contains("absent.sock"), "{why}"),
        other => panic!("{other:?}"),
    }
    let server = FakeTerminalServer::start(&dir, b"x", vec![]);
    let mut link = Link::new(server.path());
    link.exchange(&[], 0).unwrap();
    drop(server);
    assert!(matches!(
        link.exchange(b"k", WAIT_MS),
        Err(LinkError::Closed(_))
    ));
}

#[test]
fn term_sends_bytes_raw_and_parses_the_screen() {
    let dir = scratch("term-raw");
    let initial = frame(b"", b"\x1b[3;4H");
    let server = FakeTerminalServer::start(
        &dir,
        &initial,
        vec![
            Step::Output(frame(b"Hello", b"\x1b[3;4H")),
            Step::Output(Vec::new()),
        ],
    );
    let mut term = Term::new(server.path());
    term.sync().unwrap();
    assert!(term.complete());
    term.send(b"x").unwrap();
    assert_eq!(term.view().msg, "Hello");
    assert!(term.message_written());
    assert_eq!(term.view().hero.map(|p| (p.y, p.x)), Some((2, 3)));
    term.send(b"\xf0").unwrap();
    assert!(!term.message_written());
    assert_eq!(server.inputs(), [b"x".to_vec(), b"\xf0".to_vec()]);
}

#[test]
fn term_sync_redraws_an_incomplete_screen() {
    let dir = scratch("term-sync");
    let server = FakeTerminalServer::start(
        &dir,
        b"\x1b[1;1Hpartial",
        vec![Step::Output(frame(b"", b"\x1b[3;4H"))],
    );
    let mut term = Term::new(server.path());
    term.sync().unwrap();
    assert_eq!(server.inputs(), [b"\x12".to_vec()]);
    assert_eq!(term.view().hero.map(|p| (p.y, p.x)), Some((2, 3)));
}

#[test]
fn term_rereads_while_the_game_is_busy() {
    let dir = scratch("term-busy");
    let status =
        |t: u32| format!("\x1b[24;1HDlvl:1 $:0 HP:14(14) Pw:3(3) AC:7 Xp:1 T:{t}\x1b[12;40H");
    let start = format!("\x1b[2J\x1b[12;40H@{}", status(1));
    let server = FakeTerminalServer::start(
        &dir,
        start.as_bytes(),
        vec![Step::Busy(status(8).into_bytes())],
    );
    let mut term = Term::new(server.path());
    term.sync().unwrap();
    term.send(b"20s").unwrap();
    assert!(term.view().rows[23].contains("T:8"));
    let received = server.received();
    let last = received.last().unwrap();
    assert!(last.input.is_empty() && last.wait == WAIT_MS);
}

#[test]
fn term_refusals_leave_the_prompt_state_alone() {
    let dir = scratch("term-refused");
    let pick = |message: &[u8], cursor: &[u8]| {
        [
            b"\x1b[2J\x1b[1;1H".as_slice(),
            message,
            b"\x1b[3;4H@+\x1b[24;1HDlvl:1 HP:14(14) Pw:3(3) AC:7 Xp:1 T:1",
            cursor,
        ]
        .concat()
    };
    let server = FakeTerminalServer::start(
        &dir,
        &pick(b"Pick an object.", b"\x1b[3;4H"),
        vec![
            Step::Output(pick(b"a closed door", b"\x1b[3;5H")),
            Step::Refuse(400, "refused".to_owned()),
            Step::Output(pick(b"", b"\x1b[3;4H")),
        ],
    );
    let mut term = Term::new(server.path());
    term.sync().unwrap();
    assert!(term.view().getpos);
    term.send(b"l").unwrap();
    assert!(term.view().getpos);
    assert_eq!(term.view().hero, None);
    assert!(matches!(term.send(b"\x1b"), Err(LinkError::Protocol(_))));
    assert!(term.view().getpos);
    term.send(b"\x1b").unwrap();
    assert!(!term.view().getpos);
    assert_eq!(term.view().hero.map(|p| (p.y, p.x)), Some((2, 3)));
    assert_eq!(server.inputs(), [b"l".to_vec(), b"\x1b".to_vec()]);
}

#[test]
fn term_reports_held_and_closed_games() {
    let dir = scratch("term-held");
    let server = FakeTerminalServer::start(
        &dir,
        &frame(b"", b"\x1b[3;4H"),
        vec![
            Step::Refuse(410, "waiting to start".to_owned()),
            Step::Refuse(410, "game is stopping".to_owned()),
        ],
    );
    let mut term = Term::new(server.path());
    term.sync().unwrap();
    assert!(matches!(term.send(b"k"), Err(LinkError::Held(_))));
    assert!(matches!(term.send(b"k"), Err(LinkError::Closed(_))));
    assert!(server.inputs().is_empty());
}

#[test]
fn a_reply_sent_a_byte_at_a_time_is_cut_off_at_its_deadline() {
    let dir = scratch("link-dribble");
    let path = dir.join("t.sock");
    raw_server(
        &path,
        Box::new(|_, _, stream| {
            let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nX-Slow: ");
            for _ in 0..20 {
                std::thread::sleep(Duration::from_millis(500));
                if stream.write_all(b"a").is_err() {
                    break;
                }
            }
            Next::Close
        }),
    );
    let mut link = Link::new(&path);
    let started = Instant::now();
    match link.exchange(&[], 0) {
        Err(LinkError::Protocol(why)) => assert!(why.contains("did not answer within"), "{why}"),
        other => panic!("{other:?}"),
    }
    assert!(
        started.elapsed() < Duration::from_secs(7),
        "{:?}",
        started.elapsed()
    );
}

#[test]
fn slow_busy_answers_are_retried_for_a_bounded_time() {
    let dir = scratch("link-503");
    let path = dir.join("t.sock");
    let requests = Arc::new(Mutex::new(0));
    let counted = Arc::clone(&requests);
    raw_server(
        &path,
        Box::new(move |_, _, stream| {
            *counted.lock().unwrap() += 1;
            std::thread::sleep(Duration::from_millis(300));
            let _ = stream.write_all(
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 14\r\n\r\nterminal busy\n",
            );
            Next::Keep
        }),
    );
    let mut link = Link::new(&path);
    let started = Instant::now();
    match link.exchange(b"x", WAIT_MS) {
        Err(LinkError::Protocol(why)) => assert!(why.contains("503"), "{why}"),
        other => panic!("{other:?}"),
    }
    assert!(
        started.elapsed() < Duration::from_secs(7),
        "{:?}",
        started.elapsed()
    );
    assert!(*requests.lock().unwrap() < 20);
}

#[test]
fn keys_are_never_sent_twice_when_the_connection_drops_before_answering() {
    let dir = scratch("link-once");
    let path = dir.join("t.sock");
    let seen = Arc::new(Mutex::new(Vec::<Vec<u8>>::new()));
    let log = Arc::clone(&seen);
    raw_server(
        &path,
        Box::new(move |connection, input, stream| {
            if !input.is_empty() {
                log.lock().unwrap().push(input.to_vec());
                if connection == 0 {
                    return Next::Close;
                }
            }
            empty_reply(stream);
            Next::Keep
        }),
    );
    let mut link = Link::new(&path);
    link.exchange(&[], 0).unwrap();
    match link.exchange(b"y", WAIT_MS) {
        Err(LinkError::Protocol(why)) => assert!(why.contains("unknown"), "{why}"),
        other => panic!("{other:?}"),
    }
    assert_eq!(*seen.lock().unwrap(), [b"y".to_vec()]);
    link.exchange(&[], 0).unwrap();
}

#[test]
fn a_reply_cut_short_fails_the_exchange_but_not_the_link() {
    let dir = scratch("link-cut");
    let path = dir.join("t.sock");
    raw_server(
        &path,
        Box::new(move |connection, _, stream| {
            if connection == 1 {
                let _ = stream.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 90\r\n\r\n{\"outp");
                return Next::Close;
            }
            empty_reply(stream);
            Next::Close
        }),
    );
    let mut link = Link::new(&path);
    link.exchange(&[], 0).unwrap();
    assert!(matches!(link.exchange(&[], 0), Err(LinkError::Protocol(_))));
    link.exchange(&[], 0).unwrap();
}

#[test]
fn a_game_that_never_settles_is_busy_and_takes_no_further_keys() {
    let dir = scratch("term-stuck");
    let server = FakeTerminalServer::start(
        &dir,
        &frame(b"", b"\x1b[3;4H"),
        vec![Step::Stuck(Vec::new())],
    );
    let mut term = Term::new(server.path());
    term.sync().unwrap();
    assert!(matches!(term.send(b"a"), Err(LinkError::Busy(_))));
    let reads = server.received().len();
    term.deadline(Instant::now());
    assert!(matches!(term.send(b"b"), Err(LinkError::Busy(_))));
    assert_eq!(server.received().len(), reads);
    assert_eq!(server.inputs(), [b"a".to_vec()]);
}
