//! Data shared across modules: screen geometry, cells, exit codes and the reply a command produces.

use serde::{Serialize, Serializer};

/// Terminal rows.
pub const ROWS: usize = 24;
/// Terminal columns.
pub const COLS: usize = 80;
/// First terminal row of the map.
pub const MAP_TOP: u8 = 1;
/// Last terminal row of the map.
pub const MAP_BOTTOM: u8 = 21;
/// Squares in the map area.
pub const MAP_SQUARES: usize = (MAP_BOTTOM - MAP_TOP + 1) as usize * COLS;

/// The eight direction keys and their (row, column) steps, cardinal first, in the order routes try them.
pub const DIRS: [(u8, i8, i8); 8] = [
    (b'h', 0, -1),
    (b'j', 1, 0),
    (b'k', -1, 0),
    (b'l', 0, 1),
    (b'y', -1, -1),
    (b'u', -1, 1),
    (b'b', 1, -1),
    (b'n', 1, 1),
];

/// A screen square: `y` is the terminal row, `x` the terminal column; it orders by row, then column.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Pos {
    /// Terminal row, 0 to 23.
    pub y: u8,
    /// Terminal column, 0 to 79.
    pub x: u8,
}

impl Pos {
    /// The square at row `y`, column `x`.
    pub const fn new(y: u8, x: u8) -> Pos {
        Pos { y, x }
    }

    /// Whether the square lies in the map area (rows 1 to 21).
    pub const fn on_map(self) -> bool {
        self.y >= MAP_TOP && self.y <= MAP_BOTTOM && (self.x as usize) < COLS
    }

    /// The square one step away in direction (`dy`, `dx`), if it is on the screen.
    pub fn step(self, dy: i8, dx: i8) -> Option<Pos> {
        let y = self.y as i16 + dy as i16;
        let x = self.x as i16 + dx as i16;
        if (0..ROWS as i16).contains(&y) && (0..COLS as i16).contains(&x) {
            Some(Pos::new(y as u8, x as u8))
        } else {
            None
        }
    }

    /// The on-map neighbours with their direction keys, in `DIRS` order.
    pub fn neighbours(self) -> impl Iterator<Item = (u8, Pos)> {
        DIRS.into_iter().filter_map(move |(key, dy, dx)| {
            self.step(dy, dx).filter(|q| q.on_map()).map(|q| (key, q))
        })
    }

    /// The direction key that steps from `self` to the adjacent square `to`.
    pub fn direction_to(self, to: Pos) -> Option<u8> {
        let dy = to.y as i16 - self.y as i16;
        let dx = to.x as i16 - self.x as i16;
        DIRS.iter()
            .find(|&&(_, ry, rx)| ry as i16 == dy && rx as i16 == dx)
            .map(|&(key, _, _)| key)
    }

    /// King-move distance.
    pub fn chebyshev(self, other: Pos) -> u8 {
        self.y.abs_diff(other.y).max(self.x.abs_diff(other.x))
    }

    /// Index of a map square in a `MAP_SQUARES` array; `None` off the map.
    pub fn index(self) -> Option<usize> {
        self.on_map()
            .then(|| (self.y - MAP_TOP) as usize * COLS + self.x as usize)
    }

    /// The map square at a `MAP_SQUARES` index.
    pub fn from_index(index: usize) -> Pos {
        Pos::new((index / COLS) as u8 + MAP_TOP, (index % COLS) as u8)
    }
}

impl std::fmt::Display for Pos {
    /// Prints `x,y`, the order the view uses.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{},{}", self.x, self.y)
    }
}

impl Serialize for Pos {
    /// Serialises as `[x, y]`.
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        [self.x, self.y].serialize(s)
    }
}

/// A fixed-size set of map squares.
#[derive(Clone, PartialEq, Eq)]
pub struct SquareSet {
    bits: [u64; MAP_SQUARES.div_ceil(64)],
}

impl Default for SquareSet {
    fn default() -> SquareSet {
        SquareSet::new()
    }
}

impl std::fmt::Debug for SquareSet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_set().entries(self.iter()).finish()
    }
}

impl SquareSet {
    /// An empty set.
    pub const fn new() -> SquareSet {
        SquareSet {
            bits: [0; MAP_SQUARES.div_ceil(64)],
        }
    }

    /// Adds a square; off-map squares are ignored. Returns whether it was newly added.
    pub fn insert(&mut self, p: Pos) -> bool {
        match p.index() {
            Some(i) => {
                let had = self.bits[i / 64] & (1 << (i % 64)) != 0;
                self.bits[i / 64] |= 1 << (i % 64);
                !had
            }
            None => false,
        }
    }

    /// Removes a square. Returns whether it was present.
    pub fn remove(&mut self, p: Pos) -> bool {
        match p.index() {
            Some(i) => {
                let had = self.bits[i / 64] & (1 << (i % 64)) != 0;
                self.bits[i / 64] &= !(1 << (i % 64));
                had
            }
            None => false,
        }
    }

    /// Whether the square is in the set.
    pub fn contains(&self, p: Pos) -> bool {
        p.index()
            .is_some_and(|i| self.bits[i / 64] & (1 << (i % 64)) != 0)
    }

    /// Empties the set.
    pub fn clear(&mut self) {
        self.bits = [0; MAP_SQUARES.div_ceil(64)];
    }

    /// Whether the set is empty.
    pub fn is_empty(&self) -> bool {
        self.bits.iter().all(|&w| w == 0)
    }

    /// Number of squares in the set.
    pub fn len(&self) -> usize {
        self.bits.iter().map(|w| w.count_ones() as usize).sum()
    }

    /// The squares in row-then-column order.
    pub fn iter(&self) -> impl Iterator<Item = Pos> + '_ {
        (0..MAP_SQUARES)
            .filter(|&i| self.bits[i / 64] & (1 << (i % 64)) != 0)
            .map(Pos::from_index)
    }
}

/// A terminal foreground colour as the emulator stores it: SGR 30–37, 90–97 or the default.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum Colour {
    /// No colour set (SGR 39 or 0).
    #[default]
    Default,
    /// SGR 30.
    Black,
    /// SGR 31.
    Red,
    /// SGR 32.
    Green,
    /// SGR 33.
    Brown,
    /// SGR 34.
    Blue,
    /// SGR 35.
    Magenta,
    /// SGR 36.
    Cyan,
    /// SGR 37.
    White,
    /// SGR 90.
    BrightBlack,
    /// SGR 91.
    BrightRed,
    /// SGR 92.
    BrightGreen,
    /// SGR 93.
    BrightBrown,
    /// SGR 94.
    BrightBlue,
    /// SGR 95.
    BrightMagenta,
    /// SGR 96.
    BrightCyan,
    /// SGR 97.
    BrightWhite,
}

/// Colour names as fixtures store them, in `Colour` order after `Default`.
const COLOUR_NAMES: [&str; 17] = [
    "default",
    "black",
    "red",
    "green",
    "brown",
    "blue",
    "magenta",
    "cyan",
    "white",
    "brightblack",
    "brightred",
    "brightgreen",
    "brightbrown",
    "brightblue",
    "brightmagenta",
    "brightcyan",
    "brightwhite",
];

const COLOURS: [Colour; 17] = [
    Colour::Default,
    Colour::Black,
    Colour::Red,
    Colour::Green,
    Colour::Brown,
    Colour::Blue,
    Colour::Magenta,
    Colour::Cyan,
    Colour::White,
    Colour::BrightBlack,
    Colour::BrightRed,
    Colour::BrightGreen,
    Colour::BrightBrown,
    Colour::BrightBlue,
    Colour::BrightMagenta,
    Colour::BrightCyan,
    Colour::BrightWhite,
];

impl Colour {
    /// The colour of SGR code `n` (30–37 or 90–97).
    pub fn from_sgr(n: u16) -> Option<Colour> {
        match n {
            30..=37 => Some(COLOURS[(n - 30) as usize + 1]),
            90..=97 => Some(COLOURS[(n - 90) as usize + 9]),
            _ => None,
        }
    }

    /// The colour a fixture names, such as `default`, `blue` or `brightblue`.
    pub fn from_name(name: &str) -> Option<Colour> {
        COLOUR_NAMES
            .iter()
            .position(|&n| n == name)
            .map(|i| COLOURS[i])
    }
}

/// One terminal cell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cell {
    /// The character shown.
    pub ch: char,
    /// Foreground colour.
    pub fg: Colour,
    /// Bold (bright) attribute.
    pub bold: bool,
    /// Reverse video, which the game uses to highlight a pet.
    pub rev: bool,
}

impl Cell {
    /// A blank cell in the default colour.
    pub const BLANK: Cell = Cell {
        ch: ' ',
        fg: Colour::Default,
        bold: false,
        rev: false,
    };
}

impl Default for Cell {
    fn default() -> Cell {
        Cell::BLANK
    }
}

/// The 80×24 cells of one screen.
#[derive(Clone, PartialEq, Eq)]
pub struct Grid {
    /// Cells by row, then column.
    pub cells: [[Cell; COLS]; ROWS],
}

impl Default for Grid {
    fn default() -> Grid {
        Grid::new()
    }
}

impl std::fmt::Debug for Grid {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let mut row = String::with_capacity(COLS);
        let mut list = f.debug_list();
        for r in 0..ROWS {
            self.row_text(r, &mut row);
            list.entry(&row.trim_end().to_owned());
        }
        list.finish()
    }
}

impl Grid {
    /// A blank screen.
    pub const fn new() -> Grid {
        Grid {
            cells: [[Cell::BLANK; COLS]; ROWS],
        }
    }

    /// The cell at `p`, or a blank one off the screen.
    pub fn cell(&self, p: Pos) -> Cell {
        self.cells
            .get(p.y as usize)
            .and_then(|row| row.get(p.x as usize))
            .copied()
            .unwrap_or(Cell::BLANK)
    }

    /// Writes row `r`'s 80 characters into `out`, replacing its contents.
    pub fn row_text(&self, r: usize, out: &mut String) {
        out.clear();
        if let Some(row) = self.cells.get(r) {
            out.extend(row.iter().map(|c| c.ch));
        }
    }
}

/// How a command ended, as the process exit code and the reply's `code`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExitCode {
    /// The command did what was asked.
    Done = 0,
    /// The command was refused or failed; the text says why.
    Failed = 1,
    /// A bounded action stopped short; the report says why.
    Short = 2,
    /// The game is over; `facts.end` holds the end messages.
    Over = 3,
    /// The terminal is closed or unreachable.
    Closed = 4,
    /// The game has not started taking input yet.
    Held = 5,
    /// The command line was malformed.
    Usage = 64,
}

impl ExitCode {
    /// The numeric code.
    pub fn code(self) -> i32 {
        self as i32
    }
}

impl Serialize for ExitCode {
    /// Serialises as its number.
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_i32(self.code())
    }
}

/// The daemon's answer to one request: `{"code", "text", "facts"}`.
#[derive(Clone, Debug, Serialize)]
pub struct Reply {
    /// Exit code for the client.
    pub code: ExitCode,
    /// The compact text view, with any report line first.
    pub text: String,
    /// The same observation in machine form.
    pub facts: Facts,
}

impl Reply {
    /// A reply that carries only a message, such as a refusal.
    pub fn message(code: ExitCode, text: impl Into<String>) -> Reply {
        Reply {
            code,
            text: text.into(),
            facts: Facts::default(),
        }
    }
}

/// The machine form of a view.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Facts {
    /// Dungeon level number from the status line.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dlvl: Option<i32>,
    /// Game turn from the status line.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn: Option<u32>,
    /// Hit points.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hp: Option<i32>,
    /// Maximum hit points.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hpmax: Option<i32>,
    /// Experience level.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub xl: Option<i32>,
    /// Armour class.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ac: Option<i32>,
    /// The hunger word on the status line, if one is shown.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hunger: Option<String>,
    /// The hero's square as `[x, y]`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pos: Option<Pos>,
    /// What is remembered underfoot, as the `here:` part prints it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub here: Option<String>,
    /// The open prompt's text.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt: Option<String>,
    /// Messages the game wrote since the last reply.
    pub msgs: Vec<String>,
    /// Monsters in view, nearest first.
    pub monsters: Vec<Monster>,
    /// Remembered items, nearest by route first.
    pub items: Vec<Place>,
    /// Remembered features and doors, nearest by route first.
    pub seen: Vec<Place>,
    /// Reachable frontier squares, nearest group first.
    pub frontier: Vec<Frontier>,
    /// The remembered down stairs on this level.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stairs_down: Option<Pos>,
    /// The remembered up stairs on this level.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stairs_up: Option<Pos>,
    /// The last inventory the game showed.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inventory: Option<Inventory>,
    /// How the command went.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub report: Option<Report>,
    /// Prompts the sent keys passed, with the key that answered each.
    pub answered: Vec<Answered>,
    /// End-of-game messages, when the game is over.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end: Option<End>,
}

impl Facts {
    /// Empties every field, keeping allocated capacity.
    pub fn clear(&mut self) {
        self.dlvl = None;
        self.turn = None;
        self.hp = None;
        self.hpmax = None;
        self.xl = None;
        self.ac = None;
        self.hunger = None;
        self.pos = None;
        self.here = None;
        self.prompt = None;
        self.msgs.clear();
        self.monsters.clear();
        self.items.clear();
        self.seen.clear();
        self.frontier.clear();
        self.stairs_down = None;
        self.stairs_up = None;
        self.inventory = None;
        self.report = None;
        self.answered.clear();
        self.end = None;
    }
}

/// A monster in view.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Monster {
    /// The game's farlook name, when one is cached for this monster.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    /// The glyph shown.
    pub glyph: char,
    /// Terminal column.
    pub x: u8,
    /// Terminal row.
    pub y: u8,
    /// King-move distance from the hero.
    pub dist: u8,
    /// The turn the name was looked up, when that was an earlier turn.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub looked_turn: Option<u32>,
    /// Whether the terminal highlights it as the hero's pet.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub pet: bool,
}

/// A remembered feature, door or item and how a route reaches it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Place {
    /// The glyph shown or remembered.
    pub glyph: char,
    /// What it is, such as `down stairs` or `door`; empty for a plain item.
    pub label: String,
    /// Terminal column.
    pub x: u8,
    /// Terminal row.
    pub y: u8,
    /// Route length in steps; `None` when no route avoids monsters.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub steps: Option<u32>,
    /// A route exists over terrain but monsters block it.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub blocked: bool,
}

/// A frontier square: reachable known ground beside unexplored squares.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Frontier {
    /// Terminal column.
    pub x: u8,
    /// Terminal row.
    pub y: u8,
    /// Route length in steps.
    pub steps: u32,
}

/// An inventory menu as the game last showed it.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Inventory {
    /// The turn it was shown.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn: Option<u32>,
    /// Its item lines, such as `a - a +0 scalpel (weapon in right hand)`.
    pub lines: Vec<String>,
}

/// The end-of-game text.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct End {
    /// The tombstone or final line naming how the game ended, verbatim.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub death: Option<String>,
    /// Every end-of-game message line, in order.
    pub lines: Vec<String>,
}

/// How a `send`, `go` or `rest` went.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Report {
    /// The command as labelled in the text, such as `go >` or `rest 20`.
    pub verb: String,
    /// Whether it finished what was asked.
    pub done: bool,
    /// Why it stopped short, one phrase per reason.
    pub stopped: Vec<String>,
    /// Steps, searches or keys executed.
    pub steps: u32,
    /// Game turns that passed, when the status line showed them.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turns: Option<i64>,
    /// A failure that ended the command, such as a closed terminal.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// A prompt that sent keys passed, and the key that answered it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Answered {
    /// The prompt text.
    pub prompt: String,
    /// The answering key in escape syntax, such as `n`, `\e` or `\r`.
    pub key: String,
    /// The prompt takes free text, so the keys after it were typed into it.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub text_entry: bool,
}

/// Decodes the key escape syntax: `\e \r \n \t \\ \xHH`; any other character stands for its code, which must be
/// at most 0xff.
pub fn unescape(keys: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(keys.len());
    let mut chars = keys.chars();
    while let Some(ch) = chars.next() {
        if ch != '\\' {
            out.push(
                u8::try_from(u32::from(ch))
                    .map_err(|_| "send takes ASCII keys; write other bytes as \\xHH".to_owned())?,
            );
            continue;
        }
        let rest = chars.as_str();
        let (byte, used) = match rest.as_bytes().first() {
            Some(b'e') => (0x1b, 1),
            Some(b'r') => (b'\r', 1),
            Some(b'n') => (b'\n', 1),
            Some(b't') => (b'\t', 1),
            Some(b'\\') => (b'\\', 1),
            Some(b'x')
                if rest.len() >= 3 && rest.as_bytes()[1..3].iter().all(u8::is_ascii_hexdigit) =>
            {
                match u8::from_str_radix(&rest[1..3], 16) {
                    Ok(b) => (b, 3),
                    Err(_) => (b'\\', 0),
                }
            }
            _ => (b'\\', 0),
        };
        out.push(byte);
        for _ in 0..used {
            chars.next();
        }
    }
    Ok(out)
}

/// Writes one key byte in escape syntax: the inverse of `unescape` for a single byte.
pub fn escape_key(key: u8, out: &mut String) {
    match key {
        0x1b => out.push_str("\\e"),
        b'\r' => out.push_str("\\r"),
        b'\n' => out.push_str("\\n"),
        b'\t' => out.push_str("\\t"),
        b'\\' => out.push_str("\\\\"),
        0x20..=0x7e => out.push(key as char),
        _ => {
            use std::fmt::Write;
            let _ = write!(out, "\\x{key:02x}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unescape_matches_the_key_syntax() {
        assert_eq!(
            unescape(r"E-\eab\r\x12\\q\z").unwrap(),
            b"E-\x1bab\r\x12\\q\\z"
        );
        assert!(unescape("é").is_ok());
        assert!(unescape("☃").is_err());
    }

    #[test]
    fn square_set_holds_map_squares_in_order() {
        let mut set = SquareSet::new();
        assert!(set.insert(Pos::new(5, 3)));
        assert!(set.insert(Pos::new(2, 70)));
        assert!(!set.insert(Pos::new(0, 1)));
        assert_eq!(
            set.iter().collect::<Vec<_>>(),
            [Pos::new(2, 70), Pos::new(5, 3)]
        );
        assert_eq!(set.len(), 2);
    }

    #[test]
    fn positions_print_and_serialise_as_x_y() {
        let p = Pos::new(6, 32);
        assert_eq!(p.to_string(), "32,6");
        assert_eq!(serde_json::to_string(&p).unwrap(), "[32,6]");
        assert_eq!(p.direction_to(Pos::new(5, 33)), Some(b'u'));
    }
}
