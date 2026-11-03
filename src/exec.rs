//! Bounded execution: sending keys, walking a route, exploring a corridor, searching in place and the harness's
//! own information commands, each stopping at observable boundaries; and paging through the end of the game.

use crate::level::{Level, Paths};
use crate::link::Terminal;
use crate::observe::{Entity, Memory};
use crate::screen::{Phase, View};
use crate::types::{Answered, Pos};

/// Most `--More--` pages or text windows dismissed in one go.
pub const PAGES: u32 = 32;
/// Repeated searches that leave the screen unchanged before a search stops.
pub const IDLE_SEARCHES: u32 = 3;

/// What an action does.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// The caller's keys, sent one byte at a time so each prompt is seen before the key that answers it.
    Keys,
    /// Walk a known route.
    Travel,
    /// Walk to a corridor end, then follow the corridor beyond it.
    Explore,
    /// Search in place, repeatedly.
    Search,
    /// Farlook a square: `;`, then the cursor keys.
    Farlook,
    /// Look underfoot: `:`.
    LookHere,
}

/// A bounded action.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Action {
    /// What it does.
    pub kind: Kind,
    /// The keys of one step.
    pub keys: Vec<u8>,
    /// Most steps to take.
    pub steps: u32,
    /// The square it is going to or looking at.
    pub target: Option<Pos>,
    /// The known route, first step first.
    pub route: Vec<Pos>,
    /// Keys expected to answer a prompt the first keys open, with the phase it should be in.
    pub followup: Option<(Phase, Vec<u8>)>,
}

/// Which observations changed and ended an action.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Changed(pub u8);

impl Changed {
    /// The status line changed, other than turn, HP and Pw, or HP or Pw dropped.
    pub const STATUS: Changed = Changed(1);
    /// A condition changed.
    pub const CONDITIONS: Changed = Changed(2);
    /// The hero was engulfed or released.
    pub const ENGULFED: Changed = Changed(4);
    /// Remembered terrain changed.
    pub const TERRAIN: Changed = Changed(8);
    /// A new monster came into view or next to the hero.
    pub const ENTITIES: Changed = Changed(16);
    /// The game wrote a message not about a pet.
    pub const MESSAGE: Changed = Changed(32);
    /// HP that was below its maximum reached it.
    pub const HP_FULL: Changed = Changed(64);

    /// Whether every flag of `other` is set.
    pub fn contains(self, other: Changed) -> bool {
        self.0 & other.0 == other.0 && other.0 != 0
    }

    /// Sets the flags of `other`.
    pub fn insert(&mut self, other: Changed) {
        self.0 |= other.0;
    }

    /// Whether no flag is set.
    pub fn is_empty(self) -> bool {
        self.0 == 0
    }
}

/// Why an action ended.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Stop {
    /// It did everything asked.
    Completed,
    /// It took its most steps.
    StepLimit,
    /// A prompt opened.
    Prompt,
    /// The hero did not arrive where the step led.
    MovementInterrupted,
    /// Nothing on screen changed.
    NoEffect,
    /// The hero left the route.
    RouteChanged,
    /// Exploring uncovered new terrain.
    FeatureDiscovered,
    /// The corridor being followed branches.
    BranchDiscovered,
    /// The corridor being followed ends.
    CorridorEnds,
    /// Observations in `Outcome::changed` changed.
    Changed,
    /// A step left the hero in place with no turn passing; it is kept out of routes.
    Refused {
        /// The square the step left from.
        from: Pos,
        /// The square it led to.
        to: Pos,
    },
    /// The game ended.
    GameOver,
    /// Game input is held; nothing was sent.
    Held(String),
    /// The terminal closed.
    Closed(String),
    /// The terminal answered something unexpected.
    Failed(String),
}

/// A monster that ended a rest or walk by arriving.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Arrival {
    /// Its glyph.
    pub glyph: char,
    /// Its square.
    pub pos: Pos,
}

/// What an action did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// Why it ended.
    pub stop: Stop,
    /// For `Stop::Changed`, what changed.
    pub changed: Changed,
    /// For a `Changed::ENTITIES` stop, the monster that arrived.
    pub arrival: Option<Arrival>,
    /// Steps taken.
    pub steps: u32,
    /// Game turns that passed, when the status line showed them before and after.
    pub turns: Option<i64>,
    /// Message lines the game wrote meanwhile, text-window lines after the first indented by two spaces.
    pub messages: Vec<String>,
    /// Prompts the keys passed and the key that answered each.
    pub answered: Vec<Answered>,
}

impl Outcome {
    /// An empty outcome that completed.
    pub fn new() -> Outcome {
        Outcome {
            stop: Stop::Completed,
            changed: Changed::default(),
            arrival: None,
            steps: 0,
            turns: None,
            messages: Vec::new(),
            answered: Vec::new(),
        }
    }
}

impl Default for Outcome {
    fn default() -> Outcome {
        Outcome::new()
    }
}

/// Runs actions, keeping scratch buffers across calls.
pub struct Executor {
    after_run: bool,
    previous: Vec<Entity>,
    now: Vec<Entity>,
    window: Vec<String>,
}

impl Default for Executor {
    fn default() -> Executor {
        Executor::new()
    }
}

impl Executor {
    /// An executor with empty buffers.
    pub fn new() -> Executor {
        Executor {
            after_run: false,
            previous: Vec::new(),
            now: Vec::new(),
            window: Vec::new(),
        }
    }

    /// Runs `action` to its first boundary, ingesting every screen into `memory`. Keys actions record each prompt
    /// passed and the key that answered it, and flag keys typed into a text prompt; nothing is answered on the
    /// caller's behalf. Walks and rests stop for a monster only if no monster of the same glyph and colour was
    /// within one square of it at the previous step; `I` and warning digits never stop them.
    pub fn run(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        action: &Action,
    ) -> Outcome {
        todo!()
    }

    /// Pages through the end of the game, declining the disclosure questions and dismissing `--More--`, until the
    /// tombstone or the farewell is captured into `memory`.
    pub fn finish_game(&mut self, term: &mut dyn Terminal, memory: &mut Memory) -> Outcome {
        todo!()
    }

    /// Dismisses `--More--` pages, at most `PAGES`.
    pub fn pages(&mut self, term: &mut dyn Terminal, memory: &mut Memory, outcome: &mut Outcome) {
        todo!()
    }
}

/// A walk along the known route to `target`, at most `max_steps` steps.
pub fn travel(paths: &Paths, target: Pos, max_steps: u32) -> Action {
    todo!()
}

/// A walk to the corridor end `target`, then along the corridor beyond it, at most `max_steps` steps.
pub fn explore(paths: &Paths, target: Pos, max_steps: u32) -> Action {
    todo!()
}

/// A farlook of `target` from the hero's square in `view`.
pub fn farlook(view: &View, target: Pos) -> Action {
    todo!()
}

/// Up to `count` searches in place.
pub fn search(count: u32) -> Action {
    todo!()
}

/// A look underfoot.
pub fn look_here() -> Action {
    todo!()
}

/// The caller's keys.
pub fn keys(keys: &[u8]) -> Action {
    todo!()
}

/// The farlook cursor keys from `start` to `target`: `@` to the hero, moves, then `.` to pick.
pub fn cursor_keys(start: Pos, target: Pos, out: &mut Vec<u8>) {
    todo!()
}

/// Whether to redraw (`^R`) after `keys`: a counted command, or any command after a run (travel `_`, `G` or a
/// shift-run), can leave the status line's turn stale. Only when the map waits with an empty message line, so no
/// message is erased.
pub fn needs_redraw(keys: &[u8], after_run: bool, view: &View) -> bool {
    todo!()
}

/// The direction key a plain step into `target` takes from `level`'s memory: the `m` prefix is left off where the
/// game checks the square first (water, lava, traps, gas) or where the pet stands, so the game's own check or
/// swap applies.
pub fn step_keys(level: &Level, view: &View, from: Pos, target: Pos, out: &mut Vec<u8>) -> bool {
    todo!()
}
