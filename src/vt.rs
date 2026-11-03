//! A VT100/xterm screen emulator, enough of the protocol for NetHack's tty port on an 80×24 terminal.

use crate::types::{COLS, Colour, Grid, Pos, ROWS};

/// Longest escape sequence kept; a longer one is dropped.
pub const ESCAPE_MAX: usize = 64;

/// The character the DEC special graphics set draws for `ch` in line-drawing mode; other characters are unchanged.
pub fn dec_graphics(ch: char) -> char {
    todo!()
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
        todo!()
    }

    /// Clears the screen and all parser state, as after a terminal reset.
    pub fn reset(&mut self) {
        todo!()
    }

    /// Interprets terminal output. UTF-8 split across calls is carried over; invalid UTF-8 becomes U+FFFD.
    pub fn feed(&mut self, bytes: &[u8]) {
        todo!()
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

    /// Bit `r` is set when row `r` was written or erased since `clear_touched`.
    pub fn touched(&self) -> u32 {
        self.touched
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
}

const _: () = assert!(ROWS <= 32 && COLS == 80);
