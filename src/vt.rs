//! A VT100/xterm screen emulator, enough of the protocol for NetHack's tty port on an 80×24 terminal.

use crate::types::{COLS, Cell, Colour, Grid, Pos, ROWS};

/// Longest escape sequence kept; a longer one is dropped.
pub const ESCAPE_MAX: usize = 64;

/// The characters the DEC special graphics set redraws, and what it draws for each.
const DEC: [(char, char); 25] = [
    ('`', '◆'),
    ('a', '▒'),
    ('f', '°'),
    ('g', '±'),
    ('j', '┘'),
    ('k', '┐'),
    ('l', '┌'),
    ('m', '└'),
    ('n', '┼'),
    ('o', '⎺'),
    ('p', '⎻'),
    ('q', '─'),
    ('r', '⎼'),
    ('s', '⎽'),
    ('t', '├'),
    ('u', '┤'),
    ('v', '┴'),
    ('w', '┬'),
    ('x', '│'),
    ('y', '≤'),
    ('z', '≥'),
    ('{', 'π'),
    ('|', '≠'),
    ('}', '£'),
    ('~', '·'),
];

/// The character the DEC special graphics set draws for `ch` in line-drawing mode; other characters are unchanged.
pub fn dec_graphics(ch: char) -> char {
    DEC.iter()
        .find(|&&(from, _)| from == ch)
        .map_or(ch, |&(_, to)| to)
}

/// The attributes new characters are written with.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Attr {
    /// Foreground colour.
    pub fg: Colour,
    /// Bold.
    pub bold: bool,
    /// Reverse video.
    pub rev: bool,
}

/// The most parameters a sequence that fits in `ESCAPE_MAX` bytes can carry.
const PARAMS_MAX: usize = ESCAPE_MAX;

/// Every row's bit in a touched mask.
const ALL_ROWS: u32 = (1 << ROWS) - 1;

/// Stands in for a non-ASCII character inside an escape sequence, which no sequence uses.
const FOREIGN: u8 = 0x80;

/// The numeric parameters of one control sequence; `None` is an empty or non-numeric parameter.
struct Params {
    nums: [Option<u32>; PARAMS_MAX],
    len: usize,
}

impl Params {
    /// Parameter `i`, or `default` when absent or empty; with a default of 1, zero also reads as 1.
    fn num(&self, i: usize, default: u32) -> u32 {
        let v = self.get(i).unwrap_or(default);
        if v == 0 && default == 1 { 1 } else { v }
    }

    fn get(&self, i: usize) -> Option<u32> {
        if i < self.len { self.nums[i] } else { None }
    }

    fn contains(&self, n: u32) -> bool {
        self.nums[..self.len].contains(&Some(n))
    }
}

/// The emulator: a grid, a cursor and the parser state between `feed` calls.
#[derive(Clone)]
pub struct Vt {
    grid: Grid,
    cursor: Pos,
    attr: Attr,
    top: u8,
    bottom: u8,
    wrap: bool,
    saved: (Pos, Attr),
    dec: bool,
    escaping: bool,
    esc: [u8; ESCAPE_MAX],
    esc_len: usize,
    utf8: [u8; 4],
    utf8_len: usize,
    complete: bool,
    touched: u32,
}

impl Default for Vt {
    fn default() -> Vt {
        Vt::new()
    }
}

impl Vt {
    /// A blank 80×24 screen with the cursor home and every row marked touched.
    pub fn new() -> Vt {
        Vt {
            grid: Grid::new(),
            cursor: Pos::new(0, 0),
            attr: Attr::default(),
            top: 0,
            bottom: (ROWS - 1) as u8,
            wrap: false,
            saved: (Pos::new(0, 0), Attr::default()),
            dec: false,
            escaping: false,
            esc: [0; ESCAPE_MAX],
            esc_len: 0,
            utf8: [0; 4],
            utf8_len: 0,
            complete: false,
            touched: ALL_ROWS,
        }
    }

    /// Clears the screen and all parser state, as after a terminal reset.
    pub fn reset(&mut self) {
        self.grid = Grid::new();
        self.cursor = Pos::new(0, 0);
        self.attr = Attr::default();
        self.top = 0;
        self.bottom = (ROWS - 1) as u8;
        self.wrap = false;
        self.saved = (Pos::new(0, 0), Attr::default());
        self.dec = false;
        self.escaping = false;
        self.esc_len = 0;
        self.utf8_len = 0;
        self.complete = false;
        self.touched = ALL_ROWS;
    }

    /// Interprets terminal output. UTF-8 split across calls is carried over; invalid UTF-8 becomes U+FFFD.
    pub fn feed(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.byte(b);
        }
    }

    /// The current cells.
    pub fn grid(&self) -> &Grid {
        &self.grid
    }

    /// The cursor as (row, column).
    pub fn cursor(&self) -> Pos {
        self.cursor
    }

    /// Whether the whole screen has been cleared since the last reset, so every cell is current.
    pub fn complete(&self) -> bool {
        self.complete
    }

    /// Forgets which rows were touched.
    pub fn clear_touched(&mut self) {
        self.touched = 0;
    }

    /// Whether any of the first `rows` rows was written since `clear_touched`: an untouched message line holds
    /// old text.
    pub fn message_written(&self, rows: usize) -> bool {
        let mask = if rows >= 32 {
            u32::MAX
        } else {
            (1u32 << rows) - 1
        };
        self.touched & mask != 0
    }

    /// Decodes one byte of UTF-8. A byte that cannot continue the pending sequence ends it as U+FFFD and is then
    /// decoded afresh.
    fn byte(&mut self, b: u8) {
        if self.utf8_len > 0 {
            if continues_utf8(self.utf8[0], self.utf8_len, b) {
                self.utf8[self.utf8_len] = b;
                self.utf8_len += 1;
                let need = utf8_len(self.utf8[0]);
                if self.utf8_len == need {
                    self.utf8_len = 0;
                    let ch = std::str::from_utf8(&self.utf8[..need])
                        .ok()
                        .and_then(|s| s.chars().next())
                        .unwrap_or(char::REPLACEMENT_CHARACTER);
                    self.char(ch);
                }
                return;
            }
            self.utf8_len = 0;
            self.char(char::REPLACEMENT_CHARACTER);
        }
        match b {
            0x00..=0x7f => self.char(b as char),
            0xc2..=0xf4 => {
                self.utf8[0] = b;
                self.utf8_len = 1;
            }
            _ => self.char(char::REPLACEMENT_CHARACTER),
        }
    }

    fn char(&mut self, ch: char) {
        if self.escaping {
            match ch {
                // A new Escape abandons the unfinished sequence and starts another; CAN and SUB cancel it. Other
                // controls take effect without ending the sequence, and DEL is ignored, as on a hardware terminal.
                '\x1b' => self.esc_len = 0,
                '\x18' | '\x1a' => self.end_escape(),
                '\x7f' => {}
                '\x00'..='\x1f' => self.control(ch),
                _ => {
                    self.esc[self.esc_len] = if ch.is_ascii() { ch as u8 } else { FOREIGN };
                    self.esc_len += 1;
                    self.escape();
                }
            }
        } else if ch == '\x1b' {
            self.escaping = true;
            self.esc_len = 0;
        } else if (ch as u32) < 0x20 || ch == '\x7f' {
            self.control(ch);
        } else {
            self.put(ch);
        }
    }

    fn end_escape(&mut self) {
        self.escaping = false;
        self.esc_len = 0;
    }

    fn touch(&mut self, row: usize) {
        self.touched |= 1 << row;
    }

    fn y(&self) -> usize {
        self.cursor.y as usize
    }

    fn x(&self) -> usize {
        self.cursor.x as usize
    }

    fn move_to(&mut self, y: usize, x: usize) {
        self.cursor = Pos::new(y.min(ROWS - 1) as u8, x.min(COLS - 1) as u8);
    }

    fn put(&mut self, ch: char) {
        let ch = if self.dec { dec_graphics(ch) } else { ch };
        if self.wrap {
            self.wrap = false;
            self.cursor.x = 0;
            self.linefeed();
        }
        let (y, x) = (self.y(), self.x());
        self.grid.cells[y][x] = Cell {
            ch,
            fg: self.attr.fg,
            bold: self.attr.bold,
            rev: self.attr.rev,
        };
        self.touch(y);
        if x == COLS - 1 {
            self.wrap = true;
        } else {
            self.cursor.x += 1;
        }
    }

    fn control(&mut self, ch: char) {
        match ch {
            '\r' => {
                self.cursor.x = 0;
                self.wrap = false;
            }
            '\n' | '\x0b' | '\x0c' => {
                self.wrap = false;
                self.linefeed();
            }
            '\x08' => {
                self.cursor.x = self.cursor.x.saturating_sub(1);
                self.wrap = false;
            }
            '\t' => {
                let x = (self.x() / 8 + 1) * 8;
                self.cursor.x = x.min(COLS - 1) as u8;
            }
            '\x0e' => self.dec = true,
            '\x0f' => self.dec = false,
            _ => {}
        }
    }

    fn linefeed(&mut self) {
        if self.cursor.y == self.bottom {
            self.scroll(self.top as usize, self.bottom as usize, 1);
        } else if self.y() < ROWS - 1 {
            self.cursor.y += 1;
        }
    }

    fn reverse_index(&mut self) {
        if self.cursor.y == self.top {
            self.scroll(self.top as usize, self.bottom as usize, -1);
        } else {
            self.cursor.y = self.cursor.y.saturating_sub(1);
        }
    }

    /// Blanks columns `x0..x1` of row `y`.
    fn blank(&mut self, y: usize, x0: usize, x1: usize) {
        self.touch(y);
        let x1 = x1.min(COLS);
        if x0 < x1 {
            self.grid.cells[y][x0..x1].fill(Cell::BLANK);
        }
    }

    fn blank_rows(&mut self, from: usize, to: usize) {
        for y in from..to {
            self.blank(y, 0, COLS);
        }
    }

    /// Scrolls rows `top..=bottom` up by `n`, or down when `n` is negative, blanking the rows uncovered.
    fn scroll(&mut self, top: usize, bottom: usize, n: i64) {
        for y in top..=bottom {
            self.touch(y);
        }
        let block = &mut self.grid.cells[top..=bottom];
        let k = (n.unsigned_abs() as usize).min(block.len());
        let len = block.len();
        if n > 0 {
            block.rotate_left(k);
            block[len - k..].fill([Cell::BLANK; COLS]);
        } else {
            block.rotate_right(k);
            block[..k].fill([Cell::BLANK; COLS]);
        }
    }

    fn escape(&mut self) {
        let first = self.esc[0];
        if self.esc_len == 1 {
            match first {
                b'[' | b'(' | b')' | b'#' | b'%' => return,
                b'7' => self.saved = (self.cursor, self.attr),
                b'8' => {
                    (self.cursor, self.attr) = self.saved;
                    self.wrap = false;
                }
                b'M' => {
                    self.wrap = false;
                    self.reverse_index();
                }
                b'D' => {
                    self.wrap = false;
                    self.linefeed();
                }
                b'E' => {
                    self.wrap = false;
                    self.cursor.x = 0;
                    self.linefeed();
                }
                b'c' => {
                    self.reset();
                    self.complete = true;
                }
                _ => {}
            }
            self.end_escape();
            return;
        }
        if matches!(first, b'(' | b')' | b'#' | b'%') {
            if first == b'(' {
                self.dec = self.esc[1] == b'0';
            }
            self.end_escape();
            return;
        }
        let last = self.esc[self.esc_len - 1];
        if (b'@'..=b'~').contains(&last) {
            let params = self.params();
            let private =
                matches!(self.esc.get(1), Some(b'?' | b'>' | b'=' | b'!')) && self.esc_len > 2;
            self.end_escape();
            self.csi(&params, private, last);
        } else if self.esc_len >= ESCAPE_MAX {
            self.end_escape();
        }
    }

    /// The parameters between `[` and the final byte, after any private-mode markers.
    fn params(&self) -> Params {
        let mut params = Params {
            nums: [None; PARAMS_MAX],
            len: 0,
        };
        let raw = &self.esc[1..self.esc_len - 1];
        if raw.is_empty() {
            return params;
        }
        let start = raw
            .iter()
            .position(|b| !matches!(b, b'?' | b'>' | b'=' | b'!'))
            .unwrap_or(raw.len());
        for part in raw[start..].split(|&b| b == b';') {
            if params.len == PARAMS_MAX {
                break;
            }
            params.nums[params.len] = if !part.is_empty() && part.iter().all(u8::is_ascii_digit) {
                Some(part.iter().fold(0u32, |n, &d| {
                    n.saturating_mul(10).saturating_add((d - b'0') as u32)
                }))
            } else {
                None
            };
            params.len += 1;
        }
        params
    }

    fn csi(&mut self, p: &Params, private: bool, final_byte: u8) {
        // Every sequence but a colour change ends a pending wrap at the last column.
        if final_byte != b'm' {
            self.wrap = false;
        }
        let (y, x) = (self.y(), self.x());
        let n = p.num(0, 1) as usize;
        match final_byte {
            b'H' | b'f' => self.move_to(n - 1, p.num(1, 1) as usize - 1),
            b'A' => self.move_to(y.saturating_sub(n), x),
            b'B' => self.move_to(y.saturating_add(n), x),
            b'C' => self.move_to(y, x.saturating_add(n)),
            b'D' => self.move_to(y, x.saturating_sub(n)),
            b'E' => self.move_to(y.saturating_add(n), 0),
            b'F' => self.move_to(y.saturating_sub(n), 0),
            b'G' | b'`' => self.move_to(y, n - 1),
            b'd' => self.move_to(n - 1, x),
            b'J' => match p.num(0, 0) {
                0 => {
                    self.blank(y, x, COLS);
                    self.blank_rows(y + 1, ROWS);
                    if y == 0 && x == 0 {
                        self.complete = true;
                    }
                }
                1 => {
                    self.blank_rows(0, y);
                    self.blank(y, 0, x + 1);
                }
                _ => {
                    self.blank_rows(0, ROWS);
                    self.complete = true;
                }
            },
            b'K' => match p.num(0, 0) {
                0 => self.blank(y, x, COLS),
                1 => self.blank(y, 0, x + 1),
                _ => self.blank(y, 0, COLS),
            },
            b'm' if !private => self.sgr(p),
            b'r' if !private => {
                let top = n - 1;
                let bottom = match p.get(1) {
                    Some(b) if b > 0 => b as usize,
                    _ => ROWS,
                } - 1;
                if top < bottom && bottom < ROWS {
                    self.top = top as u8;
                    self.bottom = bottom as u8;
                }
                self.cursor = Pos::new(0, 0);
            }
            b'L' | b'M' if self.cursor.y >= self.top && self.cursor.y <= self.bottom => {
                let k = n.min(ROWS) as i64;
                self.scroll(
                    y,
                    self.bottom as usize,
                    if final_byte == b'L' { -k } else { k },
                );
                self.cursor.x = 0;
            }
            b'S' => self.scroll(self.top as usize, self.bottom as usize, n.min(ROWS) as i64),
            b'T' => self.scroll(
                self.top as usize,
                self.bottom as usize,
                -(n.min(ROWS) as i64),
            ),
            b'P' => {
                self.touch(y);
                let k = n.min(COLS - x);
                let row = &mut self.grid.cells[y][x..];
                row.rotate_left(k);
                let len = row.len();
                row[len - k..].fill(Cell::BLANK);
            }
            b'@' => {
                self.touch(y);
                let k = n.min(COLS - x);
                let row = &mut self.grid.cells[y][x..];
                row.rotate_right(k);
                row[..k].fill(Cell::BLANK);
            }
            b'X' => self.blank(y, x, x.saturating_add(n)),
            b'h' if private && p.contains(1049) => self.blank_rows(0, ROWS),
            _ => {}
        }
    }

    fn sgr(&mut self, p: &Params) {
        let mut attr = self.attr;
        let len = p.len.max(1);
        let mut i = 0;
        while i < len {
            let n = p.get(i).unwrap_or(0);
            match n {
                0 => attr = Attr::default(),
                1 => attr.bold = true,
                22 => attr.bold = false,
                7 => attr.rev = true,
                27 => attr.rev = false,
                39 => attr.fg = Colour::Default,
                38 | 48 => i += if p.get(i + 1) == Some(5) { 2 } else { 4 },
                _ => {
                    if let Some(fg) = Colour::from_sgr(n.min(u16::MAX as u32) as u16) {
                        attr.fg = fg;
                    }
                }
            }
            i += 1;
        }
        self.attr = attr;
    }
}

/// The length of the UTF-8 sequence a lead byte starts.
fn utf8_len(lead: u8) -> usize {
    match lead {
        0xc2..=0xdf => 2,
        0xe0..=0xef => 3,
        _ => 4,
    }
}

/// Whether `b` can be byte `at` of the UTF-8 sequence led by `lead`, ruling out overlong forms, surrogates and
/// code points past U+10FFFF.
fn continues_utf8(lead: u8, at: usize, b: u8) -> bool {
    let range = match (lead, at) {
        (0xe0, 1) => 0xa0..=0xbf,
        (0xed, 1) => 0x80..=0x9f,
        (0xf0, 1) => 0x90..=0xbf,
        (0xf4, 1) => 0x80..=0x8f,
        _ => 0x80..=0xbf,
    };
    range.contains(&b)
}

const _: () = assert!(ROWS <= 32 && COLS == 80);

#[cfg(test)]
mod tests {
    use super::*;

    fn line(vt: &Vt, r: usize) -> String {
        let mut out = String::new();
        vt.grid().row_text(r, &mut out);
        out
    }

    fn cell(vt: &Vt, y: u8, x: u8) -> Cell {
        vt.grid().cell(Pos::new(y, x))
    }

    #[test]
    fn clear_position_colour() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[H\x1b[2Jhello\x1b[3;5H\x1b[1;33m@\x1b[0m.");
        assert!(vt.complete());
        assert_eq!(&line(&vt, 0)[..5], "hello");
        assert_eq!(&line(&vt, 2)[4..6], "@.");
        let at = cell(&vt, 2, 4);
        assert_eq!((at.fg, at.bold), (Colour::Brown, true));
        let dot = cell(&vt, 2, 5);
        assert_eq!((dot.fg, dot.bold), (Colour::Default, false));
        assert_eq!(vt.cursor(), Pos::new(2, 6));
    }

    #[test]
    fn split_sequences_and_utf8() {
        let mut vt = Vt::new();
        let data = "\x1b[2J\x1b[1;1Hé\x1b[7mX\x1b[27m".as_bytes();
        for i in 0..data.len() {
            vt.feed(&data[i..i + 1]);
        }
        assert!(line(&vt, 0).starts_with("éX"));
        assert!(cell(&vt, 0, 1).rev);
    }

    #[test]
    fn invalid_utf8_becomes_replacement() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2Ja\xffb\xc3c\xe2\x82");
        vt.feed(b"d");
        assert!(line(&vt, 0).starts_with("a\u{fffd}b\u{fffd}c\u{fffd}d"));
        vt.feed(b"\x1b[1;1H\xe2\x82");
        vt.feed(b"\xac");
        assert_eq!(cell(&vt, 0, 0).ch, '€');
    }

    #[test]
    fn erase_and_wrap() {
        let mut vt = Vt::new();
        let mut data = b"\x1b[2J".to_vec();
        data.extend([b'a'; 81]);
        vt.feed(&data);
        assert_eq!(cell(&vt, 1, 0).ch, 'a');
        vt.feed(b"\x1b[1;3H\x1b[K");
        assert_eq!(line(&vt, 0), format!("aa{}", " ".repeat(78)));
    }

    #[test]
    fn colour_change_keeps_a_pending_wrap() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[1;80Hx\x1b[31my");
        assert_eq!(cell(&vt, 0, 79).ch, 'x');
        assert_eq!(cell(&vt, 1, 0).ch, 'y');
        assert_eq!(cell(&vt, 1, 0).fg, Colour::Red);
        vt.feed(b"\x1b[3;80Hx\x1b[3;80Hz");
        assert_eq!(cell(&vt, 2, 79).ch, 'z');
        assert_eq!(vt.cursor(), Pos::new(2, 79));
    }

    #[test]
    fn scroll_region() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[1;1Hone\r\ntwo\x1b[1;2r\x1b[2;1H\n");
        assert!(line(&vt, 0).starts_with("two"));
        assert!(line(&vt, 1).trim().is_empty());
    }

    #[test]
    fn reverse_index_and_line_insertion() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[1;1Hone\r\ntwo\r\nthree\x1b[1;1H\x1bM");
        assert!(line(&vt, 0).trim().is_empty());
        assert!(line(&vt, 1).starts_with("one"));
        assert!(line(&vt, 3).starts_with("three"));
        vt.feed(b"\x1b[2;3H\x1b[M");
        assert!(line(&vt, 1).starts_with("two"));
        assert_eq!(vt.cursor(), Pos::new(1, 0));
        vt.feed(b"\x1b[2L");
        assert!(line(&vt, 3).starts_with("two"));
        vt.feed(b"\x1b[S");
        assert!(line(&vt, 2).starts_with("two"));
        vt.feed(b"\x1b[T");
        assert!(line(&vt, 3).starts_with("two"));
    }

    #[test]
    fn insert_delete_and_erase_characters() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[1;1Habcdef\x1b[1;2H\x1b[2P");
        assert!(line(&vt, 0).starts_with("adef "));
        vt.feed(b"\x1b[3@");
        assert!(line(&vt, 0).starts_with("a   def "));
        vt.feed(b"\x1b[1;1H\x1b[2X");
        assert!(line(&vt, 0).starts_with("    def "));
        vt.feed(b"\x1b[1;80H\x1b[5@\x1b[5P");
        assert_eq!(line(&vt, 0).chars().count(), 80);
    }

    #[test]
    fn erase_display_parts() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[1;1Haaa\r\nbbb\r\nccc\x1b[2;2H\x1b[1J");
        assert!(line(&vt, 0).trim().is_empty());
        assert!(line(&vt, 1).starts_with("  b"));
        assert!(line(&vt, 2).starts_with("ccc"));
        vt.feed(b"\x1b[2;3H\x1b[J");
        assert!(line(&vt, 2).trim().is_empty());
        vt.feed(b"\x1b[1;1Hxyz\x1b[1;2H\x1b[1K");
        assert!(line(&vt, 0).starts_with("  z"));
        vt.feed(b"\x1b[2K");
        assert!(line(&vt, 0).trim().is_empty());
    }

    #[test]
    fn rows_written_since_cleared() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[1;1HYou miss the jackal.\x1b[5;3H@");
        vt.clear_touched();
        vt.feed(b"\x1b[5;3H.\x1b[5;4H@");
        assert_eq!(vt.touched, 1 << 4);
        assert!(!vt.message_written(1));
        vt.feed(b"\x1b[1;1H\x1b[K");
        assert!(vt.touched & 1 != 0);
        assert!(vt.message_written(1));
    }

    #[test]
    fn complete_needs_a_whole_screen_clear() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[5;1H\x1b[J");
        assert!(!vt.complete());
        vt.feed(b"\x1b[H\x1b[J");
        assert!(vt.complete());
        vt.reset();
        assert!(!vt.complete());
        assert_eq!(vt.touched, ALL_ROWS);
        vt.feed(b"x\x1bc");
        assert!(vt.complete());
        assert_eq!(cell(&vt, 0, 0).ch, ' ');
    }

    #[test]
    fn line_drawing_set() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b(0lqk\x1b(Bq\x0ex\x0fx");
        assert!(line(&vt, 0).starts_with("┌─┐q│x"));
        assert_eq!(dec_graphics('~'), '·');
        assert_eq!(dec_graphics('A'), 'A');
    }

    #[test]
    fn colours_and_extended_colour_skips() {
        let mut vt = Vt::new();
        vt.feed(
            b"\x1b[2J\x1b[94ma\x1b[38;5;196mb\x1b[38;2;1;2;3;32mc\x1b[39;1md\x1b[22;7me\x1b[me",
        );
        let shades: Vec<(Colour, bool, bool)> = (0..6)
            .map(|x| {
                let c = cell(&vt, 0, x);
                (c.fg, c.bold, c.rev)
            })
            .collect();
        assert_eq!(
            shades,
            [
                (Colour::BrightBlue, false, false),
                (Colour::BrightBlue, false, false),
                (Colour::Green, false, false),
                (Colour::Default, true, false),
                (Colour::Default, false, true),
                (Colour::Default, false, false),
            ]
        );
    }

    #[test]
    fn save_restore_and_cursor_moves() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[5;10H\x1b[1m\x1b7\x1b[H\x1b[0m\x1b8x");
        assert!(cell(&vt, 4, 9).bold);
        vt.feed(b"\x1b[3A\x1b[2C\x1b[B\x1b[0D");
        assert_eq!(vt.cursor(), Pos::new(2, 11));
        vt.feed(b"\x1b[99B\x1b[99C");
        assert_eq!(vt.cursor(), Pos::new(23, 79));
        vt.feed(b"\x1b[2F");
        assert_eq!(vt.cursor(), Pos::new(21, 0));
        vt.feed(b"\x1b[7G\x1b[4d");
        assert_eq!(vt.cursor(), Pos::new(3, 6));
        vt.feed(b"\x1b[E\t");
        assert_eq!(vt.cursor(), Pos::new(4, 8));
        vt.feed(b"\x08\x08");
        assert_eq!(vt.cursor(), Pos::new(4, 6));
    }

    #[test]
    fn interrupted_and_overlong_sequences() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[12\x1b[3;4Hx");
        assert_eq!(cell(&vt, 2, 3).ch, 'x');
        vt.feed(b"\x1b[1;1H\x1b[1\x18y");
        assert_eq!(cell(&vt, 0, 0).ch, 'y');
        let mut long = b"\x1b[".to_vec();
        long.extend([b'1'; 70]);
        long.extend(b"Hz");
        vt.feed(&long);
        assert!(line(&vt, 0).contains('z'));
    }

    #[test]
    fn alternate_screen_clears() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2Jabc\x1b[?1049h");
        assert!(line(&vt, 0).trim().is_empty());
        vt.feed(b"\x1b[?25l\x1b[1;1Hq");
        assert_eq!(cell(&vt, 0, 0).ch, 'q');
    }
}
