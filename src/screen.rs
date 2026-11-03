//! Parsing one 80×24 tty screen: the message line, prompts, `--More--`, menus, end-of-game text, the status
//! lines, the hero and map cells.

use crate::glyphs::{Conditions, Shade};
use crate::types::{Colour, Grid, Pos, ROWS};

/// What the game is waiting for, judged from the screen.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// End-of-game text or questions.
    Ended,
    /// A `--More--` page.
    More,
    /// A direction prompt.
    Direction,
    /// An object prompt, such as `What do you want to eat? [fg or ?*]`.
    Item,
    /// A spell prompt.
    Spell,
    /// A choice prompt with bracketed answers, such as `[yn]`.
    Choice,
    /// A free-text prompt.
    Text,
    /// A menu or text window with a `(end)` or `(n of m)` footer.
    Menu,
    /// A cursor-positioning prompt.
    Position,
    /// The map, waiting for a command.
    Play,
    /// None of these.
    Unknown,
}

impl Phase {
    /// The name the view prints, such as `menu` or `game over`.
    pub fn name(self) -> &'static str {
        todo!()
    }
}

/// The kind of end-of-game screen shown.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EndKind {
    /// `Do you want your possessions identified?` and the other disclosure questions.
    Disclosure,
    /// The tombstone (`REST IN PEACE`).
    Tombstone,
    /// The farewell line (`Goodbye …`, `Fare thee well …`, `Be seeing you…`).
    Farewell,
    /// The ascension line.
    Ascended,
}

/// The fields of the two status lines.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Status {
    /// The location word, such as `Dlvl:3`, `Home 1` or `Astral Plane`.
    pub location: Option<String>,
    /// Dungeon level number.
    pub dlvl: Option<i32>,
    /// Gold.
    pub gold: Option<i32>,
    /// Hit points.
    pub hp: Option<i32>,
    /// Maximum hit points.
    pub hpmax: Option<i32>,
    /// Power.
    pub pw: Option<i32>,
    /// Maximum power.
    pub pwmax: Option<i32>,
    /// Armour class.
    pub ac: Option<i32>,
    /// Experience level.
    pub xl: Option<i32>,
    /// Game turn.
    pub turn: Option<u32>,
    /// Strength as shown, such as `18/50`.
    pub strength: Option<String>,
    /// Dexterity.
    pub dex: Option<i32>,
    /// Constitution.
    pub con: Option<i32>,
    /// Intelligence.
    pub int: Option<i32>,
    /// Wisdom.
    pub wis: Option<i32>,
    /// Charisma.
    pub cha: Option<i32>,
    /// Alignment word.
    pub alignment: Option<&'static str>,
}

/// One parsed screen.
#[derive(Clone, Debug)]
pub struct View {
    /// The cells.
    pub grid: Grid,
    /// Each row's 80 characters.
    pub rows: [String; ROWS],
    /// The cursor as (row, column).
    pub cursor: Pos,
    /// The message: row 0 plus up to two wrapped continuation rows, trimmed.
    pub msg: String,
    /// How many rows the message spans (1 to 3).
    pub message_rows: u8,
    /// A `--More--` is on screen.
    pub more: bool,
    /// A menu footer `(end)` or `(n of m)` ends a row.
    pub menu: bool,
    /// The cursor waits on the message rows with no `--More--`: a question is live.
    pub asking: bool,
    /// The bracketed answers of a live choice prompt, such as `yn` or `ynq`.
    pub yn: Option<String>,
    /// The bracketed letters of a live object prompt.
    pub obj: Option<String>,
    /// The bracketed letters of a live spell prompt.
    pub spell: Option<String>,
    /// A live free-text prompt.
    pub text: bool,
    /// A live direction prompt.
    pub direction: bool,
    /// A cursor-positioning prompt.
    pub getpos: bool,
    /// End-of-game text is on screen.
    pub ended: bool,
    /// Which end-of-game screen, when `ended`.
    pub end: Option<EndKind>,
    /// Status-line fields.
    pub status: Status,
    /// Status-line conditions.
    pub cond: Conditions,
    /// Any prompt is open.
    pub prompt: bool,
    /// The hero's square.
    pub hero: Option<Pos>,
    /// The hero is engulfed: the corners around it read `/ \ \ /`.
    pub engulfed: bool,
    /// The map waits for a command with the hero shown.
    pub normal: bool,
}

impl View {
    /// Parses a screen.
    pub fn parse(grid: &Grid, cursor: Pos) -> View {
        todo!()
    }

    /// Re-parses into this view, reusing its buffers.
    pub fn reparse(&mut self, grid: &Grid, cursor: Pos) {
        todo!()
    }

    /// The character at a map square, or a space off the map.
    pub fn ch(&self, p: Pos) -> char {
        todo!()
    }

    /// The stored foreground colour at a map square, or the default off the map.
    pub fn fg(&self, p: Pos) -> Colour {
        todo!()
    }

    /// The game's colour at a map square; gray off the map.
    pub fn shade(&self, p: Pos) -> Shade {
        todo!()
    }

    /// Whether a map square is highlighted as the hero's pet.
    pub fn pet(&self, p: Pos) -> bool {
        todo!()
    }

    /// What the game is waiting for.
    pub fn phase(&self) -> Phase {
        todo!()
    }

    /// The screen as text: rows with trailing spaces removed, joined by newlines.
    pub fn text_screen(&self, out: &mut String) {
        todo!()
    }

    /// A cheap hash of cells and cursor, equal for identical screens.
    pub fn fingerprint(&self) -> u64 {
        todo!()
    }

    /// The menu page shown, as (page, pages): `(n of m)`, or (1, 1) for `(end)`.
    pub fn menu_page(&self) -> Option<(u32, u32)> {
        todo!()
    }

    /// The menu window's bounds as (left column, rows): rows `0..rows` cut at the left column hold the window,
    /// ending at its footer; (0, 24) when no footer is shown.
    pub fn menu_window(&self) -> (usize, usize) {
        todo!()
    }

    /// The selectable entries of a menu on screen, appended to `out`.
    pub fn menu_entries(&self, out: &mut Vec<MenuEntry>) {
        todo!()
    }

    /// The lines of a text window that ends in `--More--`, appended to `out`; false when `--More--` follows a
    /// message instead.
    pub fn more_window(&self, out: &mut Vec<String>) -> bool {
        todo!()
    }
}

/// Whether a menu entry is selected.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Selection {
    /// Not selected (`-`).
    None,
    /// Partly selected (`#`).
    Partial,
    /// Selected (`+` or `*`).
    All,
}

/// One selectable menu row: `a - an uncursed dagger`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuEntry {
    /// The selection key.
    pub key: char,
    /// The entry text.
    pub text: String,
    /// The selection marker.
    pub selection: Selection,
}

/// Whether `row` continues the message on `line` above it: the game splits a long message at a space, so a
/// continuation starts in column 0 with a word that would not have fitted, and not with map structure.
pub fn continues(line: &str, row: &str) -> bool {
    todo!()
}

/// Keeps a targeting cursor distinct from the hero after the game's automatic descriptions.
#[derive(Clone, Debug, Default)]
pub struct PromptContext {
    /// A positioning prompt is open.
    pub position: bool,
    status: (Option<String>, Option<u32>),
}

impl PromptContext {
    /// No positioning prompt open.
    pub fn new() -> PromptContext {
        PromptContext::default()
    }

    /// Forgets any open positioning prompt.
    pub fn reset(&mut self) {
        todo!()
    }

    /// Adjusts a freshly parsed view: while a positioning prompt is open, the cursor is the target, not the hero.
    pub fn observe(&mut self, view: &mut View) {
        todo!()
    }

    /// Notes keys about to be sent with `view` on screen: a pick or Escape closes the positioning prompt.
    pub fn before_input(&mut self, view: &View, keys: &[u8]) {
        todo!()
    }
}
