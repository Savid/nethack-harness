//! A VT100/xterm screen emulator, enough of the protocol for NetHack's tty port on an 80×24 terminal.

use crate::types::{COLS, Cell, Colour, Grid, Pos, ROWS};

/// Longest escape sequence kept; a longer one is dropped. It also bounds the parameters one sequence carries.
const ESCAPE_MAX: usize = 64;

/// The characters the DEC special graphics set redraws in line-drawing mode, and what it draws for each, in the
/// same order.
const DEC_FROM: &str = "`afgjklmnopqrstuvwxyz{|}~";
const DEC_TO: &str = "◆▒°±┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·";

/// The character the DEC special graphics set draws for `ch`; other characters are unchanged.
fn dec_graphics(ch: char) -> char {
    DEC_FROM
        .find(ch)
        .and_then(|i| DEC_TO.chars().nth(i))
        .unwrap_or(ch)
}

/// The attributes new characters are written with.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
struct Attr {
    fg: Colour,
    bold: bool,
    rev: bool,
}

/// Every row's bit in a touched mask.
const ALL_ROWS: u32 = (1 << ROWS) - 1;

/// Stands in for a non-ASCII character inside an escape sequence, which no sequence uses.
const FOREIGN: u8 = 0x80;

/// The numeric parameters of one control sequence. An empty or non-numeric parameter reads as 0, as does any
/// past the last.
struct Params {
    nums: [u32; ESCAPE_MAX],
    len: usize,
    /// Whether a private-mode marker such as `?` leads the parameters.
    private: bool,
}

impl Params {
    /// Parses the bytes between `[` and the final byte.
    fn parse(raw: &[u8]) -> Params {
        let start = raw
            .iter()
            .position(|b| !b"?>=!".contains(b))
            .unwrap_or(raw.len());
        let mut p = Params {
            nums: [0; ESCAPE_MAX],
            len: 0,
            private: start > 0,
        };
        for (num, part) in p.nums.iter_mut().zip(raw[start..].split(|&b| b == b';')) {
            if part.iter().all(u8::is_ascii_digit) {
                *num = part.iter().fold(0u32, |n, &d| {
                    n.saturating_mul(10).saturating_add(u32::from(d - b'0'))
                });
            }
            p.len += 1;
        }
        p
    }

    fn get(&self, i: usize) -> u32 {
        self.nums.get(i).copied().unwrap_or(0)
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
        *self = Vt::new();
    }

    /// Interprets terminal output. UTF-8 split across calls is carried over; invalid UTF-8 becomes U+FFFD.
    pub fn feed(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.byte(b);
        }
    }

    pub fn grid(&self) -> &Grid {
        &self.grid
    }

    pub fn cursor(&self) -> Pos {
        self.cursor
    }

    /// Whether the whole screen has been cleared since the last reset, so every cell is current.
    pub fn complete(&self) -> bool {
        self.complete
    }

    pub fn clear_touched(&mut self) {
        self.touched = 0;
    }

    /// Whether any of the first `rows` rows was written since `clear_touched`: an untouched message line holds
    /// old text.
    pub fn message_written(&self, rows: usize) -> bool {
        self.touched & ((1 << rows.min(ROWS)) - 1) != 0
    }

    /// Decodes one byte of UTF-8. A byte that cannot continue the pending sequence ends it as one U+FFFD and is
    /// then decoded afresh.
    fn byte(&mut self, b: u8) {
        if self.utf8_len == 0 && b.is_ascii() {
            self.char(char::from(b));
            return;
        }
        self.utf8[self.utf8_len] = b;
        self.utf8_len += 1;
        match std::str::from_utf8(&self.utf8[..self.utf8_len]).map(|s| s.chars().next()) {
            Ok(ch) => {
                self.utf8_len = 0;
                self.char(ch.unwrap_or(char::REPLACEMENT_CHARACTER));
            }
            // The start of a valid sequence waits for the rest.
            Err(e) if e.error_len().is_none() => {}
            Err(_) => {
                let pending = self.utf8_len > 1;
                self.utf8_len = 0;
                self.char(char::REPLACEMENT_CHARACTER);
                if pending {
                    self.byte(b);
                }
            }
        }
    }

    fn char(&mut self, ch: char) {
        match ch {
            // An Escape starts a sequence, abandoning any unfinished one; CAN and SUB cancel one. Other controls
            // take effect without ending a sequence, and DEL is ignored, as on a hardware terminal.
            '\x1b' => {
                self.escaping = true;
                self.esc_len = 0;
            }
            '\x18' | '\x1a' => self.end_escape(),
            '\x7f' => {}
            '\x00'..='\x1f' => self.control(ch),
            _ if self.escaping => {
                self.esc[self.esc_len] = if ch.is_ascii() { ch as u8 } else { FOREIGN };
                self.esc_len += 1;
                self.escape();
            }
            _ => self.put(ch),
        }
    }

    fn end_escape(&mut self) {
        self.escaping = false;
        self.esc_len = 0;
    }

    fn touch(&mut self, row: usize) {
        self.touched |= 1 << row;
    }

    fn move_to(&mut self, y: usize, x: usize) {
        self.cursor = Pos::new(y.min(ROWS - 1) as u8, x.min(COLS - 1) as u8);
    }

    fn put(&mut self, ch: char) {
        let ch = if self.dec { dec_graphics(ch) } else { ch };
        if self.wrap {
            self.cursor.x = 0;
            self.linefeed();
        }
        let (y, x) = (usize::from(self.cursor.y), usize::from(self.cursor.x));
        let Attr { fg, bold, rev } = self.attr;
        self.grid.cells[y][x] = Cell { ch, fg, bold, rev };
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
            '\n' | '\x0b' | '\x0c' => self.linefeed(),
            '\x08' => {
                self.cursor.x = self.cursor.x.saturating_sub(1);
                self.wrap = false;
            }
            '\t' => self.cursor.x = ((self.cursor.x / 8 + 1) * 8).min(COLS as u8 - 1),
            '\x0e' => self.dec = true,
            '\x0f' => self.dec = false,
            _ => {}
        }
    }

    fn linefeed(&mut self) {
        self.wrap = false;
        if self.cursor.y == self.bottom {
            self.scroll(self.top as usize, 1, true);
        } else if usize::from(self.cursor.y) < ROWS - 1 {
            self.cursor.y += 1;
        }
    }

    /// Blanks columns `x0..x1` of row `y`, stopping at the row's end.
    fn blank(&mut self, y: usize, x0: usize, x1: usize) {
        self.touch(y);
        self.grid.cells[y][x0..x1.min(COLS)].fill(Cell::BLANK);
    }

    fn blank_rows(&mut self, from: usize, to: usize) {
        for y in from..to {
            self.blank(y, 0, COLS);
        }
    }

    /// Scrolls the rows from `top` to the bottom of the scroll region up by `n`, or down when not `up`, blanking
    /// the rows uncovered.
    fn scroll(&mut self, top: usize, n: usize, up: bool) {
        let bottom = self.bottom as usize;
        for y in top..=bottom {
            self.touch(y);
        }
        let rows = &mut self.grid.cells[top..=bottom];
        shift(rows, n, up, [Cell::BLANK; COLS]);
    }

    fn escape(&mut self) {
        match self.esc[..self.esc_len] {
            // These start longer sequences.
            [b'[' | b'(' | b')' | b'#' | b'%'] => return,
            [b'7'] => self.saved = (self.cursor, self.attr),
            [b'8'] => {
                (self.cursor, self.attr) = self.saved;
                self.wrap = false;
            }
            [b'D'] => self.linefeed(),
            [b'E'] => {
                self.cursor.x = 0;
                self.linefeed();
            }
            // Reverse index: up a row, scrolling down at the top of the scroll region.
            [b'M'] => {
                self.wrap = false;
                if self.cursor.y == self.top {
                    self.scroll(self.top as usize, 1, false);
                } else {
                    self.cursor.y = self.cursor.y.saturating_sub(1);
                }
            }
            [b'c'] => {
                self.reset();
                self.complete = true;
            }
            [b'(', set] => self.dec = set == b'0',
            [b'[', .., last @ b'@'..=b'~'] => {
                let params = Params::parse(&self.esc[1..self.esc_len - 1]);
                self.csi(&params, last);
            }
            // A control sequence continues to its final byte unless it grows too long.
            [b'[', ..] if self.esc_len < ESCAPE_MAX => return,
            _ => {}
        }
        self.end_escape();
    }

    fn csi(&mut self, p: &Params, final_byte: u8) {
        // Every sequence but a colour change ends a pending wrap at the last column.
        if final_byte != b'm' {
            self.wrap = false;
        }
        let (y, x) = (usize::from(self.cursor.y), usize::from(self.cursor.x));
        // A count or a 1-based coordinate, where 0 also means 1.
        let n = p.get(0).max(1) as usize;
        match final_byte {
            b'H' | b'f' => self.move_to(n - 1, p.get(1).max(1) as usize - 1),
            b'A' => self.move_to(y.saturating_sub(n), x),
            b'B' => self.move_to(y.saturating_add(n), x),
            b'C' => self.move_to(y, x.saturating_add(n)),
            b'D' => self.move_to(y, x.saturating_sub(n)),
            b'E' => self.move_to(y.saturating_add(n), 0),
            b'F' => self.move_to(y.saturating_sub(n), 0),
            b'G' | b'`' => self.move_to(y, n - 1),
            b'd' => self.move_to(n - 1, x),
            // Erasing below from home clears the whole screen.
            b'J' => match p.get(0) {
                0 if (y, x) != (0, 0) => {
                    self.blank(y, x, COLS);
                    self.blank_rows(y + 1, ROWS);
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
            b'K' => match p.get(0) {
                0 => self.blank(y, x, COLS),
                1 => self.blank(y, 0, x + 1),
                _ => self.blank(y, 0, COLS),
            },
            b'm' if !p.private => self.sgr(p),
            b'r' if !p.private => {
                let top = n - 1;
                let bottom = match p.get(1) {
                    0 => ROWS,
                    b => b as usize,
                } - 1;
                if top < bottom && bottom < ROWS {
                    self.top = top as u8;
                    self.bottom = bottom as u8;
                }
                self.cursor = Pos::new(0, 0);
            }
            b'L' | b'M' if (self.top..=self.bottom).contains(&self.cursor.y) => {
                self.scroll(y, n, final_byte == b'M');
                self.cursor.x = 0;
            }
            b'S' | b'T' => self.scroll(self.top as usize, n, final_byte == b'S'),
            b'P' | b'@' => {
                self.touch(y);
                let row = &mut self.grid.cells[y][x..];
                shift(row, n, final_byte == b'P', Cell::BLANK);
            }
            b'X' => self.blank(y, x, x.saturating_add(n)),
            b'h' if p.private && p.nums.contains(&1049) => self.blank_rows(0, ROWS),
            _ => {}
        }
    }

    fn sgr(&mut self, p: &Params) {
        let mut i = 0;
        while i < p.len {
            match p.get(i) {
                0 => self.attr = Attr::default(),
                1 => self.attr.bold = true,
                22 => self.attr.bold = false,
                7 => self.attr.rev = true,
                27 => self.attr.rev = false,
                39 => self.attr.fg = Colour::Default,
                // An extended colour is skipped with its arguments, `5;n` or `2;r;g;b`.
                38 | 48 => i += if p.get(i + 1) == 5 { 2 } else { 4 },
                n => {
                    if let Some(fg) = u16::try_from(n).ok().and_then(Colour::from_sgr) {
                        self.attr.fg = fg;
                    }
                }
            }
            i += 1;
        }
    }
}

/// Moves `items` `n` places toward the start when `to_start`, else toward the end, filling the places left with
/// `blank`.
fn shift<T: Copy>(items: &mut [T], n: usize, to_start: bool, blank: T) {
    let len = items.len();
    let n = n.min(len);
    if to_start {
        items.rotate_left(n);
        items[len - n..].fill(blank);
    } else {
        items.rotate_right(n);
        items[..n].fill(blank);
    }
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
    fn invalid_utf8_is_replaced_by_maximal_subpart() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\xe0\x80|\xed\xa0\x80|\xf4\x90|\xc0\xaf|\xf0\x9f\x98x|\xf0\x9f");
        vt.feed(b"\x98\x80|\xf5");
        let r = '\u{fffd}';
        let want = format!("{r}{r}|{r}{r}{r}|{r}{r}|{r}{r}|{r}x|😀|{r} ");
        assert!(line(&vt, 0).starts_with(&want), "{}", line(&vt, 0));
    }

    #[test]
    fn private_markers_are_not_colours_or_regions() {
        let mut vt = Vt::new();
        vt.feed(b"\x1b[2J\x1b[?1m\x1b[>31ma\x1b[?5;10r");
        assert_eq!(
            (cell(&vt, 0, 0).bold, cell(&vt, 0, 0).fg),
            (false, Colour::Default)
        );
        assert_eq!(vt.cursor(), Pos::new(0, 1));
        vt.feed(b"\x1b[24;1H\n");
        assert_eq!(cell(&vt, 22, 0).ch, ' ');
        assert_eq!(cell(&vt, 0, 0).ch, ' ');
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
