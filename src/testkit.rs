//! Test helpers: screens built from text rows, and a terminal that plays back a script of views.

use crate::link::{LinkError, Terminal};
use crate::screen::{PromptContext, View};
use crate::types::{COLS, Cell, Grid, Pos, ROWS};

/// The first status row of `screen`.
pub const STATUS1: &str = "Hero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful";
/// The second status row of `screen`.
pub const STATUS2: &str = "Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400";

/// 24 rows: `top` as the message row, then `rows`, blank rows to row 21, and the two status rows.
pub fn screen(top: &str, rows: &[&str], status1: &str, status2: &str) -> Vec<String> {
    let mut lines: Vec<String> = Vec::with_capacity(ROWS);
    lines.push(top.to_owned());
    lines.extend(rows.iter().map(|r| (*r).to_owned()));
    lines.resize(ROWS - 2, String::new());
    lines.truncate(ROWS - 2);
    lines.push(status1.to_owned());
    lines.push(status2.to_owned());
    lines
}

/// A grid of default-coloured text rows, each padded or cut to 80 columns.
pub fn grid(rows: &[String]) -> Grid {
    let mut grid = Grid::new();
    for (r, row) in rows.iter().take(ROWS).enumerate() {
        for (c, ch) in row.chars().take(COLS).enumerate() {
            grid.cells[r][c] = Cell { ch, ..Cell::BLANK };
        }
    }
    grid
}

/// A view of default-coloured rows with the cursor at (`y`, `x`).
pub fn view(rows: &[String], y: u8, x: u8) -> View {
    View::parse(&grid(rows), Pos::new(y, x))
}

/// A terminal that shows `views[0]`; each `send` records its keys and moves to the next view, keeping the last.
pub struct ScriptedTerm {
    /// The views still to show, current first.
    pub views: Vec<View>,
    /// Every batch of keys sent.
    pub sent: Vec<Vec<u8>>,
    /// Errors to return from upcoming sends, first first; `None` sends normally.
    pub failures: Vec<Option<LinkError>>,
    prompts: PromptContext,
}

impl ScriptedTerm {
    /// A terminal that plays back `views`.
    pub fn new(views: Vec<View>) -> ScriptedTerm {
        ScriptedTerm {
            views,
            sent: Vec::new(),
            failures: Vec::new(),
            prompts: PromptContext::default(),
        }
    }
}

impl Terminal for ScriptedTerm {
    fn poll(&mut self) -> Result<(), LinkError> {
        Ok(())
    }

    fn send(&mut self, keys: &[u8]) -> Result<(), LinkError> {
        if !self.failures.is_empty()
            && let Some(error) = self.failures.remove(0)
        {
            return Err(error);
        }
        self.sent.push(keys.to_vec());
        if self.views.len() > 1 {
            self.views.remove(0);
        }
        Ok(())
    }

    fn view(&self) -> &View {
        &self.views[0]
    }

    fn message_written(&self) -> bool {
        true
    }

    fn prompts(&self) -> &PromptContext {
        &self.prompts
    }
}
