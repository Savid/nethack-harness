//! Golden views: every screen fixture rendered by `Play::show` must match `tests/goldens/<fixture>.txt`.
//!
//! Run with `GOLDENS_WRITE=1` to rewrite the goldens from the current view instead of comparing; review the diff.

mod support;

use nethack_harness::play::Play;
use support::{Fixture, FixtureTerm, fixture_names, goldens_dir};

/// The text a fresh game shows for fixture `name`, headed by `exit <code>` when the reply's code is not 0.
fn render(name: &str) -> String {
    let mut play = Play::new(FixtureTerm::new(Fixture::load(name).view()), 64);
    let reply = play.show();
    match reply.code.code() {
        0 => reply.text,
        code => format!("exit {code}\n{}", reply.text),
    }
}

#[test]
fn every_fixture_renders_its_golden() {
    let write = std::env::var_os("GOLDENS_WRITE").is_some();
    let mut failures = Vec::new();
    for name in fixture_names() {
        let text = render(&name);
        let path = goldens_dir().join(format!("{name}.txt"));
        if write {
            std::fs::write(&path, format!("{}\n", text.trim_end())).expect("write golden");
            continue;
        }
        let golden = std::fs::read_to_string(&path).expect("read golden");
        if text.trim_end() != golden.trim_end() {
            failures.push(format!("--- {name}: expected\n{golden}\n--- got\n{text}\n"));
        }
    }
    assert!(
        failures.is_empty(),
        "{} fixtures differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn every_fixture_has_a_golden() {
    for name in fixture_names() {
        assert!(
            goldens_dir().join(format!("{name}.txt")).is_file(),
            "no golden for {name}"
        );
    }
    assert_eq!(fixture_names().len(), 59);
}

#[test]
fn fixtures_load_into_grids() {
    for name in fixture_names() {
        let fixture = Fixture::load(&name);
        let grid = fixture.grid();
        let mut row = String::new();
        grid.row_text(23, &mut row);
        assert_eq!(row.trim_end(), fixture.rows[23].trim_end(), "{name}");
    }
}
