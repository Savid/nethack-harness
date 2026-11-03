//! Taking screens into memory: which level the hero is on, the messages, what the game says underfoot, the
//! entities in view, inventory menus and end-of-game text.

use crate::glyphs::{
    Shade, find_word, is_engraving_text, is_floor_engraving, is_item, is_monster, is_warning,
    trap_kind,
};
use crate::level::{Inspection, Level, Plus, Underfoot, plus_kind};
use crate::screen::{EndKind, MenuEntry, Phase, View, byte_at, strip_more};
use crate::types::{COLS, End, Inventory, MAP_BOTTOM, MAP_TOP, Pos, ROWS};

/// Messages kept for the view.
pub const MESSAGES: usize = 20;
/// Most notes gathered about one square before they are placed, and most kept for one look underfoot.
const NOTES: usize = 32;
/// Most tame names remembered; the oldest is forgotten beyond this.
const PETS: usize = 16;
/// Most levels remembered under one location label: branches share dungeon level numbers, and arrivals that no
/// stair connects (a trap door, a level teleport) start a new copy; the oldest copy beyond this is forgotten.
const LEVELS_PER_LABEL: usize = 3;
/// Most end-of-game lines kept.
const END_LINES: usize = 64;
/// Most inventory lines kept from one menu.
const INVENTORY_LINES: usize = 128;
/// Most menu pages an inventory read tracks.
const INVENTORY_PAGES: u32 = 64;

/// The headings the game puts over each object class in an inventory menu.
#[rustfmt::skip]
const HEADINGS: [&str; 17] = [
    "Illegal objects", "Weapons", "Armor", "Rings", "Amulets", "Tools", "Comestibles", "Potions", "Scrolls",
    "Spellbooks", "Wands", "Coins", "Gems/Stones", "Boulders/Statues", "Iron balls", "Chains", "Venoms",
];

/// How the game reports writing, adding to or wiping out the engraving under the hero.
#[rustfmt::skip]
const ENGRAVING_CHANGES: [&str; 10] = [
    "You write in the ", "You engrave in the ", "You engrave on the ", "You burn into the ", "You melt into the ",
    "You scribble on the ", "You scrawl on the ", "You add to the ", "You wipe out the message",
    "The engraving now reads: ",
];

/// The empty-inventory answer to an inventory display.
const EMPTY_INVENTORY: &str = "Not carrying anything.";

/// One message the game wrote, and the turn it appeared.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message {
    pub turn: Option<u32>,
    pub text: String,
}

/// What a screen square shows, for the view and the stop rules.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntityKind {
    Monster,
    /// `I` or a warning digit: something sensed but not seen.
    Unseen,
    /// An object, a boulder, or a statue drawn as a monster.
    Object,
    /// A `+` that may be a closed door or a spellbook.
    DoorOrObject,
}

/// One entity in view.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Entity {
    pub pos: Pos,
    pub glyph: char,
    pub shade: Shade,
    pub kind: EntityKind,
    /// Highlighted as the hero's pet.
    pub pet: bool,
    /// A monster glyph a farlook named as a statue.
    pub statue: bool,
}

/// Where a reading of a square came from: a farlook (`;`) of another square or a look underfoot (`:`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LookSource {
    Farlook,
    Here,
}

/// A stair use seen: leaving level `from` at square `at` led to level `to` at square `arrival`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct StairLink {
    from: usize,
    at: Pos,
    to: usize,
    arrival: Pos,
}

/// Memory across screens: levels, messages, notes, pets, inventory and the end of the game.
#[derive(Default)]
pub struct Memory {
    levels: Vec<Level>,
    /// The id of the last level added.
    last_id: u16,
    current: Option<usize>,
    links: Vec<StairLink>,
    /// The level and square the hero was last seen on in normal play.
    origin: Option<(usize, Pos)>,
    /// The level, square and turn of the last counted visit.
    visit: Option<(u16, Pos, Option<u32>)>,
    /// The last turn the status line showed.
    turn: Option<u32>,
    messages: [Option<Message>; MESSAGES],
    message_total: u64,
    aside: bool,
    aside_message: Option<String>,
    /// Notes not yet placed at the hero.
    notes: Vec<String>,
    /// Notes placed at the hero's square since it last changed, or since `begin` or `set_aside(true)`.
    reading: Vec<String>,
    /// The message or text-window lines the notes were last gathered from.
    noted: Vec<String>,
    /// Scratch for text-window lines.
    window: Vec<String>,
    /// The game reported writing or wiping an engraving under the hero since the notes were last placed.
    rewritten: bool,
    pets: Vec<String>,
    inventory: Option<Inventory>,
    reading_inventory: bool,
    inventory_pages: u64,
    inventory_total: u32,
    inventory_turn: Option<u32>,
    inventory_lines: Vec<(u32, String)>,
    entries: Vec<MenuEntry>,
    end: Option<End>,
    end_screen: Option<u64>,
    end_window: bool,
}

impl Memory {
    /// Takes in a screen: messages, notes, inventory and end text always; level identity, terrain, underfoot
    /// notes and visits when the map is shown. A level change is a new status-line location, and it reconnects a
    /// known level when the hero left from, or arrives on or beside, a stair glyph matching a known stair link.
    pub fn ingest(&mut self, view: &View) {
        if view.status.turn.is_some() {
            self.turn = view.status.turn;
        }
        let phase = view.phase();
        self.remember_message(view);
        self.collect_notes(view);
        self.read_inventory(view, phase);
        self.read_end(view);
        if phase == Phase::Play {
            self.observe_map(view);
        }
    }

    /// Marks the start of a command: the level and square the hero leaves from, for level identity, and the start
    /// of what a look underfoot reads.
    pub fn begin(&mut self, view: &View) {
        self.reading.clear();
        if view.phase() != Phase::Play || view.engulfed {
            return;
        }
        if let (Some(index), Some(hero)) = (self.current, view.hero)
            && self.levels.get(index).is_some_and(|level| {
                view.status
                    .location
                    .as_deref()
                    .is_none_or(|location| level.label == location)
            })
        {
            self.origin = Some((index, hero));
        }
    }

    /// While on, messages shown are kept out of the remembered messages: the answers to the harness's own
    /// information commands.
    pub fn set_aside(&mut self, on: bool) {
        if on && !self.aside {
            self.reading.clear();
        }
        self.aside = on;
    }

    /// The level the hero is on.
    pub fn current(&self) -> Option<&Level> {
        self.current.and_then(|i| self.levels.get(i))
    }

    pub fn current_mut(&mut self) -> Option<&mut Level> {
        self.current.and_then(|i| self.levels.get_mut(i))
    }

    /// How many messages have been remembered in all.
    pub fn message_total(&self) -> u64 {
        self.message_total
    }

    /// The remembered messages after the first `since` of all time, oldest first, at most `MESSAGES`.
    pub fn messages_since(&self, since: u64) -> impl Iterator<Item = &Message> {
        let wanted = self
            .message_total
            .saturating_sub(since)
            .min(MESSAGES as u64) as usize;
        let start = (self.message_total as usize).wrapping_sub(wanted);
        (0..wanted).filter_map(move |k| self.messages[(start + k) % MESSAGES].as_ref())
    }

    /// Whether every sentence of `message` is about a pet the game has named as tame.
    pub fn about_pet(&self, message: &str) -> bool {
        let mut any = false;
        let mut all = true;
        let mut count = 0;
        for sentence in sentences(strip_more(message)) {
            count += 1;
            let about = self
                .pets
                .iter()
                .any(|name| sentence.contains(name.as_str()));
            any |= about;
            all &= about || sentence == "You stop.";
        }
        count > 0 && any && all
    }

    /// Records the game's description of `target` from a farlook or `:` answered on `after`, with `before` the
    /// screen it was asked from; a statue or a tame name is remembered too.
    ///
    /// A `:` describes the whole square, so the notes it produced (those placed at the square since the last
    /// `begin` or `set_aside(true)`, and any still pending) replace what was remembered there.
    pub fn remember_look(&mut self, target: Pos, before: &View, after: &View, source: LookSource) {
        if !matches!(after.phase(), Phase::Play | Phase::More)
            || after.getpos
            || before.engulfed
            || after.engulfed
        {
            return;
        }
        let Some(index) = self.current else {
            return;
        };
        let turn = before.status.turn;
        if source == LookSource::Here {
            self.replace_underfoot(index, target, turn);
        }
        let message = strip_more(&after.msg).trim();
        let text = match source {
            LookSource::Farlook => last_parenthesised(message).unwrap_or(message),
            LookSource::Here => message,
        }
        .trim();
        if text.is_empty() {
            return;
        }
        let glyph = before.ch(target);
        let shade = before.shade(target);
        let Some(level) = self.levels.get_mut(index) else {
            return;
        };
        level.inspections.insert(
            target,
            Inspection {
                description: text.to_owned(),
                turn,
                glyph,
                shade,
            },
        );
        if source != LookSource::Farlook {
            return;
        }
        if is_monster(glyph) && find_word(message, "statue of").is_some() {
            level.statues.insert(target, (glyph, shade));
        }
        if let Some(tame) = text.strip_prefix("tame ") {
            match tame.split_once(" called ") {
                Some((kind, name)) => {
                    self.remember_pet(kind);
                    self.remember_pet(name);
                }
                None => self.remember_pet(tame),
            }
        }
    }

    /// The entities in a normal-play view, row then column, written into `out`.
    pub fn entities(&self, view: &View, out: &mut Vec<Entity>) {
        out.clear();
        if !view.normal || view.engulfed {
            return;
        }
        let level = self.current();
        for y in MAP_TOP..=MAP_BOTTOM {
            for x in 0..COLS as u8 {
                let pos = Pos::new(y, x);
                if Some(pos) == view.hero {
                    continue;
                }
                let glyph = view.ch(pos);
                if !(glyph == '+'
                    || glyph == 'I'
                    || glyph == '0'
                    || glyph == '`'
                    || is_monster(glyph)
                    || ('1'..='5').contains(&glyph)
                    || is_item(glyph))
                {
                    continue;
                }
                let shade = view.shade(pos);
                let plus = (glyph == '+').then(|| plus_kind(view, level, pos));
                let statue = level
                    .and_then(|l| l.statues.get(&pos))
                    .is_some_and(|&seen| seen == (glyph, shade));
                let kind = match plus {
                    Some(Plus::Object) => EntityKind::Object,
                    Some(Plus::DoorOrObject) => EntityKind::DoorOrObject,
                    Some(Plus::Door) => continue,
                    None if statue => EntityKind::Object,
                    None if is_monster(glyph) => EntityKind::Monster,
                    None if glyph == 'I' || is_warning(glyph, shade) => EntityKind::Unseen,
                    None if is_item(glyph) || glyph == '0' => EntityKind::Object,
                    None if glyph == '`' && !is_floor_engraving(glyph, shade) => EntityKind::Object,
                    None => continue,
                };
                out.push(Entity {
                    pos,
                    glyph,
                    shade,
                    kind,
                    pet: view.pet(pos),
                    statue,
                });
            }
        }
    }

    pub fn inventory(&self) -> Option<&Inventory> {
        self.inventory.as_ref()
    }

    pub fn end(&self) -> Option<&End> {
        self.end.as_ref()
    }

    /// Keeps a new message, unless it is the answer to an information command made aside.
    fn remember_message(&mut self, view: &View) {
        let msg = view.msg.as_str();
        if !self.aside && self.aside_message.as_deref() != Some(msg) {
            self.aside_message = None;
        }
        let last = self
            .message_total
            .checked_sub(1)
            .and_then(|n| self.messages[(n % MESSAGES as u64) as usize].as_ref());
        let new = !msg.is_empty()
            && !view.counting()
            && self.aside_message.as_deref() != Some(msg)
            && last.is_none_or(|m| m.text != msg);
        if !new {
            return;
        }
        if self.aside {
            self.aside_message = Some(msg.to_owned());
            return;
        }
        let slot = &mut self.messages[(self.message_total % MESSAGES as u64) as usize];
        match slot {
            Some(message) => {
                message.turn = view.status.turn;
                message.text.clear();
                message.text.push_str(msg);
            }
            None => {
                *slot = Some(Message {
                    turn: view.status.turn,
                    text: msg.to_owned(),
                })
            }
        }
        self.message_total += 1;
    }

    /// Gathers what the game says about the hero's square, from the message or a text window ending in
    /// `--More--`, until the next map screen places it at the hero.
    fn collect_notes(&mut self, view: &View) {
        self.window.clear();
        let window = view.more && view.more_window(&mut self.window);
        if window {
            if self.noted == self.window {
                return;
            }
            std::mem::swap(&mut self.noted, &mut self.window);
        } else {
            if self.noted.len() == 1 && self.noted[0] == view.msg {
                return;
            }
            self.noted.clear();
            self.noted.push(view.msg.clone());
        }
        let mut items = false;
        for line in &self.noted {
            let line = if window {
                line.trim()
            } else {
                strip_more(line).trim()
            };
            if items && !line.is_empty() {
                push_bounded(&mut self.notes, line);
                continue;
            }
            items = line == "Things that are here:";
            for sentence in sentences(line) {
                if engraving_change(sentence) {
                    self.rewritten = true;
                } else if underfoot_sentence(sentence) {
                    push_bounded(&mut self.notes, sentence);
                }
            }
        }
    }

    /// Captures an inventory menu, page by page; it is kept once every page has been shown.
    fn read_inventory(&mut self, view: &View, phase: Phase) {
        if strip_more(&view.msg).trim() == EMPTY_INVENTORY {
            self.reading_inventory = false;
            self.inventory = Some(Inventory {
                turn: self.turn,
                lines: Vec::new(),
            });
            return;
        }
        if phase != Phase::Menu {
            self.reading_inventory = false;
            return;
        }
        let (page, pages) = view.menu_page().unwrap_or((1, 1));
        if page == 0 || page > pages || pages > INVENTORY_PAGES {
            self.reading_inventory = false;
            return;
        }
        if !self.reading_inventory || pages != self.inventory_total {
            if page != 1 || !inventory_menu(view) {
                self.reading_inventory = false;
                return;
            }
            self.reading_inventory = true;
            self.inventory_pages = 0;
            self.inventory_total = pages;
            self.inventory_turn = self.turn;
            self.inventory_lines.clear();
        }
        let bit = 1u64 << (page - 1);
        if self.inventory_pages & bit != 0 {
            return;
        }
        self.inventory_pages |= bit;
        self.entries.clear();
        view.menu_entries(&mut self.entries);
        for entry in &self.entries {
            if self.inventory_lines.len() < INVENTORY_LINES {
                self.inventory_lines
                    .push((page, format!("{} - {}", entry.key, entry.text)));
            }
        }
        if self.inventory_pages == u64::MAX >> (64 - pages) {
            self.inventory_lines.sort_by_key(|&(page, _)| page);
            let lines = self
                .inventory_lines
                .drain(..)
                .map(|(_, line)| line)
                .collect();
            self.inventory = Some(Inventory {
                turn: self.inventory_turn,
                lines,
            });
            self.reading_inventory = false;
        }
    }

    /// Captures end-of-game text: the messages of the final turn, later messages, the tombstone's death line and
    /// the farewell window.
    fn read_end(&mut self, view: &View) {
        if self.end.is_none() {
            if !view.ended {
                return;
            }
            let mut end = End::default();
            for message in self.messages_since(0) {
                let text = strip_more(&message.text).trim();
                if message.turn == self.turn && !text.is_empty() && !question(text) {
                    push_line(&mut end.lines, text);
                }
            }
            self.end = Some(end);
        }
        let print = view.fingerprint();
        if self.end_screen == Some(print) {
            return;
        }
        self.end_screen = Some(print);
        let Some(end) = self.end.as_mut() else {
            return;
        };
        match view.end {
            Some(EndKind::Disclosure) => {}
            Some(EndKind::Tombstone | EndKind::Farewell) => {
                self.end_window = true;
                end_window(view, end);
            }
            Some(EndKind::Ascended) | None => {
                if self.end_window && view.more {
                    end_window(view, end);
                } else {
                    let text = strip_more(&view.msg).trim();
                    if !text.is_empty() && !question(text) {
                        push_line(&mut end.lines, text);
                    }
                }
            }
        }
    }

    /// Takes in a map screen: level identity, terrain, the notes about the hero's square, visits.
    fn observe_map(&mut self, view: &View) {
        let Some(hero) = view.hero else {
            return;
        };
        let location = view.status.location.as_deref();
        let changed = match (self.current, location) {
            (None, _) => true,
            (Some(i), Some(label)) => self.levels.get(i).is_none_or(|l| l.label != label),
            (Some(_), None) => false,
        };
        if changed {
            self.enter(view, hero, location.unwrap_or(""));
        } else if let (Some(label), Some(level)) = (location, self.current_mut())
            && level.label.is_empty()
        {
            level.label.push_str(label);
        }
        if view.engulfed {
            return;
        }
        let Some(index) = self.current else {
            return;
        };
        if self.origin != Some((index, hero)) {
            self.reading.clear();
        }
        self.origin = Some((index, hero));
        let turn = view.status.turn;
        let Some(level) = self.levels.get_mut(index) else {
            return;
        };
        level.observe(view);
        if self.rewritten {
            self.rewritten = false;
            if let Some(underfoot) = level.underfoot.get_mut(&hero) {
                underfoot.notes.retain(|note| !is_engraving_text(note));
                underfoot.turn = None;
            }
            level.engravings.insert(hero);
        }
        if !self.notes.is_empty() {
            if self.notes.iter().any(|note| is_engraving_text(note)) {
                if let Some(underfoot) = level.underfoot.get_mut(&hero) {
                    underfoot.notes.retain(|note| !is_engraving_text(note));
                }
                level.engravings.insert(hero);
            }
            level.note_underfoot(hero, &self.notes, turn);
            for note in self.notes.drain(..) {
                if self.reading.len() < NOTES {
                    self.reading.push(note);
                }
            }
        }
        let visit = (level.id, hero, turn);
        if self.visit != Some(visit) {
            *level.visits.entry(hero).or_insert(0) += 1;
            self.visit = Some(visit);
        }
    }

    /// Moves to the level at `label`: the known level a stair link or remembered stair connects, or a new one.
    fn enter(&mut self, view: &View, hero: Pos, label: &str) {
        if let Some(level) = self.current_mut() {
            level.clear_refused();
        }
        let from = self.origin.filter(|&(i, _)| Some(i) == self.current);
        let mut found = None;
        let mut stairs = false;
        if let Some((old, at)) = from {
            let departure = self
                .levels
                .get(old)
                .and_then(|l| l.terrain(at))
                .filter(|&c| c == '<' || c == '>');
            found = self.connected(old, at, hero, label, departure);
            stairs = departure.is_some()
                || found.is_some()
                || hero
                    .neighbours()
                    .any(|(_, p)| matches!(view.ch(p), '<' | '>'));
        }
        let index = match found {
            Some(index) => index,
            None => self.add_level(label),
        };
        self.current = Some(index);
        if let (Some((old, at)), true) = (from, stairs) {
            match self
                .links
                .iter_mut()
                .find(|l| l.from == old && l.at == at && l.to == index)
            {
                Some(link) => link.arrival = hero,
                None => self.links.push(StairLink {
                    from: old,
                    at,
                    to: index,
                    arrival: hero,
                }),
            }
        }
    }

    /// Adds a new level labelled `label` and returns its index. Beyond `LEVELS_PER_LABEL` levels with that label,
    /// the oldest that is not the current level is replaced, with the stair links that lead to or from it.
    fn add_level(&mut self, label: &str) -> usize {
        self.last_id = self.last_id.saturating_add(1);
        let id = self.last_id;
        let mut same = self
            .levels
            .iter()
            .enumerate()
            .filter(|&(i, level)| level.label == label && Some(i) != self.current);
        let oldest = same
            .clone()
            .min_by_key(|(_, level)| level.id)
            .map(|(i, _)| i);
        match oldest {
            Some(index) if same.nth(LEVELS_PER_LABEL - 1).is_some() => {
                self.links.retain(|l| l.from != index && l.to != index);
                if self.origin.is_some_and(|(i, _)| i == index) {
                    self.origin = None;
                }
                self.levels[index] = Level::new(id, label);
                index
            }
            _ => {
                self.levels.push(Level::new(id, label));
                self.levels.len() - 1
            }
        }
    }

    /// The known level labelled `label` that leaving level `old` at `at` and arriving at `hero` connects: the same
    /// stair taken before, the stair that brought the hero back, or a level remembering the opposite stair at or
    /// beside the arrival square.
    fn connected(
        &self,
        old: usize,
        at: Pos,
        hero: Pos,
        label: &str,
        departure: Option<char>,
    ) -> Option<usize> {
        let labelled = |i: usize| self.levels.get(i).is_some_and(|l| l.label == label);
        if let Some(link) = self
            .links
            .iter()
            .find(|l| l.from == old && l.at == at && labelled(l.to))
        {
            return Some(link.to);
        }
        if let Some(link) = self.links.iter().find(|l| {
            l.to == old
                && l.arrival.chebyshev(at) <= 1
                && l.at.chebyshev(hero) <= 1
                && labelled(l.from)
        }) {
            return Some(link.from);
        }
        let opposite = match departure? {
            '<' => '>',
            _ => '<',
        };
        self.levels.iter().enumerate().position(|(i, level)| {
            i != old
                && level.label == label
                && (level.terrain(hero) == Some(opposite)
                    || hero
                        .neighbours()
                        .any(|(_, p)| level.terrain(p) == Some(opposite)))
        })
    }

    /// Makes the notes of a look underfoot the whole of what is remembered about `target`.
    fn replace_underfoot(&mut self, index: usize, target: Pos, turn: Option<u32>) {
        for note in self.notes.drain(..) {
            if self.reading.len() < NOTES {
                self.reading.push(note);
            }
        }
        self.rewritten = false;
        let Some(level) = self.levels.get_mut(index) else {
            return;
        };
        level.underfoot.remove(&target);
        if self.reading.is_empty() {
            level.underfoot.insert(
                target,
                Underfoot {
                    notes: Vec::new(),
                    turn,
                },
            );
        } else {
            level.note_underfoot(target, &self.reading, turn);
            level.underfoot.entry(target).or_default().turn = turn;
        }
        if self.reading.iter().any(|note| is_engraving_text(note)) {
            level.engravings.insert(target);
        } else {
            level.engravings.remove(target);
        }
    }

    fn remember_pet(&mut self, name: &str) {
        let name = name.trim();
        if name.is_empty() || self.pets.iter().any(|p| p == name) {
            return;
        }
        if self.pets.len() >= PETS {
            self.pets.remove(0);
        }
        self.pets.push(name.to_owned());
    }
}

/// Whether a sentence is something the game says about the hero's square: `There is … here`, `You see here …`,
/// an engraving read-out (`Something is written here …`, `You read: "…"`) or a trap at the hero.
pub fn underfoot_sentence(sentence: &str) -> bool {
    let sentence = sentence.trim();
    let bare = sentence.strip_suffix('.').unwrap_or(sentence);
    ((sentence.starts_with("There is ") || sentence.starts_with("There are "))
        && bare.ends_with(" here"))
        || sentence
            .strip_prefix("You see here ")
            .is_some_and(|rest| !rest.is_empty())
        || is_engraving_text(sentence)
        || trap_kind(sentence).is_some()
}

/// Whether a sentence reports the engraving under the hero being written, added to or wiped out.
fn engraving_change(sentence: &str) -> bool {
    ENGRAVING_CHANGES
        .iter()
        .any(|change| sentence.starts_with(change))
        || (sentence.starts_with("The engraving on the ") && sentence.contains(" vanishes"))
}

/// Whether the menu on screen is an inventory listing: no title, the window opening with an object-class
/// heading, and lettered entries. An object prompt's listing of only the items it accepts is left out.
fn inventory_menu(view: &View) -> bool {
    let (left, rows) = view.menu_window();
    if view.rows[0][..byte_at(&view.rows[0], left)].contains("?*]") {
        return false;
    }
    let first = view.rows[..rows.min(ROWS)]
        .iter()
        .map(|row| row[byte_at(row, left)..].trim())
        .find(|line| !line.is_empty());
    let Some(first) = first else {
        return false;
    };
    let heading = first.strip_prefix("Unpaid ").unwrap_or(first);
    HEADINGS.iter().any(|name| {
        heading
            .strip_prefix(name)
            .is_some_and(|rest| rest.is_empty() || rest.starts_with(' '))
    })
}

/// Captures the lines of an end-of-game text window: the tombstone's death line and every other text line.
fn end_window(view: &View, end: &mut End) {
    let peace = view.rows.iter().position(|row| row.contains("PEACE"));
    if let Some(r) = peace
        && end.death.is_none()
    {
        let mut death = String::new();
        for row in view.rows.iter().skip(r + 4).take(4) {
            let face = row
                .find('|')
                .zip(row.rfind('|'))
                .filter(|(a, b)| a < b)
                .map(|(a, b)| row[a + 1..b].trim())
                .unwrap_or("");
            if !face.is_empty() {
                if !death.is_empty() {
                    death.push(' ');
                }
                death.push_str(face);
            }
        }
        if !death.is_empty() {
            end.death = Some(death);
        }
    }
    let stone = peace.map(|r| r.saturating_sub(4)..=r + 10);
    for (r, row) in view.rows.iter().enumerate() {
        if stone.as_ref().is_some_and(|s| s.contains(&r)) {
            continue;
        }
        let text = strip_more(row).trim();
        if text.is_empty() || end.lines.iter().any(|line| line == text) {
            continue;
        }
        if end.death.is_none()
            && text.starts_with("You ")
            && text.contains(" with ")
            && (text.ends_with(" points,") || text.ends_with(" point,"))
        {
            end.death = Some(text.to_owned());
        }
        push_line(&mut end.lines, text);
    }
}

/// Whether a message is a question awaiting a bracketed answer.
fn question(text: &str) -> bool {
    text.contains("? [")
}

fn push_line(lines: &mut Vec<String>, text: &str) {
    if lines.len() < END_LINES && lines.last().is_none_or(|last| last != text) {
        lines.push(text.to_owned());
    }
}

fn push_bounded(notes: &mut Vec<String>, note: &str) {
    if notes.len() < NOTES {
        notes.push(note.to_owned());
    }
}

/// The text inside the last pair of parentheses holding no others: a farlook's `(floating eye)`.
fn last_parenthesised(message: &str) -> Option<&str> {
    let mut end = message.len();
    while let Some(close) = message[..end].rfind(')') {
        if let Some(open) = message[..close].rfind(['(', ')'])
            && message[open..].starts_with('(')
        {
            return Some(&message[open + 1..close]);
        }
        end = close;
    }
    None
}

/// The sentences of a message: each ends at `.`, `!` or `?` (or a closing quote after one) followed by white
/// space, never inside a quotation, so an engraving read-out stays whole.
fn sentences(text: &str) -> impl Iterator<Item = &str> {
    let mut at = 0;
    std::iter::from_fn(move || {
        let rest = text.get(at..)?.trim_start();
        if rest.is_empty() {
            at = text.len();
            return None;
        }
        let start = text.len() - rest.len();
        let bytes = text.as_bytes();
        let mut quoted = false;
        for i in start..bytes.len() {
            let b = bytes[i];
            if b == b'"' {
                quoted = !quoted;
            }
            let ends = !quoted
                && (matches!(b, b'.' | b'!' | b'?')
                    || (b == b'"' && i > start && matches!(bytes[i - 1], b'.' | b'!' | b'?')));
            if ends && bytes.get(i + 1).is_some_and(u8::is_ascii_whitespace) {
                at = i + 1;
                return Some(&text[start..=i]);
            }
        }
        at = text.len();
        Some(text[start..].trim_end())
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::screen::PromptContext;
    use crate::testkit::{STATUS1, STATUS2, grid, screen, view};
    use crate::types::{Colour, Grid};

    const ROOM: [&str; 3] = [" ---- ", " |.@.| ", " ---- "];
    const HERO: Pos = Pos::new(2, 3);

    fn status2(location: &str, turn: u32) -> String {
        format!("{location} $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:{turn}")
    }

    /// A map screen with `top` as the message and the cursor on the hero at (`y`, `x`).
    fn map(top: &str, rows: &[&str], y: u8, x: u8) -> View {
        view(&screen(top, rows, STATUS1, STATUS2), y, x)
    }

    /// A map screen on `location` at `turn`.
    fn map_at(top: &str, rows: &[&str], location: &str, turn: u32, y: u8, x: u8) -> View {
        view(&screen(top, rows, STATUS1, &status2(location, turn)), y, x)
    }

    /// A room screen with the hero at (2, 3) on turn `turn`.
    fn room(top: &str, turn: u32) -> View {
        map_at(top, &ROOM, "Dlvl:3", turn, 2, 3)
    }

    /// `rows` with the given cells recoloured.
    fn coloured(rows: &[&str], cells: &[(Pos, Colour, bool, bool)], y: u8, x: u8) -> View {
        let mut g: Grid = grid(&screen("", rows, STATUS1, STATUS2));
        for &(p, fg, bold, rev) in cells {
            let cell = &mut g.cells[p.y as usize][p.x as usize];
            cell.fg = fg;
            cell.bold = bold;
            cell.rev = rev;
        }
        View::parse(&g, Pos::new(y, x))
    }

    fn notes(memory: &Memory, p: Pos) -> Vec<String> {
        memory
            .current()
            .and_then(|level| level.underfoot.get(&p))
            .map(|u| u.notes.clone())
            .unwrap_or_default()
    }

    fn texts(memory: &Memory, since: u64) -> Vec<String> {
        memory
            .messages_since(since)
            .map(|m| m.text.clone())
            .collect()
    }

    #[test]
    fn sentences_keep_a_quoted_engraving_whole() {
        let text = "Something is written here in the dust.  You read: \"Ad aerarium. Vale!\".  \
                    You read: \"Hi!\"  You see here a dagger.";
        let found: Vec<&str> = sentences(text).collect();
        assert_eq!(
            found,
            [
                "Something is written here in the dust.",
                "You read: \"Ad aerarium. Vale!\".",
                "You read: \"Hi!\"",
                "You see here a dagger.",
            ]
        );
        assert_eq!(sentences("  ").count(), 0);
        assert_eq!(
            sentences("You stop. Hachi is in the way").collect::<Vec<_>>(),
            ["You stop.", "Hachi is in the way"]
        );
    }

    #[test]
    fn engraving_read_outs_are_underfoot_sentences() {
        for read in [
            "Something is written here in the dust.",
            "Something is written here in the frost.",
            "Something is engraved here on the floor.",
            "Some text has been burned into the floor here.",
            "There's some graffiti on the floor here.",
            "You see a message scrawled in blood here.",
            "You read: \"Elbereth\".",
            "You feel the words: \"Elbereth\".",
        ] {
            assert!(is_engraving_text(read), "{read}");
            assert!(underfoot_sentence(read), "{read}");
        }
        for here in [
            "There is a staircase down here.",
            "There are several objects here.",
            "You see here a dagger.",
        ] {
            assert!(underfoot_sentence(here), "{here}");
            assert!(!is_engraving_text(here), "{here}");
        }
        assert!(underfoot_sentence("There is a dart trap here."));
        assert!(!underfoot_sentence("You hit the jackal!"));
        assert!(!underfoot_sentence("You see here"));
    }

    #[test]
    fn messages_are_kept_in_a_ring_without_repeats() {
        let mut memory = Memory::default();
        for n in 0..30 {
            let screen = room(&format!("Message {n}"), 400 + n);
            memory.ingest(&screen);
            memory.ingest(&screen);
        }
        assert_eq!(memory.message_total(), 30);
        let all = texts(&memory, 0);
        assert_eq!(all.len(), MESSAGES);
        assert_eq!(all[0], "Message 10");
        assert_eq!(all[19], "Message 29");
        assert_eq!(texts(&memory, 28), ["Message 28", "Message 29"]);
        assert!(texts(&memory, 30).is_empty());
        let last = memory.messages_since(29).next().map(|m| m.turn);
        assert_eq!(last, Some(Some(429)));
    }

    #[test]
    fn answers_shown_aside_are_not_remembered() {
        let mut memory = Memory::default();
        memory.ingest(&room("You hit the jackal.", 400));
        memory.set_aside(true);
        memory.ingest(&room("e  a floating eye (floating eye)", 400));
        memory.set_aside(false);
        memory.ingest(&room("e  a floating eye (floating eye)", 400));
        assert_eq!(texts(&memory, 0), ["You hit the jackal."]);
        memory.ingest(&room("", 400));
        memory.ingest(&room("e  a floating eye (floating eye)", 401));
        assert_eq!(memory.message_total(), 2);
    }

    #[test]
    fn engraving_read_outs_are_kept_underfoot_and_a_newer_one_replaces_the_old() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        memory.ingest(&room(
            "Something is written here in the dust.  You read: \"Elbereth\".",
            401,
        ));
        memory.ingest(&room("You see here a dagger.", 401));
        let kept = notes(&memory, HERO);
        assert!(kept.contains(&"Something is written here in the dust.".to_owned()));
        assert!(kept.contains(&"You read: \"Elbereth\".".to_owned()));
        assert!(kept.contains(&"You see here a dagger.".to_owned()));
        assert!(
            memory
                .current()
                .is_some_and(|l| l.engravings.contains(HERO))
        );

        memory.ingest(&room(
            "Something is written here in the dust.  You read: \"Elb?re?h\".",
            420,
        ));
        let kept = notes(&memory, HERO);
        assert!(kept.contains(&"You read: \"Elb?re?h\".".to_owned()));
        assert!(!kept.contains(&"You read: \"Elbereth\".".to_owned()));
        assert!(kept.contains(&"You see here a dagger.".to_owned()));
    }

    #[test]
    fn writing_or_wiping_an_engraving_drops_the_old_reading() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        memory.ingest(&room(
            "Something is written here in the dust.  You read: \"Elbereth\".",
            401,
        ));
        memory.ingest(&room(
            "You wipe out the message that was written in the dust here.",
            402,
        ));
        memory.ingest(&room("You write in the dust with your fingertip.", 402));
        let level = memory.current();
        let underfoot = level.and_then(|l| l.underfoot.get(&HERO));
        assert!(underfoot.is_some_and(|u| u.notes.iter().all(|n| !is_engraving_text(n))));
        assert_eq!(underfoot.and_then(|u| u.turn), None);
        assert!(level.is_some_and(|l| l.engravings.contains(HERO)));
    }

    #[test]
    fn a_look_underfoot_replaces_what_was_remembered_there() {
        let mut memory = Memory::default();
        let before = room("", 400);
        memory.ingest(&before);
        memory.ingest(&room(
            "Something is written here in the dust.  You read: \"Elbereth\".",
            401,
        ));
        let before = room("", 430);
        memory.ingest(&before);
        memory.begin(&before);
        let after = room("You see here a dagger.", 430);
        memory.ingest(&after);
        memory.remember_look(HERO, &before, &after, LookSource::Here);
        let level = memory.current();
        let underfoot = level.and_then(|l| l.underfoot.get(&HERO));
        assert_eq!(
            underfoot.map(|u| u.notes.clone()),
            Some(vec!["You see here a dagger.".to_owned()])
        );
        assert_eq!(underfoot.and_then(|u| u.turn), Some(430));
        assert!(level.is_some_and(|l| !l.engravings.contains(HERO)));

        // A look that says nothing about the square leaves an empty reading with its turn.
        let before = room("", 440);
        memory.ingest(&before);
        memory.begin(&before);
        let after = room("You see no objects here.", 440);
        memory.ingest(&after);
        memory.remember_look(HERO, &before, &after, LookSource::Here);
        let underfoot = memory.current().and_then(|l| l.underfoot.get(&HERO));
        assert_eq!(underfoot.map(|u| u.notes.len()), Some(0));
        assert_eq!(underfoot.and_then(|u| u.turn), Some(440));
    }

    #[test]
    fn what_the_game_says_is_underfoot_is_kept_for_that_square() {
        let mut memory = Memory::default();
        memory.ingest(&map("", &ROOM, 2, 3));
        memory.ingest(&map(
            "There is a staircase down here.  You see here a dagger.",
            &ROOM,
            2,
            3,
        ));
        assert_eq!(memory.current().and_then(|l| l.terrain(HERO)), Some('>'));
        let kept = notes(&memory, HERO);
        assert!(kept.contains(&"There is a staircase down here.".to_owned()));
        assert!(kept.contains(&"You see here a dagger.".to_owned()));
        let pad = " ".repeat(30);
        let window = [
            format!("{pad}a dagger"),
            format!("{pad}2 apples"),
            format!("{pad}--More--"),
        ];
        let rows: Vec<&str> = window.iter().map(String::as_str).collect();
        memory.ingest(&map(&format!("{pad}Things that are here:"), &rows, 3, 38));
        memory.ingest(&map("", &ROOM, 2, 3));
        let kept = notes(&memory, HERO);
        assert!(kept.contains(&"a dagger".to_owned()));
        assert!(kept.contains(&"2 apples".to_owned()));
    }

    #[test]
    fn a_trap_reported_by_a_message_is_remembered_where_the_hero_stands() {
        let rows = [" ------- ", " |.@...| ", " ------- "];
        let mut memory = Memory::default();
        memory.ingest(&map("", &rows, 2, 3));
        memory.ingest(&map(
            "A little dart shoots out at you!  You are hit by a little dart.",
            &rows,
            2,
            3,
        ));
        let level = memory.current();
        assert_eq!(level.and_then(|l| l.terrain(HERO)), Some('^'));
        assert_eq!(level.and_then(|l| l.kind(HERO)), Some("dart trap"));

        let mut memory = Memory::default();
        memory.ingest(&map("", &rows, 2, 3));
        memory.ingest(&map("There is a dart trap here.--More--", &[], 0, 34));
        memory.ingest(&map("", &rows, 2, 3));
        assert_eq!(
            memory.current().and_then(|l| l.kind(HERO)),
            Some("dart trap")
        );
    }

    fn inventory_lines(memory: &Memory) -> Option<Vec<String>> {
        memory.inventory().map(|i| i.lines.clone())
    }

    #[test]
    fn an_inventory_menu_is_captured_whoever_opened_it() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        let menu = [
            format!("{:24}a - a pair of gloves", ""),
            format!("{:24}b - a robe", "    ------"),
            format!("{:24}Comestibles", "    |.@..|"),
            format!("{:24}e - 3 food rations", "    |....|"),
            format!("{:24}f - 6 apples", " z - map fragment"),
            format!("{:24}(end)", ""),
        ];
        let rows: Vec<&str> = menu.iter().map(String::as_str).collect();
        memory.ingest(&map(&format!("{:24}Armor", ""), &rows, 7, 30));
        assert_eq!(
            inventory_lines(&memory),
            Some(
                [
                    "a - a pair of gloves",
                    "b - a robe",
                    "e - 3 food rations",
                    "f - 6 apples"
                ]
                .map(str::to_owned)
                .to_vec()
            )
        );
        assert_eq!(memory.inventory().and_then(|i| i.turn), Some(400));
    }

    #[test]
    fn an_inventory_is_kept_only_once_every_page_was_shown() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        let first = map("Weapons", &[" a - a dagger", " (1 of 2)"], 2, 10);
        let second = map("Comestibles", &[" b - an apple", " (2 of 2)"], 2, 10);
        memory.ingest(&first);
        memory.ingest(&first);
        assert_eq!(memory.inventory(), None);
        memory.ingest(&second);
        assert_eq!(
            inventory_lines(&memory),
            Some(vec!["a - a dagger".to_owned(), "b - an apple".to_owned()])
        );

        // A second reading abandoned after its first page keeps the earlier one.
        memory.ingest(&room("", 420));
        memory.ingest(&first);
        memory.ingest(&room("", 420));
        assert_eq!(
            memory.inventory().map(|i| (i.turn, i.lines.len())),
            Some((Some(400), 2))
        );

        memory.ingest(&room("Not carrying anything.", 430));
        assert_eq!(
            memory.inventory().map(|i| (i.turn, i.lines.len())),
            Some((Some(430), 0))
        );
    }

    #[test]
    fn a_menu_of_other_things_is_not_an_inventory() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        memory.ingest(&map(
            "Pick up what?",
            &["Weapons", " a - a dagger", " (end)"],
            3,
            10,
        ));
        memory.ingest(&map(
            "Currently known spells",
            &[" a - force bolt", " (end)"],
            2,
            10,
        ));
        assert_eq!(memory.inventory(), None);
    }

    /// The tombstone page of the end-of-game window.
    fn tombstone() -> Vec<String> {
        let stone = [
            "",
            "                       ----------",
            "                      /          \\",
            "                     /    REST    \\",
            "                    /      IN      \\",
            "                   /     PEACE      \\",
            "                  /                  \\",
            "                  |       Hero       |",
            "                  |       0 Au       |",
            "                  |   killed by a    |",
            "                  |  jackal, while   |",
            "                  |    fainted       |",
            "                  |                  |",
            "                  |       2026       |",
            "                 *|     *  *  *      | *",
            "        _________)/\\\\_//(\\/(/\\)/\\//\\/|_)_______",
            "",
            "",
            "Goodbye Hero the Stripling...",
            "",
            "You died in The Dungeons of Doom on dungeon level 3 with 12 points,",
            "and 0 pieces of gold, after 400 moves.",
            "--More--",
        ];
        let mut rows: Vec<String> = stone.iter().map(|s| (*s).to_owned()).collect();
        rows.resize(ROWS, String::new());
        rows
    }

    #[test]
    fn the_end_of_the_game_is_captured_verbatim() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 399));
        memory.ingest(&map_at(
            "The jackal bites!  You die...--More--",
            &ROOM,
            "Dlvl:3",
            400,
            0,
            37,
        ));
        assert_eq!(memory.end(), None);
        let disclosure = map_at(
            "Do you want your possessions identified? [ynq] (n)",
            &ROOM,
            "Dlvl:3",
            400,
            0,
            51,
        );
        memory.ingest(&disclosure);
        memory.ingest(&disclosure);
        assert_eq!(
            memory.end().map(|e| e.lines.clone()),
            Some(vec!["The jackal bites!  You die...".to_owned()])
        );
        memory.ingest(&view(&tombstone(), 22, 8));
        let mut last: Vec<String> = vec![String::new(); ROWS];
        last[0] = "You were level 1 with a maximum of 16 hit points when you died.".to_owned();
        last[2] = "--More--".to_owned();
        memory.ingest(&view(&last, 2, 8));
        let end = memory.end();
        assert_eq!(
            end.and_then(|e| e.death.clone()),
            Some("killed by a jackal, while fainted".to_owned())
        );
        assert_eq!(
            end.map(|e| e.lines.clone()),
            Some(
                [
                    "The jackal bites!  You die...",
                    "Goodbye Hero the Stripling...",
                    "You died in The Dungeons of Doom on dungeon level 3 with 12 points,",
                    "and 0 pieces of gold, after 400 moves.",
                    "You were level 1 with a maximum of 16 hit points when you died.",
                ]
                .map(str::to_owned)
                .to_vec()
            )
        );
    }

    #[test]
    fn without_a_tombstone_the_summary_line_names_the_end() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        let mut rows = vec![String::new(); ROWS];
        rows[0] = "Goodbye Hero the Stripling...".to_owned();
        rows[2] = "You quit in The Dungeons of Doom on dungeon level 3 with 12 points,".to_owned();
        rows[3] = "--More--".to_owned();
        memory.ingest(&view(&rows, 3, 8));
        assert_eq!(
            memory.end().and_then(|e| e.death.clone()),
            Some("You quit in The Dungeons of Doom on dungeon level 3 with 12 points,".to_owned())
        );
    }

    fn level_id(memory: &Memory) -> Option<u16> {
        memory.current().map(|l| l.id)
    }

    #[test]
    fn a_stair_trip_and_back_returns_to_the_remembered_level() {
        let upper = [" -------- ", " |..{@>.| ", " -------- "];
        let on_stairs = [" -------- ", " |..{.@.| ", " -------- "];
        let lower = [" ---- ", " |.@.| ", " ---- "];
        let mut memory = Memory::default();
        memory.ingest(&map_at("", &upper, "Dlvl:3", 400, 2, 5));
        let leaving = map_at("", &on_stairs, "Dlvl:3", 401, 2, 6);
        memory.ingest(&leaving);
        memory.begin(&leaving);
        assert_eq!(
            memory.current().and_then(|l| l.terrain(Pos::new(2, 6))),
            Some('>')
        );
        let arrived = map_at("", &lower, "Dlvl:4", 402, 2, 3);
        memory.ingest(&arrived);
        assert_eq!(level_id(&memory), Some(2));
        memory.begin(&arrived);
        memory.ingest(&map_at("", &on_stairs, "Dlvl:3", 403, 2, 6));
        assert_eq!(level_id(&memory), Some(1));
        memory.ingest(&map_at("", &lower, "Dlvl:4", 404, 2, 3));
        assert_eq!(level_id(&memory), Some(2));
        memory.ingest(&map_at("", &on_stairs, "Dlvl:3", 405, 2, 6));
        assert_eq!(level_id(&memory), Some(1));
        assert_eq!(memory.levels.len(), 2);
    }

    #[test]
    fn up_stairs_find_a_level_by_its_remembered_down_stair() {
        let upper = [" -------- ", " |@...>.| ", " -------- "];
        let lower = [" ------ ", " |.@<.| ", " ------ "];
        let on_up = [" ------ ", " |..@.| ", " ------ "];
        let back = [" -------- ", " |....@.| ", " -------- "];
        let mut memory = Memory::default();
        memory.ingest(&map_at("", &upper, "Dlvl:3", 400, 2, 2));
        memory.ingest(&map_at(
            "There's a trap door here.",
            &lower,
            "Dlvl:4",
            401,
            2,
            3,
        ));
        assert_eq!(level_id(&memory), Some(2));
        memory.ingest(&map_at("", &on_up, "Dlvl:4", 402, 2, 4));
        memory.ingest(&map_at("", &back, "Dlvl:3", 403, 2, 6));
        assert_eq!(level_id(&memory), Some(1));
    }

    #[test]
    fn a_new_location_reached_without_stairs_is_a_new_level() {
        let room3 = [" ------- ", " |.@...| ", " ------- "];
        let mut memory = Memory::default();
        memory.ingest(&map_at("", &room3, "Dlvl:3", 400, 2, 3));
        memory.ingest(&map_at("", &room3, "Dlvl:5", 401, 2, 3));
        memory.ingest(&map_at("", &room3, "Dlvl:3", 402, 2, 3));
        assert_eq!(level_id(&memory), Some(3));
    }

    #[test]
    fn arrivals_no_stair_connects_keep_a_bounded_number_of_levels() {
        let room = [" ------- ", " |.@...| ", " ------- "];
        let mut memory = Memory::default();
        for turn in 0..200 {
            let label = if turn % 2 == 0 { "Dlvl:3" } else { "Dlvl:4" };
            memory.ingest(&map_at("", &room, label, 400 + turn, 2, 3));
        }
        assert_eq!(memory.levels.len(), 2 * LEVELS_PER_LABEL);
        assert_eq!(level_id(&memory), Some(200));
        let mut ids: Vec<u16> = memory.levels.iter().map(|l| l.id).collect();
        ids.sort_unstable();
        assert_eq!(ids, [195, 196, 197, 198, 199, 200]);
    }

    #[test]
    fn each_location_is_its_own_level_and_prompts_keep_the_current_one() {
        let mut memory = Memory::default();
        let prompt = map("Really attack the peaceful gnome? [yn] (n)", &[], 0, 40);
        memory.ingest(&prompt);
        assert!(memory.current().is_none());
        let labels = [
            "Home 1",
            "Home 2",
            "Earth",
            "Air",
            "Fire",
            "Water",
            "Astral Plane",
        ];
        for label in labels {
            memory.ingest(&map_at("", &ROOM, label, 400, 2, 3));
            assert_eq!(memory.current().map(|l| l.label.as_str()), Some(label));
        }
        let seen: Vec<&str> = memory.levels.iter().map(|l| l.label.as_str()).collect();
        assert_eq!(seen, labels);
        memory.ingest(&prompt);
        assert_eq!(
            memory.current().map(|l| l.label.as_str()),
            Some("Astral Plane")
        );
    }

    #[test]
    fn an_engulfed_hero_keeps_memory_without_new_spatial_facts() {
        let swallowed = map_at(
            "You are swallowed!",
            &["  /-\\", "  |@|", "  \\-/"],
            "Dlvl:3",
            401,
            2,
            3,
        );
        assert!(swallowed.engulfed);
        let mut memory = Memory::default();
        memory.ingest(&swallowed);
        assert_eq!(memory.current().map(|l| l.known().count()), Some(0));
        let mut out = Vec::new();
        memory.entities(&swallowed, &mut out);
        assert!(out.is_empty());
        assert_eq!(texts(&memory, 0), ["You are swallowed!"]);
        let normal = map_at(
            "",
            &[" ------- ", " |.@.>| ", " ------- "],
            "Dlvl:3",
            402,
            2,
            3,
        );
        memory.ingest(&normal);
        assert_eq!(level_id(&memory), Some(1));
        assert_eq!(
            memory.current().and_then(|l| l.terrain(Pos::new(2, 5))),
            Some('>')
        );
        let known = memory.current().map(|l| l.known().count());
        memory.ingest(&swallowed);
        memory.remember_look(HERO, &swallowed, &normal, LookSource::Here);
        assert_eq!(memory.current().map(|l| l.known().count()), known);
        assert!(memory.current().is_some_and(|l| l.inspections.is_empty()));
    }

    #[test]
    fn entities_are_monsters_objects_and_sensed_things_but_not_engravings() {
        let rows = [
            " ---------- ",
            " |.@eI3`)`| ",
            " |..0..0..| ",
            " ---------- ",
        ];
        let shown = coloured(
            &rows,
            &[
                (Pos::new(2, 9), Colour::Blue, true, false),
                (Pos::new(3, 7), Colour::White, false, false),
            ],
            2,
            3,
        );
        let mut memory = Memory::default();
        memory.ingest(&shown);
        let mut out = Vec::new();
        memory.entities(&shown, &mut out);
        let found: Vec<(Pos, char, EntityKind)> =
            out.iter().map(|e| (e.pos, e.glyph, e.kind)).collect();
        assert_eq!(
            found,
            [
                (Pos::new(2, 4), 'e', EntityKind::Monster),
                (Pos::new(2, 5), 'I', EntityKind::Unseen),
                (Pos::new(2, 6), '3', EntityKind::Unseen),
                (Pos::new(2, 7), '`', EntityKind::Object),
                (Pos::new(2, 8), ')', EntityKind::Object),
                (Pos::new(3, 4), '0', EntityKind::Object),
                (Pos::new(3, 7), '0', EntityKind::Unseen),
            ]
        );
    }

    #[test]
    fn a_spellbook_on_the_floor_is_an_object_and_a_door_in_a_wall_is_not() {
        let rows = [" ---+--- ", " |.@.+.| ", " |.....| ", " ------- "];
        let shown = map("", &rows, 2, 3);
        let mut memory = Memory::default();
        memory.ingest(&shown);
        let mut out = Vec::new();
        memory.entities(&shown, &mut out);
        let found: Vec<(Pos, EntityKind)> = out.iter().map(|e| (e.pos, e.kind)).collect();
        assert_eq!(found, [(Pos::new(2, 5), EntityKind::Object)]);
    }

    #[test]
    fn a_farlook_records_its_description_and_an_interrupted_one_does_not() {
        let rows = [" ------- ", " |.@e..| ", " ------- "];
        let target = Pos::new(2, 4);
        let mut memory = Memory::default();
        let before = map("", &rows, 2, 3);
        memory.ingest(&before);
        let interrupted = map("Pick a monster, object or location.--More--", &[], 0, 50);
        memory.remember_look(target, &before, &interrupted, LookSource::Farlook);
        assert!(memory.current().is_some_and(|l| l.inspections.is_empty()));
        let after = map("e  a floating eye (floating eye)", &rows, 2, 3);
        memory.remember_look(target, &before, &after, LookSource::Farlook);
        let seen = memory
            .current()
            .and_then(|l| l.inspections.get(&target))
            .cloned();
        assert_eq!(
            seen.as_ref().map(|i| i.description.as_str()),
            Some("floating eye")
        );
        assert_eq!(seen.as_ref().and_then(|i| i.turn), Some(400));
        assert_eq!(seen.map(|i| i.glyph), Some('e'));
    }

    #[test]
    fn a_farlooked_statue_is_an_object_and_tame_names_identify_the_pet() {
        let rows = [" ------- ", " |.@.d.| ", " ------- "];
        let target = Pos::new(2, 5);
        let mut memory = Memory::default();
        let before = map("", &rows, 2, 3);
        memory.ingest(&before);
        memory.remember_look(
            target,
            &before,
            &map("d        a statue of a jackal", &rows, 2, 3),
            LookSource::Farlook,
        );
        let mut out = Vec::new();
        memory.entities(&before, &mut out);
        assert_eq!(out.len(), 1);
        assert_eq!((out[0].kind, out[0].statue), (EntityKind::Object, true));
        memory.remember_look(
            target,
            &before,
            &map(
                "d        a dog (tame little dog called Hachi) [seen: normal vision]",
                &rows,
                2,
                3,
            ),
            LookSource::Farlook,
        );
        assert!(memory.about_pet("You swap places with Hachi."));
        assert!(memory.about_pet("You stop.  Your little dog is in the way!--More--"));
        assert!(!memory.about_pet("The little dog bites the jackal.  The jackal bites!"));
        assert!(!memory.about_pet(""));
    }

    #[test]
    fn a_square_is_visited_once_per_turn() {
        let mut memory = Memory::default();
        memory.ingest(&room("", 400));
        memory.ingest(&room("", 400));
        memory.ingest(&room("You hear a door open.", 400));
        memory.ingest(&room("", 401));
        assert_eq!(memory.current().and_then(|l| l.visits.get(&HERO)), Some(&2));
    }

    #[test]
    fn a_targeting_cursor_does_not_update_the_map_or_visits() {
        let mut memory = Memory::default();
        let mut prompts = PromptContext::default();
        let mut first = map("", &ROOM, 2, 3);
        prompts.observe(&mut first);
        memory.ingest(&first);
        let known: Vec<(Pos, char)> = memory
            .current()
            .map(|l| l.known().collect())
            .unwrap_or_default();
        let mut target = map("Move cursor to the desired position:", &ROOM, 0, 36);
        prompts.observe(&mut target);
        memory.ingest(&target);
        prompts.before_input(&target, b"l");
        let mut moved = map("floor of a room [03,05]", &ROOM, 2, 4);
        prompts.observe(&mut moved);
        memory.ingest(&moved);
        assert_eq!(moved.phase(), Phase::Position);
        let now: Vec<(Pos, char)> = memory
            .current()
            .map(|l| l.known().collect())
            .unwrap_or_default();
        assert_eq!(now, known);
        assert_eq!(memory.current().map(|l| l.visits.len()), Some(1));
    }
}
