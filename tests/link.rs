//! The terminal protocol against the fake server: framing, cursors, keep-alive, refusals, busy reads, and the
//! screen kept in step with the game.

mod support;

use nethack_harness::link::{Exchange, Link, LinkError, Term, Terminal, WAIT_MS};
use support::scratch;
use support::server::{FakeTerminalServer, Step};

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
