//! Bounded execution: sending keys, walking a route, exploring a corridor, searching in place and the harness's
//! own information commands, each stopping at observable boundaries; and paging through the end of the game.

use std::iter::repeat_n;

use crate::glyphs::{Base, is_monster};
use crate::level::Paths;
use crate::link::{LinkError, Terminal};
use crate::observe::{Entity, EntityKind, LookSource, Memory};
use crate::screen::{MenuEntry, Phase, Status, View, strip_more};
use crate::types::{
    Answered, MAP_BOTTOM, MAP_SQUARES, MAP_TOP, NoTime, NoTimeWhy, Pos, SquareSet, escape_key,
};

/// Most `--More--` pages or text windows dismissed in one go.
pub const PAGES: u32 = 32;
/// Most message lines and most answered prompts one action reports; the earliest beyond this are counted instead,
/// so a reply stays small however many pages the keys pass.
pub const REPORTED: usize = 32;
/// Repeated searches that leave the screen unchanged before a search stops.
pub const IDLE_SEARCHES: u32 = 3;
/// Most distinct answers kept for the searches that took no time; searches with further answers are only counted.
pub const NO_TIME_ANSWERS: usize = 8;

const ESCAPE: u8 = 0x1b;
const REDRAW: u8 = 0x12;

/// What an action does: send the caller's keys, one byte at a time so each prompt is seen before the key that
/// answers it; walk a known route; walk to a corridor end and follow the corridor beyond it; search in place
/// repeatedly; farlook a square (`;`, then the cursor keys in `keys`); or look underfoot (`:`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Keys,
    Travel,
    Explore,
    Search,
    Farlook,
    LookHere,
}

/// A bounded action: the keys of one step (or a farlook's cursor keys), the most steps to take, the square it is
/// going to or looking at, the known route there, first step first, whether that route may step into water, and
/// for a walk or rest the radius around the hero within which an arriving monster stops it (`None`: anywhere).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Action {
    pub kind: Kind,
    pub keys: Vec<u8>,
    pub steps: u32,
    pub target: Option<Pos>,
    pub route: Vec<Pos>,
    pub wet: bool,
    pub near: Option<u8>,
}

/// Which observations changed and ended an action, as bit flags: the status line other than turn, HP and Pw (or
/// HP or Pw dropped); the conditions; the hero engulfed or released; remembered terrain; a monster in view where
/// no monster of its glyph and colour stood within one square at the step before; a message not about a pet; HP
/// that was below its maximum reached it.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Changed(pub u8);

impl Changed {
    pub const STATUS: Changed = Changed(1);
    pub const CONDITIONS: Changed = Changed(2);
    pub const ENGULFED: Changed = Changed(4);
    pub const TERRAIN: Changed = Changed(8);
    pub const ENTITIES: Changed = Changed(16);
    pub const MESSAGE: Changed = Changed(32);
    pub const HP_FULL: Changed = Changed(64);

    pub fn contains(self, other: Changed) -> bool {
        self.0 & other.0 == other.0 && other.0 != 0
    }

    pub fn is_empty(self) -> bool {
        self.0 == 0
    }
}

/// Why an action ended. A refused step left the hero in place with no turn passing, and is kept out of routes; a
/// refused wet step is a step into water that left the hero in place, turn or not, and is only reported.
/// `Changed` names what changed in `Outcome::changed`.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Stop {
    #[default]
    Completed,
    StepLimit,
    Prompt,
    MovementInterrupted,
    NoEffect,
    RouteChanged,
    FeatureDiscovered,
    BranchDiscovered,
    CorridorEnds,
    Changed,
    Refused {
        from: Pos,
        to: Pos,
    },
    WetRefused {
        from: Pos,
        to: Pos,
    },
    GameOver,
    Link(LinkError),
    /// The game kept showing pages past the bound.
    Failed(String),
}

/// A monster that ended a rest or walk by arriving, and its distance from the hero then.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Arrival {
    pub glyph: char,
    pub pos: Pos,
    pub dist: u8,
}

/// What an action did.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Outcome {
    pub stop: Stop,
    /// For `Stop::Changed`, what changed.
    pub changed: Changed,
    /// For a `Changed::ENTITIES` stop, the monster that arrived.
    pub arrival: Option<Arrival>,
    /// Steps taken: route steps, searches, or caller keys sent.
    pub steps: u32,
    /// Route steps sent into remembered water.
    pub wet: u32,
    /// Game turns that passed, when the status line showed them before and after.
    pub turns: Option<i64>,
    /// For a search, the searches after which the turn counter read as before, and what the screen showed with
    /// them, by distinct answer, at most `NO_TIME_ANSWERS`.
    pub no_time: u32,
    pub no_time_why: Vec<NoTime>,
    /// Message lines the game wrote meanwhile, text-window lines after the first indented by two spaces. Screens
    /// that ask a question are left out: the question is in `answered` or still open.
    pub messages: Vec<String>,
    /// Prompts the keys passed and the key that answered each.
    pub answered: Vec<Answered>,
    /// Earlier lines and prompts left out of `messages` and `answered`.
    pub messages_left_out: u32,
    pub answered_left_out: u32,
}

impl Outcome {
    fn message(&mut self, text: String) {
        capped(&mut self.messages, &mut self.messages_left_out, text);
    }

    fn answer(&mut self, answered: Answered) {
        capped(&mut self.answered, &mut self.answered_left_out, answered);
    }

    /// Message lines written so far, counting those left out.
    fn written(&self) -> usize {
        self.messages.len() + self.messages_left_out as usize
    }

    /// Records a search that took no time with what the screen showed.
    fn no_time(&mut self, why: NoTimeWhy, text: Option<String>) {
        self.no_time += 1;
        let answers = &mut self.no_time_why;
        if let Some(answer) = answers.iter_mut().find(|a| a.why == why && a.text == text) {
            answer.searches += 1;
        } else if answers.len() < NO_TIME_ANSWERS {
            answers.push(NoTime {
                searches: 1,
                why,
                text,
            });
        }
    }
}

/// Adds `item` to `list`, leaving out (and counting) the earliest beyond `REPORTED`.
fn capped<T>(list: &mut Vec<T>, left_out: &mut u32, item: T) {
    if list.len() >= REPORTED {
        list.remove(0);
        *left_out += 1;
    }
    list.push(item);
}

/// The terminal and memory one action works on, and what it has done so far.
struct Io<'a> {
    term: &'a mut dyn Terminal,
    memory: &'a mut Memory,
    outcome: Outcome,
}

/// Runs actions, keeping scratch buffers across calls.
#[derive(Default)]
pub struct Executor {
    /// The last batch sent started a run (travel `_`, `G` or a shift-run).
    after_run: bool,
    /// Monsters at the previous step of a walk or rest, and now.
    previous: Vec<Entity>,
    now: Vec<Entity>,
    /// Text-window lines and menu entries being read.
    window: Vec<String>,
    entries: Vec<MenuEntry>,
    /// The screen an information command was asked from.
    before: Option<View>,
    /// When the action began: the level it was on, its remembered terrain (`'\0'` where none) and colour by
    /// square, the squares that had shown a glyph, the open doors and the status line.
    level: Option<u16>,
    terrain: Vec<(char, Option<Base>)>,
    seen: SquareSet,
    doors: SquareSet,
    status: Status,
    /// Squares an exploration has known or stood on.
    covered: SquareSet,
}

impl Executor {
    /// Runs `action` to its first boundary, ingesting every screen into `memory`. The caller has read the screen
    /// since the game last took input, so nothing is re-read first. Keys actions record each prompt
    /// passed and the key that answered it, and flag keys typed into a text prompt; nothing is answered on the
    /// caller's behalf. Walks and rests stop for a monster only if no monster of the same glyph and colour was
    /// within one square of it at the previous step; `I` and warning digits never stop them. When the game ends
    /// during the action, the end-of-game screens are paged through as `finish_game` does.
    pub fn run(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        action: &Action,
    ) -> Outcome {
        let mut io = Io {
            term,
            memory,
            outcome: Outcome::default(),
        };
        let aside = matches!(action.kind, Kind::Farlook | Kind::LookHere);
        io.memory.set_aside(aside);
        let view = io.term.view();
        let (turn, ended_before) = (view.status.turn, view.ended);
        io.memory.ingest(view);
        io.outcome.stop = if ended_before && action.kind != Kind::Keys {
            Stop::GameOver
        } else {
            io.memory.begin(view);
            match action.kind {
                Kind::Keys => self.type_keys(&mut io, &action.keys),
                Kind::Farlook => self.farlook(&mut io, action),
                Kind::LookHere => self.look_here(&mut io, action),
                Kind::Travel | Kind::Explore | Kind::Search => self.walk(&mut io, action),
            }
            .unwrap_or_else(|stop| stop)
        };
        io.memory.set_aside(false);
        if let (Some(before), Some(after)) = (turn, io.term.view().status.turn) {
            io.outcome.turns = Some(i64::from(after) - i64::from(before));
        }
        if io.outcome.stop == Stop::GameOver && !ended_before {
            let end = self.finish_game(io.term, io.memory);
            for answered in end.answered {
                io.outcome.answer(answered);
            }
            io.outcome.stop = end.stop;
        }
        io.outcome
    }

    /// Pages through the end of the game, declining the disclosure questions and dismissing `--More--`, past the
    /// tombstone and the farewell to the score list, capturing each screen into `memory`; what the end shows is
    /// end text, not messages. Each question answered is recorded. A game that goes on (life saving) completes.
    pub fn finish_game(&mut self, term: &mut dyn Terminal, memory: &mut Memory) -> Outcome {
        let mut io = Io {
            term,
            memory,
            outcome: Outcome::default(),
        };
        io.outcome.stop = self.finish(&mut io);
        io.outcome
    }

    /// Dismisses the `--More--` pages on screen, such as a game's welcome, taking their messages into `memory`.
    pub fn dismiss(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
    ) -> Result<(), LinkError> {
        memory.ingest(term.view());
        let mut io = Io {
            term,
            memory,
            outcome: Outcome::default(),
        };
        match self.page(&mut io) {
            Err(Stop::Link(error)) => Err(error),
            _ => Ok(()),
        }
    }

    /// Sends `keys` as one batch and takes in the screen it leaves; true when the game wrote news.
    fn send(&mut self, io: &mut Io, keys: &[u8]) -> Result<bool, Stop> {
        io.term.send(keys).map_err(Stop::Link)?;
        self.after_run = is_run(keys);
        Ok(self.take(io))
    }

    /// Takes the current screen into memory and its message into `outcome`; true when it is a message not about a
    /// pet.
    fn take(&mut self, io: &mut Io) -> bool {
        let view = io.term.view();
        io.memory.ingest(view);
        self.window.clear();
        if view.more && view.more_window(&mut self.window) {
            let mut news = false;
            let lines = self.window.iter().map(|line| line.trim());
            for (n, text) in lines.filter(|text| !text.is_empty()).enumerate() {
                news |= !io.memory.about_pet(text);
                let indent = if n == 0 { "" } else { "  " };
                io.outcome.message(format!("{indent}{text}"));
            }
            return news;
        }
        let text = strip_more(&view.msg).trim();
        if question(view)
            || view.phase() == Phase::Menu
            || view.counting()
            || !io.term.message_written()
            || text.is_empty()
        {
            return false;
        }
        io.outcome.message(text.to_owned());
        !io.memory.about_pet(text)
    }

    /// Dismisses `--More--` pages; true when any page held news.
    fn page(&mut self, io: &mut Io) -> Result<bool, Stop> {
        let mut news = false;
        for _ in 0..PAGES {
            if io.term.view().phase() != Phase::More {
                return Ok(news);
            }
            news |= self.send(io, b" ")?;
        }
        if io.term.view().phase() == Phase::More {
            return Err(unfinished("--More-- pages"));
        }
        Ok(news)
    }

    /// Dismisses `--More--` pages and text windows, which have no entries to choose, before an expected prompt.
    fn notices(&mut self, io: &mut Io) -> Result<(), Stop> {
        for _ in 0..PAGES {
            self.page(io)?;
            let view = io.term.view();
            if view.phase() != Phase::Menu {
                return Ok(());
            }
            self.entries.clear();
            view.menu_entries(&mut self.entries);
            if !self.entries.is_empty() {
                return Ok(());
            }
            self.send(io, &[ESCAPE])?;
        }
        Err(unfinished("text windows"))
    }

    /// The caller's keys, one byte at a time, except that text typed into a free-text prompt goes as one batch up
    /// to the key that ends it. Each key that meets a prompt is recorded against it.
    fn type_keys(&mut self, io: &mut Io, keys: &[u8]) -> Result<Stop, Stop> {
        let after_run = self.after_run;
        // The prompt the previous key answered, while further keys may extend its answer.
        let mut open: Option<Phase> = None;
        let mut i = 0;
        while i < keys.len() {
            let view = io.term.view();
            let phase = view.phase();
            let mut end = i + 1;
            // A question or a `--More--` takes the keys sent to it as its answer; a menu's keys select entries.
            if question(view) || view.more && phase != Phase::Menu {
                if phase == Phase::Text {
                    end = keys[i..]
                        .iter()
                        .position(|&k| matches!(k, b'\r' | b'\n' | ESCAPE))
                        .map_or(keys.len(), |k| i + k + 1);
                }
                record(view, phase, open, &keys[i..end], &mut io.outcome);
                open = matches!(phase, Phase::Text | Phase::Position).then_some(phase);
            } else {
                open = None;
            }
            io.term.send(&keys[i..end]).map_err(Stop::Link)?;
            io.outcome.steps += (end - i) as u32;
            self.take(io);
            i = end;
        }
        self.after_run = is_run(keys);
        if needs_redraw(keys, after_run, io.term.view()) {
            self.send(io, &[REDRAW])?;
        }
        self.page(io)?;
        Ok(settled(io.term.view()))
    }

    /// A farlook: `;`, any notices in the way, the cursor keys, the reading, and an Escape if the game still
    /// waits for a position.
    fn farlook(&mut self, io: &mut Io, action: &Action) -> Result<Stop, Stop> {
        self.keep_before(io.term.view());
        self.send(io, b";")?;
        io.outcome.steps = 1;
        self.notices(io)?;
        let view = io.term.view();
        match view.phase() {
            Phase::Position => {}
            Phase::Play => return Ok(Stop::NoEffect),
            _ => return Ok(settled(view)),
        }
        self.send(io, &action.keys)?;
        if let (Some(target), Some(before)) = (action.target, self.before.as_ref()) {
            io.memory
                .remember_look(target, before, io.term.view(), LookSource::Farlook);
        }
        self.page(io)?;
        if io.term.view().phase() == Phase::Position {
            self.send(io, &[ESCAPE])?;
            return Ok(Stop::Prompt);
        }
        Ok(settled(io.term.view()))
    }

    /// A look underfoot: `:`, its pages, and the reading of the hero's square.
    fn look_here(&mut self, io: &mut Io, action: &Action) -> Result<Stop, Stop> {
        self.keep_before(io.term.view());
        self.send(io, &action.keys)?;
        io.outcome.steps = 1;
        self.page(io)?;
        if let Some(before) = self.before.as_ref()
            && let Some(hero) = before.hero
        {
            io.memory
                .remember_look(hero, before, io.term.view(), LookSource::Here);
        }
        Ok(settled(io.term.view()))
    }

    /// Keeps `view` as the screen an information command was asked from, reusing the buffer it last held.
    fn keep_before(&mut self, view: &View) {
        match &mut self.before {
            Some(before) => before.reparse(&view.grid, view.cursor),
            None => self.before = Some(view.clone()),
        }
    }

    /// Travel, exploration and searching: one step at a time, stopping at the first boundary.
    fn walk(&mut self, io: &mut Io, action: &Action) -> Result<Stop, Stop> {
        let exploring = action.kind == Kind::Explore;
        let moving = matches!(action.kind, Kind::Travel | Kind::Explore);
        let repeat = action.kind == Kind::Search;
        self.remember_start(io.memory);
        let start = io.term.view();
        self.status.clone_from(&start.status);
        let (cond, engulfed) = (start.cond, start.engulfed);
        monsters(io.memory, start, &mut self.previous);
        let mut idle = 0;
        for step in 0..action.steps {
            let view = io.term.view();
            let origin = view.hero;
            let (prior, prior_turn) = (view.fingerprint(), view.status.turn);
            let (prior_hp, prior_pw) = (view.status.hp, view.status.pw);
            let written = io.outcome.written();
            let buf;
            // The step's keys and, when moving, where it goes: from, to and the direction key.
            let (keys, target): (&[u8], _) = if moving {
                let from = origin.ok_or(Stop::RouteChanged)?;
                let level = io.memory.current().ok_or(Stop::RouteChanged)?;
                let to = match action.route.get(step as usize) {
                    Some(&to) => to,
                    None => match level.continuations(from, &self.covered).as_slice() {
                        [] => return Ok(Stop::CorridorEnds),
                        [to] => *to,
                        _ => return Ok(Stop::BranchDiscovered),
                    },
                };
                let key = from.direction_to(to).ok_or(Stop::RouteChanged)?;
                // The direction key goes after an `m` prefix, except where the level's memory says the game checks
                // the square first (water, lava, traps, gas) or where the pet stands, so the game's own check or
                // swap applies. A wet route's step into water keeps the prefix: it is how the game takes a
                // deliberate move into known water without its swim check.
                let wet = action.wet && level.water(to);
                let plain = !wet && (level.guarded(to) || view.pet(to) && is_monster(view.ch(to)));
                buf = [b'm', key];
                (&buf[usize::from(plain)..], Some((from, to, key, wet)))
            } else {
                (&action.keys, None)
            };
            let after_run = self.after_run;
            let mut news = self.send(io, keys)?;
            io.outcome.steps += 1;
            let wet = target.is_some_and(|(.., wet)| wet);
            io.outcome.wet += u32::from(wet);
            if needs_redraw(keys, after_run, io.term.view()) {
                news |= self.send(io, &[REDRAW])?;
            }
            news |= self.page(io)?;
            let view = io.term.view();
            if moving
                && !wet
                && view.hero == origin
                && view.phase() == Phase::Play
                && io.memory.about_pet(&view.msg)
            {
                // The pet in the way stops a step without moving; the step is tried once more.
                news |= self.send(io, keys)?;
                io.outcome.steps += 1;
                news |= self.page(io)?;
            }
            if repeat {
                untimed(io, prior, prior_turn, written);
            }
            let view = io.term.view();
            match settled(view) {
                Stop::Completed => {}
                stop => return Ok(stop),
            }
            if let Some((from, to, key, wet)) = target
                && view.hero != Some(to)
            {
                if wet && view.hero == Some(from) {
                    return Ok(Stop::WetRefused { from, to });
                }
                let refused = view.hero == Some(from)
                    && view.status.turn.is_some()
                    && view.status.turn == prior_turn
                    && !is_monster(view.ch(to));
                if refused {
                    if let Some(level) = io.memory.current_mut() {
                        level.refuse(from, key);
                    }
                    return Ok(Stop::Refused { from, to });
                }
                return Ok(if view.fingerprint() == prior {
                    Stop::NoEffect
                } else {
                    Stop::MovementInterrupted
                });
            }
            if exploring && let Some(hero) = view.hero {
                self.covered.insert(hero);
            }
            if exploring && self.discovered(io.memory) {
                return Ok(Stop::FeatureDiscovered);
            }
            // A fast hero can act twice within one turn, so one search can leave the screen unchanged.
            let unchanged = repeat && view.fingerprint() == prior;
            idle = if unchanged { idle + 1 } else { 0 };
            if idle >= IDLE_SEARCHES {
                return Ok(Stop::NoEffect);
            }
            // A rest reports what interrupted its last search too; a walk that arrived is done.
            let last = step + 1 == action.steps;
            if exploring || repeat || !last {
                monsters(io.memory, view, &mut self.now);
                let arrived = arrival(
                    (&self.previous, origin),
                    (&self.now, view.hero),
                    action.near,
                );
                std::mem::swap(&mut self.previous, &mut self.now);
                let status = dropped(prior_hp, view.status.hp)
                    || dropped(prior_pw, view.status.pw)
                    || status_moved(&self.status, &view.status);
                let (hp, hpmax) = (self.status.hp, self.status.hpmax);
                let healed = repeat
                    && !last
                    && hp.zip(hpmax).is_some_and(|(hp, hpmax)| {
                        hp < hpmax
                            && view.status.hp.unwrap_or(0) >= view.status.hpmax.unwrap_or(hpmax)
                    });
                let terrain = self.terrain_changed(io.memory, action.kind);
                let mut changed = Changed::default();
                for (flag, hit) in [
                    (Changed::STATUS, status),
                    (Changed::CONDITIONS, view.cond != cond),
                    (Changed::ENGULFED, view.engulfed != engulfed),
                    (Changed::TERRAIN, terrain),
                    (Changed::HP_FULL, healed),
                    (Changed::ENTITIES, arrived.is_some()),
                    (Changed::MESSAGE, news),
                ] {
                    if hit {
                        changed.0 |= flag.0;
                    }
                }
                if !changed.is_empty() {
                    io.outcome.changed = changed;
                    io.outcome.arrival = arrived;
                    return Ok(Stop::Changed);
                }
            }
        }
        Ok(if exploring {
            Stop::StepLimit
        } else {
            Stop::Completed
        })
    }

    /// Remembers the current level's terrain, colours, seen squares and open doors as the action's start.
    fn remember_start(&mut self, memory: &Memory) {
        self.terrain.clear();
        self.terrain.resize(MAP_SQUARES, ('\0', None));
        self.seen.clear();
        self.doors.clear();
        self.covered.clear();
        self.level = memory.current().map(|level| level.id);
        let Some(level) = memory.current() else {
            return;
        };
        for (p, ch) in level.known() {
            if let Some(i) = p.index() {
                self.terrain[i] = (ch, level.colour(p));
                self.covered.insert(p);
            }
        }
        self.seen.clone_from(&level.seen);
        self.doors.clone_from(&level.open_doors);
    }

    /// Whether remembered terrain differs from the start, or the hero is on another level. Travel watches only
    /// features, `+`, coloured `#` and open doors; exploring judges terrain by what it discovers instead.
    fn terrain_changed(&self, memory: &Memory, kind: Kind) -> bool {
        if kind == Kind::Explore {
            return false;
        }
        let Some(level) = memory.current() else {
            return self.level.is_some();
        };
        if self.level != Some(level.id) || level.open_doors != self.doors {
            return true;
        }
        (0..MAP_SQUARES).any(|i| {
            let p = Pos::from_index(i);
            let (old, was) = self.terrain[i];
            let (new, now) = (level.terrain(p).unwrap_or('\0'), level.colour(p));
            let structure = structural(old, was) != structural(new, now);
            match kind {
                Kind::Travel => feature(old) != feature(new) || structure,
                _ => old != new || structure,
            }
        })
    }

    /// Whether exploring has found a room or feature: a new or changed feature or closed door, a coloured
    /// structure, a newly opened door, or other terrain where no glyph had ever shown. Floor uncovered by a moving
    /// monster or redrawn in another colour was already known, and corridor and wall squares are followed rather
    /// than reported.
    fn discovered(&self, memory: &Memory) -> bool {
        let Some(level) = memory.current() else {
            return false;
        };
        let opened = level.open_doors.iter().any(|p| !self.doors.contains(p));
        if self.level != Some(level.id) || opened {
            return true;
        }
        level.known().any(|(p, ch)| {
            let Some(i) = p.index() else {
                return false;
            };
            let (old, colour) = self.terrain[i];
            if old == ch && colour == level.colour(p) {
                return false;
            }
            if feature(ch).is_some() {
                return old != ch;
            }
            if ch == '#' {
                return !level.corridor(p);
            }
            !matches!(ch, '|' | '-') && !self.seen.contains(p)
        })
    }

    fn finish(&mut self, io: &mut Io) -> Stop {
        for _ in 0..PAGES {
            let view = io.term.view();
            let phase = view.phase();
            let key = match phase {
                Phase::Play => return Stop::Completed,
                Phase::Choice => b'n',
                Phase::Ended if question(view) => b'n',
                Phase::Menu => ESCAPE,
                Phase::Ended | Phase::More if view.more => b' ',
                // Nothing is left to dismiss: this is the last screen, such as the score list.
                _ => return Stop::GameOver,
            };
            if key != b' ' {
                record(view, phase, None, &[key], &mut io.outcome);
            }
            let sent = io.term.send(&[key]);
            // The game exits after its last screen, which arrives with the reply the link then fails on.
            io.memory.ingest(io.term.view());
            match sent {
                Ok(()) => io.outcome.steps += 1,
                Err(_) if io.memory.end().is_some() => return Stop::GameOver,
                Err(error) => return Stop::Link(error),
            }
        }
        if io.term.view().ended {
            Stop::GameOver
        } else {
            unfinished("the end of the game")
        }
    }
}

/// Whether `view` asks a question: a choice, free text, a direction, an object, a spell or a position.
fn question(view: &View) -> bool {
    match view.phase() {
        Phase::Choice
        | Phase::Text
        | Phase::Direction
        | Phase::Item
        | Phase::Spell
        | Phase::Position => true,
        Phase::Ended => view.yn.is_some() && !view.more,
        Phase::More | Phase::Menu | Phase::Play | Phase::Unknown => false,
    }
}

/// Records `keys` as the answer to the prompt on `view`, extending the last entry when the previous keys went to
/// the same free-text or positioning prompt, and leaving out the earliest prompts beyond `REPORTED`.
fn record(view: &View, phase: Phase, open: Option<Phase>, keys: &[u8], outcome: &mut Outcome) {
    let text = strip_more(&view.msg).trim();
    let prompt = if text.is_empty() { phase.name() } else { text };
    let same = open == Some(phase)
        && outcome.answered.last().is_some_and(|last| match phase {
            Phase::Text => prompt.starts_with(last.prompt.as_str()),
            _ => prompt == last.prompt,
        });
    if !same {
        outcome.answer(Answered {
            prompt: prompt.to_owned(),
            key: String::new(),
            text_entry: phase == Phase::Text,
        });
    }
    if let Some(last) = outcome.answered.last_mut() {
        for &key in keys {
            escape_key(key, &mut last.key);
        }
    }
}

/// Records a search after which the turn counter reads as it did before (`prior_turn`) as taking no time, with what
/// the screen shows: the prompt that opened, the messages written since `written` lines had been, nothing changed
/// from the `prior` fingerprint, or other changes only. A counter not shown before or after records nothing.
fn untimed(io: &mut Io, prior: u64, prior_turn: Option<u32>, written: usize) {
    let view = io.term.view();
    if view.ended || prior_turn.is_none() || view.status.turn != prior_turn {
        return;
    }
    let outcome = &mut io.outcome;
    let lines = outcome.written().saturating_sub(written);
    let said = &outcome.messages[outcome.messages.len().saturating_sub(lines)..];
    let (why, text) = if view.phase() != Phase::Play {
        let text = strip_more(&view.msg).trim();
        let text = if text.is_empty() {
            view.phase().name()
        } else {
            text
        };
        (NoTimeWhy::Prompt, Some(text.to_owned()))
    } else if !said.is_empty() {
        let mut text = String::new();
        for line in said {
            if !text.is_empty() {
                text.push(' ');
            }
            text.push_str(line.trim());
        }
        (NoTimeWhy::Message, Some(text))
    } else if view.fingerprint() == prior {
        (NoTimeWhy::Screen, None)
    } else {
        (NoTimeWhy::Turn, None)
    };
    outcome.no_time(why, text);
}

/// How an action ends on `view` once its keys are done: the game ended, a prompt is open, or it completed.
fn settled(view: &View) -> Stop {
    if view.ended {
        Stop::GameOver
    } else if view.phase() != Phase::Play {
        Stop::Prompt
    } else {
        Stop::Completed
    }
}

/// The failure of `what` to finish within `PAGES` pages.
fn unfinished(what: &str) -> Stop {
    Stop::Failed(format!("{what} did not finish after {PAGES} pages"))
}

/// The monsters in `view` that can end a walk or rest: no objects, no `I` or warning digits, and not the
/// highlighted pet.
fn monsters(memory: &Memory, view: &View, out: &mut Vec<Entity>) {
    out.clear();
    memory.entities(view, out);
    out.retain(|e| e.kind == EntityKind::Monster && !e.pet);
}

/// The monster in `now`, nearest the hero, with no monster of its glyph and colour at or beside its square in
/// `previous`: one that came into view rather than moved a square. `previous` and `now` each come with the hero's
/// square at that step. With `near`, only a monster within `near` squares of the hero counts, and it counts as well
/// when it moved there from beyond `near` squares of the hero's square at the step before, so a monster that comes
/// closer, or that the hero comes closer to, stops the action once inside the radius.
fn arrival(
    (previous, origin): (&[Entity], Option<Pos>),
    (now, hero): (&[Entity], Option<Pos>),
    near: Option<u8>,
) -> Option<Arrival> {
    let within =
        |p: Pos, hero: Option<Pos>| near.is_none_or(|n| hero.is_some_and(|h| h.chebyshev(p) <= n));
    now.iter()
        .filter(|e| {
            within(e.pos, hero)
                && !previous.iter().any(|p| {
                    p.glyph == e.glyph
                        && p.shade == e.shade
                        && p.pos.chebyshev(e.pos) <= 1
                        && within(p.pos, origin)
                })
        })
        .min_by_key(|e| (hero.map_or(0, |h| h.chebyshev(e.pos)), e.pos))
        .map(|e| Arrival {
            glyph: e.glyph,
            pos: e.pos,
            dist: hero.map_or(0, |h| h.chebyshev(e.pos)),
        })
}

fn dropped(before: Option<i32>, after: Option<i32>) -> bool {
    matches!((before, after), (Some(b), Some(a)) if a < b)
}

/// Whether any status field other than turn, HP and Pw differs.
fn status_moved(a: &Status, b: &Status) -> bool {
    let rest = |s: &Status| Status {
        hp: None,
        pw: None,
        turn: None,
        ..s.clone()
    };
    rest(a) != rest(b)
}

/// A remembered feature or `+`, or `None` for other terrain.
fn feature(ch: char) -> Option<char> {
    (ch == '+' || crate::glyphs::feature_name(ch).is_some()).then_some(ch)
}

/// The colour of a `#` that is not a corridor (a tree, a sink, iron bars, a cloud), or `None`.
fn structural(ch: char, colour: Option<Base>) -> Option<Base> {
    colour.filter(|c| ch == '#' && matches!(c, Base::Green | Base::Cyan))
}

/// Whether `keys` start a counted command: an optional `n` or `m`, then a digit.
fn is_counted(keys: &[u8]) -> bool {
    let digits = match keys.first() {
        Some(b'n' | b'm') => &keys[1..],
        _ => keys,
    };
    digits.first().is_some_and(u8::is_ascii_digit)
}

/// Whether `keys` start a run: travel `_`, `G`, or a shift-run.
fn is_run(keys: &[u8]) -> bool {
    keys.first().is_some_and(|k| b"_GHJKLYUBN".contains(k))
}

fn action(kind: Kind, keys: &[u8], steps: u32, target: Option<Pos>) -> Action {
    Action {
        kind,
        keys: keys.to_vec(),
        steps,
        target,
        route: Vec::new(),
        wet: false,
        near: None,
    }
}

/// A travel or exploration along the known route to `target`, at most `max_steps` steps. Travel ends there;
/// exploring goes on along the corridor beyond it. A `wet` walk's route may step into remembered water.
pub fn walk(kind: Kind, paths: &Paths, target: Pos, max_steps: u32, wet: bool) -> Action {
    let mut walk = action(kind, &[], max_steps, Some(target));
    walk.wet = wet;
    paths.route(target, &mut walk.route);
    if kind == Kind::Travel {
        walk.steps = max_steps.min(walk.route.len() as u32);
    }
    walk
}

/// A farlook of `target` from the hero's square in `view`.
pub fn farlook(view: &View, target: Pos) -> Action {
    let mut farlook = action(Kind::Farlook, &[], 1, Some(target));
    cursor_keys(view.hero.unwrap_or(view.cursor), target, &mut farlook.keys);
    farlook
}

/// Up to `count` searches in place.
pub fn search(count: u32) -> Action {
    action(Kind::Search, b"s", count, None)
}

pub fn look_here() -> Action {
    action(Kind::LookHere, b":", 1, None)
}

pub fn keys(keys: &[u8]) -> Action {
    action(Kind::Keys, keys, 1, None)
}

/// The farlook cursor keys from `start` to `target`: `@` to the hero, moves, then `.` to pick.
pub fn cursor_keys(start: Pos, target: Pos, out: &mut Vec<u8>) {
    out.push(b'@');
    let across = if target.x > start.x { b'l' } else { b'h' };
    out.extend(repeat_n(across, usize::from(target.x.abs_diff(start.x))));
    let down = if target.y > start.y { b'j' } else { b'k' };
    out.extend(repeat_n(down, usize::from(target.y.abs_diff(start.y))));
    out.push(b'.');
}

/// Whether to redraw (`^R`) after `keys`: a counted command, or any command after a run (travel `_`, `G` or a
/// shift-run), can leave the status line's turn stale. Only when the map waits with an empty message line, so no
/// message is erased.
pub fn needs_redraw(keys: &[u8], after_run: bool, view: &View) -> bool {
    (is_counted(keys) || after_run)
        && (MAP_TOP..=MAP_BOTTOM).contains(&view.cursor.y)
        && view.ch(view.cursor) == '@'
        && view.rows[0].trim().is_empty()
        && !view.more
        && !view.menu
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::level::{Ground, Paths};
    use crate::testkit::{STATUS1, ScriptedTerm, grid, screen, view};
    use crate::types::{Cell, Colour, Grid};

    fn status2(hp: i32, turn: u32) -> String {
        format!("Dlvl:3 $:0 HP:{hp}(16) Pw:2(2) AC:6 Xp:2 T:{turn}")
    }

    fn put(rows: &mut [String], p: (u8, u8), ch: char) {
        let mut cells: Vec<char> = rows[p.0 as usize].chars().collect();
        cells.resize(80, ' ');
        cells[p.1 as usize] = ch;
        rows[p.0 as usize] = cells.into_iter().collect();
    }

    fn lines(message: &str, map: &[String], hp: i32, turn: u32) -> Vec<String> {
        let map: Vec<&str> = map.iter().map(String::as_str).collect();
        screen(message, &map, STATUS1, &status2(hp, turn))
    }

    fn parse(grid: &Grid, cursor: (u8, u8)) -> View {
        View::parse(grid, Pos::new(cursor.0, cursor.1))
    }

    /// A room on rows 1 to 3 with the hero at column `col` of row 2 and maybe a `d` at column `monster`.
    fn room_lines(col: u8, hp: i32, turn: u32, message: &str, monster: Option<u8>) -> Vec<String> {
        let map = [" ----------- ", " |.........| ", " ----------- "].map(str::to_owned);
        let mut rows = lines(message, &map, hp, turn);
        put(&mut rows, (2, col), '@');
        if let Some(m) = monster {
            put(&mut rows, (2, m), 'd');
        }
        rows
    }

    fn room(col: u8, hp: i32, turn: u32, message: &str, monster: Option<u8>) -> View {
        view(&room_lines(col, hp, turn, message, monster), 2, col)
    }

    fn at(col: u8, turn: u32) -> View {
        room(col, 12, turn, "", None)
    }

    fn corridor_lines(
        hero: (u8, u8),
        tiles: &[((u8, u8), char)],
        turn: u32,
        hp: i32,
    ) -> Vec<String> {
        let mut rows = lines("", &[], hp, turn);
        for &(p, ch) in tiles {
            put(&mut rows, p, ch);
        }
        put(&mut rows, hero, '@');
        rows
    }

    fn corridor(hero: (u8, u8), tiles: &[((u8, u8), char)], turn: u32) -> View {
        view(&corridor_lines(hero, tiles, turn, 12), hero.0, hero.1)
    }

    fn hall(hero: (u8, u8), turn: u32, message: &str, monster: Option<(u8, u8)>) -> View {
        let map = [" ------ ", " |....| ", " |....| ", " ------ "].map(str::to_owned);
        let mut rows = lines(message, &map, 12, turn);
        put(&mut rows, hero, '@');
        if let Some(m) = monster {
            put(&mut rows, m, 'd');
        }
        view(&rows, hero.0, hero.1)
    }

    /// A screen with only `message` on its top row and the cursor after it.
    fn asking(message: &str) -> View {
        let x = message.len().min(79) as u8;
        view(&lines(message, &[], 12, 400), 0, x)
    }

    fn start(views: Vec<View>) -> (ScriptedTerm, Memory) {
        let term = ScriptedTerm::new(views);
        let mut memory = Memory::default();
        memory.ingest(term.view());
        (term, memory)
    }

    fn sent(term: &ScriptedTerm) -> Vec<String> {
        term.sent
            .iter()
            .map(|k| String::from_utf8_lossy(k).into_owned())
            .collect()
    }

    fn walk(kind: Kind, steps: u32, target: (u8, u8), route: &[(u8, u8)]) -> Action {
        Action {
            kind,
            keys: Vec::new(),
            steps,
            target: Some(Pos::new(target.0, target.1)),
            route: route.iter().map(|&(y, x)| Pos::new(y, x)).collect(),
            wet: false,
            near: None,
        }
    }

    fn east(steps: u32) -> Action {
        let route: Vec<(u8, u8)> = (4..4 + steps as u8).map(|x| (2, x)).collect();
        walk(Kind::Travel, steps, (2, 3 + steps as u8), &route)
    }

    fn answer(prompt: &str, key: &str, text_entry: bool) -> Answered {
        Answered {
            prompt: prompt.to_owned(),
            key: key.to_owned(),
            text_entry,
        }
    }

    #[test]
    fn cursor_keys_move_from_the_hero_and_pick() {
        let mut out = Vec::new();
        cursor_keys(Pos::new(5, 10), Pos::new(3, 13), &mut out);
        assert_eq!(out, b"@lllkk.");
        out.clear();
        cursor_keys(Pos::new(5, 10), Pos::new(6, 8), &mut out);
        assert_eq!(out, b"@hhj.");
    }

    #[test]
    fn counted_commands_and_runs_are_recognised() {
        assert!(is_counted(b"20s"));
        assert!(is_counted(b"n5s"));
        assert!(!is_counted(b"s"));
        assert!(!is_counted(b"m"));
        assert!(is_run(b"_<."));
        assert!(is_run(b"L"));
        assert!(!is_run(b"l"));
        assert!(!is_run(b""));
    }

    #[test]
    fn a_route_is_walked_step_by_step() {
        let (mut term, mut memory) = start(vec![at(3, 400), at(4, 401), at(5, 402)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
        assert_eq!(sent(&term), ["ml", "ml"]);
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!((outcome.steps, outcome.turns), (2, Some(2)));
    }

    #[test]
    fn a_step_onto_a_known_trap_goes_without_the_prefix() {
        let tiles = [((2, 4), '#'), ((2, 5), '^'), ((2, 6), '#')];
        let mut confirm = corridor_lines((2, 4), &tiles, 401, 12);
        confirm[0] = "Really step onto that bear trap? [yn] (n)".to_owned();
        let confirm = view(&confirm, 0, 41);
        let (mut term, mut memory) = start(vec![
            corridor((2, 3), &tiles, 400),
            corridor((2, 4), &tiles, 401),
            confirm,
        ]);
        let action = walk(Kind::Travel, 3, (2, 6), &[(2, 4), (2, 5), (2, 6)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml", "l"]);
        assert_eq!((outcome.stop, outcome.steps), (Stop::Prompt, 2));
    }

    /// A corridor `@#}}#` from column 3 with blue water at columns 5 and 6, the hero at `col` and `message` on top.
    fn moat(col: u8, turn: u32, message: &str) -> View {
        let tiles = [((2, 4), '#'), ((2, 5), '}'), ((2, 6), '}'), ((2, 7), '#')];
        let mut rows = corridor_lines((2, 3), &tiles, turn, 12);
        put(&mut rows, (2, 3), '#');
        put(&mut rows, (2, col), '@');
        rows[0] = message.to_owned();
        let mut grid = grid(&rows);
        for x in [5, 6] {
            if x != col {
                grid.cells[2][x as usize].fg = Colour::Blue;
            }
        }
        parse(&grid, (2, col))
    }

    fn across_the_moat(wet: bool) -> Action {
        let route = [(2, 4), (2, 5), (2, 6), (2, 7)];
        Action {
            wet,
            ..walk(Kind::Travel, 4, (2, 7), &route)
        }
    }

    #[test]
    fn a_wet_walk_steps_into_water_with_the_prefix_and_counts_it() {
        let screens = || {
            vec![
                moat(3, 400, ""),
                moat(4, 401, ""),
                moat(5, 402, ""),
                moat(6, 403, ""),
                moat(7, 404, ""),
            ]
        };
        let (mut term, mut memory) = start(screens());
        assert!(memory.current().is_some_and(|l| l.water(Pos::new(2, 5))));
        let outcome = Executor::default().run(&mut term, &mut memory, &across_the_moat(true));
        assert_eq!(sent(&term), ["ml", "ml", "ml", "ml"]);
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!((outcome.steps, outcome.wet), (4, 2));
        // A dry walk sends a step into water bare, so the game's own check applies, and counts none wet.
        let (mut term, mut memory) = start(screens());
        let outcome = Executor::default().run(&mut term, &mut memory, &across_the_moat(false));
        assert_eq!(sent(&term), ["ml", "l", "l", "ml"]);
        assert_eq!(outcome.wet, 0);
    }

    #[test]
    fn a_refused_wet_step_stops_at_once_and_names_the_square() {
        let refusals = [
            moat(4, 401, "You avoid stepping into the water."),
            // The game took a turn and the hero stayed: still refused, and never tried again.
            moat(4, 402, "Pardon me, kitten."),
        ];
        for refused in refusals {
            let (mut term, mut memory) = start(vec![
                moat(3, 400, ""),
                moat(4, 401, ""),
                refused,
                moat(5, 403, ""),
            ]);
            let outcome = Executor::default().run(&mut term, &mut memory, &across_the_moat(true));
            assert_eq!(sent(&term), ["ml", "ml"]);
            let expected = Stop::WetRefused {
                from: Pos::new(2, 4),
                to: Pos::new(2, 5),
            };
            assert_eq!(outcome.stop, expected);
            assert_eq!((outcome.steps, outcome.wet), (2, 1));
            assert!(!outcome.messages.is_empty());
            // The refusal is reported, not remembered: the next wet route takes the same step.
            let level = memory.current().expect("a level");
            let mut paths = Paths::default();
            level.paths(Pos::new(2, 4), &SquareSet::new(), Ground::Wet, &mut paths);
            let mut route = Vec::new();
            assert!(paths.route(Pos::new(2, 7), &mut route));
            assert_eq!(route.first(), Some(&Pos::new(2, 5)));
            assert_eq!(paths.distance(Pos::new(2, 7)), Some(3));
        }
    }

    #[test]
    fn a_failed_step_is_refused_unchanged_or_interrupted() {
        for kind in [Kind::Travel, Kind::Explore] {
            let cases = [
                (
                    at(3, 400),
                    Stop::Refused {
                        from: Pos::new(2, 3),
                        to: Pos::new(2, 4),
                    },
                ),
                (
                    room(3, 12, 401, "You cannot pass.", None),
                    Stop::MovementInterrupted,
                ),
            ];
            for (after, stop) in cases {
                let (mut term, mut memory) = start(vec![at(3, 400), after, at(4, 402)]);
                let action = walk(kind, 8, (2, 4), &[(2, 4)]);
                let outcome = Executor::default().run(&mut term, &mut memory, &action);
                assert_eq!(sent(&term), ["ml"], "{kind:?}");
                assert_eq!((outcome.stop, outcome.steps), (stop, 1), "{kind:?}");
            }
        }
        // A monster standing in the way leaves the screen as it was: no effect, and no refusal.
        let blocked = room(3, 12, 400, "", Some(4));
        let (mut term, mut memory) = start(vec![blocked.clone(), blocked]);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(1));
        assert_eq!(outcome.stop, Stop::NoEffect);
    }

    #[test]
    fn a_refused_step_is_kept_out_of_later_routes() {
        let cases = [
            (hall((2, 2), 400, "You cannot get through.", None), true),
            (hall((2, 2), 401, "You cannot get through.", None), false),
            (hall((2, 2), 400, "", Some((3, 3))), false),
        ];
        for (after, refused) in cases {
            let (mut term, mut memory) = start(vec![hall((2, 2), 400, "", None), after]);
            let action = walk(Kind::Travel, 1, (3, 3), &[(3, 3)]);
            let outcome = Executor::default().run(&mut term, &mut memory, &action);
            assert_eq!(sent(&term), ["mn"]);
            let expected = Stop::Refused {
                from: Pos::new(2, 2),
                to: Pos::new(3, 3),
            };
            assert_eq!(outcome.stop == expected, refused, "{:?}", outcome.stop);
            let level = memory.current().expect("a level");
            let mut paths = Paths::default();
            level.paths(Pos::new(2, 2), &SquareSet::new(), Ground::Dry, &mut paths);
            let length = if refused { 2 } else { 1 };
            assert_eq!(paths.distance(Pos::new(3, 3)), Some(length));
            // Where no route avoids it, a route still takes the refused step.
            let mut occupied = SquareSet::new();
            occupied.insert(Pos::new(2, 3));
            occupied.insert(Pos::new(3, 2));
            level.paths(Pos::new(2, 2), &occupied, Ground::Dry, &mut paths);
            assert_eq!(paths.distance(Pos::new(3, 3)), Some(1));
        }
    }

    #[test]
    fn pet_messages_do_not_end_travel_and_a_pet_in_the_way_is_stepped_past() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            room(4, 12, 401, "You swap places with your little dog.", None),
            room(
                4,
                12,
                402,
                "You stop.  Your little dog is in the way!",
                None,
            ),
            at(5, 403),
            at(6, 404),
        ]);
        let dog = room(3, 12, 400, "", Some(4));
        let named = room(
            3,
            12,
            400,
            "d  a tame little dog (tame little dog)",
            Some(4),
        );
        memory.remember_look(Pos::new(2, 4), &dog, &named, LookSource::Farlook);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(3));
        assert_eq!(sent(&term), ["ml", "ml", "ml", "ml"]);
        assert_eq!((outcome.stop, outcome.steps), (Stop::Completed, 4));
    }

    #[test]
    fn changes_between_steps_end_travel() {
        let cases = [
            (room(4, 10, 401, "", None), Changed::STATUS),
            (room(4, 12, 401, "", Some(7)), Changed::ENTITIES),
            (
                room(4, 12, 401, "You hear a sound.", None),
                Changed::MESSAGE,
            ),
        ];
        for (after, field) in cases {
            let (mut term, mut memory) = start(vec![at(3, 400), after, at(5, 402)]);
            let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
            assert_eq!(sent(&term), ["ml"]);
            assert_eq!(outcome.stop, Stop::Changed);
            assert_eq!(outcome.changed, field);
        }
        let (mut term, mut memory) = start(vec![at(3, 400), room(4, 12, 401, "", Some(7))]);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
        let arrival = Arrival {
            glyph: 'd',
            pos: Pos::new(2, 7),
            dist: 3,
        };
        assert_eq!(outcome.arrival, Some(arrival));
    }

    fn dog(y: u8, x: u8) -> Entity {
        Entity {
            pos: Pos::new(y, x),
            glyph: 'd',
            shade: crate::glyphs::Shade::GRAY,
            kind: EntityKind::Monster,
            pet: false,
            statue: false,
        }
    }

    #[test]
    fn a_near_radius_lets_only_monsters_within_it_stop_an_action() {
        let hero = Some(Pos::new(10, 40));
        let none: [Entity; 0] = [];
        let far = [dog(10, 50)];
        // Without a radius a monster coming into view anywhere stops the action; within 3, only one that close.
        let anywhere = arrival((&none, hero), (&far, hero), None);
        assert_eq!(anywhere.map(|a| a.dist), Some(10));
        assert_eq!(arrival((&none, hero), (&far, hero), Some(3)), None);
        let close = [dog(12, 43)];
        let expected = Arrival {
            glyph: 'd',
            pos: Pos::new(12, 43),
            dist: 3,
        };
        assert_eq!(
            arrival((&none, hero), (&close, hero), Some(3)),
            Some(expected)
        );
        // A monster inside the radius that moves a square stays quiet; one that moves into it stops the action,
        // though without a radius the same move is no arrival.
        assert_eq!(
            arrival((&close, hero), (&[dog(11, 42)], hero), Some(3)),
            None
        );
        let entering = arrival((&[dog(10, 44)], hero), (&[dog(10, 43)], hero), Some(3));
        assert_eq!(entering.map(|a| a.pos), Some(Pos::new(10, 43)));
        assert_eq!(
            arrival((&[dog(10, 44)], hero), (&[dog(10, 43)], hero), None),
            None
        );
        // The radius is measured from the hero at each step: a monster standing still that the hero walks up to.
        let still = [dog(10, 45)];
        let walked = arrival((&still, hero), (&still, Some(Pos::new(10, 42))), Some(3));
        assert_eq!(walked.map(|a| a.dist), Some(3));
        assert_eq!(arrival((&still, hero), (&still, hero), Some(3)), None);
    }

    #[test]
    fn monsters_moving_about_in_view_do_not_end_travel() {
        let (mut term, mut memory) = start(vec![
            room(3, 12, 400, "", Some(6)),
            room(4, 12, 401, "", Some(7)),
            room(5, 12, 402, "", Some(8)),
            room(6, 12, 403, "", Some(8)),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(3));
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!(sent(&term), ["ml", "ml", "ml"]);
    }

    #[test]
    fn a_monster_where_none_stood_ends_travel_even_beside_one_already_seen() {
        let (mut term, mut memory) = start(vec![
            room(3, 12, 400, "", Some(9)),
            room(4, 12, 401, "", Some(5)),
            at(5, 402),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
        assert_eq!(
            (outcome.stop, outcome.changed),
            (Stop::Changed, Changed::ENTITIES)
        );
        assert_eq!(outcome.arrival.map(|a| a.pos), Some(Pos::new(2, 5)));
        assert_eq!(sent(&term), ["ml"]);
    }

    #[test]
    fn sensed_monsters_never_end_a_walk_or_rest() {
        let mut sensed = room_lines(4, 12, 401, "", None);
        put(&mut sensed, (2, 7), 'I');
        let mut warned = room_lines(5, 12, 402, "", None);
        put(&mut warned, (2, 7), 'I');
        put(&mut warned, (2, 9), '3');
        let views = vec![
            at(3, 400),
            view(&sensed, 2, 4),
            view(&warned, 2, 5),
            at(6, 403),
        ];
        let (mut term, mut memory) = start(views);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(3));
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!(outcome.arrival, None);
    }

    #[test]
    fn a_highlighted_pet_coming_adjacent_is_no_arrival_and_a_step_into_it_swaps() {
        let views = [(3, 9, 400), (4, 5, 401), (5, 4, 402)].map(|(hero, pet, turn)| {
            let mut g = grid(&room_lines(hero, 12, turn, "", Some(pet)));
            g.cells[2][pet as usize].rev = true;
            parse(&g, (2, hero))
        });
        let (mut term, mut memory) = start(views.to_vec());
        let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!(sent(&term), ["ml", "l"]);
    }

    #[test]
    fn travel_continues_through_map_growth_and_stops_at_a_new_feature() {
        for (glyph, stop, keys) in [
            ('.', Stop::Completed, &["ml", "ml"][..]),
            ('>', Stop::Changed, &["ml"][..]),
            ('+', Stop::Changed, &["ml"][..]),
        ] {
            let mut revealed = room_lines(4, 12, 401, "", None);
            revealed[4] = format!("   -{glyph}-");
            let (mut term, mut memory) = start(vec![at(3, 400), view(&revealed, 2, 4), at(5, 402)]);
            let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
            assert_eq!(outcome.stop, stop, "{glyph}");
            assert_eq!(sent(&term), keys, "{glyph}");
            if stop == Stop::Changed {
                assert_eq!(outcome.changed, Changed::TERRAIN);
            }
        }
    }

    #[test]
    fn a_search_goes_on_through_regeneration_and_a_free_move() {
        let views = [(12, 400), (12, 400), (13, 401), (14, 402), (14, 403)]
            .map(|(hp, turn)| room(3, hp, turn, "", None))
            .to_vec();
        let (mut term, mut memory) = start(views);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(4));
        assert_eq!((outcome.stop, outcome.steps), (Stop::Completed, 4));
        assert_eq!(sent(&term), ["s", "s", "s", "s"]);
    }

    #[test]
    fn a_search_stops_when_hp_drops_or_refills() {
        for (hp, field) in [(11, Changed::STATUS), (16, Changed::HP_FULL)] {
            let views = vec![
                room(3, 12, 400, "", None),
                room(3, hp, 401, "", None),
                room(3, 16, 402, "", None),
            ];
            let (mut term, mut memory) = start(views);
            let outcome = Executor::default().run(&mut term, &mut memory, &search(8));
            assert_eq!(sent(&term), ["s"]);
            assert_eq!((outcome.stop, outcome.changed), (Stop::Changed, field));
        }
    }

    #[test]
    fn searches_that_leave_the_turn_counter_are_kept_with_what_the_screen_showed() {
        let said = "You already found a monster.  Use 'm' prefix to force another search.";
        let views = vec![
            room(3, 12, 400, "", Some(9)),
            room(3, 12, 401, "", Some(8)),
            // The jackal moved; the turn counter did not.
            room(3, 12, 401, "", Some(9)),
            room(3, 12, 402, "", Some(9)),
            // Nothing changed.
            room(3, 12, 402, "", Some(9)),
            room(3, 12, 402, said, Some(9)),
        ];
        let mut memory = Memory::default();
        // The floor the jackal paces over was seen before it stood there.
        memory.ingest(&at(3, 399));
        memory.ingest(&views[0]);
        let mut term = ScriptedTerm::new(views);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(8));
        assert_eq!(
            (outcome.stop, outcome.steps, outcome.turns),
            (Stop::Changed, 5, Some(2))
        );
        assert_eq!(outcome.no_time, 3);
        let answers: Vec<_> = outcome
            .no_time_why
            .iter()
            .map(|a| (a.searches, a.why, a.text.as_deref()))
            .collect();
        assert_eq!(
            answers,
            [
                (1, NoTimeWhy::Turn, None),
                (1, NoTimeWhy::Screen, None),
                (1, NoTimeWhy::Message, Some(said))
            ]
        );
    }

    #[test]
    fn a_prompt_a_search_opens_without_a_turn_is_its_answer() {
        let prompt = "Really attack the gnome? [yn] (n)";
        let (mut term, mut memory) = start(vec![at(3, 400), asking(prompt)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(8));
        assert_eq!(outcome.stop, Stop::Prompt);
        assert_eq!(
            outcome.no_time_why,
            [NoTime {
                searches: 1,
                why: NoTimeWhy::Prompt,
                text: Some(prompt.to_owned())
            }]
        );
    }

    #[test]
    fn searches_with_answers_beyond_the_bound_are_only_counted() {
        let mut outcome = Outcome::default();
        for n in 0..NO_TIME_ANSWERS + 2 {
            outcome.no_time(NoTimeWhy::Message, Some(format!("message {n}")));
        }
        outcome.no_time(NoTimeWhy::Message, Some("message 0".to_owned()));
        assert_eq!(outcome.no_time, NO_TIME_ANSWERS as u32 + 3);
        assert_eq!(outcome.no_time_why.len(), NO_TIME_ANSWERS);
        assert_eq!(outcome.no_time_why[0].searches, 2);
    }

    #[test]
    fn a_walk_keeps_no_searches() {
        let (mut term, mut memory) = start(vec![at(3, 400), at(3, 400)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &east(2));
        assert_eq!((outcome.no_time, outcome.no_time_why.len()), (0, 0));
    }

    #[test]
    fn a_search_that_changes_nothing_three_times_stops() {
        let (mut term, mut memory) = start(vec![at(3, 400)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(8));
        assert_eq!(
            (outcome.stop, outcome.steps),
            (Stop::NoEffect, IDLE_SEARCHES)
        );
    }

    #[test]
    fn exploring_follows_a_corridor_round_a_corner_within_its_bound() {
        let mut tiles = vec![((2, 3), '#'), ((2, 4), '#')];
        let mut views = vec![corridor((2, 3), &tiles, 400)];
        for (turn, hero, new) in [
            (401, (2, 4), (3, 4)),
            (402, (3, 4), (4, 4)),
            (403, (4, 4), (5, 4)),
        ] {
            tiles.push((new, '#'));
            views.push(corridor(hero, &tiles, turn));
        }
        let (mut term, mut memory) = start(views);
        let action = walk(Kind::Explore, 3, (2, 4), &[(2, 4)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml", "mj", "mj"]);
        assert_eq!((outcome.stop, outcome.turns), (Stop::StepLimit, Some(3)));
        assert_eq!(term.view().hero, Some(Pos::new(4, 4)));
    }

    #[test]
    fn exploring_follows_a_diagonal_step_and_stops_where_the_corridor_ends() {
        let tiles = [((2, 3), '#'), ((2, 4), '#')];
        let turned = [((2, 3), '#'), ((2, 4), '#'), ((3, 5), '#')];
        let (mut term, mut memory) = start(vec![
            corridor((2, 3), &tiles, 400),
            corridor((2, 4), &turned, 401),
            corridor((3, 5), &turned, 402),
        ]);
        let action = walk(Kind::Explore, 8, (2, 4), &[(2, 4)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml", "mn"]);
        assert_eq!(outcome.stop, Stop::CorridorEnds);
    }

    #[test]
    fn exploring_stops_at_a_branch_without_choosing() {
        let tiles = [((2, 3), '#'), ((2, 4), '#')];
        let junction = [((2, 3), '#'), ((2, 4), '#'), ((1, 4), '#'), ((3, 4), '#')];
        let (mut term, mut memory) = start(vec![
            corridor((2, 3), &tiles, 400),
            corridor((2, 4), &junction, 401),
        ]);
        let action = walk(Kind::Explore, 8, (2, 4), &[(2, 4)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml"]);
        assert_eq!(outcome.stop, Stop::BranchDiscovered);
    }

    #[test]
    fn exploring_stops_at_a_feature_an_encounter_or_damage() {
        let cases = [
            ('+', 12, Stop::FeatureDiscovered),
            ('.', 12, Stop::FeatureDiscovered),
            ('d', 12, Stop::Changed),
            ('#', 10, Stop::Changed),
        ];
        for (glyph, hp, stop) in cases {
            let tiles = [((2, 3), '#'), ((2, 4), '#')];
            let mut revealed = vec![((2, 3), '#'), ((2, 4), '#'), ((2, 5), glyph)];
            if glyph == '+' {
                revealed.extend([((1, 5), '|'), ((3, 5), '|')]);
            }
            let after = view(&corridor_lines((2, 4), &revealed, 401, hp), 2, 4);
            let (mut term, mut memory) = start(vec![corridor((2, 3), &tiles, 400), after]);
            let action = walk(Kind::Explore, 8, (2, 4), &[(2, 4)]);
            let outcome = Executor::default().run(&mut term, &mut memory, &action);
            assert_eq!(sent(&term), ["ml"], "{glyph}");
            assert_eq!(outcome.stop, stop, "{glyph}");
        }
    }

    #[test]
    fn a_new_coloured_structure_ends_exploring_and_travel() {
        for colour in [Colour::Green, Colour::Cyan] {
            for seen_before in [false, true] {
                for kind in [Kind::Explore, Kind::Travel] {
                    let tiles = [((2, 3), '#'), ((2, 4), '#')];
                    let before = if seen_before {
                        corridor((2, 3), &[((2, 3), '#'), ((2, 4), '#'), ((2, 5), '#')], 400)
                    } else {
                        corridor((2, 3), &tiles, 400)
                    };
                    let revealed = [((2, 3), '#'), ((2, 4), '#'), ((2, 5), '#')];
                    let mut g = grid(&corridor_lines((2, 4), &revealed, 401, 12));
                    g.cells[2][5] = Cell {
                        fg: colour,
                        ..g.cells[2][5]
                    };
                    let after = parse(&g, (2, 4));
                    let views = vec![before, after, corridor((2, 5), &revealed, 402)];
                    let (mut term, mut memory) = start(views);
                    let action = walk(kind, 8, (2, 5), &[(2, 4), (2, 5)]);
                    let outcome = Executor::default().run(&mut term, &mut memory, &action);
                    let label = format!("{colour:?} {seen_before} {kind:?}");
                    assert_eq!(sent(&term), ["ml"], "{label}");
                    if kind == Kind::Explore {
                        assert_eq!(outcome.stop, Stop::FeatureDiscovered, "{label}");
                    } else {
                        assert_eq!(
                            (outcome.stop, outcome.changed),
                            (Stop::Changed, Changed::TERRAIN),
                            "{label}"
                        );
                    }
                }
            }
        }
    }

    #[test]
    fn exploring_ignores_known_floor_uncovered_or_redrawn() {
        let mut moved = grid(&room_lines(4, 12, 401, "", Some(7)));
        moved.cells[2][2].fg = Colour::Blue;
        let views = vec![
            room(3, 12, 400, "", Some(6)),
            parse(&moved, (2, 4)),
            room(5, 12, 402, "", Some(8)),
        ];
        let (mut term, mut memory) = start(views);
        let action = walk(Kind::Explore, 8, (2, 5), &[(2, 4), (2, 5)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml", "ml"]);
        assert_eq!(outcome.stop, Stop::CorridorEnds);
    }

    #[test]
    fn a_prompt_or_the_end_of_the_game_ends_a_rest() {
        let cases = [
            (
                vec![asking("Really attack the gnome? [yn] (n)")],
                Stop::Prompt,
                &["s"][..],
            ),
            (
                vec![asking("Goodbye Hero the Wizard...--More--"), score_list()],
                Stop::GameOver,
                &["s", " "][..],
            ),
        ];
        for (frames, stop, keys) in cases {
            let mut views = vec![at(3, 400)];
            views.extend(frames);
            let (mut term, mut memory) = start(views);
            let outcome = Executor::default().run(&mut term, &mut memory, &search(8));
            assert_eq!(outcome.stop, stop);
            assert_eq!(sent(&term), keys);
        }
    }

    #[test]
    fn nothing_is_sent_to_a_game_that_has_ended_unless_the_caller_sends_it() {
        let quit = asking("Do you want your possessions identified? [ynq] (n)");
        let (mut term, mut memory) = start(vec![quit.clone(), at(3, 400)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(3));
        assert_eq!(outcome.stop, Stop::GameOver);
        assert!(term.sent.is_empty());
        let outcome = Executor::default().run(&mut term, &mut memory, &keys(b"y"));
        assert_eq!(sent(&term), ["y"]);
        let prompt = "Do you want your possessions identified? [ynq] (n)";
        assert_eq!(outcome.answered, [answer(prompt, "y", false)]);
    }

    #[test]
    fn life_saving_carries_on_past_the_death_message() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            asking("In what direction?"),
            asking("You die...--More--"),
            asking("But wait...  Your medallion begins to glow!--More--"),
            room(3, 16, 401, "You feel much better!", None),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &keys(b"Fl"));
        assert_eq!(sent(&term), ["F", "l", " ", " "]);
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!(outcome.answered, [answer("In what direction?", "l", false)]);
        assert_eq!(
            outcome.messages,
            [
                "You die...",
                "But wait...  Your medallion begins to glow!",
                "You feel much better!"
            ]
        );
        assert_eq!((outcome.steps, outcome.turns), (2, Some(1)));
    }

    #[test]
    fn keys_record_every_prompt_they_pass_and_flag_text_typed_into_one() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            asking("What do you want to write with? [- a or ?*]"),
            asking("You write in the dust with your fingertip.--More--"),
            asking("What do you want to write in the dust here?"),
            room(3, 12, 401, "", None),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &keys(b"E-nnElbereth\r"));
        assert_eq!(sent(&term), ["E", "-", "n", "nElbereth\r"]);
        assert_eq!(
            outcome.answered,
            [
                answer("What do you want to write with? [- a or ?*]", "-", false),
                answer("You write in the dust with your fingertip.", "n", false),
                answer(
                    "What do you want to write in the dust here?",
                    "nElbereth\\r",
                    true
                ),
            ]
        );
        assert_eq!((outcome.stop, outcome.steps), (Stop::Completed, 13));
    }

    #[test]
    fn keys_moving_a_cursor_answer_one_positioning_prompt() {
        let target = room(3, 12, 400, "Pick a monster, object or location.", Some(4));
        let (mut term, mut memory) = start(vec![
            room(3, 12, 400, "", Some(4)),
            target.clone(),
            target.clone(),
            target,
            room(3, 12, 400, "d  a jackal (jackal)", Some(4)),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &keys(b";@l."));
        assert_eq!(sent(&term), [";", "@", "l", "."]);
        assert_eq!(
            outcome.answered,
            [answer("Pick a monster, object or location.", "@l.", false)]
        );
        assert_eq!(outcome.messages, ["d  a jackal (jackal)"]);
    }

    #[test]
    fn keys_ending_inside_a_prompt_leave_it_open() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            asking("What do you want to write in the dust here?"),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &keys(b"E"));
        assert_eq!(outcome.stop, Stop::Prompt);
        assert!(outcome.answered.is_empty());
        assert!(outcome.messages.is_empty());
    }

    #[test]
    fn a_counted_command_is_followed_by_a_redraw() {
        let views = vec![at(3, 400), at(3, 400), at(3, 400), at(3, 420), at(3, 420)];
        let (mut term, mut memory) = start(views);
        let outcome = Executor::default().run(&mut term, &mut memory, &keys(b"20s"));
        assert_eq!(sent(&term), ["2", "0", "s", "\u{12}"]);
        assert_eq!(outcome.turns, Some(20));
    }

    #[test]
    fn a_redraw_waits_for_an_empty_message_line() {
        let quiet = at(3, 400);
        let heard = room(3, 12, 400, "You hear a door open.", None);
        assert!(needs_redraw(b"20s", false, &quiet));
        assert!(needs_redraw(b"s", true, &quiet));
        assert!(!needs_redraw(b"s", false, &quiet));
        assert!(!needs_redraw(b"20s", false, &heard));
        assert!(!needs_redraw(
            b"20s",
            false,
            &asking("Really attack? [yn] (n)")
        ));
    }

    #[test]
    fn a_farlook_reads_the_square_after_its_checked_selection() {
        let before = room(3, 12, 400, "", Some(4));
        let (mut term, mut memory) = start(vec![
            before.clone(),
            room(3, 12, 400, "Pick a monster, object or location.", Some(4)),
            room(3, 12, 400, "d  a jackal (jackal)", Some(4)),
        ]);
        let outcome =
            Executor::default().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
        assert_eq!(sent(&term), [";", "@l."]);
        assert_eq!(
            (outcome.stop, outcome.steps, outcome.turns),
            (Stop::Completed, 1, Some(0))
        );
        let level = memory.current().expect("a level");
        let reading = level.inspections.get(&Pos::new(2, 4)).expect("a reading");
        assert_eq!(reading.description, "jackal");
    }

    #[test]
    fn a_text_window_before_the_farlook_position_is_dismissed() {
        let before = room(3, 12, 400, "", Some(4));
        let tip = lines(
            "Tip: Farlooking or selecting a map location",
            &[
                "".to_owned(),
                "  Press ESC to leave.".to_owned(),
                "  (end)".to_owned(),
            ],
            12,
            400,
        );
        let (mut term, mut memory) = start(vec![
            before.clone(),
            view(&tip, 3, 7),
            room(
                3,
                12,
                400,
                "Move cursor to a monster, object or location:",
                Some(4),
            ),
            room(
                3,
                12,
                400,
                "d        a dog or other canine (jackal)",
                Some(4),
            ),
        ]);
        let outcome =
            Executor::default().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
        assert_eq!(sent(&term), [";", "\u{1b}", "@l."]);
        assert_eq!(outcome.stop, Stop::Completed);
    }

    #[test]
    fn a_farlook_the_game_does_not_take_gets_no_cursor_keys() {
        let before = room(3, 12, 400, "", Some(4));
        let cases = [
            (
                room(3, 12, 400, "You cannot see there.", Some(4)),
                Stop::NoEffect,
            ),
            (asking("Really kick your pony? [yn] (n)"), Stop::Prompt),
        ];
        for (rejected, stop) in cases {
            let (mut term, mut memory) = start(vec![before.clone(), rejected, at(3, 401)]);
            let outcome =
                Executor::default().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
            assert_eq!(sent(&term), [";"]);
            assert_eq!((outcome.stop, outcome.steps), (stop, 1));
        }
    }

    #[test]
    fn a_farlook_left_at_its_position_prompt_is_cancelled() {
        let before = room(3, 12, 400, "", Some(4));
        let picking = room(3, 12, 400, "Pick a monster, object or location.", Some(4));
        let (mut term, mut memory) = start(vec![
            before.clone(),
            picking.clone(),
            picking,
            before.clone(),
        ]);
        let outcome =
            Executor::default().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
        assert_eq!(sent(&term), [";", "@l.", "\u{1b}"]);
        assert_eq!(outcome.stop, Stop::Prompt);
    }

    #[test]
    fn a_look_here_reads_the_heros_square() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            room(3, 12, 400, "You see here a dagger.", None),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &look_here());
        assert_eq!(sent(&term), [":"]);
        assert_eq!((outcome.stop, outcome.turns), (Stop::Completed, Some(0)));
        let level = memory.current().expect("a level");
        let reading = level.inspections.get(&Pos::new(2, 3)).expect("a reading");
        assert_eq!(reading.description, "You see here a dagger.");
    }

    /// A tombstone as the tty draws it.
    fn tombstone() -> View {
        let art = [
            "                       ----------",
            "                      /          \\",
            "                     /    REST    \\",
            "                    /      IN      \\",
            "                   /     PEACE      \\",
            "                  /                  \\",
            "                  |       Hero       |",
            "                  |       0 Au       |",
            "                  |   killed by a    |",
            "                  |      jackal      |",
            "                  |                  |",
            "                  |                  |",
            "                  |       2026       |",
            "                 *|     *  *  *      | *",
            "        _________)/\\\\_//(\\/(/\\)/\\//\\/|_)_______",
            "",
            "--More--",
        ];
        let mut rows: Vec<String> = vec![String::new(); 4];
        rows.extend(art.iter().map(|r| (*r).to_owned()));
        rows.resize(24, String::new());
        view(&rows, 20, 8)
    }

    /// The page after the end-of-game window: only `--More--`.
    fn blank_page() -> View {
        let mut rows = vec![String::new(); 24];
        rows[23] = "--More--".to_owned();
        view(&rows, 23, 8)
    }

    /// The score list the game prints as it exits.
    fn score_list() -> View {
        let mut rows = vec![String::new(); 24];
        rows[2] = " No  Points     Name                                                   Hp [max]"
            .to_owned();
        rows[3] = "             0  Hero-Val-Hum-Fem-Law died in The Dungeons of Doom on level 3."
            .to_owned();
        rows[4] = "                Killed by a jackal.                                    -  [16]"
            .to_owned();
        view(&rows, 6, 0)
    }

    #[test]
    fn the_end_of_the_game_is_paged_to_the_score_list_declining_disclosure() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            asking("You die...--More--"),
            asking("Do you want your possessions identified? [ynq] (n)"),
            asking("Do you want to see your attributes? [ynq] (n)"),
            tombstone(),
            blank_page(),
            score_list(),
        ]);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(5));
        assert_eq!(sent(&term), ["s", " ", "n", "n", " ", " "]);
        assert_eq!(outcome.stop, Stop::GameOver);
        assert_eq!(
            outcome.answered,
            [
                answer(
                    "Do you want your possessions identified? [ynq] (n)",
                    "n",
                    false
                ),
                answer("Do you want to see your attributes? [ynq] (n)", "n", false),
            ]
        );
        assert_eq!(
            outcome.messages.first().map(String::as_str),
            Some("You die...")
        );
        assert_eq!(
            memory.end().and_then(|end| end.death.as_deref()),
            Some("killed by a jackal")
        );
        // On the last screen, paging again sends nothing.
        let again = Executor::default().finish_game(&mut term, &mut memory);
        assert_eq!((again.stop, term.sent.len()), (Stop::GameOver, 6));
    }

    #[test]
    fn finishing_a_game_still_going_sends_nothing() {
        let (mut term, mut memory) = start(vec![at(3, 400)]);
        let outcome = Executor::default().finish_game(&mut term, &mut memory);
        assert_eq!(outcome.stop, Stop::Completed);
        assert!(term.sent.is_empty());
    }

    #[test]
    fn a_link_failure_ends_the_action_with_its_reason() {
        let error = LinkError::Held("waiting to start".to_owned());
        let (mut term, mut memory) = start(vec![at(3, 400), at(4, 401)]);
        term.failures.push(Some(error.clone()));
        let outcome = Executor::default().run(&mut term, &mut memory, &east(1));
        assert_eq!((outcome.stop, outcome.steps), (Stop::Link(error), 0));
        assert!(term.sent.is_empty());
    }

    #[test]
    fn endless_pages_fail_after_the_bound() {
        let (mut term, mut memory) = start(vec![at(3, 400), asking("You hear a noise.--More--")]);
        let outcome = Executor::default().run(&mut term, &mut memory, &search(1));
        assert!(matches!(outcome.stop, Stop::Failed(_)));
        assert_eq!(term.sent.len(), 1 + PAGES as usize);
    }

    #[test]
    fn paged_text_windows_are_kept_as_indented_message_lines() {
        let mut window = lines("", &[], 12, 400);
        window[0] = "There is a staircase up here.".to_owned();
        window[1] = String::new();
        window[2] = "Things that are here:".to_owned();
        window[3] = "7 apples".to_owned();
        window[4] = "--More--".to_owned();
        let (mut term, mut memory) = start(vec![at(3, 400), view(&window, 4, 8), at(3, 400)]);
        let outcome = Executor::default().run(&mut term, &mut memory, &look_here());
        assert_eq!(sent(&term), [":", " "]);
        assert_eq!(
            outcome.messages,
            [
                "There is a staircase up here.",
                "  Things that are here:",
                "  7 apples"
            ]
        );
    }
}
