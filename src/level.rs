//! Per-level memory of observed terrain, passability, shortest routes through known squares, and the frontier.

use std::cmp::Reverse;
use std::collections::{BTreeMap, BinaryHeap};

use crate::glyphs::{
    Base, Condition, Shade, feature_name, is_corridor_engraving, is_engraving_text,
    is_floor_engraving, is_item, is_monster, is_warning, liquid_name, shade, trap_kind,
};
use crate::screen::View;
use crate::types::{MAP_SQUARES, Pos, SquareSet};

/// Remembered refused steps per level; the oldest is forgotten beyond this.
pub const REFUSED_STEPS: usize = 64;

/// Remembered ground a route may enter. Water and lava (`}`) are not ground: routes, like the game's own travel,
/// never cross them.
const PASSABLE: &[char] = &['.', '#', '<', '>', '^', '{', '_', '\\'];

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

/// A route search's cost to a square: refused steps, remembered traps crossed, steps; compared in that order.
type Cost = (u16, u16, u16);

/// Shortest routes from one start square, reused across searches.
pub struct Paths {
    previous: Box<[Option<Pos>; MAP_SQUARES]>,
    cost: Box<[Cost; MAP_SQUARES]>,
    reached: SquareSet,
    /// Squares to expand, by cost and then the order they were queued, so equal-cost ties go first come, first
    /// served; its capacity is kept across searches.
    queue: BinaryHeap<Reverse<(Cost, u32, u16)>>,
}

impl Default for Paths {
    fn default() -> Paths {
        Paths::new()
    }
}

impl Paths {
    /// Empty search state.
    pub fn new() -> Paths {
        Paths {
            previous: Box::new([None; MAP_SQUARES]),
            cost: Box::new([(0, 0, 0); MAP_SQUARES]),
            reached: SquareSet::new(),
            queue: BinaryHeap::new(),
        }
    }

    /// Whether the last search reached `p`.
    pub fn reaches(&self, p: Pos) -> bool {
        self.reached.contains(p)
    }

    /// Steps from the start to `p`, if reached.
    pub fn distance(&self, p: Pos) -> Option<u32> {
        let i = p.index()?;
        self.reached.contains(p).then(|| u32::from(self.cost[i].2))
    }

    /// The route to `target`, excluding the start, written into `out`; false if not reached.
    pub fn route(&self, target: Pos, out: &mut Vec<Pos>) -> bool {
        out.clear();
        if !self.reached.contains(target) {
            return false;
        }
        let mut at = target;
        while let Some(i) = at.index() {
            let Some(back) = self.previous[i] else {
                break;
            };
            // A route visits each square once; a longer walk back means the state is not one search's.
            if out.len() >= MAP_SQUARES {
                out.clear();
                return false;
            }
            out.push(at);
            at = back;
        }
        out.reverse();
        true
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
    /// Remembered glyph by square; `'\0'` where nothing is known.
    terrain: Box<[char; MAP_SQUARES]>,
    colours: Box<[Option<Base>; MAP_SQUARES]>,
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
    /// Squares that show an engraving: a bright blue `` ` `` on room floor, remembered as floor `.`, or a bright
    /// blue `#` in a corridor.
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
    /// Refused steps as (square, direction key), oldest first.
    refused: Vec<(Pos, u8)>,
    /// The squares refused steps start from, for a quick test while routing.
    refused_from: SquareSet,
}

impl Level {
    /// An empty level.
    pub fn new(id: u16, label: &str) -> Level {
        Level {
            id,
            label: label.to_owned(),
            terrain: Box::new(['\0'; MAP_SQUARES]),
            colours: Box::new([None; MAP_SQUARES]),
            inferred_floor: SquareSet::new(),
            seen: SquareSet::new(),
            object_squares: SquareSet::new(),
            maybe_doors: SquareSet::new(),
            open_doors: SquareSet::new(),
            engravings: SquareSet::new(),
            visits: BTreeMap::new(),
            searches: BTreeMap::new(),
            inspections: BTreeMap::new(),
            trap_kinds: BTreeMap::new(),
            statues: BTreeMap::new(),
            underfoot: BTreeMap::new(),
            refused: Vec::with_capacity(REFUSED_STEPS + 1),
            refused_from: SquareSet::new(),
        }
    }

    /// The remembered terrain glyph at `p`.
    pub fn terrain(&self, p: Pos) -> Option<char> {
        p.index().map(|i| self.terrain[i]).filter(|&ch| ch != '\0')
    }

    /// The remembered colour of the terrain at `p`.
    pub fn colour(&self, p: Pos) -> Option<Base> {
        p.index().and_then(|i| self.colours[i])
    }

    /// The remembered squares with their glyphs, row then column.
    pub fn known(&self) -> impl Iterator<Item = (Pos, char)> + '_ {
        self.terrain
            .iter()
            .enumerate()
            .filter(|&(_, &g)| g != '\0')
            .map(|(i, &g)| (Pos::from_index(i), g))
    }

    /// Remembers terrain `ch` at `p`.
    pub fn remember_terrain(&mut self, p: Pos, ch: char, colour: Option<Base>) {
        let Some(i) = p.index() else {
            return;
        };
        let previous = self.terrain[i];
        self.terrain[i] = ch;
        if colour.is_some() {
            self.colours[i] = colour;
        } else if previous != ch {
            self.colours[i] = None;
        }
        self.inferred_floor.remove(p);
    }

    /// Takes in every map square of a screen in normal play: terrain, objects, doors, engravings and the hero's
    /// square.
    pub fn observe(&mut self, view: &View) {
        let blind = view.cond.contains(Condition::Blind);
        for i in 0..MAP_SQUARES {
            let p = Pos::from_index(i);
            let cell = view.grid.cell(p);
            let ch = cell.ch;
            let colour = shade(cell.fg, cell.bold);
            if Some(p) == view.hero || ch == ' ' || is_warning(ch, colour) || ch == 'I' {
                continue;
            }
            let statue = !self.statues.is_empty() && self.statues.get(&p) == Some(&(ch, colour));
            if is_monster(ch) && !statue {
                // A monster sensed while blind does not show its square.
                if !blind {
                    self.seen.insert(p);
                }
                continue;
            }
            self.seen.insert(p);
            let plus = (ch == '+').then(|| plus_kind(view, Some(self), p));
            if plus == Some(Plus::DoorOrObject) {
                self.maybe_doors.insert(p);
            } else {
                self.maybe_doors.remove(p);
            }
            if is_item(ch) || statue || matches!(plus, Some(Plus::Object | Plus::DoorOrObject)) {
                if self.terrain[i] == '\0' {
                    self.object_squares.insert(p);
                }
                continue;
            }
            self.object_squares.remove(p);
            if is_floor_engraving(ch, colour) {
                self.remember_terrain(p, '.', Some(colour.base));
                self.engravings.insert(p);
                self.open_doors.remove(p);
                continue;
            }
            self.remember_terrain(p, ch, Some(colour.base));
            if is_corridor_engraving(ch, colour) {
                self.engravings.insert(p);
            } else {
                self.engravings.remove(p);
            }
            if matches!(ch, '|' | '-') && colour.base == Base::Brown {
                self.open_doors.insert(p);
            } else {
                self.open_doors.remove(p);
            }
        }
        if let Some(hero) = view.hero
            && let Some(i) = hero.index()
        {
            self.seen.insert(hero);
            self.object_squares.remove(hero);
            if self.terrain[i] == '\0' {
                self.terrain[i] = '.';
                self.inferred_floor.insert(hero);
            }
        }
    }

    /// Whether `p` is a corridor: a `#` not drawn green (tree) or cyan (sink, iron bars).
    pub fn corridor(&self, p: Pos) -> bool {
        self.terrain(p) == Some('#') && !matches!(self.colour(p), Some(Base::Green | Base::Cyan))
    }

    /// The name of the remembered feature at `p`; a liquid is named by its colour, a trap by what the game said.
    pub fn kind(&self, p: Pos) -> Option<&str> {
        let ch = self.terrain(p)?;
        match ch {
            '}' => Some(
                self.colour(p)
                    .and_then(liquid_name)
                    .unwrap_or("water or lava"),
            ),
            '^' => Some(self.trap_kinds.get(&p).map_or("trap", String::as_str)),
            _ => feature_name(ch),
        }
    }

    /// Merges what the game said about the hero's square, and remembers the stairs or trap it names.
    ///
    /// Engraving read-outs and other notes are kept apart: notes of either kind replace the earlier notes of that
    /// kind and leave the other kind alone, so an item message keeps the engraving text and a fresh read-out
    /// replaces a worn one. A `:` reading describes the whole square, so its caller removes the square's entry
    /// first.
    pub fn note_underfoot(&mut self, p: Pos, notes: &[String], turn: Option<u32>) {
        let engraving = notes.iter().any(|n| is_engraving_text(n));
        let other = notes.iter().any(|n| !is_engraving_text(n));
        let entry = self.underfoot.entry(p).or_default();
        entry.notes.retain(|n| {
            if is_engraving_text(n) {
                !engraving
            } else {
                !other
            }
        });
        for note in notes {
            if !entry.notes.contains(note) {
                entry.notes.push(note.clone());
            }
        }
        entry.turn = turn;
        for note in notes {
            if let Some(stairs) = stairs_here(note) {
                self.remember_terrain(p, stairs, None);
            }
            if let Some(trap) = trap_kind(note) {
                self.remember_terrain(p, '^', None);
                self.trap_kinds.insert(p, trap.to_owned());
            }
        }
    }

    /// Keeps the step from `p` in direction `key` out of routes, forgetting the oldest beyond `REFUSED_STEPS`.
    pub fn refuse(&mut self, p: Pos, key: u8) {
        self.refused.retain(|&step| step != (p, key));
        self.refused.push((p, key));
        if self.refused.len() > REFUSED_STEPS {
            self.refused.remove(0);
        }
        self.refused_from.clear();
        for &(q, _) in &self.refused {
            self.refused_from.insert(q);
        }
    }

    /// Forgets refused steps, which may depend on the hero rather than the terrain.
    pub fn clear_refused(&mut self) {
        self.refused.clear();
        self.refused_from.clear();
    }

    /// The name of the remembered terrain at `p` that the game checks before a plain move enters it: water, lava,
    /// a trap, or a coloured `#` such as a gas cloud.
    pub fn guarded(&self, p: Pos) -> Option<String> {
        match self.terrain(p)? {
            '}' | '^' => self.kind(p).map(str::to_owned),
            '#' if !self.corridor(p) => {
                Some(format!("{} #", self.colour(p).unwrap_or(Base::Gray).name()))
            }
            _ => None,
        }
    }

    /// Whether a route may enter `p`: remembered ground, an open door, or a square seen only under an object.
    pub fn passable(&self, p: Pos) -> bool {
        self.terrain(p).is_some_and(|ch| PASSABLE.contains(&ch))
            || self.open_doors.contains(p)
            || self.object_squares.contains(p)
    }

    /// Whether a corridor walk may step onto `q`: ground or a door, but no coloured `#`.
    fn connects(&self, q: Pos) -> bool {
        let ch = self.terrain(q);
        (ch.is_some_and(|ch| ch == '+' || PASSABLE.contains(&ch)) || self.open_doors.contains(q))
            && (ch != Some('#') || self.corridor(q))
    }

    /// Whether the diagonal step from `p` to `q` joins squares that no cardinal path through `connects` joins.
    fn linked(p: Pos, q: Pos, connects: impl Fn(Pos) -> bool) -> bool {
        !(connects(Pos::new(p.y, q.x)) || connects(Pos::new(q.y, p.x)))
    }

    /// The corridor squares `p` connects to: cardinal neighbours, and diagonal ones no cardinal path joins.
    pub fn corridor_links(&self, p: Pos) -> Near {
        let mut out = Near::default();
        for (key, q) in p.neighbours() {
            if !self.connects(q) {
                continue;
            }
            if cardinal(key)
                || (self.corridor(q)
                    && self.corridor(p)
                    && Level::linked(p, q, |r| self.connects(r)))
            {
                out.push(q);
            }
        }
        out
    }

    /// Corridor squares beyond `p` not in `covered`, cardinal ones first.
    pub fn continuations(&self, p: Pos, covered: &SquareSet) -> Near {
        let mut out = Near::default();
        for (key, q) in p.neighbours() {
            if cardinal(key) && !covered.contains(q) && self.corridor(q) {
                out.push(q);
            }
        }
        if !out.is_empty() {
            return out;
        }
        for (key, q) in p.neighbours() {
            if !cardinal(key)
                && !covered.contains(q)
                && self.corridor(q)
                && Level::linked(p, q, |r| self.corridor(r))
            {
                out.push(q);
            }
        }
        out
    }

    /// Fills `paths` with shortest routes from `start` avoiding `occupied`: fewest refused steps, then fewest
    /// remembered traps, then fewest steps, as the game's own travel prefers.
    pub fn paths(&self, start: Pos, occupied: &SquareSet, paths: &mut Paths) {
        paths.reached.clear();
        paths.queue.clear();
        let Some(si) = start.index() else {
            return;
        };
        paths.previous[si] = None;
        paths.cost[si] = (0, 0, 0);
        paths.reached.insert(start);
        let mut order: u32 = 0;
        paths.queue.push(Reverse(((0, 0, 0), order, si as u16)));
        while let Some(Reverse((cost, _, index))) = paths.queue.pop() {
            let index = index as usize;
            if cost != paths.cost[index] {
                continue;
            }
            let p = Pos::from_index(index);
            let (refused, traps, length) = cost;
            let refusing = self.refused_from.contains(p);
            for (key, q) in p.neighbours() {
                if (q != start && occupied.contains(q)) || !self.passable(q) {
                    continue;
                }
                let ch = self.terrain(q);
                if ch == Some('#') && !self.corridor(q) {
                    continue;
                }
                if !cardinal(key)
                    && (self.open_doors.contains(p)
                        || self.open_doors.contains(q)
                        || self.maybe_doors.contains(p)
                        || self.maybe_doors.contains(q))
                {
                    continue;
                }
                let step = (
                    refused + u16::from(refusing && self.refused.contains(&(p, key))),
                    traps + u16::from(ch == Some('^')),
                    length + 1,
                );
                let Some(qi) = q.index() else {
                    continue;
                };
                if paths.reached.contains(q) && paths.cost[qi] <= step {
                    continue;
                }
                paths.cost[qi] = step;
                paths.previous[qi] = Some(p);
                paths.reached.insert(q);
                order += 1;
                paths.queue.push(Reverse((step, order, qi as u16)));
            }
        }
    }

    /// A square that never showed a glyph and that the hero never stood beside.
    ///
    /// Standing on a square shows all of its neighbours; one still blank afterwards is solid rock.
    pub fn unknown(&self, q: Pos) -> bool {
        !self.seen.contains(q) && !q.neighbours().any(|(_, n)| self.visits.contains_key(&n))
    }

    /// A square the hero has not stood on with an unknown square beside it in a cardinal direction.
    ///
    /// The game digs corridors one orthogonal step at a time, so the blank square diagonally past a corridor bend
    /// that a route cut across is no way on.
    pub fn frontier(&self, p: Pos) -> bool {
        !self.visits.contains_key(&p)
            && p.neighbours()
                .any(|(key, q)| cardinal(key) && self.unknown(q))
    }

    /// For each connected group of reachable frontier squares, its farthest square and route length, nearest group
    /// first, written into `out`.
    pub fn frontier_targets(&self, paths: &Paths, out: &mut Vec<(Pos, u32)>) {
        out.clear();
        let mut visited = SquareSet::new();
        let mut beside = SquareSet::new();
        for &p in self.visits.keys() {
            visited.insert(p);
            for (_, n) in p.neighbours() {
                beside.insert(n);
            }
        }
        let unknown = |q: Pos| !self.seen.contains(q) && !beside.contains(q);
        let mut edge = SquareSet::new();
        for p in paths.reached().iter() {
            if !visited.contains(p) && p.neighbours().any(|(key, q)| cardinal(key) && unknown(q)) {
                edge.insert(p);
            }
        }
        if edge.is_empty() {
            return;
        }
        let distance = |p: Pos| paths.distance(p).unwrap_or(0);
        // Each square enters the stack once, as it leaves `edge`.
        let mut stack = [Pos::default(); MAP_SQUARES];
        // The nearest distance of each group, by the index of its farthest square.
        let mut nearest = [0u32; MAP_SQUARES];
        for i in 0..MAP_SQUARES {
            let first = Pos::from_index(i);
            if !edge.remove(first) {
                continue;
            }
            stack[0] = first;
            let mut top = 1;
            let mut near = u32::MAX;
            let mut far = (0, first);
            while top > 0 {
                top -= 1;
                let p = stack[top];
                let d = distance(p);
                near = near.min(d);
                if (d, p) > far {
                    far = (d, p);
                }
                for (_, q) in p.neighbours() {
                    if edge.remove(q) {
                        stack[top] = q;
                        top += 1;
                    }
                }
            }
            if let Some(fi) = far.1.index() {
                nearest[fi] = near;
            }
            out.push((far.1, far.0));
        }
        out.sort_unstable_by_key(|&(p, d)| (p.index().map_or(0, |i| nearest[i]), p, d));
    }
}

/// Whether a direction key is a cardinal one.
fn cardinal(key: u8) -> bool {
    matches!(key, b'h' | b'j' | b'k' | b'l')
}

/// The character at the map square one step from `p`, or a space off the map.
fn beside(view: &View, p: Pos, dy: i8, dx: i8) -> char {
    p.step(dy, dx)
        .filter(|q| q.on_map())
        .map_or(' ', |q| view.grid.cell(q).ch)
}

/// The stairs a note says are at the hero's square: `There is a staircase down here.` gives `>`.
fn stairs_here(note: &str) -> Option<char> {
    let trimmed = note.strip_suffix('.').unwrap_or(note);
    let ends_here = trimmed
        .strip_suffix("here")
        .is_some_and(|head| !head.chars().next_back().is_some_and(word_char));
    if !ends_here {
        return None;
    }
    for (phrase, stairs) in [
        ("staircase up", '<'),
        ("staircase down", '>'),
        ("ladder up", '<'),
        ("ladder down", '>'),
    ] {
        let found = note.match_indices(phrase).any(|(at, _)| {
            !note[..at].chars().next_back().is_some_and(word_char)
                && !note[at + phrase.len()..]
                    .chars()
                    .next()
                    .is_some_and(word_char)
        });
        if found {
            return Some(stairs);
        }
    }
    None
}

/// A regular-expression word character.
fn word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// What a `+` at `p` is, from the screen around it and the level's memory.
///
/// A door sits in a wall line, so a `+` with a wall glyph continuing its line, or remembered as a door, is a door.
/// One on remembered ground, or with floor beside it both across and along, is in no wall line: an object. The
/// screen cannot tell the rest, such as a `+` at a corridor's end whose walls have not been seen.
pub fn plus_kind(view: &View, level: Option<&Level>, p: Pos) -> Plus {
    let remembered = level.and_then(|l| l.terrain(p));
    if door_line(view, p) || remembered == Some('+') {
        return Plus::Door;
    }
    let across = beside(view, p, -1, 0) == '.' || beside(view, p, 1, 0) == '.';
    let along = beside(view, p, 0, -1) == '.' || beside(view, p, 0, 1) == '.';
    if remembered.is_some() || (across && along) {
        Plus::Object
    } else {
        Plus::DoorOrObject
    }
}

/// Whether `p` sits in a wall line, as a door does.
pub fn door_line(view: &View, p: Pos) -> bool {
    beside(view, p, 0, -1) == '-'
        || beside(view, p, 0, 1) == '-'
        || beside(view, p, -1, 0) == '|'
        || beside(view, p, 1, 0) == '|'
}

/// The squares a walk to a closed door ends on: its orthogonal neighbours, as doors refuse diagonal moves.
pub fn door_sides(p: Pos) -> Near {
    let mut out = Near::default();
    for (key, q) in p.neighbours() {
        if cardinal(key) {
            out.push(q);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::glyphs::Conditions;
    use crate::screen::Status;
    use crate::testkit::{STATUS1, STATUS2, grid, screen};
    use crate::types::{Colour, Grid};

    /// A view of `grid` in normal play with the hero at (`y`, `x`) on turn `turn`.
    fn view_at(grid: &Grid, y: u8, x: u8, turn: u32) -> View {
        View {
            grid: grid.clone(),
            rows: std::array::from_fn(|r| {
                let mut row = String::new();
                grid.row_text(r, &mut row);
                row
            }),
            cursor: Pos::new(y, x),
            msg: String::new(),
            message_rows: 1,
            more: false,
            menu: false,
            asking: false,
            yn: None,
            obj: None,
            spell: None,
            text: false,
            direction: false,
            getpos: false,
            ended: false,
            end: None,
            status: Status {
                location: Some("Dlvl:3".to_owned()),
                dlvl: Some(3),
                turn: Some(turn),
                ..Status::default()
            },
            cond: Conditions::default(),
            prompt: false,
            hero: Some(Pos::new(y, x)),
            engulfed: false,
            normal: true,
        }
    }

    /// Map rows from terminal row 1, as default-coloured cells.
    fn map(rows: &[&str]) -> Grid {
        grid(&screen("", rows, STATUS1, STATUS2))
    }

    fn paint(grid: &mut Grid, y: usize, x: usize, fg: Colour, bold: bool) {
        grid.cells[y][x].fg = fg;
        grid.cells[y][x].bold = bold;
    }

    /// A level that observed `grid` with the hero standing at (`y`, `x`).
    fn observed(grid: &Grid, y: u8, x: u8) -> Level {
        let mut level = Level::new(1, "Dlvl:3");
        visit(&mut level, grid, y, x, 400);
        level
    }

    /// Observes `grid` with the hero at (`y`, `x`) and counts the visit.
    fn visit(level: &mut Level, grid: &Grid, y: u8, x: u8, turn: u32) {
        level.observe(&view_at(grid, y, x, turn));
        *level.visits.entry(Pos::new(y, x)).or_default() += 1;
    }

    fn search(level: &Level, start: Pos, occupied: &[Pos]) -> Paths {
        let mut blocked = SquareSet::new();
        for &q in occupied {
            blocked.insert(q);
        }
        let mut paths = Paths::new();
        level.paths(start, &blocked, &mut paths);
        paths
    }

    fn route(level: &Level, start: Pos, target: Pos, occupied: &[Pos]) -> Option<Vec<Pos>> {
        let paths = search(level, start, occupied);
        let mut out = Vec::new();
        paths.route(target, &mut out).then_some(out)
    }

    fn at(y: u8, x: u8) -> Pos {
        Pos::new(y, x)
    }

    fn frontier(level: &Level, start: Pos) -> Vec<(Pos, u32)> {
        let paths = search(level, start, &[]);
        let mut out = Vec::new();
        level.frontier_targets(&paths, &mut out);
        out
    }

    #[test]
    fn coloured_structures_are_not_corridor_routes() {
        for colour in [Colour::Green, Colour::Cyan] {
            let mut g = map(&["", "   @#>"]);
            paint(&mut g, 2, 4, colour, false);
            let mut level = observed(&g, 2, 3);
            assert!(!level.corridor(at(2, 4)));
            assert!(route(&level, at(2, 3), at(2, 5), &[]).is_none());
            let name = format!("{} #", shade(colour, false).base.name());
            assert_eq!(level.guarded(at(2, 4)), Some(name));
            // Out of sight, the structure is still remembered.
            visit(&mut level, &map(&["", "   @ >"]), 2, 3, 401);
            assert!(route(&level, at(2, 3), at(2, 5), &[]).is_none());
        }
        let level = observed(&map(&["", "   @#>"]), 2, 3);
        assert!(level.corridor(at(2, 4)));
        assert_eq!(level.guarded(at(2, 4)), None);
        assert_eq!(
            route(&level, at(2, 3), at(2, 5), &[]),
            Some(vec![at(2, 4), at(2, 5)])
        );
    }

    #[test]
    fn an_occupied_corridor_blocks_its_route_until_it_clears() {
        let level = observed(&map(&["", " >#####@"]), 2, 7);
        assert!(route(&level, at(2, 7), at(2, 1), &[at(2, 6)]).is_none());
        let paths = search(&level, at(2, 7), &[]);
        assert_eq!(paths.distance(at(2, 1)), Some(6));
        // The start itself never blocks.
        assert!(route(&level, at(2, 7), at(2, 1), &[at(2, 7)]).is_some());
    }

    #[test]
    fn an_occupied_square_is_routed_around() {
        let rows = [" ------- ", " |@.>..| ", " |.....| ", " ------- "];
        let level = observed(&map(&rows), 2, 2);
        let path = route(&level, at(2, 2), at(2, 4), &[at(2, 3)]).unwrap_or_default();
        assert_eq!(path.last(), Some(&at(2, 4)));
        assert!(!path.contains(&at(2, 3)));
        assert_eq!(path.len(), 2);
    }

    #[test]
    fn routes_reach_a_door_side_and_end_on_the_target() {
        let rows = [" --------- ", " |.@.....| ", " |....*..| ", " ----+---- "];
        let level = observed(&map(&rows), 2, 3);
        assert_eq!(level.terrain(at(4, 5)), Some('+'));
        assert!(!level.passable(at(4, 5)));
        let sides = door_sides(at(4, 5));
        assert_eq!(sides.as_slice(), [at(4, 4), at(5, 5), at(3, 5), at(4, 6)]);
        let path = route(&level, at(2, 3), at(3, 5), &[]).unwrap_or_default();
        assert_eq!(path.last(), Some(&at(3, 5)));
        assert_eq!(path.len(), 2);
        assert_eq!(route(&level, at(3, 4), at(3, 5), &[]), Some(vec![at(3, 5)]));
        // The object at 6,3 lies on ground a route may cross.
        assert!(level.object_squares.contains(at(3, 6)));
        assert!(route(&level, at(2, 3), at(3, 6), &[]).is_some());
    }

    #[test]
    fn frontier_ignores_squares_under_monsters_or_objects_and_visited_edges() {
        let first = map(&[" -------", " |.....|", " |.d$.@.", " -------"]);
        let mut level = observed(&first, 3, 6);
        assert!(level.seen.contains(at(3, 3)));
        assert_eq!(level.terrain(at(3, 3)), None);
        assert!(level.frontier(at(3, 7)));
        assert!(!level.frontier(at(3, 4)));
        assert_eq!(frontier(&level, at(3, 6)), [(at(3, 7), 1)]);
        let doorway = map(&[" -------", " |.....|", " |.d$..@", " -------"]);
        visit(&mut level, &doorway, 3, 7, 401);
        visit(&mut level, &first, 3, 6, 402);
        assert!(!level.frontier(at(3, 7)));
        assert_eq!(frontier(&level, at(3, 6)), []);
    }

    #[test]
    fn frontier_picks_each_groups_farthest_square_nearest_group_first() {
        let level = observed(&map(&["", "   @#####"]), 2, 3);
        assert_eq!(frontier(&level, at(2, 3)), [(at(2, 8), 5)]);
        // Two ways on, each its own group: east from the hero, and west past squares already walked.
        let mut level = observed(&map(&["", "##...@######"]), 2, 5);
        level.visits.insert(at(2, 4), 1);
        level.visits.insert(at(2, 3), 1);
        let targets = frontier(&level, at(2, 5));
        assert_eq!(targets, [(at(2, 11), 6), (at(2, 0), 5)]);
        // Groups as near as each other go in square order.
        let level = observed(&map(&["", "##...@######"]), 2, 5);
        assert_eq!(frontier(&level, at(2, 5)), [(at(2, 0), 5), (at(2, 11), 6)]);
    }

    #[test]
    fn a_corridor_bend_cut_across_is_no_frontier() {
        let g = map(&["", "   ###", "     #", "     #"]);
        let mut level = Level::new(1, "Dlvl:3");
        for (turn, (y, x)) in [(2, 3), (2, 4), (3, 5), (4, 5)].into_iter().enumerate() {
            visit(&mut level, &g, y, x, 400 + turn as u32);
        }
        assert!(!level.frontier(at(2, 5)));
        assert_eq!(frontier(&level, at(4, 5)), []);
        assert_eq!(level.corridor_links(at(2, 3)).len(), 1);
        assert_eq!(level.corridor_links(at(4, 5)).len(), 1);
    }

    #[test]
    fn routes_never_cross_water_or_lava() {
        let mut g = map(&["", "   @}>"]);
        paint(&mut g, 2, 4, Colour::Blue, false);
        let level = observed(&g, 2, 3);
        assert_eq!(level.kind(at(2, 4)), Some("water"));
        assert_eq!(level.guarded(at(2, 4)).as_deref(), Some("water"));
        assert!(!level.passable(at(2, 4)));
        assert!(route(&level, at(2, 3), at(2, 5), &[]).is_none());
        paint(&mut g, 2, 4, Colour::Red, false);
        assert_eq!(observed(&g, 2, 3).kind(at(2, 4)), Some("lava"));
        paint(&mut g, 2, 4, Colour::Green, false);
        assert_eq!(observed(&g, 2, 3).kind(at(2, 4)), Some("water or lava"));
    }

    #[test]
    fn routes_cross_a_known_trap_only_without_another_route() {
        let level = observed(&map(&["", "   @#^#>"]), 2, 3);
        assert_eq!(
            route(&level, at(2, 3), at(2, 7), &[]),
            Some(vec![at(2, 4), at(2, 5), at(2, 6), at(2, 7)])
        );
        assert_eq!(level.guarded(at(2, 5)).as_deref(), Some("trap"));
        let rows = [" ------- ", " |@.^.>| ", " |.....| ", " ------- "];
        let level = observed(&map(&rows), 2, 2);
        let path = route(&level, at(2, 2), at(2, 6), &[]).unwrap_or_default();
        assert!(!path.contains(&at(2, 4)));
        assert_eq!(path.len(), 4);
    }

    #[test]
    fn routes_cross_squares_seen_only_under_objects() {
        let level = observed(&map(&["", "   @#$#>"]), 2, 3);
        assert!(level.object_squares.contains(at(2, 5)));
        assert_eq!(
            route(&level, at(2, 3), at(2, 7), &[]),
            Some(vec![at(2, 4), at(2, 5), at(2, 6), at(2, 7)])
        );
    }

    #[test]
    fn diagonal_corridor_steps_connect_unless_a_cardinal_path_joins_them() {
        let level = observed(&map(&["", "   @#", "     #", "      ##"]), 2, 3);
        assert_eq!(
            level.corridor_links(at(2, 4)).as_slice(),
            [at(2, 3), at(3, 5)]
        );
        assert_eq!(
            level.corridor_links(at(3, 5)).as_slice(),
            [at(2, 4), at(4, 6)]
        );
        assert_eq!(level.corridor_links(at(4, 7)).as_slice(), [at(4, 6)]);
        let bend = observed(&map(&["", "   @##", "     ##"]), 2, 3);
        assert_eq!(
            bend.corridor_links(at(2, 5)).as_slice(),
            [at(2, 4), at(3, 5)]
        );
        assert_eq!(bend.corridor_links(at(3, 6)).as_slice(), [at(3, 5)]);
        let mut covered = SquareSet::new();
        covered.insert(at(2, 4));
        assert_eq!(
            bend.continuations(at(2, 5), &covered).as_slice(),
            [at(3, 5)]
        );
        let diagonal = observed(&map(&["", "   @#", "     #"]), 2, 3);
        let mut covered = SquareSet::new();
        covered.insert(at(2, 3));
        assert_eq!(
            diagonal.continuations(at(2, 4), &covered).as_slice(),
            [at(3, 5)]
        );
    }

    #[test]
    fn mapped_corridors_have_endpoints_and_junctions() {
        let level = observed(
            &map(&["", "   @############", "         #", "         #"]),
            2,
            3,
        );
        assert_eq!(level.corridor_links(at(2, 9)).len(), 3);
        assert_eq!(level.corridor_links(at(2, 15)).len(), 1);
        assert_eq!(level.corridor_links(at(4, 9)).len(), 1);
        assert_eq!(level.corridor_links(at(2, 12)).len(), 2);
    }

    #[test]
    fn a_plus_is_a_door_in_a_wall_line_an_object_on_floor_or_undecided() {
        let rows = [" ---+--- ", " |.@.+.| ", " |.....| ", " ------- "];
        let level = observed(&map(&rows), 2, 3);
        assert_eq!(level.terrain(at(1, 4)), Some('+'));
        assert_eq!(level.terrain(at(2, 5)), None);
        assert!(level.object_squares.contains(at(2, 5)));
        assert!(!level.maybe_doors.contains(at(2, 5)));
        let rows = [" -----", " |...|", " |.@.+", " |...|  +", " -----"];
        let level = observed(&map(&rows), 3, 3);
        assert_eq!(level.terrain(at(3, 5)), Some('+'));
        assert!(level.maybe_doors.contains(at(4, 8)));
        assert!(level.object_squares.contains(at(4, 8)));
        let paths = search(&level, at(3, 3), &[]);
        assert_eq!(paths.distance(at(3, 4)), Some(1));
        assert!(!paths.reaches(at(4, 8)));
    }

    #[test]
    fn doors_are_neither_entered_nor_left_diagonally() {
        let rows = [" ----- ", " |...| ", " |...| ", " --|-- ", "   .   "];
        let mut g = map(&rows);
        paint(&mut g, 4, 3, Colour::Brown, false);
        let level = observed(&g, 2, 2);
        assert!(level.open_doors.contains(at(4, 3)));
        assert!(level.passable(at(4, 3)));
        assert_eq!(
            route(&level, at(2, 2), at(5, 3), &[]),
            Some(vec![at(3, 3), at(4, 3), at(5, 3)])
        );
        // From the corner square the door is only a diagonal away: the route steps beside it first.
        assert_eq!(
            route(&level, at(3, 2), at(4, 3), &[]),
            Some(vec![at(3, 3), at(4, 3)])
        );
    }

    #[test]
    fn a_bright_blue_backtick_is_engraved_floor_and_any_other_is_a_boulder() {
        let mut g = map(&["", "   @.`.>"]);
        paint(&mut g, 2, 5, Colour::Blue, true);
        let mut level = observed(&g, 2, 3);
        assert_eq!(level.terrain(at(2, 5)), Some('.'));
        assert!(level.engravings.contains(at(2, 5)));
        assert!(level.passable(at(2, 5)));
        assert!(!level.object_squares.contains(at(2, 5)));
        assert_eq!(
            route(&level, at(2, 3), at(2, 7), &[]).map(|r| r.len()),
            Some(4)
        );
        // Seen again without the engraving, the square stays floor and loses the flag.
        visit(&mut level, &map(&["", "   @...>"]), 2, 3, 401);
        assert!(!level.engravings.contains(at(2, 5)));
        assert_eq!(level.terrain(at(2, 5)), Some('.'));

        for (fg, bold) in [
            (Colour::Default, false),
            (Colour::Blue, false),
            (Colour::White, true),
        ] {
            let mut g = map(&["", "   @.`.>"]);
            paint(&mut g, 2, 5, fg, bold);
            let level = observed(&g, 2, 3);
            assert_eq!(level.terrain(at(2, 5)), Some('`'));
            assert!(!level.engravings.contains(at(2, 5)));
            assert!(!level.passable(at(2, 5)));
            assert!(route(&level, at(2, 3), at(2, 7), &[]).is_none());
        }
    }

    #[test]
    fn a_white_zero_is_a_warning_and_any_other_a_boulder() {
        let mut g = map(&["", "   @.0.>"]);
        paint(&mut g, 2, 5, Colour::Default, true);
        let level = observed(&g, 2, 3);
        assert_eq!(level.terrain(at(2, 5)), None);
        assert!(!level.object_squares.contains(at(2, 5)));

        let mut g = map(&["", "   @...>"]);
        let mut level = observed(&g, 2, 3);
        g.cells[2][5].ch = '0';
        paint(&mut g, 2, 5, Colour::White, false);
        visit(&mut level, &g, 2, 3, 401);
        assert_eq!(level.terrain(at(2, 5)), Some('.'));
        assert_eq!(
            route(&level, at(2, 3), at(2, 7), &[]).map(|r| r.len()),
            Some(4)
        );

        let level = observed(&map(&["", "   @.0.>"]), 2, 3);
        assert_eq!(level.terrain(at(2, 5)), Some('0'));
        assert!(route(&level, at(2, 3), at(2, 7), &[]).is_none());
    }

    #[test]
    fn a_bright_blue_corridor_square_is_an_engraved_corridor() {
        let mut g = map(&["", "   @###>"]);
        paint(&mut g, 2, 5, Colour::BrightBlue, false);
        let level = observed(&g, 2, 3);
        assert!(level.corridor(at(2, 5)));
        assert!(level.engravings.contains(at(2, 5)));
        assert_eq!(level.guarded(at(2, 5)), None);
        assert!(route(&level, at(2, 3), at(2, 7), &[]).is_some());
    }

    #[test]
    fn the_heros_square_is_inferred_floor_until_a_glyph_shows() {
        let mut level = observed(&map(&["", "   @"]), 2, 3);
        assert_eq!(level.terrain(at(2, 3)), Some('.'));
        assert!(level.inferred_floor.contains(at(2, 3)));
        visit(&mut level, &map(&["", "   .@"]), 2, 4, 401);
        assert!(!level.inferred_floor.contains(at(2, 3)));
        assert!(level.inferred_floor.contains(at(2, 4)));
    }

    #[test]
    fn a_blind_hero_does_not_see_the_square_of_a_sensed_monster() {
        let g = map(&["", "   @.  d"]);
        let mut view = view_at(&g, 2, 3, 400);
        view.cond.insert(Condition::Blind);
        let mut level = Level::new(1, "Dlvl:3");
        level.observe(&view);
        assert!(!level.seen.contains(at(2, 7)));
        let level = observed(&g, 2, 3);
        assert!(level.seen.contains(at(2, 7)));
        assert_eq!(level.terrain(at(2, 7)), None);
    }

    #[test]
    fn a_statue_is_an_object_on_its_square() {
        let g = map(&[" ------- ", " |.@.d.| ", " ------- "]);
        let mut level = observed(&g, 2, 3);
        assert!(!level.object_squares.contains(at(2, 5)));
        level.statues.insert(at(2, 5), ('d', Shade::GRAY));
        visit(&mut level, &g, 2, 3, 401);
        assert!(level.object_squares.contains(at(2, 5)));
        assert!(level.passable(at(2, 5)));
    }

    #[test]
    fn features_are_named() {
        let level = observed(&map(&[" -------- ", " |.@_^.>| ", " -------- "]), 2, 3);
        assert_eq!(level.kind(at(2, 4)), Some("altar"));
        assert_eq!(level.kind(at(2, 5)), Some("trap"));
        assert_eq!(level.kind(at(2, 7)), Some("down stairs"));
        assert_eq!(level.kind(at(2, 6)), None);
        let known: Vec<(Pos, char)> = level.known().filter(|&(p, _)| p.y == 2).collect();
        assert_eq!(known.first(), Some(&(at(2, 1), '|')));
        assert_eq!(known.len(), 8);
    }

    #[test]
    fn underfoot_notes_name_stairs_and_traps() {
        let mut level = observed(&map(&[" ------- ", " |.@...| ", " ------- "]), 2, 3);
        let notes = ["There is a staircase down here.", "You see here a dagger."].map(String::from);
        level.note_underfoot(at(2, 3), &notes, Some(401));
        assert_eq!(level.terrain(at(2, 3)), Some('>'));
        assert!(!level.inferred_floor.contains(at(2, 3)));
        assert_eq!(level.underfoot[&at(2, 3)].notes, notes);
        let trap = ["A little dart shoots out at you!".to_owned()];
        level.note_underfoot(at(2, 4), &trap, Some(402));
        assert_eq!(level.terrain(at(2, 4)), Some('^'));
        assert_eq!(level.kind(at(2, 4)), Some("dart trap"));
        level.note_underfoot(at(2, 5), &["There is a ladder up here".to_owned()], None);
        assert_eq!(level.terrain(at(2, 5)), Some('<'));
        level.note_underfoot(
            at(2, 2),
            &["The staircase down here is narrow".to_owned()],
            None,
        );
        assert_eq!(level.terrain(at(2, 2)), Some('.'));
    }

    #[test]
    fn underfoot_notes_merge_engraving_text_with_other_notes() {
        let mut level = Level::new(1, "Dlvl:3");
        let p = at(2, 3);
        let read = [
            "Something is written here in the dust.",
            "You read: \"Elbereth\".",
        ]
        .map(String::from);
        level.note_underfoot(p, &read, Some(400));
        level.note_underfoot(p, &["You see here a dagger.".to_owned()], Some(401));
        assert_eq!(
            level.underfoot[&p].notes,
            [read[0].as_str(), read[1].as_str(), "You see here a dagger."]
        );
        assert_eq!(level.underfoot[&p].turn, Some(401));
        let worn = [
            "Something is written here in the dust.",
            "You read: \"Elb?reth\".",
        ]
        .map(String::from);
        level.note_underfoot(p, &worn, Some(405));
        assert_eq!(
            level.underfoot[&p].notes,
            ["You see here a dagger.", worn[0].as_str(), worn[1].as_str()]
        );
        level.note_underfoot(p, &["You see here 2 apples.".to_owned()], Some(406));
        assert_eq!(
            level.underfoot[&p].notes,
            [worn[0].as_str(), worn[1].as_str(), "You see here 2 apples."]
        );
    }

    #[test]
    fn refused_steps_are_bounded_and_routed_around() {
        let mut level = Level::new(1, "Dlvl:3");
        for x in 0..70 {
            level.refuse(at(2, x), b'l');
        }
        assert_eq!(level.refused.len(), REFUSED_STEPS);
        assert!(!level.refused.contains(&(at(2, 0), b'l')));
        assert!(level.refused.contains(&(at(2, 69), b'l')));
        level.refuse(at(2, 10), b'l');
        assert_eq!(level.refused.last(), Some(&(at(2, 10), b'l')));
        assert_eq!(level.refused.len(), REFUSED_STEPS);
        level.clear_refused();
        assert!(level.refused.is_empty());

        let rows = [" ------ ", " |@..>| ", " |....| ", " ------ "];
        let mut level = observed(&map(&rows), 2, 2);
        level.refuse(at(2, 2), b'l');
        let path = route(&level, at(2, 2), at(2, 5), &[]).unwrap_or_default();
        assert_eq!(path.first(), Some(&at(3, 3)));
        assert_eq!(path.last(), Some(&at(2, 5)));
        // A refused step is still taken when no route avoids it.
        let mut corridor = observed(&map(&["", "   @##>"]), 2, 3);
        corridor.refuse(at(2, 4), b'l');
        assert_eq!(
            route(&corridor, at(2, 3), at(2, 6), &[]).map(|r| r.len()),
            Some(3)
        );
    }

    #[test]
    fn remembered_terrain_keeps_its_colour_only_while_the_glyph_stays() {
        let mut level = Level::new(1, "Dlvl:3");
        let p = at(2, 3);
        level.remember_terrain(p, '#', Some(Base::Green));
        level.remember_terrain(p, '#', None);
        assert_eq!(level.colour(p), Some(Base::Green));
        level.remember_terrain(p, '.', None);
        assert_eq!(level.colour(p), None);
        level.remember_terrain(Pos::new(0, 3), '#', None);
        assert_eq!(level.terrain(Pos::new(0, 3)), None);
    }

    #[test]
    fn paths_are_reused_and_report_distances() {
        let level = observed(&map(&["", "   @#####"]), 2, 3);
        let mut paths = Paths::new();
        level.paths(at(2, 3), &SquareSet::new(), &mut paths);
        assert_eq!(paths.distance(at(2, 8)), Some(5));
        assert_eq!(paths.distance(at(2, 3)), Some(0));
        let mut out = vec![at(9, 9)];
        assert!(paths.route(at(2, 3), &mut out));
        assert!(out.is_empty());
        level.paths(at(2, 8), &SquareSet::new(), &mut paths);
        assert_eq!(paths.distance(at(2, 3)), Some(5));
        assert!(!paths.reaches(at(3, 3)));
        assert!(!paths.route(at(3, 3), &mut out));
        assert_eq!(paths.distance(at(3, 3)), None);
        assert_eq!(paths.reached().len(), 6);
    }
}
