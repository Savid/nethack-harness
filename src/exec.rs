//! Bounded execution: sending keys, walking a route, exploring a corridor, searching in place and the harness's
//! own information commands, each stopping at observable boundaries; and paging through the end of the game.

use crate::glyphs::{Base, is_monster};
use crate::level::{Level, Paths};
use crate::link::{LinkError, Terminal};
use crate::observe::{Entity, EntityKind, LookSource, Memory};
use crate::screen::{EndKind, MenuEntry, Phase, Status, View};
use crate::types::{Answered, MAP_BOTTOM, MAP_SQUARES, MAP_TOP, Pos, SquareSet, escape_key};

/// Most `--More--` pages or text windows dismissed in one go.
pub const PAGES: u32 = 32;
/// Most message lines and most answered prompts one action reports; the earliest beyond this are counted instead,
/// so a reply stays small however many pages the keys pass.
pub const REPORTED: usize = 32;
/// Repeated searches that leave the screen unchanged before a search stops.
pub const IDLE_SEARCHES: u32 = 3;

const ESCAPE: u8 = 0x1b;
const REDRAW: u8 = 0x12;

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
    /// A monster came into view where no monster of its glyph and colour stood within one square at the step
    /// before.
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
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Stop {
    /// It did everything asked.
    #[default]
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
    /// The terminal failed.
    Link(LinkError),
    /// The game kept showing pages past the bound.
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
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Outcome {
    /// Why it ended.
    pub stop: Stop,
    /// For `Stop::Changed`, what changed.
    pub changed: Changed,
    /// For a `Changed::ENTITIES` stop, the monster that arrived.
    pub arrival: Option<Arrival>,
    /// Steps taken: route steps, searches, or caller keys sent.
    pub steps: u32,
    /// Game turns that passed, when the status line showed them before and after.
    pub turns: Option<i64>,
    /// Message lines the game wrote meanwhile, text-window lines after the first indented by two spaces. Screens
    /// that ask a question are left out: the question is in `answered` or still open.
    pub messages: Vec<String>,
    /// Prompts the keys passed and the key that answered each.
    pub answered: Vec<Answered>,
    /// Earlier message lines left out of `messages`.
    pub messages_left_out: u32,
    /// Earlier prompts left out of `answered`.
    pub answered_left_out: u32,
}

impl Outcome {
    /// Adds a message line, leaving out the earliest beyond `REPORTED`.
    fn message(&mut self, text: String) {
        if self.messages.len() >= REPORTED {
            self.messages.remove(0);
            self.messages_left_out += 1;
        }
        self.messages.push(text);
    }
}

/// Which remembered terrain ends an action when it changes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Watch {
    /// Any square.
    All,
    /// Features, `+`, coloured `#` and open doors.
    Features,
    /// None: exploring judges terrain by what it discovers instead.
    Nothing,
}

/// Runs actions, keeping scratch buffers across calls.
pub struct Executor {
    /// The last batch sent started a run (travel `_`, `G` or a shift-run).
    after_run: bool,
    /// Monsters at the previous step of a walk or rest.
    previous: Vec<Entity>,
    /// Monsters now.
    now: Vec<Entity>,
    /// Text-window lines being read.
    window: Vec<String>,
    /// Menu entries being read.
    entries: Vec<MenuEntry>,
    /// The keys of one route step.
    step: Vec<u8>,
    /// The screen an information command was asked from.
    before: Option<View>,
    /// Remembered terrain when the action began; `'\0'` where none.
    terrain: Box<[char; MAP_SQUARES]>,
    /// Remembered terrain colours when the action began.
    colours: Box<[Option<Base>; MAP_SQUARES]>,
    /// Squares that had shown a glyph when the action began.
    seen: SquareSet,
    /// Open doors when the action began.
    doors: SquareSet,
    /// Squares an exploration has known or stood on.
    covered: SquareSet,
    /// The level the action began on.
    level: Option<u16>,
    /// The status line when the action began.
    status: Status,
    /// The game had already ended when the action began.
    ended_before: bool,
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
            entries: Vec::new(),
            step: Vec::with_capacity(2),
            before: None,
            terrain: Box::new(['\0'; MAP_SQUARES]),
            colours: Box::new([None; MAP_SQUARES]),
            seen: SquareSet::new(),
            doors: SquareSet::new(),
            covered: SquareSet::new(),
            level: None,
            status: Status::default(),
            ended_before: false,
        }
    }

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
        let mut outcome = Outcome::default();
        let aside = matches!(action.kind, Kind::Farlook | Kind::LookHere);
        if aside {
            memory.set_aside(true);
        }
        self.ended_before = false;
        let mut turn = None;
        outcome.stop = match self.execute(term, memory, action, &mut outcome, &mut turn) {
            Ok(stop) | Err(stop) => stop,
        };
        if aside {
            memory.set_aside(false);
        }
        if let (Some(before), Some(after)) = (turn, term.view().status.turn) {
            outcome.turns = Some(i64::from(after) - i64::from(before));
        }
        if outcome.stop == Stop::GameOver && !self.ended_before {
            let end = self.finish_game(term, memory);
            for text in end.messages {
                outcome.message(text);
            }
            for answered in end.answered {
                outcome.answered.push(answered);
                if outcome.answered.len() > REPORTED {
                    outcome.answered.remove(0);
                    outcome.answered_left_out += 1;
                }
            }
            outcome.stop = end.stop;
        }
        outcome
    }

    /// Pages through the end of the game, declining the disclosure questions and dismissing `--More--`, until the
    /// tombstone or the farewell is captured into `memory`. Each question answered is recorded. A game that goes
    /// on (life saving) completes.
    pub fn finish_game(&mut self, term: &mut dyn Terminal, memory: &mut Memory) -> Outcome {
        let mut outcome = Outcome::default();
        outcome.stop = match self.finish(term, memory, &mut outcome) {
            Ok(stop) | Err(stop) => stop,
        };
        outcome
    }

    fn execute(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        action: &Action,
        outcome: &mut Outcome,
        turn: &mut Option<u32>,
    ) -> Result<Stop, Stop> {
        let view = term.view();
        memory.ingest(view);
        *turn = view.status.turn;
        self.ended_before = view.ended;
        if view.ended && action.kind != Kind::Keys {
            return Ok(Stop::GameOver);
        }
        memory.begin(view);
        match action.kind {
            Kind::Keys => self.type_keys(term, memory, &action.keys, outcome),
            Kind::Farlook => self.farlook(term, memory, action, outcome),
            Kind::LookHere => self.look_here(term, memory, action, outcome),
            Kind::Travel | Kind::Explore | Kind::Search => self.walk(term, memory, action, outcome),
        }
    }

    /// Sends `keys` as one batch and takes in the screen it leaves; true when the game wrote news.
    fn send(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        keys: &[u8],
        outcome: &mut Outcome,
    ) -> Result<bool, Stop> {
        term.send(keys).map_err(Stop::Link)?;
        self.after_run = is_run(keys);
        Ok(self.take(term, memory, outcome))
    }

    /// Takes the current screen into memory and its message into `outcome`; true when it is a message not about a
    /// pet.
    fn take(&mut self, term: &dyn Terminal, memory: &mut Memory, outcome: &mut Outcome) -> bool {
        let view = term.view();
        memory.ingest(view);
        self.window.clear();
        if view.more && view.more_window(&mut self.window) {
            let mut news = false;
            let mut first = true;
            for line in &self.window {
                let text = line.trim();
                if text.is_empty() {
                    continue;
                }
                news |= !memory.about_pet(text);
                outcome.message(if first {
                    text.to_owned()
                } else {
                    format!("  {text}")
                });
                first = false;
            }
            return news;
        }
        if question(view)
            || view.phase() == Phase::Menu
            || view.counting()
            || !term.message_written()
        {
            return false;
        }
        let text = view.msg.replace("--More--", "");
        let text = text.trim();
        if text.is_empty() {
            return false;
        }
        outcome.message(text.to_owned());
        !memory.about_pet(text)
    }

    /// Dismisses `--More--` pages; true when any page held news.
    fn page(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        outcome: &mut Outcome,
    ) -> Result<bool, Stop> {
        let mut news = false;
        for _ in 0..PAGES {
            if term.view().phase() != Phase::More {
                return Ok(news);
            }
            news |= self.send(term, memory, b" ", outcome)?;
        }
        if term.view().phase() == Phase::More {
            return Err(Stop::Failed(format!(
                "--More-- pages did not finish after {PAGES} pages"
            )));
        }
        Ok(news)
    }

    /// Dismisses `--More--` pages and text windows, which have no entries to choose, before an expected prompt.
    fn notices(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        outcome: &mut Outcome,
    ) -> Result<(), Stop> {
        for _ in 0..PAGES {
            self.page(term, memory, outcome)?;
            let view = term.view();
            if view.phase() != Phase::Menu {
                return Ok(());
            }
            self.entries.clear();
            view.menu_entries(&mut self.entries);
            if !self.entries.is_empty() {
                return Ok(());
            }
            self.send(term, memory, &[ESCAPE], outcome)?;
        }
        Err(Stop::Failed(format!(
            "text windows did not finish after {PAGES} pages"
        )))
    }

    /// The caller's keys, one byte at a time, except that text typed into a free-text prompt goes as one batch up
    /// to the key that ends it. Each key that meets a prompt is recorded against it.
    fn type_keys(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        keys: &[u8],
        outcome: &mut Outcome,
    ) -> Result<Stop, Stop> {
        let after_run = self.after_run;
        // The prompt the previous key answered, while further keys may extend its answer.
        let mut open: Option<Phase> = None;
        let mut i = 0;
        while i < keys.len() {
            let view = term.view();
            let phase = view.phase();
            let mut end = i + 1;
            if asks(view) {
                if phase == Phase::Text {
                    end = keys[i..]
                        .iter()
                        .position(|&k| matches!(k, b'\r' | b'\n' | ESCAPE))
                        .map_or(keys.len(), |k| i + k + 1);
                }
                record(view, phase, open, &keys[i..end], outcome);
                open = matches!(phase, Phase::Text | Phase::Position).then_some(phase);
            } else {
                open = None;
            }
            term.send(&keys[i..end]).map_err(Stop::Link)?;
            outcome.steps += (end - i) as u32;
            self.take(term, memory, outcome);
            i = end;
        }
        self.after_run = is_run(keys);
        if needs_redraw(keys, after_run, term.view()) {
            self.send(term, memory, &[REDRAW], outcome)?;
            self.after_run = false;
        }
        self.page(term, memory, outcome)?;
        Ok(settled(term.view()))
    }

    /// A farlook: `;`, any notices in the way, the cursor keys, the reading, and an Escape if the game still
    /// waits for a position.
    fn farlook(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        action: &Action,
        outcome: &mut Outcome,
    ) -> Result<Stop, Stop> {
        self.keep_before(term.view());
        self.send(term, memory, &action.keys, outcome)?;
        outcome.steps = 1;
        self.page(term, memory, outcome)?;
        if let Some((phase, keys)) = &action.followup {
            self.notices(term, memory, outcome)?;
            let view = term.view();
            if view.phase() != *phase {
                return Ok(match view.phase() {
                    Phase::Play => Stop::NoEffect,
                    _ if view.ended => Stop::GameOver,
                    _ => Stop::Prompt,
                });
            }
            self.send(term, memory, keys, outcome)?;
            if let (Some(target), Some(before)) = (action.target, self.before.as_ref()) {
                memory.remember_look(target, before, term.view(), LookSource::Farlook);
            }
            self.page(term, memory, outcome)?;
        }
        if term.view().phase() == Phase::Position {
            self.send(term, memory, &[ESCAPE], outcome)?;
            return Ok(Stop::Prompt);
        }
        Ok(settled(term.view()))
    }

    /// A look underfoot: `:`, its pages, and the reading of the hero's square.
    fn look_here(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        action: &Action,
        outcome: &mut Outcome,
    ) -> Result<Stop, Stop> {
        self.keep_before(term.view());
        self.send(term, memory, &action.keys, outcome)?;
        outcome.steps = 1;
        self.page(term, memory, outcome)?;
        if let Some(before) = self.before.as_ref()
            && let Some(hero) = before.hero
        {
            memory.remember_look(hero, before, term.view(), LookSource::Here);
        }
        Ok(settled(term.view()))
    }

    /// Keeps `view` as the screen an information command was asked from, reusing the buffer it last held.
    fn keep_before(&mut self, view: &View) {
        match &mut self.before {
            Some(before) => before.reparse(&view.grid, view.cursor),
            None => self.before = Some(view.clone()),
        }
    }

    /// Travel, exploration and searching: one step at a time, stopping at the first boundary.
    fn walk(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        action: &Action,
        outcome: &mut Outcome,
    ) -> Result<Stop, Stop> {
        let exploring = action.kind == Kind::Explore;
        let moving = matches!(action.kind, Kind::Travel | Kind::Explore);
        let repeat = action.kind == Kind::Search;
        let watch = match action.kind {
            Kind::Explore => Watch::Nothing,
            Kind::Travel => Watch::Features,
            _ => Watch::All,
        };
        self.remember_start(memory);
        let start = term.view();
        self.status.clone_from(&start.status);
        let (cond, engulfed) = (start.cond, start.engulfed);
        let (hp, hpmax) = (start.status.hp, start.status.hpmax);
        monsters(memory, start, &mut self.previous);
        let mut idle = 0;
        let mut step = 0;
        while step < action.steps {
            let view = term.view();
            let origin = view.hero;
            let (prior, prior_turn) = (view.fingerprint(), view.status.turn);
            let (prior_hp, prior_pw) = (view.status.hp, view.status.pw);
            let prior_engulfed = view.engulfed;
            let mut buf = [0u8; 2];
            let mut target = None;
            let keys: &[u8] = if moving {
                let Some(from) = origin else {
                    return Ok(Stop::RouteChanged);
                };
                let Some(level) = memory.current() else {
                    return Ok(Stop::RouteChanged);
                };
                let to = match action.route.get(step as usize) {
                    Some(&to) => to,
                    None => {
                        let options = level.continuations(from, &self.covered);
                        match options.as_slice() {
                            [] => return Ok(Stop::CorridorEnds),
                            [to] => *to,
                            _ => return Ok(Stop::BranchDiscovered),
                        }
                    }
                };
                self.step.clear();
                if !step_keys(level, view, from, to, &mut self.step) {
                    return Ok(Stop::RouteChanged);
                }
                let len = self.step.len().min(buf.len());
                buf[..len].copy_from_slice(&self.step[..len]);
                target = Some(to);
                &buf[..len]
            } else {
                &action.keys
            };
            let after_run = self.after_run;
            let mut news = self.send(term, memory, keys, outcome)?;
            outcome.steps += 1;
            if needs_redraw(keys, after_run, term.view()) {
                news |= self.send(term, memory, &[REDRAW], outcome)?;
            }
            news |= self.page(term, memory, outcome)?;
            let view = term.view();
            if moving
                && view.hero == origin
                && view.phase() == Phase::Play
                && memory.about_pet(&view.msg)
            {
                // The pet in the way stops a step without moving; the step is tried once more.
                news |= self.send(term, memory, keys, outcome)?;
                outcome.steps += 1;
                news |= self.page(term, memory, outcome)?;
            }
            if repeat
                && !prior_engulfed
                && let (Some(from), Some(level)) = (origin, memory.current_mut())
            {
                *level.searches.entry(from).or_insert(0) += 1;
            }
            let view = term.view();
            if view.ended {
                return Ok(Stop::GameOver);
            }
            if view.phase() != Phase::Play {
                return Ok(Stop::Prompt);
            }
            if let (Some(from), Some(to)) = (origin, target)
                && view.hero != Some(to)
            {
                let refused = view.hero == Some(from)
                    && view.status.turn.is_some()
                    && view.status.turn == prior_turn
                    && !is_monster(view.ch(to));
                if refused {
                    if let (Some(level), Some(key)) = (memory.current_mut(), from.direction_to(to))
                    {
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
            if exploring {
                if let Some(hero) = view.hero {
                    self.covered.insert(hero);
                }
                if self.discovered(memory) {
                    return Ok(Stop::FeatureDiscovered);
                }
            }
            // A fast hero can act twice within one turn, so one search can leave the screen unchanged.
            idle = if repeat && view.fingerprint() == prior {
                idle + 1
            } else {
                0
            };
            if idle >= IDLE_SEARCHES {
                return Ok(Stop::NoEffect);
            }
            step += 1;
            // A rest reports what interrupted its last search too; a walk that arrived is done.
            if exploring || repeat || step < action.steps {
                let mut changed = Changed::default();
                let drained =
                    dropped(prior_hp, view.status.hp) || dropped(prior_pw, view.status.pw);
                if status_moved(&self.status, &view.status) || drained {
                    changed.insert(Changed::STATUS);
                }
                if view.cond != cond {
                    changed.insert(Changed::CONDITIONS);
                }
                if view.engulfed != engulfed {
                    changed.insert(Changed::ENGULFED);
                }
                if self.terrain_changed(memory, watch) {
                    changed.insert(Changed::TERRAIN);
                }
                if repeat
                    && step < action.steps
                    && let (Some(hp), Some(hpmax)) = (hp, hpmax)
                    && hp < hpmax
                    && view.status.hp.unwrap_or(0) >= view.status.hpmax.unwrap_or(hpmax)
                {
                    changed.insert(Changed::HP_FULL);
                }
                monsters(memory, view, &mut self.now);
                if let Some(arrival) = arrival(&self.previous, &self.now, view.hero) {
                    changed.insert(Changed::ENTITIES);
                    outcome.arrival = Some(arrival);
                }
                std::mem::swap(&mut self.previous, &mut self.now);
                if news {
                    changed.insert(Changed::MESSAGE);
                }
                if !changed.is_empty() {
                    outcome.changed = changed;
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
        self.terrain.fill('\0');
        self.colours.fill(None);
        self.seen.clear();
        self.doors.clear();
        self.covered.clear();
        self.level = None;
        let Some(level) = memory.current() else {
            return;
        };
        self.level = Some(level.id);
        for (p, ch) in level.known() {
            if let Some(i) = p.index() {
                self.terrain[i] = ch;
                self.colours[i] = level.colour(p);
                self.covered.insert(p);
            }
        }
        self.seen = level.seen.clone();
        self.doors = level.open_doors.clone();
    }

    /// Whether the remembered terrain `watch` covers differs from the start, or the hero is on another level.
    fn terrain_changed(&self, memory: &Memory, watch: Watch) -> bool {
        if watch == Watch::Nothing {
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
            let (old, new) = (self.terrain[i], level.terrain(p).unwrap_or('\0'));
            let (was, now) = (self.colours[i], level.colour(p));
            let structure = structural(old, was) != structural(new, now);
            match watch {
                Watch::All => old != new || structure,
                _ => feature(old) != feature(new) || structure,
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
        if self.level != Some(level.id) {
            return true;
        }
        if level.open_doors.iter().any(|p| !self.doors.contains(p)) {
            return true;
        }
        level.known().any(|(p, ch)| {
            let Some(i) = p.index() else {
                return false;
            };
            let old = self.terrain[i];
            if old == ch && self.colours[i] == level.colour(p) {
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

    fn finish(
        &mut self,
        term: &mut dyn Terminal,
        memory: &mut Memory,
        outcome: &mut Outcome,
    ) -> Result<Stop, Stop> {
        for _ in 0..PAGES {
            let view = term.view();
            if matches!(
                view.end,
                Some(EndKind::Tombstone | EndKind::Farewell | EndKind::Ascended)
            ) {
                return Ok(Stop::GameOver);
            }
            let phase = view.phase();
            let key = match phase {
                Phase::Play => return Ok(Stop::Completed),
                Phase::More => b' ',
                Phase::Choice => b'n',
                Phase::Ended if view.more => b' ',
                Phase::Ended if view.yn.is_some() => b'n',
                Phase::Menu => ESCAPE,
                _ => b' ',
            };
            if key != b' ' {
                record(view, phase, None, &[key], outcome);
            }
            match self.send(term, memory, &[key], outcome) {
                Ok(_) => outcome.steps += 1,
                // The game exits after its last screen; what was captured stands.
                Err(_) if memory.end().is_some() => return Ok(Stop::GameOver),
                Err(stop) => return Err(stop),
            }
        }
        if term.view().ended {
            Ok(Stop::GameOver)
        } else {
            Err(Stop::Failed(format!(
                "the end of the game did not finish after {PAGES} pages"
            )))
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

/// Whether `view` waits for an answer the keys sent to it give: a question or a `--More--`. Menus are not
/// counted: their keys select entries.
fn asks(view: &View) -> bool {
    question(view) || view.more && view.phase() != Phase::Menu
}

/// Records `keys` as the answer to the prompt on `view`, extending the last entry when the previous keys went to
/// the same free-text or positioning prompt, and leaving out the earliest prompts beyond `REPORTED`.
fn record(view: &View, phase: Phase, open: Option<Phase>, keys: &[u8], outcome: &mut Outcome) {
    let answered = &mut outcome.answered;
    let text = view.msg.replace("--More--", "");
    let text = text.trim();
    let prompt = if text.is_empty() { phase.name() } else { text };
    let same = open == Some(phase)
        && answered.last().is_some_and(|last| match phase {
            Phase::Text => prompt.starts_with(last.prompt.as_str()),
            _ => prompt == last.prompt,
        });
    if !same {
        if answered.len() >= REPORTED {
            answered.remove(0);
            outcome.answered_left_out += 1;
        }
        answered.push(Answered {
            prompt: prompt.to_owned(),
            key: String::new(),
            text_entry: phase == Phase::Text,
        });
    }
    if let Some(last) = answered.last_mut() {
        for &key in keys {
            escape_key(key, &mut last.key);
        }
    }
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

/// The monsters in `view` that can end a walk or rest: no objects, no `I` or warning digits, and not the
/// highlighted pet.
fn monsters(memory: &Memory, view: &View, out: &mut Vec<Entity>) {
    out.clear();
    memory.entities(view, out);
    out.retain(|e| e.kind == EntityKind::Monster && !e.pet);
}

/// The monster in `now`, nearest the hero, with no monster of its glyph and colour at or beside its square in
/// `previous`.
fn arrival(previous: &[Entity], now: &[Entity], hero: Option<Pos>) -> Option<Arrival> {
    now.iter()
        .filter(|e| {
            !previous
                .iter()
                .any(|p| p.glyph == e.glyph && p.shade == e.shade && p.pos.chebyshev(e.pos) <= 1)
        })
        .min_by_key(|e| (hero.map_or(0, |h| h.chebyshev(e.pos)), e.pos))
        .map(|e| Arrival {
            glyph: e.glyph,
            pos: e.pos,
        })
}

/// Whether a status field fell.
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
    (ch == '#')
        .then_some(colour)
        .flatten()
        .filter(|c| matches!(c, Base::Green | Base::Cyan))
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
    matches!(
        keys.first(),
        Some(b'_' | b'G' | b'H' | b'J' | b'K' | b'L' | b'Y' | b'U' | b'B' | b'N')
    )
}

/// A walk along the known route to `target`, at most `max_steps` steps.
pub fn travel(paths: &Paths, target: Pos, max_steps: u32) -> Action {
    let mut route = Vec::new();
    paths.route(target, &mut route);
    Action {
        kind: Kind::Travel,
        keys: Vec::new(),
        steps: max_steps.min(route.len() as u32),
        target: Some(target),
        route,
        followup: None,
    }
}

/// A walk to the corridor end `target`, then along the corridor beyond it, at most `max_steps` steps.
pub fn explore(paths: &Paths, target: Pos, max_steps: u32) -> Action {
    let mut route = Vec::new();
    paths.route(target, &mut route);
    Action {
        kind: Kind::Explore,
        keys: Vec::new(),
        steps: max_steps,
        target: Some(target),
        route,
        followup: None,
    }
}

/// A farlook of `target` from the hero's square in `view`.
pub fn farlook(view: &View, target: Pos) -> Action {
    let mut keys = Vec::new();
    cursor_keys(view.hero.unwrap_or(view.cursor), target, &mut keys);
    Action {
        kind: Kind::Farlook,
        keys: b";".to_vec(),
        steps: 1,
        target: Some(target),
        route: Vec::new(),
        followup: Some((Phase::Position, keys)),
    }
}

/// Up to `count` searches in place.
pub fn search(count: u32) -> Action {
    Action {
        kind: Kind::Search,
        keys: b"s".to_vec(),
        steps: count,
        target: None,
        route: Vec::new(),
        followup: None,
    }
}

/// A look underfoot.
pub fn look_here() -> Action {
    Action {
        kind: Kind::LookHere,
        keys: b":".to_vec(),
        steps: 1,
        target: None,
        route: Vec::new(),
        followup: None,
    }
}

/// The caller's keys.
pub fn keys(keys: &[u8]) -> Action {
    Action {
        kind: Kind::Keys,
        keys: keys.to_vec(),
        steps: 1,
        target: None,
        route: Vec::new(),
        followup: None,
    }
}

/// The farlook cursor keys from `start` to `target`: `@` to the hero, moves, then `.` to pick.
pub fn cursor_keys(start: Pos, target: Pos, out: &mut Vec<u8>) {
    out.push(b'@');
    let (dx, dy) = (
        i16::from(target.x) - i16::from(start.x),
        i16::from(target.y) - i16::from(start.y),
    );
    let across = if dx > 0 { b'l' } else { b'h' };
    out.extend(std::iter::repeat_n(across, dx.unsigned_abs() as usize));
    let down = if dy > 0 { b'j' } else { b'k' };
    out.extend(std::iter::repeat_n(down, dy.unsigned_abs() as usize));
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

/// The direction key a plain step into `target` takes from `level`'s memory: the `m` prefix is left off where the
/// game checks the square first (water, lava, traps, gas) or where the pet stands, so the game's own check or
/// swap applies.
pub fn step_keys(level: &Level, view: &View, from: Pos, target: Pos, out: &mut Vec<u8>) -> bool {
    let Some(key) = from.direction_to(target) else {
        return false;
    };
    let pet = view.pet(target) && is_monster(view.ch(target));
    if level.guarded(target).is_none() && !pet {
        out.push(b'm');
    }
    out.push(key);
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::level::Paths;
    use crate::testkit::{STATUS1, ScriptedTerm, grid, screen};
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

    fn shown(rows: &[String], cursor: (u8, u8)) -> View {
        parse(&grid(rows), cursor)
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
        shown(&room_lines(col, hp, turn, message, monster), (2, col))
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
        shown(&corridor_lines(hero, tiles, turn, 12), hero)
    }

    fn hall(hero: (u8, u8), turn: u32, message: &str, monster: Option<(u8, u8)>) -> View {
        let map = [" ------ ", " |....| ", " |....| ", " ------ "].map(str::to_owned);
        let mut rows = lines(message, &map, 12, turn);
        put(&mut rows, hero, '@');
        if let Some(m) = monster {
            put(&mut rows, m, 'd');
        }
        shown(&rows, hero)
    }

    /// A screen with only `message` on its top row and the cursor after it.
    fn asking(message: &str) -> View {
        let x = message.len().min(79) as u8;
        shown(&lines(message, &[], 12, 400), (0, x))
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
            followup: None,
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
    fn builders_bound_their_actions() {
        assert_eq!((search(7).keys, search(7).steps), (b"s".to_vec(), 7));
        assert_eq!(look_here().keys, b":");
        let typed = keys(b"E-");
        assert_eq!(
            (typed.kind, typed.keys, typed.steps),
            (Kind::Keys, b"E-".to_vec(), 1)
        );
        let mut both = Changed::STATUS;
        both.insert(Changed::MESSAGE);
        assert!(both.contains(Changed::MESSAGE) && !both.contains(Changed::TERRAIN));
        assert!(!Changed::default().contains(Changed::default()));
    }

    #[test]
    fn a_route_is_walked_step_by_step() {
        let (mut term, mut memory) = start(vec![at(3, 400), at(4, 401), at(5, 402)]);
        let outcome = Executor::new().run(&mut term, &mut memory, &east(2));
        assert_eq!(sent(&term), ["ml", "ml"]);
        assert_eq!(outcome.stop, Stop::Completed);
        assert_eq!((outcome.steps, outcome.turns), (2, Some(2)));
    }

    #[test]
    fn a_step_onto_a_known_trap_goes_without_the_prefix() {
        let tiles = [((2, 4), '#'), ((2, 5), '^'), ((2, 6), '#')];
        let mut confirm = corridor_lines((2, 4), &tiles, 401, 12);
        confirm[0] = "Really step onto that bear trap? [yn] (n)".to_owned();
        let confirm = shown(&confirm, (0, 41));
        let (mut term, mut memory) = start(vec![
            corridor((2, 3), &tiles, 400),
            corridor((2, 4), &tiles, 401),
            confirm,
        ]);
        let action = walk(Kind::Travel, 3, (2, 6), &[(2, 4), (2, 5), (2, 6)]);
        let outcome = Executor::new().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml", "l"]);
        assert_eq!((outcome.stop, outcome.steps), (Stop::Prompt, 2));
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
                let outcome = Executor::new().run(&mut term, &mut memory, &action);
                assert_eq!(sent(&term), ["ml"], "{kind:?}");
                assert_eq!((outcome.stop, outcome.steps), (stop, 1), "{kind:?}");
            }
        }
        // A monster standing in the way leaves the screen as it was: no effect, and no refusal.
        let blocked = room(3, 12, 400, "", Some(4));
        let (mut term, mut memory) = start(vec![blocked.clone(), blocked]);
        let outcome = Executor::new().run(&mut term, &mut memory, &east(1));
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
            let outcome = Executor::new().run(&mut term, &mut memory, &action);
            assert_eq!(sent(&term), ["mn"]);
            let expected = Stop::Refused {
                from: Pos::new(2, 2),
                to: Pos::new(3, 3),
            };
            assert_eq!(outcome.stop == expected, refused, "{:?}", outcome.stop);
            let level = memory.current().expect("a level");
            let mut paths = Paths::new();
            level.paths(Pos::new(2, 2), &SquareSet::new(), &mut paths);
            let length = if refused { 2 } else { 1 };
            assert_eq!(paths.distance(Pos::new(3, 3)), Some(length));
            // Where no route avoids it, a route still takes the refused step.
            let mut occupied = SquareSet::new();
            occupied.insert(Pos::new(2, 3));
            occupied.insert(Pos::new(3, 2));
            level.paths(Pos::new(2, 2), &occupied, &mut paths);
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
        let outcome = Executor::new().run(&mut term, &mut memory, &east(3));
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
            let outcome = Executor::new().run(&mut term, &mut memory, &east(2));
            assert_eq!(sent(&term), ["ml"]);
            assert_eq!(outcome.stop, Stop::Changed);
            assert_eq!(outcome.changed, field);
        }
        let (mut term, mut memory) = start(vec![at(3, 400), room(4, 12, 401, "", Some(7))]);
        let outcome = Executor::new().run(&mut term, &mut memory, &east(2));
        let arrival = Arrival {
            glyph: 'd',
            pos: Pos::new(2, 7),
        };
        assert_eq!(outcome.arrival, Some(arrival));
    }

    #[test]
    fn monsters_moving_about_in_view_do_not_end_travel() {
        let (mut term, mut memory) = start(vec![
            room(3, 12, 400, "", Some(6)),
            room(4, 12, 401, "", Some(7)),
            room(5, 12, 402, "", Some(8)),
            room(6, 12, 403, "", Some(8)),
        ]);
        let outcome = Executor::new().run(&mut term, &mut memory, &east(3));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &east(2));
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
            shown(&sensed, (2, 4)),
            shown(&warned, (2, 5)),
            at(6, 403),
        ];
        let (mut term, mut memory) = start(views);
        let outcome = Executor::new().run(&mut term, &mut memory, &east(3));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &east(2));
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
            let (mut term, mut memory) =
                start(vec![at(3, 400), shown(&revealed, (2, 4)), at(5, 402)]);
            let outcome = Executor::new().run(&mut term, &mut memory, &east(2));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &search(4));
        assert_eq!((outcome.stop, outcome.steps), (Stop::Completed, 4));
        assert_eq!(sent(&term), ["s", "s", "s", "s"]);
        let level = memory.current().expect("a level");
        assert_eq!(level.searches.get(&Pos::new(2, 3)), Some(&4));
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
            let outcome = Executor::new().run(&mut term, &mut memory, &search(8));
            assert_eq!(sent(&term), ["s"]);
            assert_eq!((outcome.stop, outcome.changed), (Stop::Changed, field));
        }
    }

    #[test]
    fn a_search_that_changes_nothing_three_times_stops() {
        let (mut term, mut memory) = start(vec![at(3, 400)]);
        let outcome = Executor::new().run(&mut term, &mut memory, &search(8));
        assert_eq!(
            (outcome.stop, outcome.steps),
            (Stop::NoEffect, IDLE_SEARCHES)
        );
    }

    #[test]
    fn a_search_while_engulfed_credits_no_square() {
        let inside = ["  /-\\", "  |@|", "  \\-/"].map(str::to_owned);
        let before = shown(&lines("You are swallowed!", &inside, 12, 400), (2, 3));
        let after = shown(&lines("You are swallowed!", &inside, 12, 401), (2, 3));
        let (mut term, mut memory) = start(vec![before, after]);
        let outcome = Executor::new().run(&mut term, &mut memory, &search(1));
        assert_eq!((sent(&term), outcome.steps), (vec!["s".to_owned()], 1));
        assert!(
            memory
                .current()
                .is_none_or(|level| level.searches.is_empty())
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
        let outcome = Executor::new().run(&mut term, &mut memory, &action);
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
        let outcome = Executor::new().run(&mut term, &mut memory, &action);
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
        let outcome = Executor::new().run(&mut term, &mut memory, &action);
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
            let after = shown(&corridor_lines((2, 4), &revealed, 401, hp), (2, 4));
            let (mut term, mut memory) = start(vec![corridor((2, 3), &tiles, 400), after]);
            let action = walk(Kind::Explore, 8, (2, 4), &[(2, 4)]);
            let outcome = Executor::new().run(&mut term, &mut memory, &action);
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
                    let outcome = Executor::new().run(&mut term, &mut memory, &action);
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
        let outcome = Executor::new().run(&mut term, &mut memory, &action);
        assert_eq!(sent(&term), ["ml", "ml"]);
        assert_eq!(outcome.stop, Stop::CorridorEnds);
    }

    #[test]
    fn a_prompt_or_the_end_of_the_game_ends_a_rest() {
        let cases = [
            (asking("Really attack the gnome? [yn] (n)"), Stop::Prompt),
            (asking("Goodbye Hero the Wizard...--More--"), Stop::GameOver),
        ];
        for (frame, stop) in cases {
            let (mut term, mut memory) = start(vec![at(3, 400), frame]);
            let outcome = Executor::new().run(&mut term, &mut memory, &search(8));
            assert_eq!(outcome.stop, stop);
            assert_eq!(sent(&term), ["s"]);
        }
    }

    #[test]
    fn nothing_is_sent_to_a_game_that_has_ended_unless_the_caller_sends_it() {
        let quit = asking("Do you want your possessions identified? [ynq] (n)");
        let (mut term, mut memory) = start(vec![quit.clone(), at(3, 400)]);
        let outcome = Executor::new().run(&mut term, &mut memory, &search(3));
        assert_eq!(outcome.stop, Stop::GameOver);
        assert!(term.sent.is_empty());
        let outcome = Executor::new().run(&mut term, &mut memory, &keys(b"y"));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &keys(b"Fl"));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &keys(b"E-nnElbereth\r"));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &keys(b";@l."));
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
        let outcome = Executor::new().run(&mut term, &mut memory, &keys(b"E"));
        assert_eq!(outcome.stop, Stop::Prompt);
        assert!(outcome.answered.is_empty());
        assert!(outcome.messages.is_empty());
    }

    #[test]
    fn a_counted_command_is_followed_by_a_redraw() {
        let views = vec![at(3, 400), at(3, 400), at(3, 400), at(3, 420), at(3, 420)];
        let (mut term, mut memory) = start(views);
        let outcome = Executor::new().run(&mut term, &mut memory, &keys(b"20s"));
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
            Executor::new().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
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
            shown(&tip, (3, 7)),
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
            Executor::new().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
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
                Executor::new().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
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
            Executor::new().run(&mut term, &mut memory, &farlook(&before, Pos::new(2, 4)));
        assert_eq!(sent(&term), [";", "@l.", "\u{1b}"]);
        assert_eq!(outcome.stop, Stop::Prompt);
    }

    #[test]
    fn a_look_here_reads_the_heros_square() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            room(3, 12, 400, "You see here a dagger.", None),
        ]);
        let outcome = Executor::new().run(&mut term, &mut memory, &look_here());
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
        shown(&rows, (20, 8))
    }

    #[test]
    fn the_end_of_the_game_is_paged_to_the_tombstone_declining_disclosure() {
        let (mut term, mut memory) = start(vec![
            at(3, 400),
            asking("You die...--More--"),
            asking("Do you want your possessions identified? [ynq] (n)"),
            asking("Do you want to see your attributes? [ynq] (n)"),
            tombstone(),
        ]);
        let outcome = Executor::new().run(&mut term, &mut memory, &search(5));
        assert_eq!(sent(&term), ["s", " ", "n", "n"]);
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
        assert!(memory.end().is_some());
        // Once captured, paging again sends nothing.
        let again = Executor::new().finish_game(&mut term, &mut memory);
        assert_eq!((again.stop, term.sent.len()), (Stop::GameOver, 4));
    }

    #[test]
    fn finishing_a_game_still_going_sends_nothing() {
        let (mut term, mut memory) = start(vec![at(3, 400)]);
        let outcome = Executor::new().finish_game(&mut term, &mut memory);
        assert_eq!(outcome.stop, Stop::Completed);
        assert!(term.sent.is_empty());
    }

    #[test]
    fn a_link_failure_ends_the_action_with_its_reason() {
        let error = LinkError::Held("waiting to start".to_owned());
        let (mut term, mut memory) = start(vec![at(3, 400), at(4, 401)]);
        term.failures.push(Some(error.clone()));
        let outcome = Executor::new().run(&mut term, &mut memory, &east(1));
        assert_eq!((outcome.stop, outcome.steps), (Stop::Link(error), 0));
        assert!(term.sent.is_empty());
    }

    #[test]
    fn endless_pages_fail_after_the_bound() {
        let (mut term, mut memory) = start(vec![at(3, 400), asking("You hear a noise.--More--")]);
        let outcome = Executor::new().run(&mut term, &mut memory, &search(1));
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
        let (mut term, mut memory) = start(vec![at(3, 400), shown(&window, (4, 8)), at(3, 400)]);
        let outcome = Executor::new().run(&mut term, &mut memory, &look_here());
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
