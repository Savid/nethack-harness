//! Level memory and routing over recorded screens.

mod support;

use nethack_harness::glyphs::{Shade, is_floor_engraving, shade};
use nethack_harness::level::{Level, Paths, Plus, plus_kind};
use nethack_harness::screen::{Status, View};
use nethack_harness::types::{Grid, MAP_SQUARES, Pos, SquareSet};
use support::{Fixture, fixture_names};

/// The fixture's screen in normal play, with the hero on the cursor when the cursor is on `@`.
fn view_of(fixture: &Fixture) -> View {
    let grid: Grid = fixture.grid();
    let cursor = fixture.cursor();
    let hero = (grid.cell(cursor).ch == '@').then_some(cursor);
    View {
        rows: std::array::from_fn(|r| {
            let mut row = String::new();
            grid.row_text(r, &mut row);
            row
        }),
        grid,
        cursor,
        message_rows: 1,
        status: Status {
            turn: Some(1),
            ..Status::default()
        },
        hero,
        normal: true,
        ..View::default()
    }
}

fn observed(name: &str) -> (Level, View) {
    let view = view_of(&Fixture::load(name));
    let mut level = Level::new(1, "Dlvl:1");
    level.observe(&view);
    if let Some(hero) = view.hero {
        level.visits.insert(hero, 1);
    }
    (level, view)
}

fn search(level: &Level, start: Pos) -> Paths {
    let mut paths = Paths::default();
    level.paths(start, &SquareSet::new(), &mut paths);
    paths
}

fn route(paths: &Paths, target: Pos) -> Option<Vec<Pos>> {
    let mut out = Vec::new();
    paths.route(target, &mut out).then_some(out)
}

#[test]
fn routes_never_cross_remembered_lava() {
    let (level, view) = observed("lava-room");
    let hero = view.hero.unwrap_or_default();
    assert_eq!(hero, Pos::new(11, 57));
    let liquid: Vec<Pos> = level
        .known()
        .filter(|&(_, ch)| ch == '}')
        .map(|(p, _)| p)
        .collect();
    assert_eq!(liquid.len(), 9);
    assert!(liquid.iter().all(|&p| level.kind(p) == Some("lava")));
    assert!(
        level.guarded(Pos::new(11, 56)),
        "the square west of the hero"
    );
    let paths = search(&level, hero);
    assert!(paths.reached().iter().count() > 10);
    for p in paths.reached().iter() {
        let path = route(&paths, p).unwrap_or_default();
        assert!(
            !path.iter().any(|q| liquid.contains(q)),
            "route to {p} crosses liquid"
        );
    }
    assert_eq!(
        route(&paths, Pos::new(5, 34)).and_then(|r| r.first().copied()),
        Some(Pos::new(12, 56))
    );
}

#[test]
fn routes_cross_a_recorded_trap_only_without_another_route() {
    let (level, view) = observed("trap-room");
    let hero = view.hero.unwrap_or_default();
    let trap = Pos::new(8, 59);
    assert!(level.guarded(trap) && level.kind(trap) == Some("trap"));
    let paths = search(&level, hero);
    assert_eq!(route(&paths, trap), Some(vec![trap]));
    let south = route(&paths, Pos::new(19, 60)).unwrap_or_default();
    assert_eq!(south.len(), 12);
    assert!(!south.contains(&trap));
}

#[test]
fn a_plus_at_a_corridor_end_is_entered_only_cardinally() {
    let (level, view) = observed("live-corridor-plus");
    let hero = view.hero.unwrap_or_default();
    assert_eq!(hero, Pos::new(15, 25));
    let plus = Pos::new(14, 26);
    assert_eq!(plus_kind(&view, Some(&level), plus), Plus::DoorOrObject);
    assert!(level.maybe_doors.contains(plus));
    assert_eq!(level.terrain(Pos::new(13, 19)), Some('+'));
    let paths = search(&level, hero);
    assert_eq!(
        route(&paths, plus),
        Some(vec![Pos::new(14, 25), plus]),
        "no diagonal step onto a possible door"
    );
    // The default-coloured ` beside the hero is a boulder.
    assert_eq!(level.terrain(Pos::new(15, 24)), Some('`'));
    assert!(!level.passable(Pos::new(15, 24)));
}

#[test]
fn every_recorded_backtick_is_classified_by_colour() {
    let (mut engraved, mut boulders) = (0, 0);
    for name in fixture_names() {
        let (level, view) = observed(&name);
        for i in 0..MAP_SQUARES {
            let p = Pos::from_index(i);
            let cell = view.grid.cell(p);
            if cell.ch != '`' || Some(p) == view.hero {
                continue;
            }
            if is_floor_engraving(cell.ch, shade(cell.fg, cell.bold)) {
                engraved += 1;
                assert_eq!(level.terrain(p), Some('.'), "{name} {p}");
                assert!(level.engravings.contains(p), "{name} {p}");
                assert!(level.passable(p), "{name} {p}");
            } else {
                boulders += 1;
                assert_eq!(level.terrain(p), Some('`'), "{name} {p}");
                assert!(!level.engravings.contains(p), "{name} {p}");
                assert!(!level.passable(p), "{name} {p}");
            }
        }
    }
    assert!(engraved > 0 && boulders > 0);
}

#[test]
fn engraved_floor_in_a_recorded_room_is_routed_through() {
    let (level, view) = observed("seed1001-0150");
    let engraved = Pos::new(7, 44);
    assert_eq!(
        shade(view.grid.cell(engraved).fg, view.grid.cell(engraved).bold),
        Shade::BRIGHT_BLUE
    );
    let start = Pos::new(7, 43);
    let paths = search(&level, start);
    assert_eq!(route(&paths, Pos::new(7, 46)).map(|r| r.len()), Some(3));
    assert_eq!(
        route(&paths, Pos::new(7, 46)),
        Some(vec![engraved, Pos::new(7, 45), Pos::new(7, 46)])
    );
}

#[test]
fn every_recorded_screen_is_remembered_without_panicking() {
    for name in fixture_names() {
        let (level, view) = observed(&name);
        if let Some(hero) = view.hero {
            let paths = search(&level, hero);
            let mut out = Vec::new();
            level.frontier_targets(&paths, &mut out);
            assert!(out.windows(2).all(|w| w[0] != w[1]), "{name}");
        }
    }
}
