//! Parsing one 80×24 tty screen: the message line, prompts, `--More--`, menus, end-of-game text, the status
//! lines, the hero and map cells.

use crate::glyphs::{self, Conditions, Shade, contains_ignore_case, find_word, is_word};
use crate::types::{Grid, MAP_SQUARES, Pos, ROWS};

/// What the game is waiting for, judged from the screen.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// End-of-game text or questions.
    Ended,
    More,
    Direction,
    /// An object prompt, such as `What do you want to eat? [fg or ?*]`.
    Item,
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
    Unknown,
}

impl Phase {
    /// The name the view prints, such as `menu` or `game over`.
    pub fn name(self) -> &'static str {
        match self {
            Phase::Ended => "game over",
            Phase::More => "more",
            Phase::Direction => "direction",
            Phase::Item => "item",
            Phase::Spell => "spell",
            Phase::Choice => "choice",
            Phase::Text => "text",
            Phase::Menu => "menu",
            Phase::Position => "position",
            Phase::Play => "play",
            Phase::Unknown => "unknown",
        }
    }
}

/// The kind of end-of-game screen shown: the disclosure questions (`Do you want your possessions identified?`),
/// the tombstone (`REST IN PEACE`), the farewell line (`Goodbye …`, `Be seeing you…`) or the ascension line.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EndKind {
    Disclosure,
    Tombstone,
    Farewell,
    Ascended,
}

/// The fields of the two status lines.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Status {
    /// The location word, such as `Dlvl:3`, `Home 1` or `Astral Plane`.
    pub location: Option<String>,
    pub dlvl: Option<i32>,
    pub gold: Option<i32>,
    pub hp: Option<i32>,
    pub hpmax: Option<i32>,
    pub pw: Option<i32>,
    pub pwmax: Option<i32>,
    pub ac: Option<i32>,
    pub xl: Option<i32>,
    pub turn: Option<u32>,
    /// Strength as shown, such as `18/50`.
    pub strength: Option<String>,
    pub dex: Option<i32>,
    pub con: Option<i32>,
    pub int: Option<i32>,
    pub wis: Option<i32>,
    pub cha: Option<i32>,
    pub alignment: Option<&'static str>,
}

/// One parsed screen.
#[derive(Clone, Debug, Default)]
pub struct View {
    pub grid: Grid,
    /// Each row's 80 characters.
    pub rows: [String; ROWS],
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
    pub end: Option<EndKind>,
    pub status: Status,
    pub cond: Conditions,
    /// Any prompt is open.
    pub prompt: bool,
    pub hero: Option<Pos>,
    /// The hero is engulfed: the corners around it read `/ \ \ /`.
    pub engulfed: bool,
    /// The map waits for a command with the hero shown.
    pub normal: bool,
}

/// Messages that open a free-text prompt.
#[rustfmt::skip]
const TEXT_PROMPTS: [&str; 13] = [
    "What do you want to write", "What do you want to name", "What do you want to call", "What do you want to add",
    "Call a ", "Call an ", "Name it", "What monster", "write in the", "What do you want to engrave",
    "who are you?", "What do you want to wish", "What do you want to genocide",
];

/// Messages, matched without regard to ASCII case, that ask for a direction.
const DIRECTION_PROMPTS: [&str; 3] = ["in what direction", "which direction", "open where?"];

/// Messages that open a cursor-positioning prompt.
#[rustfmt::skip]
const POSITION_PROMPTS: [&str; 8] = [
    "Where do you want to travel", "Where do you want to cast the spell", "For instructions type a ?",
    "For instructions type a '?", "For instructions type a \"?", "Pick a ", "Pick an ", "Move cursor to ",
];

/// The questions the game asks after the hero's death or departure, before the tombstone.
#[rustfmt::skip]
const DISCLOSURE_QUESTIONS: [&str; 6] = [
    "possessions identified?", "Do you want to see what you had when you", "Do you want to see your attributes?",
    "Do you want an account of creatures vanquished?", "Do you want to see your conduct",
    "Do you want to see the dungeon overview?",
];

/// The words each role's farewell starts with.
const FAREWELLS: [&str; 5] = ["Goodbye", "Fare thee well", "Sayonara", "Aloha", "Farvel"];

/// Location words of the second status line, tried in order at each word start.
#[rustfmt::skip]
const LOCATIONS: [(&str, Tail); 11] = [
    ("Dlvl:", Tail::Digits), ("Home ", Tail::Digits), ("Tutorial:", Tail::Digits), ("Fort Ludios", Tail::Nothing),
    ("Astral Plane", Tail::Nothing), ("Earth", Tail::Nothing), ("Air", Tail::Nothing), ("Fire", Tail::Nothing),
    ("Water", Tail::Nothing), ("Plane of ", Tail::Word), ("End Game", Tail::Nothing),
];

/// What follows a location word's fixed text.
#[derive(Clone, Copy)]
enum Tail {
    Nothing,
    Digits,
    Word,
}

/// Alignment words of the first status line.
const ALIGNMENTS: [&str; 4] = ["Lawful", "Neutral", "Chaotic", "Unaligned"];

impl View {
    pub fn parse(grid: &Grid, cursor: Pos) -> View {
        let mut view = View::default();
        view.reparse(grid, cursor);
        view
    }

    /// Re-parses into this view, reusing its buffers.
    pub fn reparse(&mut self, grid: &Grid, cursor: Pos) {
        self.grid.clone_from(grid);
        for (r, row) in self.rows.iter_mut().enumerate() {
            grid.row_text(r, row);
        }
        self.cursor = cursor;
        self.parse_message();
        self.more = self.rows.iter().any(|r| r.contains("--More--"));
        self.menu = self.rows.iter().any(|r| footer(r).is_some());
        // A question is live only while the cursor waits on the message rows; once answered, its text can stay on
        // screen but must not be answered again.
        self.asking = (cursor.y as usize) < self.message_rows as usize && !self.more;
        self.parse_prompts();
        self.parse_end();
        self.parse_status();
        self.prompt = self.more
            || self.menu
            || self.yn.is_some()
            || self.obj.is_some()
            || self.spell.is_some()
            || self.text
            || self.direction
            || self.getpos;
        self.hero = self.find_hero();
        self.engulfed = self.hero.is_some_and(|hero| {
            let at = |dy, dx| hero.step(dy, dx).map_or(' ', |p| self.ch(p));
            at(-1, -1) == '/' && at(-1, 1) == '\\' && at(1, -1) == '\\' && at(1, 1) == '/'
        });
        self.normal = self.hero.is_some() && !self.prompt && !self.ended;
    }

    fn parse_message(&mut self) {
        let mut wrapped = 0;
        while wrapped < 2 && continues(self.rows[wrapped].trim_end(), &self.rows[wrapped + 1]) {
            wrapped += 1;
        }
        self.message_rows = wrapped as u8 + 1;
        self.msg.clear();
        self.msg.push_str(self.rows[0].trim());
        for row in &self.rows[1..=wrapped] {
            self.msg.push(' ');
            self.msg.push_str(row.trim());
        }
    }

    fn parse_prompts(&mut self) {
        let msg = self.msg.as_str();
        let asking = self.asking;
        let letters = bracket_letters(msg).filter(|_| asking);
        let obj = letters.filter(|_| msg.contains("What do you want"));
        let spell = letters.filter(|_| msg.contains("Cast which spell?"));
        let yn = choice_answers(msg).filter(|_| asking && obj.is_none());
        self.text = asking && obj.is_none() && TEXT_PROMPTS.iter().any(|p| msg.contains(p));
        self.direction = asking
            && DIRECTION_PROMPTS
                .iter()
                .any(|p| contains_ignore_case(msg, p, false));
        self.getpos = POSITION_PROMPTS.iter().any(|p| msg.contains(p));
        set_str(&mut self.yn, yn);
        set_str(&mut self.obj, obj);
        set_str(&mut self.spell, spell);
    }

    fn parse_end(&mut self) {
        let msg = self.msg.as_str();
        let rows = || self.rows.iter().map(|r| r.trim_start());
        let farewells = || rows().filter_map(farewell);
        let ascended = msg.starts_with("You ascend to the status of Demigod...")
            || msg.starts_with("You ascend to the status of Demigoddess...")
            || farewells()
                .any(|f| f.contains(" the Demigod...") || f.contains(" the Demigoddess..."));
        // The tombstone carves `REST`, `IN` and `PEACE` first on three consecutive rows.
        let carved = |row: &str, word| {
            row.split(|c: char| c.is_whitespace() || c == '/' || c == '\\' || c == '|')
                .find(|w| !w.is_empty())
                == Some(word)
        };
        let tombstone = self
            .rows
            .windows(3)
            .any(|w| carved(&w[0], "REST") && carved(&w[1], "IN") && carved(&w[2], "PEACE"));
        self.end = if ascended {
            Some(EndKind::Ascended)
        } else if tombstone {
            Some(EndKind::Tombstone)
        } else if farewells().any(names_role) || rows().any(|r| r.starts_with("Be seeing you...")) {
            Some(EndKind::Farewell)
        } else if DISCLOSURE_QUESTIONS.iter().any(|q| msg.contains(q))
            || (msg.contains("Do you want a list of") && msg.contains("species"))
        {
            Some(EndKind::Disclosure)
        } else {
            None
        };
        self.ended = self.end.is_some();
    }

    fn parse_status(&mut self) {
        let (first, second) = (self.rows[ROWS - 2].as_str(), self.rows[ROWS - 1].as_str());
        // A field is read from the first status line, else the second.
        let lines = [first, second];
        let number = |prefix, signed| lines.iter().find_map(|l| number_after(l, prefix, signed));
        let pair = |prefix, signed| lines.iter().find_map(|l| pair_after(l, prefix, signed));
        let status = &mut self.status;
        set_str(&mut status.location, location(second));
        status.dlvl = number("Dlvl:", false);
        status.gold = number("$:", false);
        (status.hp, status.hpmax) = pair("HP:", true).unzip();
        (status.pw, status.pwmax) = pair("Pw:", false).unzip();
        status.ac = number("AC:", true);
        status.xl = lines.into_iter().find_map(experience);
        status.turn = number("T:", false).map(|n| n as u32);
        status.dex = number("Dx:", false);
        status.con = number("Co:", false);
        status.int = number("In:", false);
        status.wis = number("Wi:", false);
        status.cha = number("Ch:", false);
        set_str(&mut status.strength, lines.into_iter().find_map(strength));
        status.alignment = ALIGNMENTS
            .iter()
            .filter_map(|a| find_word(first, a).map(|i| (i, *a)))
            .min_by_key(|&(i, _)| i)
            .map(|(_, a)| a);
        let right = &first[byte_at(first, 40)..];
        self.cond = Conditions(glyphs::conditions(right).0 | glyphs::conditions(second).0);
    }

    fn find_hero(&self) -> Option<Pos> {
        let cursor = self.cursor;
        if self.prompt {
            return None;
        }
        // The tty leaves the cursor on the hero, whatever its symbol.
        if !self.ch(cursor).is_whitespace() {
            return Some(cursor);
        }
        // A cursor on the message line means a question is open.
        if self.asking && !self.msg.is_empty() {
            return None;
        }
        let distance = |p: Pos| p.y.abs_diff(cursor.y) as u32 + p.x.abs_diff(cursor.x) as u32;
        (0..MAP_SQUARES)
            .map(Pos::from_index)
            .filter(|&p| self.ch(p) == '@')
            .min_by_key(|&p| distance(p))
    }

    /// The character at a map square, or a space off the map.
    pub fn ch(&self, p: Pos) -> char {
        if p.on_map() {
            self.grid.cell(p).ch
        } else {
            ' '
        }
    }

    /// The game's colour at a map square; gray off the map.
    pub fn shade(&self, p: Pos) -> Shade {
        if p.on_map() {
            let cell = self.grid.cell(p);
            glyphs::shade(cell.fg, cell.bold)
        } else {
            Shade::GRAY
        }
    }

    /// Whether a map square is highlighted as the hero's pet.
    pub fn pet(&self, p: Pos) -> bool {
        p.on_map() && self.grid.cell(p).rev
    }

    /// What the game is waiting for: the first of these that holds.
    pub fn phase(&self) -> Phase {
        let text = self.asking && !self.msg.is_empty() && !self.counting();
        [
            (self.ended, Phase::Ended),
            (self.more, Phase::More),
            (self.direction, Phase::Direction),
            (self.obj.is_some(), Phase::Item),
            (self.spell.is_some(), Phase::Spell),
            (self.yn.is_some(), Phase::Choice),
            (text, Phase::Text),
            (self.menu, Phase::Menu),
            (self.getpos, Phase::Position),
            (self.normal, Phase::Play),
        ]
        .into_iter()
        .find_map(|(holds, phase)| holds.then_some(phase))
        .unwrap_or(Phase::Unknown)
    }

    /// Whether the message line echoes a count being typed (`Count: 20`): the game waits for the command it
    /// repeats, not for an answer.
    pub fn counting(&self) -> bool {
        let count = self.msg.strip_prefix("Count: ").unwrap_or("");
        self.asking && !count.is_empty() && count.bytes().all(|b| b.is_ascii_digit())
    }

    /// A cheap hash of cells and cursor, equal for identical screens.
    pub fn fingerprint(&self) -> u64 {
        // Each step is a bijection of the running hash for a fixed word, so screens differing in one cell never
        // collide.
        const K: u64 = 0x517c_c1b7_2722_0a95;
        let mix = |h: u64, w: u64| (h.rotate_left(5) ^ w).wrapping_mul(K);
        let mut h = mix(0, ((self.cursor.y as u64) << 8) | self.cursor.x as u64);
        for row in &self.grid.cells {
            for cell in row {
                let w = cell.ch as u64
                    | (cell.fg as u64) << 21
                    | (cell.bold as u64) << 26
                    | (cell.rev as u64) << 27;
                h = mix(h, w);
            }
        }
        h
    }

    /// The menu page shown, as (page, pages): `(n of m)`, or (1, 1) for `(end)`.
    pub fn menu_page(&self) -> Option<(u32, u32)> {
        self.rows.iter().find_map(|r| Some(footer(r)?.1))
    }

    /// The menu window's bounds as (left column, rows): rows `0..rows` cut at the left column hold the window,
    /// ending at its footer; (0, 24) when no footer is shown.
    pub fn menu_window(&self) -> (usize, usize) {
        let found = (0..ROWS).find_map(|r| Some((r, footer(&self.rows[r])?.0)));
        // Corner windows leave the map to their left; the footer has the entries' one-column inset.
        found.map_or((0, ROWS), |(r, col)| (col.saturating_sub(1), r + 1))
    }

    /// The selectable entries of a menu on screen, appended to `out`.
    pub fn menu_entries(&self, out: &mut Vec<MenuEntry>) {
        let (left, rows) = self.menu_window();
        for row in &self.rows[..rows] {
            out.extend(menu_entry(&row[byte_at(row, left)..]));
        }
    }

    /// The lines of a text window that ends in `--More--`, appended to `out`; false when `--More--` follows a
    /// message instead.
    pub fn more_window(&self, out: &mut Vec<String>) -> bool {
        let footer = (self.message_rows as usize..ROWS).find_map(|r| {
            let row = &self.rows[r];
            let head = &row[..row.find("--More--")?];
            // A window's footer stands apart; one that follows text on its row ends a message.
            (head.is_empty() || head.ends_with(' ')).then(|| (r, head.chars().count()))
        });
        let Some((rows, col)) = footer else {
            return false;
        };
        let lines = self.rows[..rows]
            .iter()
            .map(|line| line[byte_at(line, col)..].trim_end());
        out.extend(lines.skip_while(|text| text.is_empty()).map(str::to_owned));
        true
    }
}

/// One selectable menu row: `a - an uncursed dagger`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuEntry {
    pub key: char,
    pub text: String,
}

/// Whether `row` continues the message on `line` above it: the game splits a long message at a space, so a
/// continuation starts in column 0 with a word that would not have fitted, and not with map structure.
fn continues(line: &str, row: &str) -> bool {
    let first = row.split(' ').next().unwrap_or("");
    !line.is_empty()
        && !first.is_empty()
        && line.chars().count() + 1 + first.chars().count() > 79
        && (row.starts_with("--More--") || !row.starts_with(['|', '-', '#', '.', '+']))
}

/// Keeps a targeting cursor distinct from the hero after the game's automatic descriptions.
#[derive(Clone, Debug, Default)]
pub struct PromptContext {
    /// A positioning prompt is open.
    pub position: bool,
    /// The location and turn last shown while it is open.
    location: Option<String>,
    turn: Option<u32>,
}

impl PromptContext {
    /// Forgets any open positioning prompt.
    pub fn reset(&mut self) {
        *self = PromptContext::default();
    }

    /// Adjusts a freshly parsed view: while a positioning prompt is open, the cursor is the target, not the hero.
    pub fn observe(&mut self, view: &mut View) {
        let location = view.status.location.as_deref();
        let turn = view.status.turn;
        let moved = matches!((self.location.as_deref(), location), (Some(b), Some(a)) if a != b);
        let waited = matches!((self.turn, turn), (Some(b), Some(a)) if a != b);
        if view.ended || (self.position && (moved || waited)) {
            self.reset();
        }
        if view.getpos && !view.more && !view.menu {
            self.position = true;
        }
        if self.position && !view.ended {
            // Full-screen help and target menus can cover the status lines, so a missing field keeps its value.
            if location.is_some() {
                set_str(&mut self.location, location);
            }
            self.turn = turn.or(self.turn);
            view.getpos = true;
            view.prompt = true;
            view.hero = None;
            view.normal = false;
            view.engulfed = false;
        }
    }

    /// Notes keys about to be sent with `view` on screen: a pick or Escape closes the positioning prompt.
    pub fn before_input(&mut self, view: &View, keys: &[u8]) {
        // Help pages and target menus return to the positioning prompt; their Escape belongs to the nested window.
        if self.position && !view.more && !view.menu && keys.iter().any(|k| b".,;:\x1b".contains(k))
        {
            self.reset();
        }
    }
}

/// The text with `--More--` and anything after it cut off.
pub fn strip_more(text: &str) -> &str {
    text.find("--More--").map_or(text, |at| &text[..at])
}

/// Replaces an optional string, reusing its buffer.
fn set_str(slot: &mut Option<String>, value: Option<&str>) {
    match value {
        Some(v) => v.clone_into(slot.get_or_insert_default()),
        None => *slot = None,
    }
}

/// The byte offset of character column `col` in `row`, or its length past the end.
pub fn byte_at(row: &str, col: usize) -> usize {
    row.char_indices().nth(col).map_or(row.len(), |(i, _)| i)
}

/// The leading ASCII digits of `s` (at least one) and the rest.
fn digits(s: &str) -> Option<(&str, &str)> {
    let end = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    (end > 0).then(|| s.split_at(end))
}

/// The number at the start of `s`, negative after a `-` if `signed`, its size saturating at `i32::MAX`; and the
/// rest.
fn number(s: &str, signed: bool) -> Option<(i32, &str)> {
    let (negative, s) = match s.strip_prefix('-') {
        Some(rest) if signed => (true, rest),
        _ => (false, s),
    };
    let (d, rest) = digits(s)?;
    let n = d.parse().unwrap_or(i32::MAX);
    Some((if negative { -n } else { n }, rest))
}

/// The first number that directly follows `prefix` in `text`.
fn number_after(text: &str, prefix: &str, signed: bool) -> Option<i32> {
    text.match_indices(prefix)
        .find_map(|(i, _)| Some(number(&text[i + prefix.len()..], signed)?.0))
}

/// The first `prefix N(M)` in `text`, as (N, M).
fn pair_after(text: &str, prefix: &str, signed: bool) -> Option<(i32, i32)> {
    text.match_indices(prefix).find_map(|(i, _)| {
        let (n, rest) = number(&text[i + prefix.len()..], signed)?;
        let (m, rest) = number(rest.strip_prefix('(')?, false)?;
        rest.starts_with(')').then_some((n, m))
    })
}

/// The experience level shown as `Xp:N`, `XL:N` or `Exp:N`, whichever comes first.
fn experience(text: &str) -> Option<i32> {
    text.char_indices().find_map(|(i, _)| {
        let rest = &text[i..];
        let level = ["Xp:", "XL:", "Exp:"]
            .iter()
            .find_map(|p| rest.strip_prefix(p))?;
        Some(number(level, false)?.0)
    })
}

/// Strength as shown after `St:`: digits, optionally `/` and digits or `/**`.
fn strength(text: &str) -> Option<&str> {
    text.match_indices("St:").find_map(|(i, _)| {
        let shown = &text[i + 3..];
        let (whole, rest) = digits(shown)?;
        let fraction = match rest.strip_prefix('/') {
            Some(after) if after.starts_with("**") => 3,
            Some(after) => digits(after).map_or(0, |(d, _)| 1 + d.len()),
            None => 0,
        };
        Some(&shown[..whole.len() + fraction])
    })
}

/// The first location word in the second status line.
fn location(text: &str) -> Option<&str> {
    let mut word_starts = text
        .char_indices()
        .filter(|&(i, c)| is_word(c) && !text[..i].chars().next_back().is_some_and(is_word));
    word_starts.find_map(|(i, _)| {
        let rest = &text[i..];
        LOCATIONS.iter().find_map(|&(fixed, tail)| {
            let after = rest.strip_prefix(fixed)?;
            let tail = match tail {
                Tail::Nothing => "",
                Tail::Digits => digits(after)?.0,
                Tail::Word => after
                    .split(|c: char| !is_word(c))
                    .next()
                    .filter(|w| !w.is_empty())?,
            };
            let end = fixed.len() + tail.len();
            (!rest[end..].starts_with(is_word)).then(|| &rest[..end])
        })
    })
}

/// The answers of a choice prompt: a message ending in `[letters]`, optionally followed by a default ` (x)`.
fn choice_answers(msg: &str) -> Option<&str> {
    let without_default = msg.strip_suffix(')').and_then(|s| {
        let mut chars = s.chars();
        chars.next_back()?;
        chars.as_str().strip_suffix(" (")
    });
    let inner = without_default.unwrap_or(msg).strip_suffix(']')?;
    let head = inner.trim_end_matches(|c: char| c.is_ascii_alphabetic() || c == '#' || c == '-');
    let answers = &inner[head.len()..];
    (!answers.is_empty() && head.ends_with('[')).then_some(answers)
}

/// The letters of an object or spell prompt: a message ending in `[…]`, without a trailing ` or ?*`.
fn bracket_letters(msg: &str) -> Option<&str> {
    let inner = msg.strip_suffix(']')?;
    let after_close = inner.rsplit_once(']').map_or(inner, |(_, after)| after);
    let (_, letters) = after_close.split_once('[')?;
    Some(letters.strip_suffix(" or ?*").unwrap_or(letters))
}

/// The text of a row that starts with a role's farewell word, from the second character of the name on, so that a
/// ` the ` in it follows a name.
fn farewell(row: &str) -> Option<&str> {
    let name = FAREWELLS
        .iter()
        .find_map(|w| row.strip_prefix(w)?.strip_prefix(' '))?;
    let mut chars = name.chars();
    chars.next();
    Some(chars.as_str())
}

/// Whether a farewell names the hero and role: `NAME the ROLE...`.
fn names_role(farewell: &str) -> bool {
    matches!(
        (farewell.find(" the "), farewell.rfind("...")),
        (Some(the), Some(dots)) if dots > the + 5
    )
}

/// A menu footer at the end of a row: its column and the page it shows, (1, 1) for `(end)`.
fn footer(row: &str) -> Option<(usize, (u32, u32))> {
    let row = row.trim_end();
    if let Some(head) = row.strip_suffix("(end)") {
        return Some((head.chars().count(), (1, 1)));
    }
    let (head, shown) = row.strip_suffix(')')?.rsplit_once('(')?;
    let (page, rest) = number(shown, false)?;
    let (pages, rest) = number(rest.strip_prefix(" of ")?, false)?;
    rest.is_empty()
        .then(|| (head.chars().count(), (page as u32, pages as u32)))
}

/// A menu row `KEY MARK TEXT`, such as `a - a dagger` or `b + 2 arrows`. The mark is `-`, `#` or `+`/`*` for an
/// entry not, partly or wholly selected.
fn menu_entry(row: &str) -> Option<MenuEntry> {
    let mut chars = row.trim_start().chars();
    let key = chars.next().filter(|c| ('!'..='~').contains(c))?;
    let rest = chars.as_str().strip_prefix(char::is_whitespace)?;
    let rest = rest.trim_start().strip_prefix(['-', '#', '+', '*'])?;
    let text = rest.strip_prefix(char::is_whitespace)?.trim().to_owned();
    (!text.is_empty()).then_some(MenuEntry { key, text })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testkit::{self, STATUS1, STATUS2, grid, screen};
    use crate::types::Colour;

    fn scr(top: &str, rows: &[&str]) -> Vec<String> {
        screen(top, rows, STATUS1, STATUS2)
    }

    /// A view with the cursor where the default room's hero stands.
    fn view(lines: &[String]) -> View {
        testkit::view(lines, 2, 3)
    }

    fn room() -> Vec<String> {
        scr("", &[" ---- ", " |.@.| ", " ---- "])
    }

    fn padded(text: &str) -> String {
        format!("{text:<80}")
    }

    #[test]
    fn a_count_echo_is_no_prompt() {
        let counting = testkit::view(&scr("Count: 20", &[" ---- ", " |.@.| ", " ---- "]), 0, 9);
        assert!(counting.counting());
        assert_eq!(counting.phase(), Phase::Unknown);
        let named = testkit::view(
            &scr("Count: what?", &[" ---- ", " |.@.| ", " ---- "]),
            0,
            12,
        );
        assert!(!named.counting());
        assert_eq!(named.phase(), Phase::Text);
    }

    #[test]
    fn quest_and_endgame_status_labels() {
        for location in [
            "Home 1",
            "Home 6",
            "Earth",
            "Air",
            "Fire",
            "Water",
            "Astral Plane",
            "Fort Ludios",
            "Tutorial:1",
            "Plane of Fire",
            "End Game",
        ] {
            let status2 = format!("{location} $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400");
            let v = view(&screen(
                "",
                &[" ---- ", " |.@.| ", " ---- "],
                STATUS1,
                &status2,
            ));
            assert_eq!(v.status.location.as_deref(), Some(location));
            assert!(v.normal, "{location}");
        }
        let v = view(&screen(
            "",
            &[],
            "Fire the Evoker St:8 Dx:11 Co:15 In:18 Wi:11 Ch:12 Chaotic",
            STATUS2,
        ));
        assert_eq!(v.status.location.as_deref(), Some("Dlvl:3"));
        let v = view(&screen("", &[], STATUS1, "Earthly $:0 HP:1(1)"));
        assert_eq!(v.status.location, None);
    }

    #[test]
    fn status_and_hero() {
        let v = view(&room());
        assert!(v.normal);
        assert_eq!(v.hero, Some(Pos::new(2, 3)));
        let s = &v.status;
        assert_eq!(s.dlvl, Some(3));
        assert_eq!((s.hp, s.hpmax, s.turn), (Some(12), Some(16), Some(400)));
        assert_eq!(
            (s.pw, s.pwmax, s.ac, s.xl, s.gold),
            (Some(2), Some(2), Some(6), Some(2), Some(0))
        );
        assert_eq!(s.strength.as_deref(), Some("16"));
        assert_eq!(
            [s.dex, s.con, s.int, s.wis, s.cha],
            [Some(12), Some(14), Some(9), Some(10), Some(8)]
        );
        assert_eq!(s.alignment, Some("Lawful"));
        assert_eq!(v.phase(), Phase::Play);
    }

    #[test]
    fn negative_and_alternative_status_fields() {
        let v = view(&screen(
            "",
            &[],
            STATUS1,
            "Dlvl:12 $:40 HP:-3(20) Pw:0(5) AC:-4 Exp:7 T:9",
        ));
        let s = &v.status;
        assert_eq!(
            (s.hp, s.hpmax, s.ac, s.xl),
            (Some(-3), Some(20), Some(-4), Some(7))
        );
        let v = view(&screen("", &[], "Hero St:3", "HP:4 XL:3 T:x"));
        assert_eq!(
            (v.status.hp, v.status.xl, v.status.turn),
            (None, Some(3), None)
        );
        assert_eq!(v.status.alignment, None);
    }

    #[test]
    fn exceptional_strength_is_preserved() {
        for strength in ["18/50", "18/**", "25"] {
            let status1 =
                format!("Hero the Warrior St:{strength} Dx:12 Co:14 In:9 Wi:10 Ch:8 Neutral");
            let v = view(&screen("", &[], &status1, STATUS2));
            assert_eq!(v.status.strength.as_deref(), Some(strength));
            assert_eq!(v.status.alignment, Some("Neutral"));
        }
    }

    #[test]
    fn prompts() {
        assert!(testkit::view(&scr("You hit the jackal.--More--", &[]), 0, 30).more);
        let v = testkit::view(&scr("Really attack the gnome? [yn] (n)", &[]), 0, 30);
        assert_eq!(v.yn.as_deref(), Some("yn"));
        assert_eq!(v.phase(), Phase::Choice);
        let v = testkit::view(&scr("What do you want to eat? [fg or ?*]", &[]), 0, 30);
        assert_eq!((v.obj.as_deref(), v.yn.as_deref()), (Some("fg"), None));
        assert_eq!(v.phase(), Phase::Item);
        assert!(!v.normal);
        assert!(v.hero.is_none());
    }

    #[test]
    fn answered_question_is_not_live() {
        let v = view(&scr(
            "Really attack the gnome? [yn] (n) y",
            &[" ---- ", " |.@.| "],
        ));
        assert!(!v.asking);
        assert_eq!(v.yn, None);
        assert!(v.normal);
        let v = view(&scr(
            "Really attack the gnome? [yn] (n)",
            &[" ---- ", " |.@.| "],
        ));
        assert_eq!(v.yn, None);
        assert_eq!(v.phase(), Phase::Play);
    }

    #[test]
    fn choice_answers_forms() {
        assert_eq!(choice_answers("Continue? [ynq]"), Some("ynq"));
        assert_eq!(choice_answers("Pay? [yn] (y)"), Some("yn"));
        assert_eq!(choice_answers("Which? [a-c#]"), Some("a-c#"));
        assert_eq!(choice_answers("Which? [a b]"), None);
        assert_eq!(choice_answers("Which? []"), None);
        assert_eq!(choice_answers("no brackets"), None);
        assert_eq!(bracket_letters("What? [[ab]"), Some("[ab"));
        assert_eq!(bracket_letters("What? [x] then [ab or ?*]"), Some("ab"));
        assert_eq!(bracket_letters("What? [- or ?*]"), Some("-"));
        assert_eq!(bracket_letters("What? ]"), None);
    }

    #[test]
    fn farlook_cursor_is_not_the_hero() {
        let v = view(&scr("Move cursor to a monster, object or location:", &[]));
        assert!(v.getpos);
        assert!(!v.normal);
        assert!(v.hero.is_none());
    }

    #[test]
    fn source_position_prompts() {
        for message in [
            "(For instructions type a '?')",
            "Move cursor to the desired position:",
            "Move cursor to the spot to hit:",
            "Where do you want to cast the spell?",
        ] {
            let v = view(&scr(message, &[]));
            assert_eq!(v.phase(), Phase::Position, "{message}");
            assert!(v.hero.is_none());
        }
    }

    #[test]
    fn spell_and_custom_direction_prompts() {
        let v = testkit::view(&scr("Cast which spell? [a-c *?]", &[]), 0, 28);
        assert_eq!(
            (v.phase(), v.spell.as_deref()),
            (Phase::Spell, Some("a-c *?"))
        );
        for message in [
            "Talk to whom? (in what direction)",
            "At whom? (in what direction)",
            "Open where? [.>]",
            "In What Direction?",
        ] {
            let v = testkit::view(&scr(message, &[]), 0, message.len() as u8);
            assert_eq!(v.phase(), Phase::Direction, "{message}");
        }
    }

    #[test]
    fn wrapped_text_remains_input() {
        let lines = scr(
            &format!("What do you want to engrave? {}", "x".repeat(52)),
            &["xxx"],
        );
        let v = testkit::view(&lines, 1, 3);
        assert_eq!(v.message_rows, 2);
        assert_eq!(v.phase(), Phase::Text);
        assert!(v.text);
        assert!(v.hero.is_none());
    }

    #[test]
    fn message_split_at_a_space_continues_on_the_next_row() {
        let first = "(The seed chose your character; your role, rank, gender and alignment options";
        let v = testkit::view(
            &scr(first, &["were ignored.)--More--", "", " ---- ", " |.@.| "]),
            1,
            22,
        );
        assert_eq!(v.msg, format!("{first} were ignored.)--More--"));
        assert_eq!(v.phase(), Phase::More);
        let pad = " ".repeat(31);
        let corner = testkit::view(
            &scr(
                &format!("{pad}There is a staircase up out of the dungeon here."),
                &[
                    "",
                    &format!("{pad}Things that are here:"),
                    &format!("{pad}--More--"),
                ],
            ),
            3,
            39,
        );
        assert_eq!(
            corner.msg,
            "There is a staircase up out of the dungeon here."
        );
        assert_eq!(corner.message_rows, 1);
    }

    #[test]
    fn continuation_with_dots_or_more_is_part_of_the_message() {
        for (first, rest) in [
            (
                "You read: \"They say that a gnome with a wand of digging can do wonders, but so",
                "can you...--More--",
            ),
            (
                "You hear the footsteps of a guard on patrol.  You see here a piece of the",
                "piece.--More--",
            ),
        ] {
            let v = testkit::view(
                &scr(first, &[rest, "", " ---- ", " |.@.| "]),
                1,
                rest.len() as u8,
            );
            assert_eq!(v.msg, format!("{first} {rest}"));
            assert_eq!(v.message_rows, 2);
        }
    }

    #[test]
    fn map_rows_never_continue_a_message() {
        let first = "x".repeat(78);
        for row in ["|....", "-----", "#....", ".....", "+...."] {
            assert!(!continues(&first, row));
        }
        assert!(continues(&first, "--More--"));
        assert!(continues(&first, "ab"));
        assert!(!continues(&first, " ab"));
        assert!(!continues("", "ab"));
        assert!(!continues("short", "ab"));
    }

    #[test]
    fn menu_search_reads_text_above_existing_menu() {
        let v = testkit::view(&scr("Search for:", &[" a - a dagger", " (end)"]), 0, 12);
        assert!(v.menu);
        assert_eq!(v.phase(), Phase::Text);
    }

    #[test]
    fn menu_entries_window_and_page() {
        let pad = " ".repeat(40);
        let v = testkit::view(
            &scr(
                &format!("{pad} Weapons"),
                &[
                    &format!("{:<40} a - an uncursed dagger", " |..@..|"),
                    &format!("{pad} b + 2 arrows (in quiver)"),
                    &format!("{pad} c # some food"),
                    &format!("{pad} d *   "),
                    &format!("{pad} (1 of 2)"),
                ],
            ),
            5,
            50,
        );
        assert!(v.menu);
        assert_eq!(v.phase(), Phase::Menu);
        assert_eq!(v.menu_page(), Some((1, 2)));
        assert_eq!(v.menu_window(), (40, 6));
        let mut entries = Vec::new();
        v.menu_entries(&mut entries);
        let got: Vec<(char, &str)> = entries.iter().map(|e| (e.key, e.text.as_str())).collect();
        assert_eq!(
            got,
            [
                ('a', "an uncursed dagger"),
                ('b', "2 arrows (in quiver)"),
                ('c', "some food"),
            ]
        );
        let end = testkit::view(&scr("", &[" a - a dagger", " (end)"]), 2, 6);
        assert_eq!(end.menu_page(), Some((1, 1)));
        assert_eq!(end.menu_window(), (0, 3));
        let plain = view(&room());
        assert_eq!((plain.menu_page(), plain.menu_window()), (None, (0, ROWS)));
    }

    #[test]
    fn more_window_lines() {
        let pad = " ".repeat(30);
        let v = testkit::view(
            &scr(
                "",
                &[
                    &format!("{pad}Things that are here:"),
                    &format!("{pad}a dagger"),
                    &format!("{pad}--More--"),
                ],
            ),
            3,
            38,
        );
        let mut lines = Vec::new();
        assert!(v.more_window(&mut lines));
        assert_eq!(lines, ["Things that are here:", "a dagger"]);
        let message = testkit::view(&scr("You hit the jackal.--More--", &[]), 0, 27);
        lines.clear();
        assert!(!message.more_window(&mut lines));
        assert!(lines.is_empty());
    }

    #[test]
    fn hero_found_by_nearest_at_sign() {
        let lines = scr("", &[" ---- ", " |...| ", " ---- ", "  @     @"]);
        let v = testkit::view(&lines, 4, 7);
        assert_eq!(v.hero, Some(Pos::new(4, 8)));
        let v = testkit::view(&lines, 0, 0);
        assert_eq!(v.hero, Some(Pos::new(4, 2)));
        let blank = testkit::view(&scr("", &[]), 0, 0);
        assert_eq!(blank.hero, None);
        assert_eq!(blank.phase(), Phase::Unknown);
    }

    #[test]
    fn engulfed_hero() {
        let v = testkit::view(&scr("", &[" /-\\", " |@|", " \\-/"]), 2, 2);
        assert_eq!(v.hero, Some(Pos::new(2, 2)));
        assert!(v.engulfed);
        assert!(!view(&room()).engulfed);
    }

    #[test]
    fn cell_accessors_off_the_map() {
        let mut g = grid(&room());
        g.cells[2][3].fg = Colour::Blue;
        g.cells[2][3].bold = true;
        g.cells[2][2].rev = true;
        g.cells[0][0].rev = true;
        let v = View::parse(&g, Pos::new(2, 3));
        assert_eq!(v.ch(Pos::new(2, 3)), '@');
        assert_eq!(v.ch(Pos::new(0, 0)), ' ');
        assert!(v.shade(Pos::new(2, 3)).bright);
        assert_eq!(v.shade(Pos::new(23, 0)), Shade::GRAY);
        assert!(v.pet(Pos::new(2, 2)));
        assert!(!v.pet(Pos::new(0, 0)));
    }

    #[test]
    fn fingerprints_tell_screens_apart() {
        let a = view(&room());
        let b = view(&room());
        assert_eq!(a.fingerprint(), b.fingerprint());
        let moved = testkit::view(&room(), 2, 4);
        assert_ne!(a.fingerprint(), moved.fingerprint());
        let mut g = grid(&room());
        g.cells[2][3].rev = true;
        assert_ne!(
            a.fingerprint(),
            View::parse(&g, Pos::new(2, 3)).fingerprint()
        );
    }

    #[test]
    fn reparse_matches_parse() {
        let mut v = testkit::view(&scr("Really attack the gnome? [yn] (n)", &[]), 0, 30);
        let lines = room();
        v.reparse(&grid(&lines), Pos::new(2, 3));
        let fresh = view(&lines);
        assert_eq!(v.yn, None);
        assert_eq!(v.msg, fresh.msg);
        assert_eq!(v.hero, fresh.hero);
        assert_eq!(v.status, fresh.status);
        assert_eq!(v.fingerprint(), fresh.fingerprint());
    }

    #[test]
    fn short_condition_forms() {
        let v = testkit::view(
            &screen(
                "",
                &[],
                STATUS1,
                "Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400 TermIll Ston Blnd Trap",
            ),
            0,
            0,
        );
        assert_eq!(v.cond, glyphs::conditions("Blind TermIll Stone Trapped"));
        let v = view(&screen(
            "",
            &[],
            &format!("{:<40}Hungry Conf", "Weak the Stripling"),
            STATUS2,
        ));
        assert_eq!(v.cond, glyphs::conditions("Hungry Conf"));
    }

    #[test]
    fn death_message_allows_life_saving_pagination() {
        assert_eq!(
            testkit::view(&scr("You die...--More--", &[]), 0, 20).phase(),
            Phase::More
        );
        assert!(!testkit::view(&scr("You die...", &[]), 0, 10).ended);
    }

    #[test]
    fn explicit_ascension_is_an_observed_terminal_result() {
        for message in [
            "You ascend to the status of Demigod...--More--",
            "You ascend to the status of Demigoddess...",
            "Goodbye Jane Doe the Demigoddess...",
        ] {
            let v = view(&scr(message, &[]));
            assert_eq!(v.phase(), Phase::Ended);
            assert_eq!(v.end, Some(EndKind::Ascended));
        }
        let v = view(&scr(
            "You read: \"You ascend to the status of Demigod...\"",
            &[],
        ));
        assert_eq!(v.end, None);
    }

    #[test]
    fn engravings_are_not_death() {
        let v = testkit::view(
            &scr(
                "You read: \"They say that you will be killed by an exploding tin.  You die...\"",
                &[],
            ),
            0,
            0,
        );
        assert!(!v.ended);
        let v = testkit::view(
            &scr("Do you want your possessions identified? [ynq] (n)", &[]),
            0,
            50,
        );
        assert!(v.ended);
        assert_eq!(v.end, Some(EndKind::Disclosure));
    }

    #[test]
    fn every_disclosure_question_ends_play() {
        for question in [
            "Do you want to see what you had when you died? [ynq] (n)",
            "Do you want to see your attributes? [ynq] (n)",
            "Do you want an account of creatures vanquished? [ynaq] (n)",
            "Do you want a list of species genocided? [ynq] (n)",
            "Do you want to see your conduct and achievements? [ynq] (n)",
            "Do you want to see the dungeon overview? [ynq] (n)",
        ] {
            let v = testkit::view(&scr(question, &[]), 0, question.len() as u8);
            assert_eq!(v.end, Some(EndKind::Disclosure), "{question}");
            assert_eq!(v.phase(), Phase::Ended);
        }
    }

    #[test]
    fn all_role_farewells_end_play() {
        for word in ["Goodbye", "Fare thee well", "Sayonara", "Aloha", "Farvel"] {
            let v = view(&scr(&format!("{word} Jane Doe the Demigoddess..."), &[]));
            assert_eq!(v.phase(), Phase::Ended);
            let v = view(&scr("", &[&format!("  {word} Hero the Valkyrie...")]));
            assert_eq!(v.end, Some(EndKind::Farewell), "{word}");
        }
        assert!(!view(&scr("You read: \"Goodbye Hero the Valkyrie...\"", &[])).ended);
        assert!(!view(&scr("Goodbye the end", &[])).ended);
        assert_eq!(
            view(&scr("", &["Be seeing you..."])).end,
            Some(EndKind::Farewell)
        );
    }

    #[test]
    fn tombstone_is_recognised() {
        let stone = [
            "                       ----------",
            "                      /          \\",
            "                     /    REST    \\",
            "                    /      IN      \\",
            "                   /     PEACE      \\",
            "                  /                  \\",
            "                  |       Hero       |",
            "                  |  killed by a jackal |",
            "",
            "Goodbye Hero the Stripling...",
        ];
        let v = testkit::view(&scr("", &stone), 12, 0);
        assert_eq!(v.end, Some(EndKind::Tombstone));
        assert_eq!(v.phase(), Phase::Ended);
        assert!(!view(&scr("You read: \"REST IN PEACE\"", &[])).ended);
        assert!(!view(&scr("Take a REST", &["IN the", "PEACE"])).ended);
    }

    #[test]
    fn descriptions_keep_targeting_until_confirmation() {
        let mut context = PromptContext::default();
        let mut v = view(&scr("Move cursor to the desired position:", &[]));
        context.observe(&mut v);
        assert!(context.position);
        context.before_input(&v, b"l");
        let mut described = testkit::view(&scr("floor of a room [03,05]", &[]), 2, 4);
        context.observe(&mut described);
        assert_eq!(described.phase(), Phase::Position);
        assert!(described.hero.is_none());
        assert!(!described.normal);
        context.before_input(&described, b".");
        let mut after = view(&room());
        context.observe(&mut after);
        assert_eq!(after.phase(), Phase::Play);
    }

    fn targeting() -> (PromptContext, View) {
        let mut context = PromptContext::default();
        let mut v = view(&scr("Move cursor to the desired position:", &[]));
        context.observe(&mut v);
        (context, v)
    }

    #[test]
    fn nested_windows_do_not_end_targeting() {
        for (lines, y, x, key) in [
            (scr("Targeting instructions --More--", &[]), 0, 37, b" "),
            (
                scr("Choose a target", &[" a - goblin", " (end)"]),
                2,
                6,
                b"\x1b",
            ),
        ] {
            let (mut context, _) = targeting();
            let mut nested = testkit::view(&lines, y, x);
            context.observe(&mut nested);
            context.before_input(&nested, key);
            let mut described = testkit::view(&scr("floor of a room [03,05]", &[]), 2, 4);
            context.observe(&mut described);
            assert_eq!(described.phase(), Phase::Position);
        }
    }

    #[test]
    fn full_height_help_preserves_targeting_without_status() {
        for (foot, key) in [("--More--", b" "), ("(end)", b"\x1b")] {
            let (mut context, _) = targeting();
            let mut lines = vec![padded("Targeting instructions"); ROWS - 1];
            lines.push(padded(foot));
            let mut nested = testkit::view(&lines, 23, foot.len() as u8);
            context.observe(&mut nested);
            assert_eq!(nested.status.location, None);
            assert_eq!(nested.status.turn, None);
            context.before_input(&nested, key);
            let mut restored = testkit::view(&scr("floor of a room [03,05]", &[]), 2, 4);
            context.observe(&mut restored);
            assert_eq!(restored.phase(), Phase::Position);
            assert!(restored.hero.is_none());
        }
    }

    #[test]
    fn selection_and_changed_turn_end_targeting() {
        for key in b".,;:\x1b" {
            let (mut context, v) = targeting();
            context.before_input(&v, &[*key]);
            let mut after = view(&room());
            context.observe(&mut after);
            assert_eq!(after.phase(), Phase::Play);
        }
        let (mut context, _) = targeting();
        let mut changed = view(&screen(
            "",
            &[" ---- ", " |.@.| ", " ---- "],
            STATUS1,
            "Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:401",
        ));
        context.observe(&mut changed);
        assert_eq!(changed.phase(), Phase::Play);
        assert!(!context.position);
    }

    #[test]
    fn ordinary_description_never_starts_targeting() {
        let mut v = view(&scr(
            "floor of a room [03,05]",
            &[" ---- ", " |.@.| ", " ---- "],
        ));
        PromptContext::default().observe(&mut v);
        assert_eq!(v.phase(), Phase::Play);
    }
}
