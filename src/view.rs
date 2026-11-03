//! The compact text view and its machine form, and the cache of farlook names.
//!
//! Text parts, in order: the report line; the two status rows with spaces squashed; `msg:` lines; for a prompt,
//! `prompt:`, `cursor:` and a `keys:` hint; for any other non-map screen, `screen (MODE):` and its rows; the map
//! with a column ruler and row numbers; then `you: X,Y  here: …`, `monsters:`, `seen:`, `items:`, `frontier:`,
//! `inventory:`, `answered:` and `end:`. Coordinates print as `x,y`: x the terminal column, y the terminal row.

use crate::level::{Level, Near, Paths};
use crate::observe::{Entity, Memory};
use crate::screen::{Phase, View};
use crate::types::{Answered, Facts, Pos, Report};

/// Most monsters named by farlook in one look.
pub const FARLOOKS: usize = 8;
/// Most entries listed under `seen:`, `items:` and `frontier:` before `+N more`.
pub const LISTED: usize = 6;
/// The refusal for a map drawn with non-ASCII symbols.
pub const ASCII: &str = "the map holds non-ASCII symbols: the harness reads the default ASCII symbol set, so turn \
                         off DECgraphics, IBMgraphics and other symsets";

/// A farlook name kept for one monster.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Named {
    /// The level it was seen on.
    pub level: u16,
    /// Its square.
    pub pos: Pos,
    /// Its glyph.
    pub glyph: char,
    /// Its colour.
    pub shade: crate::glyphs::Shade,
    /// Highlighted as the pet.
    pub pet: bool,
    /// The game's description.
    pub name: String,
    /// The turn it was looked at.
    pub turn: u32,
}

/// Farlook names by (level, square, glyph, colour, pet highlight). A name lasts while its monster stays on its
/// square looking the same; only monsters in view are kept, so the cache never outgrows the screen.
#[derive(Clone, Debug, Default)]
pub struct Names {
    entries: Vec<Named>,
}

impl Names {
    /// No names.
    pub fn new() -> Names {
        Names::default()
    }

    /// The cached name and look turn for `entity` on `level`.
    pub fn get(&self, level: u16, entity: &Entity) -> Option<(&str, u32)> {
        todo!()
    }

    /// Keeps only the names whose monster is still in `entities` on `level`.
    pub fn retain_visible(&mut self, level: u16, entities: &[Entity]) {
        todo!()
    }

    /// Caches a name for `entity`.
    pub fn insert(&mut self, level: u16, entity: &Entity, name: &str, turn: u32) {
        todo!()
    }

    /// The monsters in `entities` with no cached name, nearest to `hero` first, at most `FARLOOKS`, written into
    /// `out`.
    pub fn unnamed(&self, level: u16, hero: Pos, entities: &[Entity], out: &mut Vec<Entity>) {
        todo!()
    }

    /// Forgets every name.
    pub fn clear(&mut self) {
        self.entries.clear();
    }
}

/// A remembered place, or one a `go` target names: its square, glyph, label and the squares a walk to it ends on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Landmark {
    /// The square it is on.
    pub pos: Pos,
    /// Its glyph; `None` for a chosen square or frontier.
    pub glyph: Option<char>,
    /// What it is, such as `down stairs`, `door`, `item` or `frontier`.
    pub label: String,
    /// Squares a walk to it may end on.
    pub goals: Near,
}

/// Remembered features, doors and items on `level`, with the objects among `entities`, row then column, written
/// into `out`. They are what `seen:` and `items:` list and what `go` can name.
pub fn landmarks(level: &Level, entities: &[Entity], out: &mut Vec<Landmark>) {
    todo!()
}

/// Everything a view is rendered from.
pub struct Scene<'a> {
    /// The screen.
    pub view: &'a View,
    /// What is remembered.
    pub memory: &'a Memory,
    /// Cached farlook names.
    pub names: &'a Names,
    /// Messages the game wrote since the last reply.
    pub fresh: &'a [String],
    /// How the command went, for `send`, `go` and `rest`.
    pub report: Option<&'a Report>,
    /// Prompts the keys passed.
    pub answered: &'a [Answered],
}

/// Renders views, reusing its route and entity buffers.
pub struct Renderer {
    paths: Paths,
    terrain_paths: Paths,
    entities: Vec<Entity>,
    route: Vec<Pos>,
    line: String,
}

impl Default for Renderer {
    fn default() -> Renderer {
        Renderer::new()
    }
}

impl Renderer {
    /// A renderer with empty buffers.
    pub fn new() -> Renderer {
        Renderer {
            paths: Paths::new(),
            terrain_paths: Paths::new(),
            entities: Vec::new(),
            route: Vec::new(),
            line: String::new(),
        }
    }

    /// Writes the text view into `text` and its machine form into `facts`, replacing both. Fails with `ASCII`
    /// when the map or a prompt screen holds non-ASCII symbols.
    pub fn render(
        &mut self,
        scene: &Scene,
        text: &mut String,
        facts: &mut Facts,
    ) -> Result<(), String> {
        todo!()
    }
}

/// The report line, such as `go >: 5 steps, 5 turns, now at 40,7; done` or
/// `rest 20: 3 searches, 3 turns; stopped: a monster came into view or next to the hero (d 41,8)`.
pub fn report_line(report: &Report, hero: Option<Pos>, out: &mut String) {
    todo!()
}

/// The `keys:` hint for a prompt: the answers the screen itself offers.
pub fn hint(view: &View, phase: Phase, out: &mut String) -> bool {
    todo!()
}

/// The map rows with a column ruler and row numbers, cut to the columns in use.
pub fn map_lines(view: &View, out: &mut String) {
    todo!()
}

/// The `here:` text for the hero's square: remembered terrain and what the game said there.
pub fn here(level: &Level, hero: Pos, out: &mut String) {
    todo!()
}
