//! Data shared across modules: screen geometry, cells, exit codes and the reply a command produces.

use serde::{Deserialize, Serialize, Serializer};

pub const ROWS: usize = 24;
pub const COLS: usize = 80;
/// The map is terminal rows `MAP_TOP..=MAP_BOTTOM`.
pub const MAP_TOP: u8 = 1;
pub const MAP_BOTTOM: u8 = 21;
pub const MAP_ROWS: usize = (MAP_BOTTOM - MAP_TOP + 1) as usize;
pub const MAP_SQUARES: usize = MAP_ROWS * COLS;

/// The eight direction keys and their (row, column) steps, cardinal first, in the order routes try them.
#[rustfmt::skip]
pub const DIRS: [(u8, i8, i8); 8] = [
    (b'h', 0, -1), (b'j', 1, 0), (b'k', -1, 0), (b'l', 0, 1),
    (b'y', -1, -1), (b'u', -1, 1), (b'b', 1, -1), (b'n', 1, 1),
];

/// A screen square: `y` is the terminal row, `x` the terminal column; it orders by row, then column.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Pos {
    pub y: u8,
    pub x: u8,
}

impl Pos {
    pub const fn new(y: u8, x: u8) -> Pos {
        Pos { y, x }
    }

    pub const fn on_map(self) -> bool {
        self.y >= MAP_TOP && self.y <= MAP_BOTTOM && (self.x as usize) < COLS
    }

    /// The square one step away in direction (`dy`, `dx`), if it is on the screen.
    pub fn step(self, dy: i8, dx: i8) -> Option<Pos> {
        let y = self.y.checked_add_signed(dy)?;
        let x = self.x.checked_add_signed(dx)?;
        ((y as usize) < ROWS && (x as usize) < COLS).then_some(Pos::new(y, x))
    }

    /// The on-map neighbours with their direction keys, in `DIRS` order.
    pub fn neighbours(self) -> impl Iterator<Item = (u8, Pos)> {
        DIRS.into_iter().filter_map(move |(key, dy, dx)| {
            self.step(dy, dx).filter(|q| q.on_map()).map(|q| (key, q))
        })
    }

    /// The direction key that steps from `self` to the adjacent square `to`.
    pub fn direction_to(self, to: Pos) -> Option<u8> {
        let (key, _) = self.neighbours().find(|&(_, q)| q == to)?;
        Some(key)
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

/// A fixed-size set of map squares; off-map squares are never in it.
#[derive(Clone, Default, PartialEq, Eq)]
pub struct SquareSet {
    bits: [u64; MAP_SQUARES.div_ceil(64)],
}

impl std::fmt::Debug for SquareSet {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_set().entries(self.iter()).finish()
    }
}

impl SquareSet {
    pub const fn new() -> SquareSet {
        SquareSet {
            bits: [0; MAP_SQUARES.div_ceil(64)],
        }
    }

    pub fn insert(&mut self, p: Pos) {
        if let Some(i) = p.index() {
            self.bits[i / 64] |= 1 << (i % 64);
        }
    }

    /// Removes a square; returns whether it was present.
    pub fn remove(&mut self, p: Pos) -> bool {
        let had = self.contains(p);
        if let Some(i) = p.index() {
            self.bits[i / 64] &= !(1 << (i % 64));
        }
        had
    }

    pub fn contains(&self, p: Pos) -> bool {
        p.index()
            .is_some_and(|i| self.bits[i / 64] & (1 << (i % 64)) != 0)
    }

    pub fn clear(&mut self) {
        *self = SquareSet::new();
    }

    pub fn is_empty(&self) -> bool {
        self.bits.iter().all(|&w| w == 0)
    }

    /// The squares in row-then-column order.
    pub fn iter(&self) -> impl Iterator<Item = Pos> + '_ {
        (0..MAP_SQUARES)
            .filter(|&i| self.bits[i / 64] & (1 << (i % 64)) != 0)
            .map(Pos::from_index)
    }
}

/// A terminal foreground colour as the emulator stores it: the default, SGR 30–37 or SGR 90–97. Fixtures name
/// colours in lower case, such as `brightblue`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Colour {
    #[default]
    Default,
    Black,
    Red,
    Green,
    Brown,
    Blue,
    Magenta,
    Cyan,
    White,
    BrightBlack,
    BrightRed,
    BrightGreen,
    BrightBrown,
    BrightBlue,
    BrightMagenta,
    BrightCyan,
    BrightWhite,
}

impl Colour {
    pub fn from_sgr(n: u16) -> Option<Colour> {
        use Colour::*;
        #[rustfmt::skip]
        const SGR: [Colour; 16] = [
            Black, Red, Green, Brown, Blue, Magenta, Cyan, White,
            BrightBlack, BrightRed, BrightGreen, BrightBrown, BrightBlue, BrightMagenta, BrightCyan, BrightWhite,
        ];
        match n {
            30..=37 => Some(SGR[usize::from(n - 30)]),
            90..=97 => Some(SGR[usize::from(n - 82)]),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cell {
    pub ch: char,
    pub fg: Colour,
    pub bold: bool,
    /// Reverse video, which the game uses to highlight a pet.
    pub rev: bool,
}

impl Cell {
    pub const BLANK: Cell = Cell {
        ch: ' ',
        fg: Colour::Default,
        bold: false,
        rev: false,
    };
}

/// The 80×24 cells of one screen, by row, then column.
#[derive(Clone, PartialEq, Eq)]
pub struct Grid {
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
    Done = 0,
    /// Refused or failed; the text says why.
    Failed = 1,
    /// A bounded action stopped short; the report says why.
    Short = 2,
    /// The game is over; `facts.end` holds the end messages.
    Over = 3,
    /// The terminal is closed or unreachable.
    Closed = 4,
    /// The game has not started taking input yet.
    Held = 5,
    /// The command line or request was malformed.
    Usage = 64,
}

impl ExitCode {
    pub fn code(self) -> i32 {
        self as i32
    }
}

impl Serialize for ExitCode {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_i32(self.code())
    }
}

/// The answer to one request: the exit code, the text view with any report line first, and its machine form.
#[derive(Clone, Debug, Serialize)]
pub struct Reply {
    pub code: ExitCode,
    pub text: String,
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

/// The machine form of a view; the README describes each field.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Facts {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub dlvl: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hp: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hpmax: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub xl: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ac: Option<i32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hunger: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pos: Option<Pos>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub here: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt: Option<String>,
    pub msgs: Vec<String>,
    pub monsters: Vec<Monster>,
    pub items: Vec<Place>,
    pub seen: Vec<Place>,
    pub frontier: Vec<Frontier>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stairs_down: Option<Pos>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stairs_up: Option<Pos>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub inventory: Option<Inventory>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub report: Option<Report>,
    pub answered: Vec<Answered>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end: Option<End>,
}

/// A monster in view.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Monster {
    /// The game's farlook name, when one is kept for this monster.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,
    pub glyph: char,
    pub x: u8,
    pub y: u8,
    /// King-move distance from the hero.
    pub dist: u8,
    /// The turn the name was looked up, when that was an earlier turn.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub looked_turn: Option<u32>,
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub pet: bool,
}

/// A remembered feature, door or item and how a route reaches it.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Place {
    pub glyph: char,
    /// What it is, such as `down stairs` or `door`; empty for a plain item.
    pub label: String,
    pub x: u8,
    pub y: u8,
    /// Route length in steps; `None` when no route avoids monsters.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub steps: Option<u32>,
    /// A route exists over terrain but monsters block it.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub blocked: bool,
    /// The caller told the harness about its square, which the screen has not shown.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub told: bool,
}

/// A frontier square: reachable known ground beside unexplored squares.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Frontier {
    pub x: u8,
    pub y: u8,
    pub steps: u32,
}

/// An inventory menu as the game last showed it, and the turn it showed it.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Inventory {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turn: Option<u32>,
    pub lines: Vec<String>,
}

/// The end-of-game text: the tombstone or final line naming how the game ended, and every end-of-game line.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct End {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub death: Option<String>,
    pub lines: Vec<String>,
}

/// How a `send`, `go` or `rest` went.
#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize)]
pub struct Report {
    /// The command as labelled in the text, such as `go >` or `rest 20`.
    pub verb: String,
    pub done: bool,
    /// Why it stopped short, one phrase per reason.
    pub stopped: Vec<String>,
    /// Steps, searches or keys executed.
    pub steps: u32,
    /// For `go --wet`, the steps sent into water.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wet: Option<u32>,
    /// For `go`, the steps onto squares that were told when the walk began.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub told: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub turns: Option<i64>,
    /// For `rest`, when the turn counter was shown before and after it: the searches after which it read as before.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub no_time: Option<u32>,
    /// What the screen showed with those searches, one entry per distinct answer in the order first seen.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub no_time_why: Vec<NoTime>,
    /// A failure that ended the command, such as a closed terminal.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

/// What the screen showed with a search after which the turn counter read as before.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub enum NoTimeWhy {
    /// The game wrote a message.
    #[serde(rename = "message")]
    Message,
    /// A prompt opened.
    #[serde(rename = "prompt")]
    Prompt,
    /// Nothing on the screen changed.
    #[serde(rename = "screen unchanged")]
    Screen,
    /// The screen changed, with no message or prompt, but the turn counter did not.
    #[serde(rename = "turn counter unchanged")]
    Turn,
}

/// Searches of a rest that took no game time with one answer on screen, and the message or prompt text.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct NoTime {
    pub searches: u32,
    pub why: NoTimeWhy,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
}

/// A prompt that sent keys passed, and the key that answered it in escape syntax.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Answered {
    pub prompt: String,
    pub key: String,
    /// The prompt takes free text, so the keys after it were typed into it.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub text_entry: bool,
}

/// The named key escapes: `\e \r \n \t \\`, with the byte each stands for.
#[rustfmt::skip]
const ESCAPES: [(u8, u8); 5] = [(0x1b, b'e'), (b'\r', b'r'), (b'\n', b'n'), (b'\t', b't'), (b'\\', b'\\')];

/// Decodes the key escape syntax: the named escapes and `\xHH`; any other character must be ASCII and stands for
/// its code, so `\xHH` is the only way to send a byte above 0x7f.
pub fn unescape(keys: &str) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(keys.len());
    let mut rest = keys;
    while let Some(ch) = rest.chars().next() {
        if !ch.is_ascii() {
            return Err("send takes ASCII keys; write other bytes as \\xHH".to_owned());
        }
        let next = rest.as_bytes().get(1).copied();
        let named = ESCAPES.iter().find(|&&(_, name)| Some(name) == next);
        let hex = rest
            .get(2..4)
            .filter(|h| h.bytes().all(|b| b.is_ascii_hexdigit()))
            .and_then(|h| u8::from_str_radix(h, 16).ok());
        let (byte, used) = match (ch, named, next, hex) {
            ('\\', Some(&(byte, _)), _, _) => (byte, 2),
            ('\\', None, Some(b'x'), Some(byte)) => (byte, 4),
            _ => (ch as u8, 1),
        };
        out.push(byte);
        rest = &rest[used..];
    }
    Ok(out)
}

/// Writes one key byte in escape syntax: the inverse of `unescape` for a single byte.
pub fn escape_key(key: u8, out: &mut String) {
    if let Some(&(_, name)) = ESCAPES.iter().find(|&&(byte, _)| byte == key) {
        out.push('\\');
        out.push(name as char);
    } else if (0x20..=0x7e).contains(&key) {
        out.push(key as char);
    } else {
        use std::fmt::Write;
        let _ = write!(out, "\\x{key:02x}");
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
        assert!(unescape("é").is_err());
        assert_eq!(unescape(r"\xe9").unwrap(), [0xe9]);
        assert!(unescape("☃").is_err());
    }

    #[test]
    fn square_set_holds_map_squares_in_order() {
        let mut set = SquareSet::new();
        set.insert(Pos::new(5, 3));
        set.insert(Pos::new(2, 70));
        set.insert(Pos::new(0, 1));
        assert_eq!(
            set.iter().collect::<Vec<_>>(),
            [Pos::new(2, 70), Pos::new(5, 3)]
        );
        assert!(set.remove(Pos::new(5, 3)) && !set.remove(Pos::new(5, 3)));
    }

    #[test]
    fn positions_print_and_serialise_as_x_y() {
        let p = Pos::new(6, 32);
        assert_eq!(p.to_string(), "32,6");
        assert_eq!(serde_json::to_string(&p).unwrap(), "[32,6]");
        assert_eq!(p.direction_to(Pos::new(5, 33)), Some(b'u'));
    }
}
