//! Golden parity: every screen fixture rendered by `Play::show` must match `tests/goldens/<fixture>.txt`, except
//! the fixtures `tests/goldens/DIFFERENCES.md` lists under a `### <fixture>` heading with the reason.

mod support;

use nethack_harness::play::Play;
use support::{Fixture, FixtureTerm, fixture_names, goldens_dir};

/// The fixtures `DIFFERENCES.md` excuses.
fn excused() -> Vec<String> {
    let text = std::fs::read_to_string(goldens_dir().join("DIFFERENCES.md")).unwrap_or_default();
    text.lines()
        .filter_map(|line| line.strip_prefix("### "))
        .map(|name| name.trim().trim_matches('`').to_owned())
        .collect()
}

#[test]
#[ignore = "runs once the view renders"]
fn every_fixture_renders_its_golden() {
    let excused = excused();
    let mut failures = Vec::new();
    for name in fixture_names() {
        let golden = std::fs::read_to_string(goldens_dir().join(format!("{name}.txt")))
            .expect("read golden");
        let mut play = Play::new(FixtureTerm::new(Fixture::load(&name).view()), 64);
        let reply = play.show();
        let text = if reply.code.code() == 0 {
            reply.text
        } else {
            format!("error: {}", reply.text)
        };
        if text.trim_end() != golden.trim_end() && !excused.contains(&name) {
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
