//! The commands a caller plays with: `look`, `send`, `go` and `rest`, each answered with a view.

use crate::exec::{Executor, Outcome};
use crate::level::Paths;
use crate::link::Terminal;
use crate::observe::Memory;
use crate::types::{Pos, Reply, Report, SquareSet};
use crate::view::{Landmark, Names, Renderer};

/// Most keys one `send` takes.
pub const KEYS_MAX: usize = 256;
/// Most searches one `rest` takes.
pub const REST_MAX: u32 = 200;
/// Most steps one `go` takes unless configured lower.
pub const STEPS_MAX: u32 = 64;
/// The refusal while a prompt is open.
pub const PROMPT_OPEN: &str = "a prompt is open: answer or cancel it with send first";

/// One game played through a terminal: memory, the executor, names and the renderer, reused across commands.
pub struct Play<T: Terminal> {
    term: T,
    memory: Memory,
    exec: Executor,
    names: Names,
    renderer: Renderer,
    paths: Paths,
    fresh: Vec<String>,
    reported: u64,
    last_square: Option<(u16, Pos)>,
    items: SquareSet,
    max_steps: u32,
}

impl<T: Terminal> Play<T> {
    /// Play over `term`, walking at most `max_steps` steps per `go`.
    pub fn new(term: T, max_steps: u32) -> Play<T> {
        todo!()
    }

    /// The terminal.
    pub fn term(&self) -> &T {
        &self.term
    }

    /// The terminal, mutably.
    pub fn term_mut(&mut self) -> &mut T {
        &mut self.term
    }

    /// The memory.
    pub fn memory(&self) -> &Memory {
        &self.memory
    }

    /// Reads the screen and renders it. While the map waits for a command, it first names monsters by farlook
    /// (only new or moved ones, at most `FARLOOKS`) and re-reads a known engraving or object square underfoot with
    /// `:` when the turn has advanced since its last reading; both take no game time.
    pub fn look(&mut self) -> Reply {
        todo!()
    }

    /// Renders the current screen as it stands, sending nothing: no farlooks and no look underfoot.
    pub fn show(&mut self) -> Reply {
        todo!()
    }

    /// Sends the caller's keys and renders the result, with every prompt they passed and its answer.
    pub fn send(&mut self, keys: &[u8]) -> Reply {
        todo!()
    }

    /// Walks toward `target` (`x,y`, a map glyph, a feature kind, or `frontier`) along the known route.
    pub fn go(&mut self, target: &str) -> Reply {
        todo!()
    }

    /// Searches in place up to `count` times.
    pub fn rest(&mut self, count: u32) -> Reply {
        todo!()
    }

    /// The places a `go` target names on `level`, written into `out`; an error says why none match.
    pub fn places(&mut self, target: &str, out: &mut Vec<Landmark>) -> Result<(), String> {
        todo!()
    }

    /// Why no known route reaches `target`, from remembered facts.
    pub fn unreachable(&mut self, target: &str, places: &[Landmark]) -> String {
        todo!()
    }

    /// The report for `outcome` of the command labelled `verb`.
    pub fn report(&self, verb: &str, outcome: &Outcome) -> Report {
        todo!()
    }

    /// Whether the game verifiably waits for a command: no prompt, the cursor on `@`, the status lines shown.
    pub fn plain(&self) -> bool {
        todo!()
    }
}
