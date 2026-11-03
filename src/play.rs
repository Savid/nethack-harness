//! The commands a caller plays with: `look`, `send`, `go` and `rest`, each answered with a view.

use crate::exec::{self, Executor, Kind, Outcome, Stop};
use crate::level::{Paths, Underfoot, door_sides};
use crate::link::{LinkError, Terminal};
use crate::observe::{Entity, EntityKind, Memory};
use crate::screen::Phase;
use crate::types::{Answered, ExitCode, Facts, Pos, Reply, Report, SquareSet};
use crate::view::{LISTED, Landmark, Names, Renderer, Scene, landmarks};

/// Most keys one `send` takes.
pub const KEYS_MAX: usize = 256;
/// Most searches one `rest` takes.
pub const REST_MAX: u32 = 200;
/// Most steps one `go` takes unless configured lower.
pub const STEPS_MAX: u32 = 64;
/// The refusal while a prompt is open.
pub const PROMPT_OPEN: &str = "a prompt is open: answer or cancel it with send first";

/// What the last bounded action was, for its report.
#[derive(Clone, Copy, Debug)]
struct Last {
    kind: Kind,
    target: Option<Pos>,
    route: usize,
}

/// One game played through a terminal: memory, the executor, names and the renderer, reused across commands.
pub struct Play<T: Terminal> {
    term: T,
    memory: Memory,
    exec: Executor,
    names: Names,
    renderer: Renderer,
    paths: Paths,
    terrain_paths: Paths,
    fresh: Vec<String>,
    reported: u64,
    last_square: Option<(u16, Pos)>,
    items: SquareSet,
    items_level: Option<u16>,
    max_steps: u32,
    entities: Vec<Entity>,
    unnamed: Vec<Entity>,
    landmarks: Vec<Landmark>,
    edges: Vec<(Pos, u32)>,
    route: Vec<Pos>,
    occupied: SquareSet,
    answered: Vec<Answered>,
    last: Option<Last>,
    text: String,
    facts: Facts,
}

/// The phrase for each changed observation, in the order a report lists them.
const CHANGES: [(exec::Changed, &str); 7] = [
    (exec::Changed::STATUS, "the status line changed"),
    (exec::Changed::CONDITIONS, "a condition changed"),
    (exec::Changed::ENGULFED, "the hero was engulfed"),
    (exec::Changed::TERRAIN, "new terrain came into view"),
    (exec::Changed::HP_FULL, "HP is full"),
    (
        exec::Changed::ENTITIES,
        "a monster came into view or next to the hero",
    ),
    (exec::Changed::MESSAGE, "a message appeared"),
];

/// Parses `x,y` with optional spaces around each number.
fn coordinates(target: &str) -> Option<(u32, u32)> {
    let (x, y) = target.split_once(',')?;
    let number = |s: &str| {
        let s = s.trim();
        (!s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
            .then(|| s.parse::<u32>().unwrap_or(u32::MAX))
    };
    Some((number(x)?, number(y)?))
}

impl<T: Terminal> Play<T> {
    /// Play over `term`, walking at most `max_steps` steps per `go`.
    pub fn new(term: T, max_steps: u32) -> Play<T> {
        Play {
            term,
            memory: Memory::new(),
            exec: Executor::new(),
            names: Names::new(),
            renderer: Renderer::new(),
            paths: Paths::new(),
            terrain_paths: Paths::new(),
            fresh: Vec::new(),
            reported: 0,
            last_square: None,
            items: SquareSet::new(),
            items_level: None,
            max_steps: max_steps.clamp(1, STEPS_MAX),
            entities: Vec::new(),
            unnamed: Vec::new(),
            landmarks: Vec::new(),
            edges: Vec::new(),
            route: Vec::new(),
            occupied: SquareSet::new(),
            answered: Vec::new(),
            last: None,
            text: String::new(),
            facts: Facts::default(),
        }
    }

    /// The terminal.
    pub fn term(&self) -> &T {
        &self.term
    }

    /// The terminal, mutably.
    pub fn term_mut(&mut self) -> &mut T {
        &mut self.term
    }

    /// Reads the screen and renders it. While the map waits for a command, it first names monsters by farlook
    /// (only new or moved ones, at most `FARLOOKS`) and re-reads a known engraving or object square underfoot with
    /// `:` when the turn has advanced since its last reading; both take no game time.
    pub fn look(&mut self) -> Reply {
        if let Err(error) = self.term.poll() {
            return self.failure("look", &error);
        }
        self.memory.ingest(self.term.view());
        if self.term.view().ended {
            self.finish_game();
        }
        self.inquire();
        let code = self.over().unwrap_or(ExitCode::Done);
        self.reply(code, None)
    }

    /// Renders the current screen as it stands, sending nothing: no farlooks and no look underfoot.
    pub fn show(&mut self) -> Reply {
        self.memory.ingest(self.term.view());
        self.keep_visible_names();
        let code = self.over().unwrap_or(ExitCode::Done);
        self.reply(code, None)
    }

    /// Sends the caller's keys and renders the result, with every prompt they passed and its answer.
    pub fn send(&mut self, keys: &[u8]) -> Reply {
        if keys.is_empty() || keys.len() > KEYS_MAX {
            return Reply::message(ExitCode::Usage, format!("send takes 1 to {KEYS_MAX} keys"));
        }
        if let Some(reply) = self.prepare("send") {
            return reply;
        }
        let action = exec::keys(keys);
        self.execute("send", &action)
    }

    /// Walks toward `target` (`x,y`, a map glyph, a feature kind, or `frontier`) along the known route.
    pub fn go(&mut self, target: &str) -> Reply {
        let verb = format!("go {target}");
        if let Some(reply) = self.prepare(&verb) {
            return reply;
        }
        let view = self.term.view();
        if view.phase() != Phase::Play {
            return Reply::message(ExitCode::Failed, PROMPT_OPEN);
        }
        if view.engulfed {
            return Reply::message(ExitCode::Failed, "go is unavailable while engulfed");
        }
        let mut places = std::mem::take(&mut self.landmarks);
        let chosen = self
            .places(target, &mut places)
            .and_then(|()| self.choose(target, &places));
        self.landmarks = places;
        let goal = match chosen {
            Ok(goal) => goal,
            Err(message) => return Reply::message(ExitCode::Failed, message),
        };
        let explore = target.trim().eq_ignore_ascii_case("frontier")
            && self
                .memory
                .current()
                .is_some_and(|level| level.corridor(goal) && level.corridor_links(goal).len() <= 1);
        let action = if explore {
            exec::explore(&self.paths, goal, self.max_steps)
        } else {
            exec::travel(&self.paths, goal, self.max_steps)
        };
        self.execute(&verb, &action)
    }

    /// Searches in place up to `count` times.
    pub fn rest(&mut self, count: u32) -> Reply {
        if count == 0 || count > REST_MAX {
            return Reply::message(
                ExitCode::Usage,
                format!("rest takes 1 to {REST_MAX} searches"),
            );
        }
        let verb = format!("rest {count}");
        if let Some(reply) = self.prepare(&verb) {
            return reply;
        }
        if self.term.view().phase() != Phase::Play {
            return Reply::message(ExitCode::Failed, PROMPT_OPEN);
        }
        self.execute(&verb, &exec::search(count))
    }

    /// The places a `go` target names on the current level, written into `out`; an error says why none match. It
    /// leaves the routes from the hero around monsters in the play's path buffer.
    pub fn places(&mut self, target: &str, out: &mut Vec<Landmark>) -> Result<(), String> {
        out.clear();
        self.route_from_hero();
        let Some(level) = self.memory.current() else {
            return Err(PROMPT_OPEN.to_owned());
        };
        if let Some((x, y)) = coordinates(target) {
            let p = match (u8::try_from(y), u8::try_from(x)) {
                (Ok(y), Ok(x)) => Pos::new(y, x),
                _ => return Err(format!("{target} is off the map")),
            };
            if !p.on_map() {
                return Err(format!("{target} is off the map"));
            }
            if level.terrain(p) == Some('+') {
                out.push(Landmark {
                    pos: p,
                    glyph: Some('+'),
                    label: "door".to_owned(),
                    goals: door_sides(p),
                });
            } else {
                out.push(Landmark::at(p, None, "chosen square"));
            }
            return Ok(());
        }
        let word = target.trim().to_lowercase();
        if word == "frontier" {
            level.frontier_targets(&self.paths, &mut self.edges);
            out.extend(
                self.edges
                    .iter()
                    .map(|&(p, _)| Landmark::at(p, None, "frontier")),
            );
            return Ok(());
        }
        landmarks(level, &self.entities, out);
        out.retain(|place| {
            let glyph = place
                .glyph
                .is_some_and(|g| word.chars().eq(std::iter::once(g)));
            glyph
                || place.label == word
                || place.label != "open door" && place.label.split(' ').any(|part| part == word)
        });
        if out.is_empty() {
            return Err(format!(
                "no remembered '{target}' on this level; use x,y, <, >, frontier or a kind listed under seen"
            ));
        }
        Ok(())
    }

    /// Why no known route reaches `target`, from remembered facts.
    pub fn unreachable(&mut self, target: &str, places: &[Landmark]) -> String {
        self.route_from_hero();
        let Some(level) = self.memory.current() else {
            return format!("no known route to {target}");
        };
        let Some(hero) = self.term.view().hero else {
            return format!("no known route to {target}");
        };
        level.paths(hero, &SquareSet::new(), &mut self.terrain_paths);
        let mut blockers = SquareSet::new();
        for place in places {
            for &goal in place.goals.as_slice() {
                if self.terrain_paths.route(goal, &mut self.route) {
                    for &q in &self.route {
                        if self.occupied.contains(q) {
                            blockers.insert(q);
                        }
                    }
                }
            }
        }
        if !blockers.is_empty() {
            let mut text = format!("the known route to {target} is blocked by a monster at ");
            join(blockers.iter().take(3), " or ", &mut text);
            return text;
        }
        if let [place] = places
            && place.label == "chosen square"
        {
            return if level.terrain(place.pos).is_none()
                && !level.object_squares.contains(place.pos)
            {
                format!("{target} has not been seen")
            } else {
                format!("no known route to {target}")
            };
        }
        if !target.trim().eq_ignore_ascii_case("frontier") {
            return format!("no known route to {target}");
        }
        let mut facts = String::from("no reachable known square borders unexplored ones");
        landmarks(level, &self.entities, &mut self.landmarks);
        let mut doors = 0;
        for place in &self.landmarks {
            if place.label != "door" && place.label != "door or object" {
                continue;
            }
            if doors == LISTED {
                break;
            }
            facts.push_str(if doors == 0 { "; doors: " } else { ", " });
            doors += 1;
            facts.push_str(&place.pos.to_string());
            if place.label != "door" {
                facts.push(' ');
                facts.push_str(&place.label);
            }
            match place
                .goals
                .as_slice()
                .iter()
                .filter_map(|&goal| self.terrain_paths.distance(goal))
                .min()
            {
                Some(steps) => facts.push_str(&format!(" ({steps})")),
                None => facts.push_str(" (no route)"),
            }
        }
        let mut blocking = SquareSet::new();
        for p in self.terrain_paths.reached().iter() {
            if level.frontier(p) && self.terrain_paths.route(p, &mut self.route) {
                for &q in &self.route {
                    if self.occupied.contains(q) {
                        blocking.insert(q);
                    }
                }
            }
        }
        let mut monsters: Vec<Pos> = blocking.iter().collect();
        monsters.extend(
            self.occupied
                .iter()
                .filter(|&q| level.terrain(q).is_none() && !blocking.contains(q)),
        );
        if !monsters.is_empty() {
            facts.push_str("; monsters in the way or on unexplored squares: ");
            join(monsters.iter().copied().take(3), ", ", &mut facts);
        }
        let mut boulders = self
            .entities
            .iter()
            .filter(|e| e.kind == EntityKind::Object && matches!(e.glyph, '0' | '`'))
            .take(LISTED)
            .peekable();
        if boulders.peek().is_some() {
            facts.push_str("; boulders: ");
            for (i, boulder) in boulders.enumerate() {
                if i > 0 {
                    facts.push_str(", ");
                }
                facts.push_str(&format!("{} {}", boulder.glyph, boulder.pos));
            }
        }
        let mut ends = level
            .visits
            .keys()
            .filter(|&&p| level.corridor(p) && level.corridor_links(p).len() <= 1)
            .take(LISTED)
            .peekable();
        if ends.peek().is_some() {
            facts.push_str("; corridor ends: ");
            for (i, &p) in ends.enumerate() {
                if i > 0 {
                    facts.push_str(", ");
                }
                let searched = level.searches.get(&p).copied().unwrap_or(0);
                facts.push_str(&format!("{p} (searched {searched})"));
            }
        }
        facts
    }

    /// The report for `outcome` of the command labelled `verb`.
    pub fn report(&self, verb: &str, outcome: &Outcome) -> Report {
        let mut report = Report {
            verb: verb.to_owned(),
            done: false,
            stopped: Vec::new(),
            steps: outcome.steps,
            turns: outcome.turns,
            error: None,
        };
        let (kind, target, route) = self.last.map_or((Kind::Keys, None, 0), |last| {
            (last.kind, last.target, last.route)
        });
        let hero = self.term.view().hero;
        if refused_search(kind, outcome) {
            report.stopped.push("no time passed".to_owned());
            return report;
        }
        match &outcome.stop {
            Stop::Link(error) => report.error = Some(error.to_string()),
            Stop::Failed(error) => report.error = Some(error.clone()),
            Stop::Completed => {
                if matches!(kind, Kind::Travel | Kind::Explore)
                    && target.is_some()
                    && hero != target
                {
                    report.stopped.push(format!(
                        "it reached its step limit, {} squares short",
                        route.saturating_sub(outcome.steps as usize)
                    ));
                } else {
                    report.done = true;
                }
            }
            Stop::Refused { from, to } => report
                .stopped
                .push(format!("step {from}->{to} refused; routing around it")),
            Stop::Changed if !outcome.changed.is_empty() => {
                for (flag, phrase) in CHANGES {
                    if !outcome.changed.contains(flag) {
                        continue;
                    }
                    match outcome.arrival {
                        Some(arrival) if flag == exec::Changed::ENTITIES => report
                            .stopped
                            .push(format!("{phrase} ({} {})", arrival.glyph, arrival.pos)),
                        _ => report.stopped.push(phrase.to_owned()),
                    }
                }
            }
            stop => report.stopped.push(
                match stop {
                    Stop::StepLimit => "it reached its step limit",
                    Stop::Prompt => "a prompt opened",
                    Stop::MovementInterrupted => "the hero did not arrive where expected",
                    Stop::NoEffect => "nothing changed",
                    Stop::RouteChanged => "the hero left the route",
                    Stop::FeatureDiscovered => "new terrain came into view",
                    Stop::BranchDiscovered => "the corridor branches",
                    Stop::CorridorEnds => "the corridor ends",
                    Stop::GameOver => "the game ended",
                    _ => "the screen changed",
                }
                .to_owned(),
            ),
        }
        if kind == Kind::Keys && report.error.is_none() && outcome.stop != Stop::GameOver {
            report.done = true;
            report.stopped.clear();
        }
        report
    }

    /// Whether the game verifiably waits for a command: no prompt, the cursor on `@`, the status lines shown.
    pub fn plain(&self) -> bool {
        let view = self.term.view();
        view.phase() == Phase::Play
            && !view.engulfed
            && view
                .hero
                .is_some_and(|hero| view.cursor == hero && view.ch(hero) == '@')
            && !self.term.prompts().position
            && view.status.hp.is_some()
            && view.status.turn.is_some()
            && view.status.location.is_some()
    }

    /// Reads the screen before a command; a link failure or a finished game is answered here, sending nothing.
    fn prepare(&mut self, verb: &str) -> Option<Reply> {
        if let Err(error) = self.term.poll() {
            return Some(self.failure(verb, &error));
        }
        self.memory.ingest(self.term.view());
        if self.term.view().ended {
            self.finish_game();
        }
        if let Some(code) = self.over() {
            let report = Report {
                verb: verb.to_owned(),
                error: Some("the game is over; nothing was sent".to_owned()),
                ..Report::default()
            };
            return Some(self.reply(code, Some(report)));
        }
        None
    }

    /// Runs a caller's action and answers with its report and the view after it.
    fn execute(&mut self, verb: &str, action: &exec::Action) -> Reply {
        self.last = Some(Last {
            kind: action.kind,
            target: action.target,
            route: action.route.len(),
        });
        let mut outcome = self.exec.run(&mut self.term, &mut self.memory, action);
        self.fresh.clear();
        if outcome.messages_left_out > 0 {
            self.fresh.push(format!(
                "({} earlier lines left out)",
                outcome.messages_left_out
            ));
        }
        self.fresh.append(&mut outcome.messages);
        self.answered.clear();
        if outcome.answered_left_out > 0 {
            self.answered.push(Answered {
                prompt: format!("{} earlier prompts left out", outcome.answered_left_out),
                key: String::new(),
                text_entry: false,
            });
        }
        self.answered.append(&mut outcome.answered);
        self.reported = self.memory.message_total();
        let report = self.report(verb, &outcome);
        let code = match &outcome.stop {
            Stop::Link(error) => error.code(),
            Stop::Failed(_) => ExitCode::Failed,
            Stop::GameOver => ExitCode::Over,
            _ if refused_search(action.kind, &outcome) => ExitCode::Failed,
            _ if report.done => ExitCode::Done,
            _ => ExitCode::Short,
        };
        let code = self.over().unwrap_or(code);
        // After a link failure the game may still be busy or gone, so nothing more is sent to it.
        if code != ExitCode::Over && !matches!(outcome.stop, Stop::Link(_)) {
            self.inquire();
        }
        self.reply(code, Some(report))
    }

    /// Pages through the end of the game. What it shows is reported as the end text rather than as messages; the
    /// questions it declined are reported as answered.
    fn finish_game(&mut self) {
        let mut outcome = self.exec.finish_game(&mut self.term, &mut self.memory);
        self.answered.append(&mut outcome.answered);
        self.reported = self.memory.message_total();
    }

    /// `Over` once the game has ended.
    fn over(&self) -> Option<ExitCode> {
        (self.memory.end().is_some() || self.term.view().ended).then_some(ExitCode::Over)
    }

    /// Answers a link failure; once the game has ended, with the last screen and the end text.
    fn failure(&mut self, verb: &str, error: &LinkError) -> Reply {
        let Some(code) = self.over() else {
            return Reply::message(error.code(), format!("{verb}: {error}"));
        };
        let report = Report {
            verb: verb.to_owned(),
            error: Some(error.to_string()),
            ..Report::default()
        };
        self.reply(code, Some(report))
    }

    /// Renders the view with `report` under `code`; a refused view is answered with its reason instead.
    fn reply(&mut self, code: ExitCode, report: Option<Report>) -> Reply {
        for message in self.memory.messages_since(self.reported) {
            let text = message.text.replace("--More--", "");
            let text = text.trim();
            if !text.is_empty() {
                self.fresh.push(text.to_owned());
            }
        }
        self.reported = self.memory.message_total();
        let scene = Scene {
            view: self.term.view(),
            memory: &self.memory,
            names: &self.names,
            fresh: &self.fresh,
            report: report.as_ref(),
            answered: &self.answered,
        };
        let rendered = self
            .renderer
            .render(&scene, &mut self.text, &mut self.facts);
        self.fresh.clear();
        self.answered.clear();
        if let Some((level, items)) = self.renderer.listed_items() {
            self.items.clone_from(items);
            self.items_level = Some(level);
        }
        match rendered {
            Ok(()) => Reply {
                code,
                text: std::mem::take(&mut self.text),
                facts: std::mem::take(&mut self.facts),
            },
            Err(message) => Reply::message(
                if code == ExitCode::Done {
                    ExitCode::Failed
                } else {
                    code
                },
                message,
            ),
        }
    }

    /// Fills the entity buffer and the squares monsters block, and routes from the hero around them. A route
    /// step into the highlighted pet swaps places with it, so the pet blocks nothing.
    fn route_from_hero(&mut self) {
        let view = self.term.view();
        self.memory.entities(view, &mut self.entities);
        self.occupied.clear();
        for entity in &self.entities {
            if matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) && !entity.pet {
                self.occupied.insert(entity.pos);
            }
        }
        if let (Some(level), Some(hero)) = (self.memory.current(), view.hero) {
            level.paths(hero, &self.occupied, &mut self.paths);
        }
    }

    /// The square to walk to: the nearest goal of `places` a route reaches, preferring one the hero is not on.
    fn choose(&mut self, target: &str, places: &[Landmark]) -> Result<Pos, String> {
        let mut best: Option<(u32, Pos)> = None;
        let mut here: Option<Pos> = None;
        for place in places {
            for &goal in place.goals.as_slice() {
                match self.paths.distance(goal) {
                    Some(0) if here.is_none_or(|h| goal < h) => here = Some(goal),
                    Some(steps) if best.is_none_or(|b| (steps, goal) < b) => {
                        best = Some((steps, goal));
                    }
                    _ => {}
                }
            }
        }
        match (best, here) {
            (Some((_, goal)), _) => Ok(goal),
            (None, Some(goal)) => Err(format!("already at {goal}")),
            (None, None) => Err(self.unreachable(target, places)),
        }
    }

    /// Names monsters by farlook and looks underfoot, while the game waits for a command.
    fn inquire(&mut self) {
        if !self.plain() {
            self.keep_visible_names();
            return;
        }
        self.name_monsters();
        if self.plain() {
            self.look_underfoot();
        }
        self.memory.ingest(self.term.view());
    }

    /// Drops names whose monster left its square or changed its look.
    fn keep_visible_names(&mut self) {
        let view = self.term.view();
        if !view.normal {
            return;
        }
        let Some(level) = self.memory.current().map(|level| level.id) else {
            return;
        };
        self.memory.entities(view, &mut self.entities);
        self.names.retain_visible(level, &self.entities);
    }

    /// Names each monster in view by farlook. A name is kept while the monster stays on its square looking the
    /// same; one that moved or arrived is looked at again, nearest first, at most `FARLOOKS` per look.
    fn name_monsters(&mut self) {
        let view = self.term.view();
        let (Some(level), Some(hero)) = (self.memory.current().map(|level| level.id), view.hero)
        else {
            return;
        };
        let turn = view.status.turn;
        self.memory.entities(view, &mut self.entities);
        self.names.retain_visible(level, &self.entities);
        self.names
            .unnamed(level, hero, &self.entities, &mut self.unnamed);
        let unnamed = std::mem::take(&mut self.unnamed);
        for entity in &unnamed {
            if !self.plain() {
                break;
            }
            let name = self.farlook(entity);
            if self.term.view().status.turn != turn {
                break;
            }
            if let (Some(name), Some(turn)) = (name, turn) {
                self.names.insert(level, entity, &name, turn);
            }
        }
        self.unnamed = unnamed;
    }

    /// The game's own farlook description of `entity`, or `None`. The executor keeps the look's messages out of
    /// the remembered ones and closes the targeting prompt.
    fn farlook(&mut self, entity: &Entity) -> Option<String> {
        let p = entity.pos;
        if let Some(level) = self.memory.current_mut() {
            level.inspections.remove(&p);
        }
        let action = exec::farlook(self.term.view(), p);
        let outcome = self.exec.run(&mut self.term, &mut self.memory, &action);
        if outcome.stop != Stop::Completed || outcome.turns.unwrap_or(0) != 0 {
            return None;
        }
        let seen = self.memory.current()?.inspections.get(&p)?;
        (seen.glyph == entity.glyph && seen.shade == entity.shade).then(|| seen.description.clone())
    }

    /// Asks the game what is underfoot (`:`, no game time): on arriving at a square never seen under the hero
    /// or listed under `items:`, and on a known engraving square whenever the turn has advanced since its last
    /// reading. The reading replaces what was noted there, so a scuffed or wiped engraving is not kept.
    fn look_underfoot(&mut self) {
        let view = self.term.view();
        let turn = view.status.turn;
        let (Some(level), Some(hero)) = (self.memory.current(), view.hero) else {
            return;
        };
        let arrived = self.last_square != Some((level.id, hero));
        self.last_square = Some((level.id, hero));
        let known = level.underfoot.get(&hero);
        if known.is_some_and(|known| known.turn == turn) {
            return;
        }
        let object = level.inferred_floor.contains(hero)
            || self.items_level == Some(level.id) && self.items.contains(hero);
        let engraving = level.engravings.contains(hero)
            || known.is_some_and(|known| {
                known
                    .notes
                    .iter()
                    .any(|note| crate::observe::engraving_sentence(note))
            });
        if !(arrived && object || engraving) {
            return;
        }
        let outcome = self
            .exec
            .run(&mut self.term, &mut self.memory, &exec::look_here());
        if outcome.stop != Stop::Completed || outcome.turns.unwrap_or(0) != 0 {
            return;
        }
        let notes: Vec<String> = outcome
            .messages
            .iter()
            .map(|message| message.trim().to_owned())
            .filter(|message| !message.is_empty())
            .collect();
        if let Some(level) = self.memory.current_mut() {
            level.underfoot.insert(hero, Underfoot::default());
            level.note_underfoot(hero, &notes, turn);
        }
    }
}

/// Whether a rest's searches passed no game time and changed nothing but, at most, the message: the game refused
/// them. A fast hero's single search, or one that monsters acted during, can also leave the turn counter where it
/// was, so a lone search that completed, or one that changed the status or brought a monster, is not refused.
fn refused_search(kind: Kind, outcome: &Outcome) -> bool {
    kind == Kind::Search
        && outcome.turns == Some(0)
        && match outcome.stop {
            Stop::Completed => outcome.steps > 1,
            Stop::NoEffect => outcome.steps > 0,
            Stop::Changed => outcome.changed == exec::Changed::MESSAGE,
            _ => false,
        }
}

/// Appends squares joined by `separator`.
fn join(squares: impl Iterator<Item = Pos>, separator: &str, out: &mut String) {
    for (i, p) in squares.enumerate() {
        if i > 0 {
            out.push_str(separator);
        }
        out.push_str(&p.to_string());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::level::Inspection;
    use crate::screen::View;
    use crate::testkit::{STATUS1, STATUS2, ScriptedTerm, screen, view};
    use crate::types::{COLS, Cell, Colour, Grid, ROWS};
    use serde::Deserialize;

    /// A recorded screen, editable before it is parsed.
    #[derive(Clone, Deserialize)]
    struct Data {
        rows: Vec<String>,
        fg: Vec<Vec<String>>,
        bold: Vec<Vec<bool>>,
        rev: Vec<Vec<bool>>,
        cursor: [u8; 2],
    }

    impl Data {
        fn load(name: &str) -> Data {
            let path = format!(
                "{}/tests/fixtures/screens/{name}.json",
                env!("CARGO_MANIFEST_DIR")
            );
            serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
        }

        fn message(mut self, text: &str) -> Data {
            self.rows[0] = format!("{text:<80}");
            self
        }

        fn cursor(mut self, y: u8, x: u8) -> Data {
            self.cursor = [y, x];
            self
        }

        fn turn(mut self, from: u32, to: u32) -> Data {
            self.rows[23] = self.rows[23].replace(&format!("T:{from} "), &format!("T:{to} "));
            self
        }

        fn set(&mut self, y: usize, x: usize, ch: char) {
            let mut row: Vec<char> = self.rows[y].chars().collect();
            row[x] = ch;
            self.rows[y] = row.into_iter().collect();
        }

        /// A copy with the hero moved to `x,y`, leaving `under` where it stood.
        fn walked(&self, x: usize, y: usize, turn: Option<u32>, under: char) -> Data {
            let mut data = self.clone();
            let [r, c] = data.cursor;
            data.set(r as usize, c as usize, under);
            data.set(y, x, '@');
            if let Some(turn) = turn {
                let row = &data.rows[23];
                let at = row.find("T:").unwrap();
                let end = row[at + 2..]
                    .find(|c: char| !c.is_ascii_digit())
                    .map_or(row.len(), |e| at + 2 + e);
                data.rows[23] = format!("{}T:{turn}{}", &row[..at], &row[end..]);
            }
            data.cursor = [y as u8, x as u8];
            data
        }

        /// A copy with the cell at `from` swapped with the one at `to`, at turn `turn`.
        fn moved(&self, from: (usize, usize), to: (usize, usize), turn: u32) -> Data {
            let mut data = self.clone();
            let a = self.rows[from.0].chars().nth(from.1).unwrap();
            let b = self.rows[to.0].chars().nth(to.1).unwrap();
            data.set(to.0, to.1, a);
            data.set(from.0, from.1, b);
            let fg = data.fg[from.0][from.1].clone();
            data.fg[from.0][from.1] = std::mem::replace(&mut data.fg[to.0][to.1], fg);
            let bold = data.bold[from.0][from.1];
            data.bold[from.0][from.1] = std::mem::replace(&mut data.bold[to.0][to.1], bold);
            let rev = data.rev[from.0][from.1];
            data.rev[from.0][from.1] = std::mem::replace(&mut data.rev[to.0][to.1], rev);
            data.turn(1, turn)
        }

        fn view(&self) -> View {
            let mut grid = Grid::new();
            for r in 0..ROWS {
                for (c, ch) in self.rows[r].chars().take(COLS).enumerate() {
                    grid.cells[r][c] = Cell {
                        ch,
                        fg: Colour::from_name(&self.fg[r][c]).unwrap(),
                        bold: self.bold[r][c],
                        rev: self.rev[r][c],
                    };
                }
            }
            View::parse(&grid, Pos::new(self.cursor[0], self.cursor[1]))
        }
    }

    fn start() -> Data {
        Data::load("seed1001-start")
    }

    fn farlook(name: &str) -> [View; 2] {
        [
            start()
                .message("Pick a monster, object or location.")
                .view(),
            start()
                .message(&format!("d        a dog or other canine ({name})"))
                .view(),
        ]
    }

    fn stairs() -> View {
        start()
            .message("There is a staircase up out of the dungeon here.")
            .view()
    }

    /// A text screen with the default status rows and the cursor at (`y`, `x`).
    fn text(top: &str, rows: &[&str], y: u8, x: u8) -> View {
        view(&screen(top, rows, STATUS1, STATUS2), y, x)
    }

    /// A text screen at turn `turn`.
    fn at_turn(top: &str, rows: &[&str], y: u8, x: u8, turn: u32) -> View {
        let status = format!("Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:{turn}");
        view(&screen(top, rows, STATUS1, &status), y, x)
    }

    fn play(views: Vec<View>) -> Play<ScriptedTerm> {
        Play::new(ScriptedTerm::new(views), 8)
    }

    fn sent(play: &Play<ScriptedTerm>) -> Vec<String> {
        play.term()
            .sent
            .iter()
            .map(|keys| String::from_utf8_lossy(keys).into_owned())
            .collect()
    }

    /// The character printed at `x,y`, read through the ruler and the row numbers.
    fn column(text: &str, x: usize, y: usize) -> char {
        let lines: Vec<&str> = text.lines().collect();
        let units = lines
            .iter()
            .position(|line| {
                line.starts_with("   ")
                    && !line.trim().is_empty()
                    && line.trim().bytes().all(|b| b.is_ascii_digit())
            })
            .unwrap();
        let left: usize = lines[units - 1]
            .split_whitespace()
            .next()
            .unwrap()
            .parse()
            .unwrap();
        let prefix = format!("{y:>2} ");
        let row = lines[units + 1..]
            .iter()
            .find(|line| line.starts_with(&prefix))
            .unwrap();
        row.chars().nth(3 + x - left).unwrap()
    }

    fn has_line(text: &str, wanted: &str) -> bool {
        text.lines().any(|line| line == wanted)
    }

    #[test]
    fn look_names_monsters_and_underfoot_once_and_coordinates_read_off_the_map() {
        let mut views = vec![start().view()];
        views.extend(farlook("tame little dog"));
        views.extend(farlook("jackal"));
        views.push(stairs());
        let mut play = play(views);
        let reply = play.look();
        assert_eq!(reply.code, ExitCode::Done);
        assert_eq!(sent(&play), [";", "@k.", ";", "@llll.", ":"]);
        let lines: Vec<&str> = reply.text.lines().collect();
        let monsters = "monsters: d tame little dog 32,5 adjacent k; d jackal 36,6 4 away";
        let at = lines.iter().position(|line| *line == monsters).unwrap();
        assert_eq!(
            lines[at - 1],
            "you: 32,6  here: < up stairs; There is a staircase up out of the dungeon here."
        );
        assert!(has_line(
            &reply.text,
            "seen: + door 32,7 (adjacent j); + door 30,5 (1)"
        ));
        assert_eq!(lines[0], "Dlvl:1 $:0 HP:14(14) Pw:5(5) AC:4 Xp:1 T:1");
        let printed: Vec<char> = [(32, 6), (32, 5), (36, 6), (30, 5), (33, 4)]
            .iter()
            .map(|&(x, y)| column(&reply.text, x, y))
            .collect();
        assert_eq!(printed, ['@', 'd', 'd', '+', '/']);
        assert!(!reply.text.contains("a dog or other canine"));
        assert_eq!(
            reply.facts.monsters[0].name.as_deref(),
            Some("tame little dog")
        );
        assert_eq!(reply.facts.stairs_up, Some(Pos::new(6, 32)));
        play.look();
        assert_eq!(play.term().sent.len(), 5);
    }

    #[test]
    fn a_farlook_answer_that_runs_to_more_is_recorded_in_full() {
        let before = || Data::load("seed4242-start");
        let prompt = || {
            before()
                .message("Pick a monster, object or location.")
                .view()
        };
        let views = vec![
            before().view(),
            prompt(),
            Data::load("seed4242-farlook-more").view(),
            before().view(),
            prompt(),
            before().message("Z        a zombie (kobold zombie)").view(),
            before().view(),
        ];
        let mut play = play(views);
        let reply = play.look();
        assert_eq!(sent(&play)[..5], [";", "@l.", " ", ";", "@j."]);
        assert!(has_line(
            &reply.text,
            "monsters: d tame little dog 19,6 adjacent l; Z kobold zombie 18,7 adjacent j"
        ));
    }

    #[test]
    fn names_never_come_from_another_monster_square_or_turn() {
        let mut views = vec![start().view()];
        views.extend(farlook("tame little dog"));
        views.extend(farlook("jackal"));
        views.push(stairs());
        let mut play = play(views);
        play.look();
        let unlooked = start().moved((6, 36), (6, 37), 2).view();
        play.term_mut().views = vec![unlooked.clone()];
        let reply = play.look();
        assert!(has_line(
            &reply.text,
            "monsters: d tame little dog (looked T:1) 32,5 adjacent k; d 37,6 5 away"
        ));
        assert_eq!(sent(&play)[5..], [";"]);
        let jackal = Pos::new(6, 37);
        let inspection = Inspection {
            description: "jackal".to_owned(),
            turn: Some(1),
            glyph: 'd',
            shade: unlooked.shade(jackal),
        };
        play.memory
            .current_mut()
            .unwrap()
            .inspections
            .insert(jackal, inspection);
        play.term_mut().views = vec![unlooked];
        assert!(!play.look().text.contains("jackal"));
    }

    #[test]
    fn naming_waits_until_the_game_is_verifiably_waiting_for_a_command() {
        let cursor = start().message("floor of a room").cursor(6, 34).view();
        assert_eq!(cursor.hero, Some(Pos::new(6, 34)));
        for unsure in [cursor, start().cursor(5, 32).view()] {
            let mut play = play(vec![unsure]);
            play.look();
            assert!(play.term().sent.is_empty());
        }
    }

    #[test]
    fn show_sends_nothing_and_marks_the_pet() {
        let mut play = play(vec![start().view()]);
        let reply = play.show();
        assert!(play.term().sent.is_empty());
        assert!(has_line(
            &reply.text,
            "monsters: d pet 32,5 adjacent k; d 36,6 4 away"
        ));
        assert!(reply.facts.monsters[0].pet);
    }

    #[test]
    fn a_look_after_a_command_does_not_repeat_its_messages() {
        let room = [" ------ ", " |.@..| ", " ------ "];
        let quiet = text("", &room, 2, 3);
        let heard = text("You hear a door open.", &room, 2, 3);
        let mut play = play(vec![quiet, heard]);
        play.show();
        let reply = play.send(b"s");
        assert_eq!(reply.code, ExitCode::Done);
        assert!(has_line(&reply.text, "msg: You hear a door open."));
        assert!(!reply.text.starts_with("send"));
        assert!(!play.look().text.contains("msg:"));
    }

    #[test]
    fn go_walks_the_nearest_named_target_and_stops_on_a_message() {
        let at = |col: usize, turn: u32, message: &str| {
            let mut row: Vec<char> = " |....>| ".chars().collect();
            row[col] = '@';
            let row: String = row.into_iter().collect();
            at_turn(
                message,
                &[" ------- ", &row, " ------- "],
                2,
                col as u8,
                turn,
            )
        };
        let mut play = play(vec![
            at(2, 400, ""),
            at(3, 401, ""),
            at(4, 402, "You hear a door open."),
        ]);
        play.show();
        let reply = play.go(">");
        assert_eq!(sent(&play), ["ml", "ml"]);
        assert_eq!(reply.code, ExitCode::Short);
        assert_eq!(
            reply.text.lines().next(),
            Some("go >: 2 steps, 2 turns, now at 4,2; stopped: a message appeared")
        );
        assert_eq!(play.last.and_then(|last| last.target), Some(Pos::new(2, 6)));
        assert!(has_line(&reply.text, "msg: You hear a door open."));
        let report = reply.facts.report.unwrap();
        assert_eq!((report.done, report.steps), (false, 2));
        for (target, error) in [
            ("altar", "no remembered"),
            ("99,1", "off the map"),
            ("4,2", "already at"),
            ("9,9", "has not been seen"),
        ] {
            let reply = play.go(target);
            assert_eq!(reply.code, ExitCode::Failed, "{target}");
            assert!(reply.text.contains(error), "{target}: {}", reply.text);
        }
        assert_eq!(play.term().sent.len(), 2);
    }

    #[test]
    fn go_names_a_refused_step_and_routes_around_it() {
        let at = |hero: (usize, usize), turn: u32, message: &str| {
            let mut rows: Vec<Vec<char>> = [" |....| ", " |..>.| "]
                .iter()
                .map(|row| row.chars().collect())
                .collect();
            rows[hero.0 - 2][hero.1] = '@';
            let rows: Vec<String> = rows
                .into_iter()
                .map(|row| row.into_iter().collect())
                .collect();
            at_turn(
                message,
                &[" ------ ", &rows[0], &rows[1], " ------ "],
                hero.0 as u8,
                hero.1 as u8,
                turn,
            )
        };
        let refused = || at((2, 3), 400, "You cannot get through.");
        // The look underfoot after the first walk, on a square never seen under the hero, answers with the
        // same screen.
        let mut play = play(vec![
            at((2, 3), 400, ""),
            refused(),
            refused(),
            at((3, 3), 401, ""),
            at((3, 4), 402, ""),
        ]);
        play.show();
        let reply = play.go(">");
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "go >: 1 step, 0 turns, now at 3,2; stopped: step 3,2->4,3 refused; routing around it"
            )
        );
        assert_eq!(reply.code, ExitCode::Short);
        let reply = play.go(">");
        assert_eq!(sent(&play), ["mn", ":", "mj", "ml"]);
        assert_eq!(
            reply.text.lines().next(),
            Some("go >: 2 steps, 2 turns, now at 4,3; done")
        );
        assert_eq!(reply.code, ExitCode::Done);
    }

    #[test]
    fn go_door_walks_beside_another_door_when_already_beside_one() {
        let mut play = play(vec![start().view()]);
        play.go("door");
        assert_eq!(
            play.last.and_then(|last| last.target),
            Some(Pos::new(5, 31))
        );
    }

    #[test]
    fn go_to_a_closed_door_square_walks_beside_it() {
        let mut play = play(vec![start().view()]);
        play.go("30,5");
        assert_eq!(
            play.last.and_then(|last| last.target),
            Some(Pos::new(5, 31))
        );
        let reply = play.go("32,7");
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(reply.text.contains("already at 32,6"), "{}", reply.text);
    }

    #[test]
    fn go_names_the_monster_blocking_the_only_route() {
        let mut play = play(vec![text("", &["", " >##F#@"], 2, 6)]);
        play.memory.ingest(&text("", &["", " >####@"], 2, 6));
        let reply = play.go(">");
        assert!(
            reply.text.contains("blocked by a monster at 4,2"),
            "{}",
            reply.text
        );
        assert!(play.term().sent.is_empty());
    }

    #[test]
    fn a_named_corridor_end_is_the_destination_and_frontier_explores_on() {
        for (target, kind) in [("5,2", Kind::Travel), ("frontier", Kind::Explore)] {
            let mut play = Play::new(ScriptedTerm::new(vec![text("", &["", "   @##"], 2, 3)]), 4);
            play.show();
            play.go(target);
            assert_eq!(play.last.map(|last| last.kind), Some(kind), "{target}");
        }
    }

    #[test]
    fn frontier_skips_the_rock_beside_a_known_corridor() {
        let mut play = play(vec![text("", &["", "   @#####"], 2, 3)]);
        assert!(has_line(&play.show().text, "frontier: 8,2 (5)"));
    }

    /// The hero walked the corridor from the stairs room to the lower room at T:43, leaving a two-wide stretch
    /// at 13,5 and 13,6 unwalked with the squares east of it never seen; every square stood on, as x,y.
    const WALK: [(usize, usize); 30] = [
        (4, 4),
        (5, 4),
        (6, 3),
        (6, 5),
        (6, 6),
        (6, 7),
        (6, 8),
        (6, 9),
        (6, 10),
        (6, 11),
        (6, 12),
        (6, 13),
        (6, 14),
        (6, 15),
        (6, 16),
        (6, 17),
        (6, 18),
        (7, 3),
        (7, 6),
        (8, 4),
        (8, 6),
        (9, 4),
        (9, 6),
        (10, 4),
        (10, 6),
        (11, 4),
        (11, 6),
        (12, 4),
        (12, 5),
        (7, 19),
    ];

    #[test]
    fn frontier_reaches_a_corridor_stretch_the_hero_never_walked() {
        let corridor = Data::load("corridor-blob");
        let mut play = play(vec![corridor.view()]);
        for &(x, y) in &WALK {
            play.memory.ingest(&corridor.walked(x, y, None, '.').view());
        }
        let reply = play.show();
        let frontier = reply
            .text
            .lines()
            .find(|line| line.starts_with("frontier: "))
            .unwrap()
            .to_owned();
        let rest = frontier
            .strip_prefix("frontier: 13,5 (")
            .or_else(|| frontier.strip_prefix("frontier: 13,6 ("))
            .and_then(|rest| rest.strip_suffix(')'));
        assert!(
            rest.is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit())),
            "{frontier}"
        );
        play.go("frontier");
        let goal = &reply.facts.frontier[0];
        let goal = Pos::new(goal.y, goal.x);
        assert_eq!(play.last.and_then(|last| last.target), Some(goal));
        assert_eq!(sent(&play)[..1], ["mk"]);
    }

    #[test]
    fn a_corridor_bend_the_walk_cut_across_is_no_frontier() {
        // The hero stepped diagonally from 4,2 to 5,3; the corner at 5,2 was never stood on.
        let rows = screen("", &["", "   ###", "     #", "     #"], STATUS1, STATUS2);
        let corridor = Data {
            rows: rows.iter().map(|row| format!("{row:<80}")).collect(),
            fg: vec![vec!["default".to_owned(); COLS]; ROWS],
            bold: vec![vec![false; COLS]; ROWS],
            rev: vec![vec![false; COLS]; ROWS],
            cursor: [2, 3],
        };
        let end: Vec<View> = [403, 404, 405]
            .iter()
            .map(|&turn| corridor.walked(5, 4, Some(turn), '#').view())
            .collect();
        let mut play = play(end);
        for (turn, (x, y)) in (400..).zip([(3, 2), (4, 2), (5, 3), (5, 4)]) {
            play.memory
                .ingest(&corridor.walked(x, y, Some(turn), '#').view());
        }
        assert!(!play.show().text.contains("frontier:"));
        play.rest(2);
        let reply = play.go("frontier");
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(
            reply
                .text
                .ends_with("corridor ends: 3,2 (searched 0), 5,4 (searched 2)"),
            "{}",
            reply.text
        );
    }

    #[test]
    fn no_frontier_lists_what_remains() {
        // The + at 8,4 is in no wall line the screen shows and on no known floor: a door or an object.
        let rows = [" -----", " |...|", " |.@.+", " |...|  +", " -----"];
        let mut play = play(vec![text("", &rows, 3, 3)]);
        play.show();
        let reply = play.go("frontier");
        assert!(
            reply
                .text
                .contains("doors: 5,3 (1), 8,4 door or object (no route)"),
            "{}",
            reply.text
        );
    }

    #[test]
    fn rest_refuses_at_a_prompt_and_reports_a_search_the_game_refused() {
        let prompt = text("What do you want to eat? [k or ?*]", &[], 0, 35);
        let mut play = play(vec![prompt]);
        let reply = play.rest(3);
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(reply.text.contains("prompt is open"));
        assert!(play.term().sent.is_empty());
        let room = [" ---- ", " |.@d| ", " ---- "];
        let refused = text(
            "You already found a monster.  Use 'm' prefix to force another search.",
            &room,
            2,
            3,
        );
        let mut play = Play::new(ScriptedTerm::new(vec![text("", &room, 2, 3), refused]), 8);
        play.show();
        let reply = play.rest(3);
        assert_eq!(reply.code, ExitCode::Failed);
        assert_eq!(reply.facts.report.and_then(|report| report.turns), Some(0));
    }

    #[test]
    fn rest_and_go_count_bounds_are_checked_before_anything_is_sent() {
        let mut play = play(vec![start().view()]);
        assert_eq!(play.rest(0).code, ExitCode::Usage);
        assert_eq!(play.rest(REST_MAX + 1).code, ExitCode::Usage);
        assert_eq!(play.send(b"").code, ExitCode::Usage);
        assert_eq!(play.send(&[b'a'; KEYS_MAX + 1]).code, ExitCode::Usage);
        assert!(play.term().sent.is_empty());
    }

    #[test]
    fn game_over_prompt_is_shown_with_its_choices_and_ends_commands() {
        let mut play = play(vec![Data::load("seed4242-quit").view()]);
        let reply = play.show();
        assert_eq!(reply.code, ExitCode::Over);
        assert!(reply.text.contains("screen (game over):"));
        assert!(has_line(&reply.text, "keys: y n q"));
        let reply = play.rest(3);
        assert_eq!(reply.code, ExitCode::Over);
        assert!(
            reply
                .text
                .starts_with("rest 3: the game is over; nothing was sent"),
            "{}",
            reply.text
        );
    }

    #[test]
    fn non_ascii_map_symbols_are_refused() {
        let dec = text("", &[" ----", " |\u{b7}@\u{b7}|"], 2, 3);
        let reply = play(vec![dec]).show();
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(reply.text.contains("ASCII"));
    }

    #[test]
    fn view_sizes_stay_within_budget() {
        for (name, budget) in [
            ("seed1001-start", 600),
            ("seed11-0240", 1300),
            ("seed7-0240", 1900),
            ("seed4242-inventory", 1300),
            ("seed4242-quit", 500),
        ] {
            let text = play(vec![Data::load(name).view()]).show().text;
            assert!(text.len() <= budget, "{name}: {}", text.len());
        }
    }

    #[test]
    fn facts_carry_the_view_in_machine_form() {
        let reply = play(vec![Data::load("craft-jackal").view()]).show();
        let facts = &reply.facts;
        assert_eq!(
            (
                facts.dlvl,
                facts.turn,
                facts.hp,
                facts.hpmax,
                facts.xl,
                facts.ac
            ),
            (Some(3), Some(400), Some(12), Some(16), Some(2), Some(6))
        );
        assert_eq!(facts.here.as_deref(), Some("unknown"));
        let json = serde_json::to_value(&reply).unwrap();
        assert_eq!(json["code"], 0);
        assert_eq!(json["facts"]["pos"], serde_json::json!([4, 3]));
        assert_eq!(
            json["facts"]["monsters"],
            serde_json::json!([{"glyph": "d", "x": 5, "y": 3, "dist": 1}])
        );
    }

    #[test]
    fn an_inventory_the_game_showed_is_part_of_later_views() {
        let mut play = play(vec![Data::load("seed4242-inventory").view()]);
        play.show();
        play.term_mut().views = vec![Data::load("seed4242-start").view()];
        let reply = play.show();
        let line = reply
            .text
            .lines()
            .find(|line| line.starts_with("inventory: "))
            .unwrap();
        assert!(line.starts_with("inventory: (T:1) "), "{line}");
        assert!(
            line.contains("a - a +0 scalpel (weapon in right hand)"),
            "{line}"
        );
        let inventory = reply.facts.inventory.unwrap();
        assert_eq!(inventory.turn, Some(1));
        assert!(
            inventory
                .lines
                .iter()
                .any(|line| line == "c - an uncursed stethoscope")
        );
    }

    #[test]
    fn a_death_pages_to_the_tombstone_and_reports_the_end() {
        let room = [" ------ ", " |.@d.| ", " ------ "];
        let tombstone: Vec<String> = [
            "",
            "                       ----------",
            "                      /          \\",
            "                     /    REST    \\",
            "                    /      IN      \\",
            "                   /     PEACE      \\",
            "                  /                  \\",
            "                  |       Hero       |",
            "                  |      0 Au        |",
            "                  |   killed by a    |",
            "                  |      jackal      |",
            "                  |                  |",
            "                  |                  |",
            "                  |       2026       |",
            "                 *|     *  *  *      | *",
            "        _________)/\\\\_//(\\/(/\\)/\\//\\/|_)_______",
            "",
            "--More--",
        ]
        .iter()
        .map(|row| (*row).to_owned())
        .collect();
        let mut play = play(vec![
            text("", &room, 2, 3),
            text("You die...--More--", &room, 0, 18),
            text(
                "Do you want your possessions identified? [ynq] (n)",
                &room,
                0,
                51,
            ),
            view(&tombstone, 17, 8),
        ]);
        let reply = play.send(b"F");
        assert_eq!(reply.code, ExitCode::Over, "{}", reply.text);
        let end = reply.facts.end.as_ref().unwrap();
        assert_eq!(end.death.as_deref(), Some("killed by a jackal"));
        assert!(
            has_line(&reply.text, "end: killed by a jackal"),
            "{}",
            reply.text
        );
        assert!(!reply.text.contains("msg:   |"), "{}", reply.text);
        assert!(has_line(
            &reply.text,
            "answered: \"Do you want your possessions identified? [ynq] (n)\" \u{2190} n"
        ));
        let reply = play.go(">");
        assert_eq!(reply.code, ExitCode::Over);
        assert!(
            reply
                .text
                .starts_with("go >: the game is over; nothing was sent")
        );
        assert!(has_line(&reply.text, "end: killed by a jackal"));
    }

    #[test]
    fn an_engraving_underfoot_is_read_again_once_the_turn_advances() {
        // A room with a bright blue engraving at 4,2, the hero beside it or on it.
        let room = |hero: usize, turn: u32, message: &str| {
            let mut row: Vec<char> = " |....| ".chars().collect();
            row[hero] = '@';
            let row: String = row.into_iter().collect();
            let status = format!("Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:{turn}");
            let rows = screen(message, &[" ------ ", &row, " ------ "], STATUS1, &status);
            let mut grid = crate::testkit::grid(&rows);
            if hero != 4 {
                grid.cells[2][4] = Cell {
                    ch: '`',
                    fg: Colour::Blue,
                    bold: true,
                    rev: false,
                };
            }
            View::parse(&grid, Pos::new(2, hero as u8))
        };
        let first = "Something is written here in the dust.  You read: \"Elbereth\".";
        let second = "Something is written here in the dust.  You read: \"Elbe?eth\".";
        let mut play = play(vec![room(4, 401, ""), room(4, 401, first)]);
        play.memory.ingest(&room(3, 400, ""));
        let reply = play.look();
        assert_eq!(sent(&play), [":"]);
        assert!(
            reply.facts.here.as_deref().unwrap().ends_with(first),
            "{}",
            reply.text
        );
        play.look();
        assert_eq!(sent(&play), [":"]);
        play.term_mut().views = vec![room(4, 402, ""), room(4, 402, second)];
        let reply = play.look();
        assert_eq!(sent(&play), [":", ":"]);
        let here = reply.facts.here.unwrap();
        assert!(here.ends_with(second) && !here.contains(first), "{here}");
    }

    #[test]
    fn a_monster_arriving_stops_a_rest_short_and_is_named_with_its_square() {
        let mut play = play(vec![start().view()]);
        play.last = Some(Last {
            kind: Kind::Search,
            target: None,
            route: 0,
        });
        let outcome = Outcome {
            stop: Stop::Changed,
            changed: exec::Changed::ENTITIES,
            arrival: Some(exec::Arrival {
                glyph: 'd',
                pos: Pos::new(8, 41),
            }),
            steps: 3,
            turns: Some(3),
            ..Outcome::new()
        };
        let report = play.report("rest 20", &outcome);
        assert!(!report.done);
        assert_eq!(
            report.stopped,
            ["a monster came into view or next to the hero (d 41,8)"]
        );
    }

    /// A room with a jackal beside the hero at 4,2, with `top` as the message and the given HP and turn.
    fn jackal(top: &str, hp: u32, turn: u32) -> View {
        let status = format!("Dlvl:3 $:0 HP:{hp}(16) Pw:2(2) AC:6 Xp:2 T:{turn}");
        let rows = [" ------- ", " |..@d.| ", " |.....| ", " ------- "];
        view(&screen(top, &rows, STATUS1, &status), 2, 4)
    }

    #[test]
    fn a_rest_interrupted_while_the_turn_stands_still_stops_short() {
        // A fast hero's search can end before the turn counter moves while monsters still act.
        let mut play = play(vec![
            jackal("", 12, 400),
            jackal("The jackal bites!", 9, 400),
        ]);
        let reply = play.rest(5);
        assert_eq!(reply.code, ExitCode::Short, "{}", reply.text);
        assert!(
            reply.text.starts_with(
                "rest 5: 1 search, 0 turns; stopped: the status line changed, a message appeared"
            ),
            "{}",
            reply.text
        );
        let calm = [" ------- ", " |..@..| ", " |.....| ", " ------- "];
        let arrived = [" ------- ", " |..@..| ", " |....d| ", " ------- "];
        let mut arrival = Play::new(
            ScriptedTerm::new(vec![text("", &calm, 2, 4), text("", &arrived, 2, 4)]),
            8,
        );
        assert_eq!(arrival.rest(5).code, ExitCode::Short);
    }

    #[test]
    fn a_rest_reports_a_hit_on_its_last_search() {
        let mut play = play(vec![
            jackal("", 12, 400),
            jackal("", 12, 401),
            jackal("The jackal bites!", 9, 402),
        ]);
        let reply = play.rest(2);
        assert_eq!(reply.code, ExitCode::Short, "{}", reply.text);
        assert!(
            reply.text.contains("2 searches, 2 turns; stopped:"),
            "{}",
            reply.text
        );
    }

    #[test]
    fn searches_that_pass_no_time_are_refused() {
        let room = [" ------- ", " |..@..| ", " ------- "];
        let mut play = play(vec![text("", &room, 2, 4)]);
        let reply = play.rest(5);
        assert_eq!(reply.code, ExitCode::Failed, "{}", reply.text);
        assert!(
            reply
                .text
                .starts_with("rest 5: 3 searches, 0 turns; stopped: no time passed"),
            "{}",
            reply.text
        );
    }

    #[test]
    fn a_count_echo_is_not_a_text_prompt() {
        let room = [" ------- ", " |..@..| ", " ------- "];
        let mut play = play(vec![
            at_turn("", &room, 2, 4, 400),
            at_turn("", &room, 2, 4, 400),
            at_turn("Count: 20", &room, 0, 9, 400),
            at_turn("", &room, 2, 4, 420),
        ]);
        let reply = play.send(b"20s");
        assert_eq!(sent(&play)[..3], ["2", "0", "s"]);
        assert!(
            reply.facts.answered.is_empty(),
            "{:?}",
            reply.facts.answered
        );
        assert!(!reply.text.contains("Count:"), "{}", reply.text);
    }

    #[test]
    fn held_input_is_reported_with_its_own_code() {
        let mut play = play(vec![start().view()]);
        play.term_mut().failures = vec![Some(LinkError::Held("waiting to start".to_owned()))];
        let reply = play.send(b"s");
        assert_eq!(reply.code, ExitCode::Held);
        assert!(reply.text.starts_with("send: "), "{}", reply.text);
        assert!(play.term().sent.is_empty());
    }

    #[test]
    fn coordinates_parse_with_spaces_and_large_numbers() {
        assert_eq!(coordinates(" 5 , 2 "), Some((5, 2)));
        assert_eq!(coordinates("999,1"), Some((999, 1)));
        assert_eq!(coordinates(">"), None);
        assert_eq!(coordinates("a,1"), None);
        assert_eq!(coordinates(",1"), None);
    }
}
