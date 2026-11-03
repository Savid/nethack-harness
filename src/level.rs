//! Per-level memory of observed terrain, passability, shortest routes through known squares, and the frontier.

use std::collections::BTreeMap;

use crate::glyphs::{Base, Shade};
use crate::screen::View;
use crate::types::{MAP_SQUARES, Pos, SquareSet};

/// Remembered refused steps per level; the oldest is forgotten beyond this.
pub const REFUSED_STEPS: usize = 64;

/// What a `+` is: a closed door and a spellbook are both drawn `+`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Plus {
    /// It sits in a wall line, or was remembered as a door.
    Door,
    /// It lies on remembered ground or has floor beside it both across and along.
    Object,
    /// The screen cannot tell.
    DoorOrObject,
}

/// What the game said about a square the hero stood on.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Underfoot {
    /// The notes, merged across readings: `There is …`, `You see here …`, engraving read-outs, trap sentences.
    pub notes: Vec<String>,
    /// The turn of the last reading.
    pub turn: Option<u32>,
}

/// A farlook or `:` reading of a square.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Inspection {
    /// The game's description.
    pub description: String,
    /// The turn it was read.
    pub turn: Option<u32>,
    /// The glyph shown when it was read.
    pub glyph: char,
    /// The colour shown when it was read.
    pub shade: Shade,
}

/// The squares a route can move to from one square.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Near {
    buf: [Pos; 8],
    len: u8,
}

impl Near {
    /// Adds a square.
    pub fn push(&mut self, p: Pos) {
        if (self.len as usize) < self.buf.len() {
            self.buf[self.len as usize] = p;
            self.len += 1;
        }
    }

    /// The squares, in the order added.
    pub fn as_slice(&self) -> &[Pos] {
        &self.buf[..self.len as usize]
    }

    /// How many squares.
    pub fn len(&self) -> usize {
        self.len as usize
    }

    /// Whether there are none.
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

/// Shortest routes from one start square, reused across searches.
pub struct Paths {
    start: Pos,
    previous: Box<[Option<Pos>; MAP_SQUARES]>,
    cost: Box<[(u16, u16, u16); MAP_SQUARES]>,
    reached: SquareSet,
}

impl Default for Paths {
    fn default() -> Paths {
        Paths::new()
    }
}

impl Paths {
    /// Empty search state.
    pub fn new() -> Paths {
        todo!()
    }

    /// The start square of the last search.
    pub fn start(&self) -> Pos {
        self.start
    }

    /// Whether the last search reached `p`.
    pub fn reaches(&self, p: Pos) -> bool {
        self.reached.contains(p)
    }

    /// Steps from the start to `p`, if reached.
    pub fn distance(&self, p: Pos) -> Option<u32> {
        todo!()
    }

    /// The route to `target`, excluding the start, written into `out`; false if not reached.
    pub fn route(&self, target: Pos, out: &mut Vec<Pos>) -> bool {
        todo!()
    }

    /// The reached squares, row then column.
    pub fn reached(&self) -> &SquareSet {
        &self.reached
    }
}

/// Everything remembered about one level.
pub struct Level {
    /// Stable id, numbered from 1 in the order levels were first seen.
    pub id: u16,
    /// The status-line location when it was first seen, such as `Dlvl:3`.
    pub label: String,
    terrain: Box<[u8; MAP_SQUARES]>,
    colours: Box<[Option<Base>; MAP_SQUARES]>,
    feature_turns: BTreeMap<Pos, u32>,
    /// Squares assumed floor because the hero stood there before any glyph showed.
    pub inferred_floor: SquareSet,
    /// Squares that ever showed a glyph.
    pub seen: SquareSet,
    /// Squares known only as showing an object.
    pub object_squares: SquareSet,
    /// Squares showing a `+` that may be a closed door or an object: routes neither enter nor leave them
    /// diagonally.
    pub maybe_doors: SquareSet,
    /// Open doors: `|` or `-` drawn brown or yellow.
    pub open_doors: SquareSet,
    /// Floor squares that show a floor engraving.
    pub engravings: SquareSet,
    /// Times the hero stood on each square, counted once per turn.
    pub visits: BTreeMap<Pos, u32>,
    /// Searches made from each square.
    pub searches: BTreeMap<Pos, u32>,
    /// Farlook and `:` readings by square.
    pub inspections: BTreeMap<Pos, Inspection>,
    /// Trap names learned from messages, by square.
    pub trap_kinds: BTreeMap<Pos, String>,
    /// Statues: monster glyphs a farlook named as statues, with the colour shown.
    pub statues: BTreeMap<Pos, (char, Shade)>,
    /// What the game said underfoot, by square.
    pub underfoot: BTreeMap<Pos, Underfoot>,
    refused: Vec<(Pos, u8)>,
}

impl Level {
    /// An empty level.
    pub fn new(id: u16, label: &str) -> Level {
        todo!()
    }

    /// The remembered terrain glyph at `p`.
    pub fn terrain(&self, p: Pos) -> Option<char> {
        todo!()
    }

    /// The remembered colour of the terrain at `p`.
    pub fn colour(&self, p: Pos) -> Option<Base> {
        todo!()
    }

    /// The turn a feature at `p` was last observed.
    pub fn feature_turn(&self, p: Pos) -> Option<u32> {
        self.feature_turns.get(&p).copied()
    }

    /// The remembered squares with their glyphs, row then column.
    pub fn known(&self) -> impl Iterator<Item = (Pos, char)> + '_ {
        self.terrain
            .iter()
            .enumerate()
            .filter(|&(_, &g)| g != 0)
            .map(|(i, &g)| (Pos::from_index(i), g as char))
    }

    /// Remembers terrain `ch` at `p`.
    pub fn remember_terrain(&mut self, p: Pos, ch: char, turn: Option<u32>, colour: Option<Base>) {
        todo!()
    }

    /// Takes in every map square of a screen in normal play: terrain, objects, doors, engravings and the hero's
    /// square.
    pub fn observe(&mut self, view: &View) {
        todo!()
    }

    /// Whether `p` is a corridor: a `#` not drawn green (tree) or cyan (sink, iron bars).
    pub fn corridor(&self, p: Pos) -> bool {
        todo!()
    }

    /// The name of the remembered feature at `p`; a liquid is named by its colour, a trap by what the game said.
    pub fn kind(&self, p: Pos) -> Option<&str> {
        todo!()
    }

    /// Merges what the game said about the hero's square, and remembers the stairs or trap it names.
    pub fn note_underfoot(&mut self, p: Pos, notes: &[String], turn: Option<u32>) {
        todo!()
    }

    /// Keeps the step from `p` in direction `key` out of routes, forgetting the oldest beyond `REFUSED_STEPS`.
    pub fn refuse(&mut self, p: Pos, key: u8) {
        todo!()
    }

    /// Forgets refused steps, which may depend on the hero rather than the terrain.
    pub fn clear_refused(&mut self) {
        self.refused.clear();
    }

    /// The name of the remembered terrain at `p` that the game checks before a plain move enters it: water, lava,
    /// a trap, or a coloured `#` such as a gas cloud.
    pub fn guarded(&self, p: Pos) -> Option<String> {
        todo!()
    }

    /// Whether a route may enter `p`: remembered ground, an open door, or a square seen only under an object.
    pub fn passable(&self, p: Pos) -> bool {
        todo!()
    }

    /// The corridor squares `p` connects to: cardinal neighbours, and diagonal ones no cardinal path joins.
    pub fn corridor_links(&self, p: Pos) -> Near {
        todo!()
    }

    /// Corridor squares beyond `p` not in `covered`, cardinal ones first.
    pub fn continuations(&self, p: Pos, covered: &SquareSet) -> Near {
        todo!()
    }

    /// Fills `paths` with shortest routes from `start` avoiding `occupied`: fewest refused steps, then fewest
    /// remembered traps, then fewest steps, as the game's own travel prefers.
    pub fn paths(&self, start: Pos, occupied: &SquareSet, paths: &mut Paths) {
        todo!()
    }

    /// A square that never showed a glyph and that the hero never stood beside.
    pub fn unknown(&self, q: Pos) -> bool {
        todo!()
    }

    /// A square the hero has not stood on with an unknown square beside it in a cardinal direction.
    pub fn frontier(&self, p: Pos) -> bool {
        todo!()
    }

    /// For each connected group of reachable frontier squares, its farthest square and route length, nearest group
    /// first, written into `out`.
    pub fn frontier_targets(&self, paths: &Paths, out: &mut Vec<(Pos, u32)>) {
        todo!()
    }
}

/// What a `+` at `p` is, from the screen around it and the level's memory.
pub fn plus_kind(view: &View, level: Option<&Level>, p: Pos) -> Plus {
    todo!()
}

/// Whether `p` sits in a wall line, as a door does.
pub fn door_line(view: &View, p: Pos) -> bool {
    todo!()
}

/// The squares a walk to a closed door ends on: its orthogonal neighbours, as doors refuse diagonal moves.
pub fn door_sides(p: Pos) -> Near {
    todo!()
}
