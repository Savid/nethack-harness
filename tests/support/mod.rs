//! Shared test support: screen fixtures loaded into grids and views, a terminal that shows one fixture, and the
//! fake terminal server.
#![allow(dead_code, unused_variables)]

pub mod server;

use std::path::{Path, PathBuf};

use nethack_harness::link::{LinkError, Terminal};
use nethack_harness::screen::{PromptContext, View};
use nethack_harness::types::{COLS, Cell, Colour, Grid, Pos, ROWS};
use serde::Deserialize;

/// The fixture directory.
pub fn fixtures_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/screens")
}

/// The golden directory.
pub fn goldens_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/goldens")
}

/// A scratch directory for one test under cargo's per-target temporary directory, emptied first.
pub fn scratch(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch directory");
    dir
}

/// Fixture names without the `.json` extension, sorted.
pub fn fixture_names() -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(fixtures_dir())
        .expect("read fixtures")
        .filter_map(|entry| {
            let name = entry.ok()?.file_name().into_string().ok()?;
            name.strip_suffix(".json").map(str::to_owned)
        })
        .collect();
    names.sort();
    names
}

/// A recorded screen: rows, per-cell colour names and attributes, and the cursor as [row, column].
#[derive(Clone, Debug, Deserialize)]
pub struct Fixture {
    /// 24 rows of text.
    pub rows: Vec<String>,
    /// Colour per cell.
    pub fg: Vec<Vec<Colour>>,
    /// Bold per cell.
    pub bold: Vec<Vec<bool>>,
    /// Reverse video per cell.
    pub rev: Vec<Vec<bool>>,
    /// Cursor row and column.
    pub cursor: [u8; 2],
}

impl Fixture {
    /// The fixture `name` (without `.json`).
    pub fn load(name: &str) -> Fixture {
        let path = fixtures_dir().join(format!("{name}.json"));
        let text = std::fs::read_to_string(&path).expect("read fixture");
        serde_json::from_str(&text).expect("parse fixture")
    }

    /// The cells.
    pub fn grid(&self) -> Grid {
        let mut grid = Grid::new();
        for r in 0..ROWS {
            let chars: Vec<char> = self
                .rows
                .get(r)
                .map(|row| row.chars().collect())
                .unwrap_or_default();
            for c in 0..COLS {
                grid.cells[r][c] = Cell {
                    ch: chars.get(c).copied().unwrap_or(' '),
                    fg: self
                        .fg
                        .get(r)
                        .and_then(|row| row.get(c))
                        .copied()
                        .unwrap_or_default(),
                    bold: self
                        .bold
                        .get(r)
                        .and_then(|row| row.get(c))
                        .copied()
                        .unwrap_or(false),
                    rev: self
                        .rev
                        .get(r)
                        .and_then(|row| row.get(c))
                        .copied()
                        .unwrap_or(false),
                };
            }
        }
        grid
    }

    /// The cursor.
    pub fn cursor(&self) -> Pos {
        Pos::new(self.cursor[0], self.cursor[1])
    }

    /// The parsed view.
    pub fn view(&self) -> View {
        View::parse(&self.grid(), self.cursor())
    }
}

/// The parsed view of fixture `name`.
pub fn load_view(name: &str) -> View {
    Fixture::load(name).view()
}

/// A terminal that shows one screen: it refuses input, or with `still` takes keys without changing the screen.
pub struct FixtureTerm {
    view: View,
    prompts: PromptContext,
    still: bool,
}

impl FixtureTerm {
    /// A terminal showing `view` that refuses input.
    pub fn new(view: View) -> FixtureTerm {
        FixtureTerm {
            view,
            prompts: PromptContext::default(),
            still: false,
        }
    }

    /// A terminal showing `view` that takes keys and keeps showing it.
    pub fn still(view: View) -> FixtureTerm {
        FixtureTerm {
            still: true,
            ..FixtureTerm::new(view)
        }
    }
}

impl Terminal for FixtureTerm {
    fn poll(&mut self) -> Result<(), LinkError> {
        Ok(())
    }

    fn send(&mut self, _keys: &[u8]) -> Result<(), LinkError> {
        if self.still {
            return Ok(());
        }
        Err(LinkError::Protocol("a fixture takes no input".to_owned()))
    }

    fn view(&self) -> &View {
        &self.view
    }

    fn message_written(&self) -> bool {
        !self.still
    }

    fn prompts(&self) -> &PromptContext {
        &self.prompts
    }
}
