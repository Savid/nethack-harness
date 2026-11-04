//! The daemon's socket binding. It lives in its own test binary because a child process spawned concurrently by
//! another test briefly holds a copy of every listening socket, which would make the socket look live.

mod support;

use nethack_harness::daemon::bind;
use support::scratch;

#[test]
fn a_stale_socket_file_is_replaced_and_a_live_one_kept() {
    let dir = scratch("daemon-bind");
    let socket = dir.join("sock");
    let first = bind(&socket).unwrap();
    assert_eq!(
        bind(&socket).unwrap_err().kind(),
        std::io::ErrorKind::AddrInUse
    );
    // A live daemon keeps its socket even when a connect to it would fail, as with a full queue.
    std::fs::remove_file(&socket).unwrap();
    assert_eq!(
        bind(&socket).unwrap_err().kind(),
        std::io::ErrorKind::AddrInUse
    );
    drop(first);
    let second = bind(&socket).unwrap();
    drop(second);
    assert!(socket.exists());
    let _third = bind(&socket).unwrap();
}
