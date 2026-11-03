//! Taking screens into memory: which level the hero is on, the messages, what the game says underfoot, the
//! entities in view, inventory menus and end-of-game text.

use crate::glyphs::Shade;
use crate::level::Level;
use crate::screen::View;
use crate::types::{End, Inventory, Pos};

/// Messages kept for the view.
pub const MESSAGES: usize = 20;

/// One message the game wrote.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Message {
    /// The turn it appeared.
    pub turn: Option<u32>,
    /// Its text.
    pub text: String,
}

/// What a screen square shows, for the view and the stop rules.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EntityKind {
    /// A monster symbol.
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
    /// Its square.
    pub pos: Pos,
    /// Its glyph.
    pub glyph: char,
    /// Its colour.
    pub shade: Shade,
    /// What it is.
    pub kind: EntityKind,
    /// Highlighted as the hero's pet.
    pub pet: bool,
    /// A monster glyph a farlook named as a statue.
    pub statue: bool,
}

/// Where a reading of a square came from.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LookSource {
    /// A farlook (`;`) of another square.
    Farlook,
    /// A look underfoot (`:`).
    Here,
}

/// Memory across screens: levels, messages, notes, pets, inventory and the end of the game.
pub struct Memory {
    levels: Vec<Level>,
    current: Option<usize>,
    origin: Option<(usize, Pos, Option<i32>)>,
    visit: Option<(u16, Pos, Option<u32>)>,
    messages: [Option<Message>; MESSAGES],
    message_total: u64,
    aside: bool,
    aside_message: Option<String>,
    notes: Vec<String>,
    noted: Vec<String>,
    pets: Vec<String>,
    inventory: Option<Inventory>,
    reading_inventory: bool,
    end: Option<End>,
}

impl Default for Memory {
    fn default() -> Memory {
        Memory::new()
    }
}

impl Memory {
    /// Nothing remembered.
    pub fn new() -> Memory {
        todo!()
    }

    /// Takes in a screen: messages, notes, inventory and end text always; level identity, terrain, underfoot
    /// notes and visits when the map is shown. A level change is a new status-line location, and it reconnects a
    /// known level when the hero left from, or arrives on or beside, a stair glyph matching a known stair link.
    pub fn ingest(&mut self, view: &View) {
        todo!()
    }

    /// Marks the start of a command: the level, square and Dlvl the hero leaves from, for level identity.
    pub fn begin(&mut self, view: &View) {
        todo!()
    }

    /// While on, messages shown are kept out of the remembered messages: the answers to the harness's own
    /// information commands.
    pub fn set_aside(&mut self, on: bool) {
        todo!()
    }

    /// The level the hero is on.
    pub fn current(&self) -> Option<&Level> {
        self.current.and_then(|i| self.levels.get(i))
    }

    /// The level the hero is on, mutably.
    pub fn current_mut(&mut self) -> Option<&mut Level> {
        self.current.and_then(|i| self.levels.get_mut(i))
    }

    /// How many messages have been remembered in total.
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
        todo!()
    }

    /// Records the game's description of `target` from a farlook or `:` answered on `after`, with `before` the
    /// screen it was asked from; a statue or a tame name is remembered too.
    pub fn remember_look(&mut self, target: Pos, before: &View, after: &View, source: LookSource) {
        todo!()
    }

    /// The entities in a normal-play view, row then column, written into `out`.
    pub fn entities(&self, view: &View, out: &mut Vec<Entity>) {
        todo!()
    }

    /// The last inventory menu the game showed.
    pub fn inventory(&self) -> Option<&Inventory> {
        self.inventory.as_ref()
    }

    /// The end-of-game text captured so far.
    pub fn end(&self) -> Option<&End> {
        self.end.as_ref()
    }
}

/// Whether a sentence is something the game says about the hero's square: `There is … here`, `You see here …`,
/// an engraving read-out (`Something is written here …`, `You read: "…"`) or a trap at the hero.
pub fn underfoot_sentence(sentence: &str) -> bool {
    todo!()
}

/// Whether a sentence is an engraving read-out.
pub fn engraving_sentence(sentence: &str) -> bool {
    todo!()
}
