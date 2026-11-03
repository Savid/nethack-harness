//! Memory over recorded screens: every fixture is taken in, engravings are told from boulders by colour, an
//! inventory menu and the end of the game are captured, and a farlook names the pet.

mod support;

use nethack_harness::observe::{EntityKind, LookSource, Memory};
use nethack_harness::types::Pos;
use support::{fixture_names, load_view};

#[test]
fn every_fixture_is_taken_in() {
    let mut out = Vec::new();
    for name in fixture_names() {
        let view = load_view(&name);
        let mut memory = Memory::new();
        memory.ingest(&view);
        memory.ingest(&view);
        memory.entities(&view, &mut out);
        if view.normal {
            assert!(memory.current().is_some(), "{name}");
        }
        if !view.normal || view.engulfed {
            assert!(out.is_empty(), "{name}");
        }
        assert!(out.iter().all(|e| Some(e.pos) != view.hero), "{name}");
        assert!(memory.message_total() <= 1, "{name}");
    }
}

#[test]
fn a_bright_blue_backtick_is_an_engraving_and_any_other_a_boulder() {
    let view = load_view("seed1001-0150");
    let mut memory = Memory::new();
    memory.ingest(&view);
    let mut out = Vec::new();
    memory.entities(&view, &mut out);
    let backticks: Vec<(Pos, EntityKind)> = out
        .iter()
        .filter(|e| e.glyph == '`')
        .map(|e| (e.pos, e.kind))
        .collect();
    assert_eq!(backticks, [(Pos::new(7, 38), EntityKind::Object)]);
}

#[test]
fn the_inventory_menu_is_captured() {
    let mut memory = Memory::new();
    memory.ingest(&load_view("seed4242-start"));
    memory.ingest(&load_view("seed4242-inventory"));
    let inventory = memory.inventory().cloned().unwrap_or_default();
    assert_eq!(inventory.turn, Some(1));
    assert_eq!(inventory.lines.len(), 12);
    assert_eq!(inventory.lines[0], "$ - 1665 gold pieces");
    assert_eq!(
        inventory.lines[1],
        "a - a +0 scalpel (weapon in right hand)"
    );
    assert_eq!(inventory.lines[11], "c - an uncursed stethoscope");
}

#[test]
fn the_disclosure_question_begins_the_end() {
    let mut memory = Memory::new();
    memory.ingest(&load_view("seed4242-start"));
    assert!(memory.end().is_none());
    memory.ingest(&load_view("seed4242-quit"));
    let end = memory.end().cloned().unwrap_or_default();
    assert!(memory.end().is_some());
    assert_eq!(end.death, None);
    assert!(end.lines.iter().all(|line| !line.contains("identified")));
}

#[test]
fn a_farlook_of_the_pet_names_it() {
    let mut memory = Memory::new();
    let before = load_view("seed4242-start");
    memory.ingest(&before);
    let after = load_view("seed4242-farlook-more");
    let dog = Pos::new(6, 19);
    assert_eq!(before.ch(dog), 'd');
    memory.remember_look(dog, &before, &after, LookSource::Farlook);
    let seen = memory
        .current()
        .and_then(|level| level.inspections.get(&dog))
        .map(|i| i.description.clone());
    assert_eq!(seen.as_deref(), Some("tame little dog"));
    assert!(memory.about_pet("You swap places with your little dog."));
}
