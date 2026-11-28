//! The commands a caller plays with: `look`, `send`, `go`, `rest`, `explore` and `fight`, each answered with a
//! view, and `learn`, which takes in squares the caller tells of.

use crate::exec::{self, Changed, Executor, Kind, Outcome, Stop};
use crate::glyphs::{is_engraving_text, numbered};
use crate::level::{Ground, Paths, Squares};
use crate::link::{LinkError, Term, Terminal};
use crate::observe::{Entity, EntityKind, Memory};
use crate::screen::{Phase, strip_more};
use crate::types::{Answered, COLS, ExitCode, MAP_BOTTOM, MAP_TOP, Pos, Reply, Report, SquareSet};
use crate::view::{BLANK, Landmark, Names, Renderer, Scene, landmarks};

/// Most keys one `send` takes.
pub const KEYS_MAX: usize = 256;
/// Most searches one `rest` takes.
pub const REST_MAX: u32 = 200;
/// Most steps one `go` takes.
pub const STEPS_MAX: u32 = 64;
/// Most legs one `explore` walks.
pub const LEGS_MAX: u32 = 30;
/// Most attacks one `fight` makes.
pub const SWINGS_MAX: u32 = 20;
/// Most unexplored squares a stop for no reachable frontier names.
const UNEXPLORED_SHOWN: usize = 4;
/// The refusal while a prompt is open.
pub const PROMPT_OPEN: &str = "a prompt is open: answer or cancel it with send first";

/// One game played through a terminal: memory, the executor, names and the renderer, reused across commands.
pub struct Play<T: Terminal> {
    term: T,
    memory: Memory,
    exec: Executor,
    names: Names,
    renderer: Renderer,
    fresh: Vec<String>,
    reported: u64,
    last_square: Option<(u16, Pos)>,
    unnamed: Vec<Entity>,
    landmarks: Vec<Landmark>,
    edges: Vec<(Pos, u32)>,
    answered: Vec<Answered>,
    /// The last caller action, for its report.
    last: Option<exec::Action>,
    /// Entities on screen, for `fight`'s target.
    nearby: Vec<Entity>,
}

/// The phrase for each changed observation, in the order a report lists them.
#[rustfmt::skip]
const CHANGES: [(Changed, &str); 7] = [
    (Changed::STATUS, "the status line changed"),
    (Changed::CONDITIONS, "a condition changed"),
    (Changed::ENGULFED, "the hero was engulfed"),
    (Changed::TERRAIN, "new terrain came into view"),
    (Changed::HP_FULL, "HP is full"),
    (Changed::ENTITIES, "a monster came into view or next to the hero"),
    (Changed::MESSAGE, "a message appeared"),
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
    /// Play over `term`.
    pub fn new(term: T) -> Play<T> {
        Play {
            term,
            memory: Memory::default(),
            exec: Executor::default(),
            names: Names::default(),
            renderer: Renderer::default(),
            fresh: Vec::new(),
            reported: 0,
            last_square: None,
            unnamed: Vec::new(),
            landmarks: Vec::new(),
            edges: Vec::new(),
            answered: Vec::new(),
            last: None,
            nearby: Vec::new(),
        }
    }

    pub fn term_mut(&mut self) -> &mut T {
        &mut self.term
    }

    /// Reads the screen and renders it. While the map waits for a command, it first reads the inventory (`i`)
    /// unless it holds one from this turn, names monsters by farlook (only new or moved ones, at most `FARLOOKS`)
    /// and re-reads a known engraving or object square underfoot with `:` when the turn has advanced since its last
    /// reading; all take no game time.
    pub fn look(&mut self) -> Reply {
        if let Some(reply) = self.read("look") {
            return reply;
        }
        self.reread_inventory();
        self.inquire();
        self.reply(self.over().unwrap_or(ExitCode::Done), None)
    }

    /// Renders the current screen as it stands, sending nothing: no farlooks and no look underfoot.
    pub fn show(&mut self) -> Reply {
        self.memory.ingest(self.term.view());
        self.keep_visible_names();
        self.reply(self.over().unwrap_or(ExitCode::Done), None)
    }

    /// Sends the caller's keys, 1 to `KEYS_MAX` as the request check allows, and renders the result, with every
    /// prompt they passed and its answer. A prompt whose text holds one of `guard` stops the keys before it; the
    /// rest are not sent and the prompt stays open. With `at`, the keys are typed only where the game waits at
    /// one of its texts; anywhere else none is sent.
    pub fn send(&mut self, keys: &[u8], guard: &[String], at: &[String]) -> Reply {
        if let Some(reply) = self.prepare("send") {
            return reply;
        }
        self.execute("send", exec::guarded_keys(keys, guard, at))
    }

    /// Walks toward `target` (`x,y`, a map glyph, a feature kind, or `frontier`) along the known route over
    /// `ground`: a `Wet` route may step into remembered water, a `Lava` one into remembered water and lava, an
    /// `Inland` one steps beside none. With `near`, only
    /// a monster arriving within that many squares of the hero stops the walk.
    pub fn go(
        &mut self,
        target: &str,
        ground: Ground,
        near: Option<u8>,
        avoid: &SquareSet,
    ) -> Reply {
        let mut verb = "go ".to_owned();
        match ground {
            Ground::Dry => {}
            Ground::Wet => verb.push_str("--wet "),
            Ground::Lava => verb.push_str("--lava "),
            Ground::Inland => verb.push_str("--inland "),
        }
        if let Some(near) = near {
            verb.push_str(&format!("--near {near} "));
        }
        if !avoid.is_empty() {
            verb.push_str(&format!("--avoid {} ", avoid.len()));
        }
        verb.push_str(target);
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
        match self.walk_to(target, ground, near, avoid) {
            Ok(action) => self.execute(&verb, action),
            Err(message) => Reply::message(ExitCode::Failed, message),
        }
    }

    /// The walk toward `target` over `ground` along the known route, or why there is none. A walk to a frontier
    /// square at a corridor's end follows the corridor beyond it.
    fn walk_to(
        &mut self,
        target: &str,
        ground: Ground,
        near: Option<u8>,
        avoid: &SquareSet,
    ) -> Result<exec::Action, String> {
        let mut places = std::mem::take(&mut self.landmarks);
        let chosen = self
            .places(target, ground, &mut places)
            .and_then(|()| self.choose(target, &places));
        self.landmarks = places;
        let goal = chosen?;
        let explore = target.trim().eq_ignore_ascii_case("frontier")
            && self
                .memory
                .current()
                .is_some_and(|level| level.corridor(goal) && level.corridor_links(goal).len() <= 1);
        let kind = if explore { Kind::Explore } else { Kind::Travel };
        let wet = matches!(ground, Ground::Wet | Ground::Lava);
        // Told squares to keep out of, the route is searched again around them as well as around the monsters:
        // the caller knows what this level holds that the harness cannot see in it, a Rider's reach, a row of
        // trap doors, the crowd on the stairs. A goal with no route around them is no route at all, and says so.
        let paths = match self.renderer.routes.avoiding(&self.memory, goal, avoid) {
            Some(paths) if !paths.reached().contains(goal) => {
                return Err(format!(
                    "no route to {target} that keeps out of the {} square{} told of",
                    avoid.len(),
                    if avoid.len() == 1 { "" } else { "s" }
                ));
            }
            Some(paths) => paths,
            None => &self.renderer.routes.paths,
        };
        let mut action = exec::walk(kind, paths, goal, STEPS_MAX, wet);
        action.lava = ground == Ground::Lava;
        action.near = near;
        Ok(action)
    }

    /// Walks up to `legs` legs of `go frontier`, 1 to `LEGS_MAX` as the request check allows, each to the nearest
    /// frontier by known route. A leg that ends the way a walk ends on its own (done, new terrain came into view,
    /// the corridor ends or branches, the step limit) is followed by the next. Any other stop ends the exploration
    /// with that leg's report, as do down stairs coming into view where the level showed none before, a leg that
    /// takes no step, and no frontier left. With `near`, only a monster arriving within that many squares of the
    /// hero stops a leg.
    pub fn explore(&mut self, legs: u32, near: Option<u8>) -> Reply {
        let verb = match near {
            Some(near) => format!("explore --near {near} {legs}"),
            None => format!("explore {legs}"),
        };
        if let Some(reply) = self.prepare(&verb) {
            return reply;
        }
        let view = self.term.view();
        if view.phase() != Phase::Play {
            return Reply::message(ExitCode::Failed, PROMPT_OPEN);
        }
        if view.engulfed {
            return Reply::message(ExitCode::Failed, "explore is unavailable while engulfed");
        }
        let stairs = self.stairs_down_shown();
        let mut outcome = Outcome::default();
        let mut walked = 0;
        // Why the exploration stopped, when not for its last leg's own stop; or whether it walked every leg.
        let mut end = None;
        let mut all = false;
        while walked < legs {
            let action = match self.walk_to("frontier", Ground::Dry, near, &SquareSet::new()) {
                Ok(action) => action,
                Err(why) if walked == 0 => return Reply::message(ExitCode::Failed, why),
                Err(why) => {
                    end = Some(format!("no frontier is left: {why}"));
                    break;
                }
            };
            let leg = self.exec.run(&mut self.term, &mut self.memory, &action);
            walked += 1;
            let onward = leg.steps > 0 && walks_on(&leg);
            let still = leg.steps == 0 && walks_on(&leg);
            self.last = Some(action);
            outcome.absorb(leg);
            if still {
                end = Some("a leg took no step".to_owned());
            } else if onward && !stairs && self.stairs_down_shown() {
                end = Some("down stairs came into view".to_owned());
            }
            all = onward && end.is_none() && walked == legs;
            if !onward || end.is_some() {
                break;
            }
        }
        self.finish(&verb, outcome, |report| {
            report.legs = Some(walked);
            if all {
                report.done = true;
                report.stopped.clear();
            }
            if let Some(why) = end {
                report.done = false;
                report.stopped = vec![why];
            }
        })
    }

    /// Whether the current level shows down stairs it was not told of.
    fn stairs_down_shown(&self) -> bool {
        let Some(level) = self.memory.current() else {
            return false;
        };
        (MAP_TOP..=MAP_BOTTOM).any(|y| {
            (0..COLS as u8).any(|x| {
                let p = Pos::new(y, x);
                level.terrain(p) == Some('>') && !level.told.contains(p)
            })
        })
    }

    /// Attacks toward `direction`, one of `hjklyubn`, with `F` up to `swings` times, 1 to `SWINGS_MAX` as the
    /// request check allows, while the monster shown there at the start is still shown there. It stops once HP is
    /// at or below `hp`, a prompt opens, a condition changes, or the hero is engulfed or released; while engulfed it
    /// attacks the engulfer whatever the direction, until released. It makes no attack with HP already at or below
    /// `hp`, or with no monster shown there.
    pub fn fight(&mut self, direction: u8, swings: u32, hp: i32) -> Reply {
        let verb = format!("fight {} {swings} {hp}", char::from(direction));
        if let Some(reply) = self.prepare(&verb) {
            return reply;
        }
        let view = self.term.view();
        if view.phase() != Phase::Play {
            return Reply::message(ExitCode::Failed, PROMPT_OPEN);
        }
        if let Some(low) = low_hp(view, hp) {
            return Reply::message(
                ExitCode::Failed,
                format!("{low}, at or below {hp}; no attack was made"),
            );
        }
        let (engulfed, conditions) = (view.engulfed, view.cond);
        let square = view.hero.and_then(|hero| beside(hero, direction));
        let target = match (engulfed, square) {
            (true, _) => None,
            (false, Some(square)) => match self.shown_at(square) {
                Some(entity) => Some(entity),
                None => {
                    return Reply::message(
                        ExitCode::Failed,
                        format!(
                            "no monster is shown at {square}; send F{} to attack it anyway",
                            char::from(direction)
                        ),
                    );
                }
            },
            (false, None) => return Reply::message(ExitCode::Failed, "the hero is not shown"),
        };
        let mut outcome = Outcome::default();
        let mut end = None;
        for _ in 0..swings {
            let action = exec::swing(direction);
            let swing = self.exec.run(&mut self.term, &mut self.memory, &action);
            let completed = swing.stop == Stop::Completed;
            self.last = Some(action);
            outcome.absorb(swing);
            if !completed {
                break;
            }
            let view = self.term.view();
            end = if let Some(low) = low_hp(view, hp) {
                Some(format!("{low}, at or below {hp}"))
            } else if view.engulfed != engulfed {
                Some(
                    if engulfed {
                        "the hero was released"
                    } else {
                        "the hero was engulfed"
                    }
                    .to_owned(),
                )
            } else if view.cond != conditions {
                Some("a condition changed".to_owned())
            } else if let (Some(target), Some(square)) = (target, square) {
                match self.shown_at(square) {
                    None => Some(format!("no monster is shown at {square}")),
                    Some(now) if (now.glyph, now.shade) != (target.glyph, target.shade) => {
                        Some(format!("another monster is shown at {square}"))
                    }
                    Some(_) => None,
                }
            } else {
                None
            };
            if end.is_some() {
                break;
            }
        }
        self.finish(&verb, outcome, |report| {
            if let Some(why) = end {
                report.done = false;
                report.stopped = vec![why];
            }
        })
    }

    /// The monster, or the `I` or warning digit, shown at `square`.
    fn shown_at(&mut self, square: Pos) -> Option<Entity> {
        self.memory.entities(self.term.view(), &mut self.nearby);
        self.nearby
            .iter()
            .copied()
            .find(|e| e.pos == square && matches!(e.kind, EntityKind::Monster | EntityKind::Unseen))
    }

    /// Searches in place up to `count` times, 1 to `REST_MAX` as the request check allows. With `near`, only a
    /// monster arriving within that many squares of the hero stops the rest.
    pub fn rest(&mut self, count: u32, near: Option<u8>) -> Reply {
        let verb = match near {
            Some(near) => format!("rest --near {near} {count}"),
            None => format!("rest {count}"),
        };
        if let Some(reply) = self.prepare(&verb) {
            return reply;
        }
        if self.term.view().phase() != Phase::Play {
            return Reply::message(ExitCode::Failed, PROMPT_OPEN);
        }
        let mut action = exec::search(count);
        action.near = near;
        self.execute(&verb, action)
    }

    /// Takes in the squares of the current level the caller tells of, as `Level::learn` does: `rows` are the map's
    /// rows, a space where nothing is told, and `colours` the game's colour number of each square as a hex digit.
    /// Both are the map's size, as the request check allows. Sends nothing to the game.
    pub fn learn(&mut self, rows: &[String], colours: &[String]) -> Reply {
        if let Some(reply) = self.prepare("learn") {
            return reply;
        }
        let Some(level) = self.memory.current_mut() else {
            return Reply::message(ExitCode::Failed, "learn: no level has been seen yet");
        };
        let at = |lines: &[String], p: Pos| {
            let line = lines.get(usize::from(p.y.checked_sub(MAP_TOP)?))?;
            line.as_bytes().get(usize::from(p.x)).copied()
        };
        let learned = level.learn(|p| {
            let ch = at(rows, p).filter(|&ch| ch != b' ')?;
            let colour = char::from(at(colours, p)?).to_digit(16)?;
            Some((char::from(ch), numbered(u8::try_from(colour).ok()?)?))
        });
        let squares = if learned == 1 { "square" } else { "squares" };
        Reply::message(ExitCode::Done, format!("learned {learned} {squares}"))
    }

    /// The places a `go` target names on the current level, written into `out`; an error says why none match. It
    /// leaves the routes from the hero around monsters over `ground` in the play's path buffer: for `frontier`,
    /// routes that enter no square the caller told of.
    pub fn places(
        &mut self,
        target: &str,
        ground: Ground,
        out: &mut Vec<Landmark>,
    ) -> Result<(), String> {
        out.clear();
        let word = target.trim().to_lowercase();
        let squares = if word == "frontier" {
            Squares::Shown
        } else {
            Squares::Known
        };
        let routes = &mut self.renderer.routes;
        routes.from_hero(&self.memory, self.term.view(), ground, squares);
        let Some(level) = self.memory.current() else {
            return Err(PROMPT_OPEN.to_owned());
        };
        if let Some((x, y)) = coordinates(target) {
            let p = match (u8::try_from(y), u8::try_from(x)) {
                (Ok(y), Ok(x)) if Pos::new(y, x).on_map() => Pos::new(y, x),
                _ => return Err(format!("{target} is off the map")),
            };
            if level.terrain(p) == Some('+') {
                out.push(Landmark::door(p));
            } else {
                out.push(Landmark::at(p, None, "chosen square"));
            }
            return Ok(());
        }
        if word == "frontier" {
            level.frontier_targets(&routes.paths, &mut self.edges);
            for &(p, _) in &self.edges {
                out.push(Landmark::at(p, None, "frontier"));
            }
            return Ok(());
        }
        landmarks(level, &routes.entities, out);
        out.retain(|place| {
            place.glyph.is_some_and(|g| word.chars().eq([g]))
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

    /// Why no known route over the ground of the last search reaches `target`: the monsters standing on every route
    /// over terrain, for an `Inland` search a route over terrain only beside water, the target never seen, or no
    /// route at all, with the closed door a target with one goal lies behind.
    pub fn unreachable(&mut self, target: &str, places: &[Landmark]) -> String {
        let routes = &mut self.renderer.routes;
        let frontier = target.trim().eq_ignore_ascii_case("frontier");
        let none = if frontier {
            "no reachable known square borders unexplored ones".to_owned()
        } else {
            format!("no known route to {target}")
        };
        let (Some(level), Some(hero)) = (self.memory.current(), self.term.view().hero) else {
            return none;
        };
        let occupied = routes.occupied.clone();
        let terrain = routes.terrain(level, hero);
        let mut blockers = SquareSet::new();
        let mut route = Vec::new();
        let mut goals = Vec::new();
        if frontier {
            goals.extend(
                terrain
                    .reached()
                    .iter()
                    .filter(|&p| level.frontier_sensed(p)),
            );
        } else {
            goals.extend(places.iter().flat_map(|place| place.goals.as_slice()));
        }
        for goal in goals {
            if terrain.route(goal, &mut route) {
                for &q in route.iter().filter(|&&q| occupied.contains(q)) {
                    blockers.insert(q);
                }
            }
        }
        if !blockers.is_empty() {
            let at: Vec<String> = blockers.iter().take(3).map(|p| p.to_string()).collect();
            let at = at.join(" or ");
            return format!("the known route to {target} is blocked by a monster at {at}");
        }
        if routes.ground() == Ground::Inland {
            let squares = if frontier {
                Squares::Shown
            } else {
                Squares::Known
            };
            let mut dry = Paths::default();
            level.paths(hero, &SquareSet::new(), Ground::Dry, squares, &mut dry);
            let ashore = if frontier {
                dry.reached().iter().any(|p| level.frontier_sensed(p))
            } else {
                places
                    .iter()
                    .flat_map(|place| place.goals.as_slice())
                    .any(|&goal| dry.reaches(goal))
            };
            if ashore {
                return format!("every known route to {target} passes beside water");
            }
        }
        if let [place] = places
            && place.label == "chosen square"
            && level.terrain(place.pos).is_none()
            && !level.object_squares.contains(place.pos)
        {
            return format!("{target} has not been seen");
        }
        let behind = places
            .iter()
            .filter(|place| !frontier && place.goals.as_slice() == [place.pos])
            .find_map(|place| routes.behind(level, hero, place.pos));
        match behind {
            Some(door) => format!("{none}: it is behind the closed door at {door}"),
            None if frontier => {
                // What is still unexplored, so a stop beside ground no one looked at shows where it is.
                let mut left = Vec::new();
                level.unexplored(hero, UNEXPLORED_SHOWN + 1, &mut left);
                if left.is_empty() {
                    return none;
                }
                let mut names: Vec<String> = left
                    .iter()
                    .take(UNEXPLORED_SHOWN)
                    .map(|&(q, sensed)| {
                        if sensed {
                            format!("{q} (only a monster showed there)")
                        } else {
                            q.to_string()
                        }
                    })
                    .collect();
                if left.len() > UNEXPLORED_SHOWN {
                    names.push("more".to_owned());
                }
                format!(
                    "{none}; unexplored beside remembered ground, none reachable: {}",
                    names.join(", ")
                )
            }
            None => none,
        }
    }

    pub fn report(&self, verb: &str, outcome: &Outcome) -> Report {
        let mut report = Report {
            verb: verb.to_owned(),
            steps: outcome.steps,
            turns: outcome.turns,
            ..Report::default()
        };
        let (kind, target, route) = self.last.as_ref().map_or((Kind::Keys, None, 0), |last| {
            (last.kind, last.target, last.route.len())
        });
        if self.last.as_ref().is_some_and(|last| last.wet) {
            report.wet = Some(outcome.wet);
        }
        let walked = matches!(kind, Kind::Travel | Kind::Explore);
        if walked {
            report.told = Some(outcome.told);
        }
        if kind == Kind::Search && outcome.turns.is_some() {
            report.no_time = Some(outcome.no_time);
            report.no_time_why.clone_from(&outcome.no_time_why);
        }
        let near = self.last.as_ref().and_then(|last| last.near);
        let stopped = &mut report.stopped;
        match &outcome.stop {
            _ if refused_search(kind, outcome) => {
                stopped.push(format!("no time passed{}", self.beside_hero()));
            }
            Stop::Link(error) => report.error = Some(error.to_string()),
            Stop::Failed(error) => report.error = Some(error.clone()),
            Stop::Guarded { prompt, unsent } => {
                let keys = if *unsent == 1 { "key was" } else { "keys were" };
                stopped.push(format!(
                    "the guarded prompt \"{prompt}\" opened; {unsent} {keys} not sent"
                ));
            }
            Stop::Elsewhere {
                waiting,
                expected,
                unsent,
            } => {
                let keys = if *unsent == 1 { "key was" } else { "keys were" };
                stopped.push(format!(
                    "the game waits at \"{waiting}\", not at {expected}; {unsent} {keys} not sent"
                ));
            }
            // The caller's own keys are done once sent, unless the game ended.
            stop if kind == Kind::Keys && *stop != Stop::GameOver => report.done = true,
            Stop::Completed if walked && target.is_some() && self.term.view().hero != target => {
                let short = route.saturating_sub(outcome.steps as usize);
                stopped.push(format!("it reached its step limit, {short} squares short"));
            }
            Stop::Completed => report.done = true,
            Stop::Refused { from, to } => {
                stopped.push(format!("step {from}->{to} refused; routing around it"));
            }
            Stop::WetRefused { from, to } => {
                stopped.push(format!("wet step {from}->{to} refused"));
            }
            Stop::Changed if !outcome.changed.is_empty() => {
                for (flag, phrase) in CHANGES {
                    if !outcome.changed.contains(flag) {
                        continue;
                    }
                    match (outcome.arrival, near) {
                        (Some(arrival), Some(near)) if flag == Changed::ENTITIES => {
                            let (glyph, pos, dist) =
                                (shown(arrival.glyph), arrival.pos, arrival.dist);
                            stopped.push(format!(
                                "a monster came within {near} squares of the hero ({glyph} {pos}, {dist} away)"
                            ));
                        }
                        (Some(arrival), None) if flag == Changed::ENTITIES => {
                            stopped.push(format!(
                                "{phrase} ({} {})",
                                shown(arrival.glyph),
                                arrival.pos
                            ));
                        }
                        _ => stopped.push(phrase.to_owned()),
                    }
                }
            }
            Stop::MovementInterrupted => stopped.push(format!(
                "the hero did not arrive where expected{}",
                self.beside_hero()
            )),
            stop => stopped.push(
                match stop {
                    Stop::StepLimit => "it reached its step limit",
                    Stop::Prompt => "a prompt opened",
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
        report
    }

    /// The monsters beside the hero on the screen, other than the pet, as `; beside the hero: ` and each one's
    /// glyph, kept farlook name and square; "" for none.
    fn beside_hero(&self) -> String {
        let view = self.term.view();
        let (Some(level), Some(hero)) = (self.memory.current(), view.hero) else {
            return String::new();
        };
        let mut entities = Vec::new();
        self.memory.entities(view, &mut entities);
        let mut out = String::new();
        let beside = entities
            .iter()
            .filter(|e| e.kind == EntityKind::Monster && !e.pet && e.pos.chebyshev(hero) == 1);
        for (i, entity) in beside.enumerate() {
            out.push_str(if i == 0 { "; beside the hero: " } else { ", " });
            let name = self.names.get(level.id, entity).map(|(name, _)| name);
            match (entity.glyph, name) {
                (' ', Some(name)) => out.push_str(&format!("{name} {BLANK}")),
                (' ', None) => out.push_str(BLANK),
                (glyph, Some(name)) => out.push_str(&format!("{glyph} {name}")),
                (glyph, None) => out.push(glyph),
            }
            out.push_str(&format!(" {}", entity.pos));
        }
        out
    }

    /// Whether the game verifiably waits for a command: no prompt, the cursor on `@`, the status lines shown.
    pub fn plain(&self) -> bool {
        let view = self.term.view();
        view.phase() == Phase::Play
            && !view.engulfed
            && view.hero == Some(view.cursor)
            && view.ch(view.cursor) == '@'
            && !self.term.prompts().position
            && view.status.hp.is_some()
            && view.status.turn.is_some()
            && view.status.location.is_some()
    }

    /// Reads the screen into memory, paging through the end of a game that has just ended. A link failure is
    /// answered here; once the game has ended, with the last screen and the end text. A game that has ended exits
    /// after its last screen, so its terminal closing then leaves nothing to report.
    fn read(&mut self, verb: &str) -> Option<Reply> {
        if let Err(error) = self.term.poll() {
            return match self.over() {
                Some(_) if matches!(error, LinkError::Closed(_)) => None,
                Some(code) => Some(self.reply(code, Some(failed(verb, &error.to_string())))),
                None => Some(Reply::message(error.code(), format!("{verb}: {error}"))),
            };
        }
        self.memory.ingest(self.term.view());
        if self.term.view().ended {
            self.finish_game();
        }
        None
    }

    /// Reads the screen before a command; a link failure or a finished game is answered here, sending nothing.
    fn prepare(&mut self, verb: &str) -> Option<Reply> {
        if let Some(reply) = self.read(verb) {
            return Some(reply);
        }
        let code = self.over()?;
        let report = failed(verb, "the game is over; nothing was sent");
        Some(self.reply(code, Some(report)))
    }

    /// Runs a caller's action and answers with its report and the view after it.
    fn execute(&mut self, verb: &str, action: exec::Action) -> Reply {
        let outcome = self.exec.run(&mut self.term, &mut self.memory, &action);
        self.last = Some(action);
        self.finish(verb, outcome, |_| {})
    }

    /// Answers the last caller action, which did `outcome`, with its report, as `adjust` changes it, and the view
    /// after it.
    fn finish(
        &mut self,
        verb: &str,
        mut outcome: Outcome,
        adjust: impl FnOnce(&mut Report),
    ) -> Reply {
        let kind = self.last.as_ref().map_or(Kind::Keys, |last| last.kind);
        let refused = refused_search(kind, &outcome);
        self.fresh.clear();
        let lines = outcome.messages_left_out;
        if lines > 0 {
            self.fresh.push(format!("({lines} earlier lines left out)"));
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
        let mut report = self.report(verb, &outcome);
        adjust(&mut report);
        let code = match &outcome.stop {
            Stop::Link(error) => error.code(),
            Stop::Failed(_) => ExitCode::Failed,
            Stop::GameOver => ExitCode::Over,
            _ if refused => ExitCode::Failed,
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

    /// Renders the view with `report` under `code`; a refused view is answered with its reason instead.
    fn reply(&mut self, code: ExitCode, report: Option<Report>) -> Reply {
        for message in self.memory.messages_since(self.reported) {
            let text = strip_more(&message.text).trim();
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
        let rendered = self.renderer.render(&scene);
        self.fresh.clear();
        self.answered.clear();
        match rendered {
            Ok((text, facts)) => Reply { code, text, facts },
            Err(message) if code == ExitCode::Done => Reply::message(ExitCode::Failed, message),
            Err(message) => Reply::message(code, message),
        }
    }

    /// The square to walk to: the nearest goal of `places` a route reaches, preferring one the hero is not on.
    fn choose(&mut self, target: &str, places: &[Landmark]) -> Result<Pos, String> {
        let mut best: Option<(u32, Pos)> = None;
        let mut here: Option<Pos> = None;
        for place in places {
            for &goal in place.goals.as_slice() {
                match self.renderer.routes.paths.distance(goal) {
                    Some(0) if here.is_none_or(|h| goal < h) => here = Some(goal),
                    Some(n) if best.is_none_or(|b| (n, goal) < b) => best = Some((n, goal)),
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

    /// Reads the inventory while the game waits for a command, unless memory holds one from this turn.
    fn reread_inventory(&mut self) {
        let turn = self.term.view().status.turn;
        if !self.plain()
            || self
                .memory
                .inventory()
                .is_some_and(|held| held.turn == turn)
        {
            return;
        }
        let action = exec::inventory();
        self.exec.run(&mut self.term, &mut self.memory, &action);
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
        let (true, Some(level)) = (view.normal, self.memory.current()) else {
            return;
        };
        let entities = &mut self.renderer.routes.entities;
        self.memory.entities(view, entities);
        self.names.retain_visible(level.id, entities);
    }

    /// Names each monster in view by farlook. A name is kept while the monster stays on its square looking the
    /// same; one that moved or arrived is looked at again, nearest first, at most `FARLOOKS` per look.
    fn name_monsters(&mut self) {
        self.keep_visible_names();
        let view = self.term.view();
        let (Some(level), Some(hero)) = (self.memory.current().map(|level| level.id), view.hero)
        else {
            return;
        };
        let turn = view.status.turn;
        let entities = &self.renderer.routes.entities;
        self.names.unnamed(level, hero, entities, &mut self.unnamed);
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
    /// or listed under `items:`, on a known engraving square whenever the turn has advanced since its last
    /// reading, and once the game reports an object reaching or leaving the square. Memory makes the reading
    /// what is noted there, so a scuffed or wiped engraving is not kept, nor an object no longer there.
    fn look_underfoot(&mut self) {
        let view = self.term.view();
        let (Some(level), Some(hero)) = (self.memory.current(), view.hero) else {
            return;
        };
        let arrived = self.last_square != Some((level.id, hero));
        self.last_square = Some((level.id, hero));
        let known = level.underfoot.get(&hero);
        let stale = known.is_some_and(|known| known.stale);
        if !stale && known.is_some_and(|known| known.turn == view.status.turn) {
            return;
        }
        let object =
            level.inferred_floor.contains(hero) || self.renderer.listed_item(level.id, hero);
        let engraving = level.engravings.contains(hero)
            || known.is_some_and(|known| known.notes.iter().any(|note| is_engraving_text(note)));
        if arrived && object || engraving || stale {
            let action = exec::look_here();
            self.exec.run(&mut self.term, &mut self.memory, &action);
        }
    }
}

impl Play<Term> {
    /// Brings a screen that was never completely drawn in step with the game (`Term::sync`) and dismisses the
    /// `--More--` pages it opens with, such as the game's welcome, keeping their messages for the next view.
    pub fn sync(&mut self) -> Result<(), LinkError> {
        if self.term.complete() {
            return Ok(());
        }
        self.term.sync()?;
        self.exec.dismiss(&mut self.term, &mut self.memory)
    }
}

/// Whether a walk leg ended the way a walk ends on its own, so an exploration walks on.
fn walks_on(leg: &Outcome) -> bool {
    match leg.stop {
        Stop::Completed
        | Stop::StepLimit
        | Stop::FeatureDiscovered
        | Stop::BranchDiscovered
        | Stop::CorridorEnds => true,
        Stop::Changed => leg.changed == Changed::TERRAIN,
        _ => false,
    }
}

/// `HP h of m` once the status line shows HP at or below `floor`.
fn low_hp(view: &crate::screen::View, floor: i32) -> Option<String> {
    let (hp, most) = (view.status.hp?, view.status.hpmax?);
    (hp <= floor).then(|| format!("HP {hp} of {most}"))
}

/// The square beside `hero` toward `direction`, one of `hjklyubn`, while on the map.
fn beside(hero: Pos, direction: u8) -> Option<Pos> {
    let (dx, dy): (i16, i16) = match direction {
        b'h' => (-1, 0),
        b'j' => (0, 1),
        b'k' => (0, -1),
        b'l' => (1, 0),
        b'y' => (-1, -1),
        b'u' => (1, -1),
        b'b' => (-1, 1),
        b'n' => (1, 1),
        _ => return None,
    };
    let x = u8::try_from(i16::from(hero.x) + dx).ok()?;
    let y = u8::try_from(i16::from(hero.y) + dy).ok()?;
    let p = Pos::new(y, x);
    p.on_map().then_some(p)
}

fn failed(verb: &str, error: &str) -> Report {
    Report {
        verb: verb.to_owned(),
        error: Some(error.to_owned()),
        ..Report::default()
    }
}

/// A monster's glyph as a stop names it: `blank` for one the screen draws blank.
fn shown(glyph: char) -> String {
    if glyph == ' ' {
        "blank".to_owned()
    } else {
        glyph.to_string()
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
            Stop::Changed => outcome.changed == Changed::MESSAGE,
            _ => false,
        }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::level::Inspection;
    use crate::screen::View;
    use crate::testkit::{STATUS1, STATUS2, ScriptedTerm, map, screen, view};
    use crate::types::{COLS, Cell, Colour, Grid, MAP_ROWS, NoTime, NoTimeWhy, ROWS};
    use serde::Deserialize;

    /// A recorded screen, editable before it is parsed.
    #[derive(Clone, Deserialize)]
    struct Data {
        rows: Vec<String>,
        fg: Vec<Vec<Colour>>,
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
            let fg = data.fg[from.0][from.1];
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
                        fg: self.fg[r][c],
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
    /// A text screen at turn `turn`.
    fn at_turn(top: &str, rows: &[&str], y: u8, x: u8, turn: u32) -> View {
        let status = format!("Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:{turn}");
        view(&screen(top, rows, STATUS1, &status), y, x)
    }

    /// Play over `views`, holding an inventory read on the first view's turn, so a look reads none.
    fn play(views: Vec<View>) -> Play<ScriptedTerm> {
        let turn = views.first().and_then(|view| view.status.turn);
        let mut play = Play::new(ScriptedTerm::new(views));
        play.memory.hold_inventory(turn);
        play
    }

    fn sent(play: &Play<ScriptedTerm>) -> Vec<String> {
        play.term
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
        // The monsters come above the map, the hero's square below it.
        let you = "you: 32,6  here: < up stairs; There is a staircase up out of the dungeon here.";
        let below = lines.iter().position(|line| *line == you).unwrap();
        assert!(
            lines[at + 1].starts_with("   30") && at < below,
            "{}",
            reply.text
        );
        assert!(has_line(
            &reply.text,
            "seen: + door 32,7 (adjacent j), 30,5 (1)"
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
        assert_eq!(play.term.sent.len(), 5);
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
        play.term.views = vec![unlooked.clone()];
        play.memory.hold_inventory(Some(2));
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
        play.term.views = vec![unlooked];
        assert!(!play.look().text.contains("jackal"));
    }

    #[test]
    fn naming_waits_until_the_game_is_verifiably_waiting_for_a_command() {
        let cursor = start().message("floor of a room").cursor(6, 34).view();
        assert_eq!(cursor.hero, Some(Pos::new(6, 34)));
        for unsure in [cursor, start().cursor(5, 32).view()] {
            let mut play = play(vec![unsure]);
            play.look();
            assert!(play.term.sent.is_empty());
        }
    }

    #[test]
    fn show_sends_nothing_and_marks_the_pet() {
        let mut play = play(vec![start().view()]);
        let reply = play.show();
        assert!(play.term.sent.is_empty());
        assert!(has_line(
            &reply.text,
            "monsters: d pet 32,5 adjacent k; d 36,6 4 away"
        ));
        assert!(reply.facts.monsters[0].pet);
    }

    #[test]
    fn a_look_after_a_command_does_not_repeat_its_messages() {
        let room = [" ------ ", " |.@..| ", " ------ "];
        let quiet = map("", &room, 2, 3);
        let heard = map("You hear a door open.", &room, 2, 3);
        let mut play = play(vec![quiet, heard]);
        play.show();
        let reply = play.send(b"s", &[], &[]);
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
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
        assert_eq!(sent(&play), ["ml", "ml"]);
        assert_eq!(reply.code, ExitCode::Short);
        assert_eq!(
            reply.text.lines().next(),
            Some("go >: 2 steps, 2 turns, now at 4,2; stopped: a message appeared")
        );
        assert_eq!(
            play.last.as_ref().and_then(|last| last.target),
            Some(Pos::new(2, 6))
        );
        assert!(has_line(&reply.text, "msg: You hear a door open."));
        let report = reply.facts.report.unwrap();
        assert_eq!((report.done, report.steps), (false, 2));
        for (target, error) in [
            ("altar", "no remembered"),
            ("99,1", "off the map"),
            ("4,2", "already at"),
            ("9,9", "has not been seen"),
        ] {
            let reply = play.go(target, Ground::Dry, None, &SquareSet::new());
            assert_eq!(reply.code, ExitCode::Failed, "{target}");
            assert!(reply.text.contains(error), "{target}: {}", reply.text);
        }
        assert_eq!(play.term.sent.len(), 2);
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
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "go >: 1 step, 0 turns, now at 3,2; stopped: step 3,2->4,3 refused; routing around it"
            )
        );
        assert_eq!(reply.code, ExitCode::Short);
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
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
        play.go("door", Ground::Dry, None, &SquareSet::new());
        assert_eq!(
            play.last.as_ref().and_then(|last| last.target),
            Some(Pos::new(5, 31))
        );
    }

    #[test]
    fn go_to_a_closed_door_square_walks_beside_it() {
        let mut play = play(vec![start().view()]);
        play.go("30,5", Ground::Dry, None, &SquareSet::new());
        assert_eq!(
            play.last.as_ref().and_then(|last| last.target),
            Some(Pos::new(5, 31))
        );
        let reply = play.go("32,7", Ground::Dry, None, &SquareSet::new());
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(reply.text.contains("already at 32,6"), "{}", reply.text);
    }

    #[test]
    fn go_names_the_monster_blocking_the_only_route() {
        let mut play = play(vec![map("", &["", " >##F#@"], 2, 6)]);
        play.memory.ingest(&map("", &["", " >####@"], 2, 6));
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
        assert!(
            reply.text.contains("blocked by a monster at 4,2"),
            "{}",
            reply.text
        );
        assert!(play.term.sent.is_empty());
    }

    /// `avoid` keeps the route out of squares only the caller knows about: a monster's reach, a row of trap
    /// doors, a crowded stair. The harness cannot see why they are dangerous, so it is told.
    #[test]
    fn go_routes_around_the_squares_it_is_told_to_avoid() {
        // A three-row room, the hero at 3,3 and the target two steps west at
        // 1,3: one way straight along row 3, others around by rows 2 and 4.
        let room = || map("", &["", " .......", " ..@....", " ......."], 3, 3);
        let squares = |list: &[(u8, u8)]| {
            let mut out = SquareSet::new();
            for &(x, y) in list {
                out.insert(Pos::new(y, x));
            }
            out
        };
        // The route the walk planned, which is what `avoid` decides; the
        // scripted terminal never moves the hero, so the walk itself stops at
        // its first step either way.
        let route = |p: &Play<ScriptedTerm>| {
            p.last
                .as_ref()
                .map(|last| last.route.clone())
                .unwrap_or_default()
        };
        // Told nothing, the route goes straight along the hero's own row.
        let mut p = play(vec![room()]);
        p.go("1,3", Ground::Dry, None, &SquareSet::new());
        assert_eq!(route(&p), [Pos::new(3, 2), Pos::new(3, 1)]);
        // Told to keep off the square between, it goes round by another row.
        let mut p = play(vec![room()]);
        p.go("1,3", Ground::Dry, None, &squares(&[(2, 3)]));
        let around = route(&p);
        assert!(
            !around.is_empty()
                && !around.contains(&Pos::new(3, 2))
                && around.last() == Some(&Pos::new(3, 1)),
            "routed {around:?}; want a way to 1,3 that never enters 2,3"
        );
        // Walled in by what it was told to avoid, the walk is refused and says
        // so rather than taking a route through them.
        let mut p = play(vec![room()]);
        let reply = p.go(
            "1,3",
            Ground::Dry,
            None,
            &squares(&[(2, 2), (2, 3), (2, 4)]),
        );
        assert_eq!(reply.code, ExitCode::Failed, "{}", reply.text);
        assert!(
            reply
                .text
                .contains("no route to 1,3 that keeps out of the 3 squares told of"),
            "{}",
            reply.text
        );
        assert!(p.term.sent.is_empty(), "sent {:?}", p.term.sent);
        // The target itself is never avoided: a caller that names the square it
        // walks to still gets there, straight.
        let mut p = play(vec![room()]);
        p.go("1,3", Ground::Dry, None, &squares(&[(1, 3)]));
        assert_eq!(route(&p), [Pos::new(3, 2), Pos::new(3, 1)]);
    }

    #[test]
    fn a_named_corridor_end_is_the_destination_and_frontier_explores_on() {
        for (target, kind) in [("5,2", Kind::Travel), ("frontier", Kind::Explore)] {
            let mut play = Play::new(ScriptedTerm::new(vec![map("", &["", "   @##"], 2, 3)]));
            play.show();
            play.go(target, Ground::Dry, None, &SquareSet::new());
            assert_eq!(
                play.last.as_ref().map(|last| last.kind),
                Some(kind),
                "{target}"
            );
        }
    }

    #[test]
    fn frontier_skips_the_rock_beside_a_known_corridor() {
        let mut play = play(vec![map("", &["", "   @#####"], 2, 3)]);
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
        play.go("frontier", Ground::Dry, None, &SquareSet::new());
        let goal = &reply.facts.frontier[0];
        let goal = Pos::new(goal.y, goal.x);
        assert_eq!(play.last.as_ref().and_then(|last| last.target), Some(goal));
        assert_eq!(sent(&play)[..1], ["mk"]);
    }

    #[test]
    fn a_corridor_bend_the_walk_cut_across_is_no_frontier() {
        // The hero stepped diagonally from 4,2 to 5,3; the corner at 5,2 was never stood on.
        let rows = screen("", &["", "   ###", "     #", "     #"], STATUS1, STATUS2);
        let corridor = Data {
            rows: rows.iter().map(|row| format!("{row:<80}")).collect(),
            fg: vec![vec![Colour::Default; COLS]; ROWS],
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
        play.rest(2, None);
        let reply = play.go("frontier", Ground::Dry, None, &SquareSet::new());
        assert_eq!(reply.code, ExitCode::Failed);
        assert_eq!(
            reply.text,
            "no reachable known square borders unexplored ones"
        );
    }

    #[test]
    fn no_frontier_says_so_or_names_the_monster_in_the_way() {
        let rows = [" -----", " |...|", " |.@.+", " |...|", " -----"];
        let mut play = play(vec![map("", &rows, 3, 3)]);
        play.show();
        let reply = play.go("frontier", Ground::Dry, None, &SquareSet::new());
        assert_eq!(reply.code, ExitCode::Failed);
        assert_eq!(
            reply.text,
            "no reachable known square borders unexplored ones"
        );
        // The jackal stands in the doorway the hero saw empty a turn before.
        let empty = [" -----", " |...|", " |.@..####", " |...|", " -----"];
        let rows = [" -----", " |...|", " |.@.d####", " |...|", " -----"];
        let mut blocked = Play::new(ScriptedTerm::new(vec![map("", &rows, 3, 3)]));
        blocked.memory.ingest(&map("", &empty, 3, 3));
        blocked.show();
        let reply = blocked.go("frontier", Ground::Dry, None, &SquareSet::new());
        assert_eq!(
            reply.text,
            "the known route to frontier is blocked by a monster at 5,3"
        );
        // Ground seen past the room's wall, beside squares no one looked at, but no route reaches it: the stop
        // names them, nearest first.
        let rows = [" -----", " |.@.|  .", " -----"];
        let mut cut = Play::new(ScriptedTerm::new(vec![map("", &rows, 2, 3)]));
        cut.show();
        let reply = cut.go("frontier", Ground::Dry, None, &SquareSet::new());
        assert_eq!(
            reply.text,
            "no reachable known square borders unexplored ones; unexplored beside remembered ground, none \
             reachable: 7,2, 8,1, 8,3, 9,2"
        );
    }

    #[test]
    fn stairs_with_no_route_name_the_closed_door_they_lie_behind() {
        let rows = [
            "",
            " -----     -------",
            " |.@.|     |..>..|",
            " |...|     +.....+",
            " -----     -------",
        ];
        let mut play = Play::new(ScriptedTerm::new(vec![map("", &rows, 3, 3)]));
        let look = play.show();
        // Of the stairs' room's two doors, the one nearer the hero.
        assert!(
            look.text
                .lines()
                .any(|line| line.starts_with("seen: > down stairs 14,3 (behind closed door 11,4);")),
            "{}",
            look.text
        );
        assert_eq!(look.facts.seen[0].behind, Some(Pos::new(4, 11)));
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
        assert_eq!(reply.code, ExitCode::Failed);
        assert_eq!(
            reply.text,
            "no known route to >: it is behind the closed door at 11,4"
        );
        // Without a closed door on the edge of the stairs' area, there is no route and nothing more.
        let walls = rows.map(|row| row.replace('+', "|"));
        let walls = walls.each_ref().map(String::as_str);
        let mut walled = Play::new(ScriptedTerm::new(vec![map("", &walls, 3, 3)]));
        assert!(walled.show().text.contains("> down stairs 14,3 (no route)"));
        assert_eq!(
            walled.go("14,3", Ground::Dry, None, &SquareSet::new()).text,
            "no known route to 14,3"
        );
        assert!(play.term.sent.is_empty() && walled.term.sent.is_empty());
    }

    #[test]
    fn told_squares_are_walked_and_marked_but_leave_the_map_and_the_frontier_as_shown() {
        let corridor = |x: u8, row: &str| at_turn("", &["", row], 2, x, 397 + u32::from(x));
        let start = || vec![corridor(3, "   @##")];
        let (mut shown, mut play) = (play(start()), play(start()));
        let rows: Vec<String> = (0..MAP_ROWS)
            .map(|r| format!("{:<80}", if r == 1 { "   @#####>" } else { "" }))
            .collect();
        let reply = play.learn(&rows, &vec!["7".repeat(COLS); MAP_ROWS]);
        assert_eq!(
            (reply.code, reply.text.as_str()),
            (ExitCode::Done, "learned 4 squares")
        );
        assert!(play.term.sent.is_empty());
        let (before, after) = (shown.look(), play.look());
        let unmarked = |text: &str| -> Vec<String> {
            let lines = text.lines().filter(|line| !line.starts_with("seen:"));
            lines.map(str::to_owned).collect()
        };
        assert_eq!(unmarked(&after.text), unmarked(&before.text));
        assert!(has_line(&after.text, "frontier: 5,2 (2)"));
        assert!(has_line(&after.text, "seen: > down stairs 9,2 (6) (told)"));
        assert!(after.facts.seen[0].told && after.facts.stairs_down.is_none());
        // The screen shows each told square as the hero comes beside it.
        play.term.views.extend([
            corridor(4, "   #@#"),
            corridor(5, "   ##@#"),
            corridor(6, "   ###@#"),
            corridor(7, "   ####@#"),
            corridor(8, "   #####@>"),
            corridor(9, "   ######@"),
        ]);
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
        assert_eq!(
            reply.text.lines().next(),
            Some("go >: 6 steps, 4 told, 6 turns, now at 9,2; done")
        );
        assert_eq!(reply.facts.report.and_then(|report| report.told), Some(4));
        assert!(!reply.text.contains("(told)"), "{}", reply.text);
        assert_eq!(reply.facts.stairs_down, Some(Pos::new(2, 9)));
        let reply = shown.go("frontier", Ground::Dry, None, &SquareSet::new());
        assert_eq!(reply.facts.report.and_then(|report| report.told), Some(0));
    }

    #[test]
    fn rest_refuses_at_a_prompt() {
        let prompt = map("What do you want to eat? [k or ?*]", &[], 0, 35);
        let mut play = play(vec![prompt]);
        let reply = play.rest(3, None);
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(reply.text.contains("prompt is open"));
        assert!(play.term.sent.is_empty());
    }

    #[test]
    fn game_over_prompt_is_shown_with_its_choices_and_ends_commands() {
        let mut play = play(vec![Data::load("seed4242-quit").view()]);
        let reply = play.show();
        assert_eq!(reply.code, ExitCode::Over);
        assert!(reply.text.contains("screen (game over):"));
        assert!(has_line(&reply.text, "keys: y n q"));
        let reply = play.rest(3, None);
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
        let dec = map("", &[" ----", " |\u{b7}@\u{b7}|"], 2, 3);
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
        play.term.views = vec![Data::load("seed4242-start").view()];
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
    fn a_death_pages_to_the_score_list_and_reports_the_end() {
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
        let mut closing = vec![String::new(); ROWS];
        closing[23] = "--More--".to_owned();
        let mut list = vec![String::new(); ROWS];
        list[2] = " No  Points     Name                                                   Hp [max]"
            .to_owned();
        list[3] = "             0  Hero-Val-Hum-Fem-Law died in The Dungeons of Doom on".to_owned();
        list[4] = "                level 1.  Killed by a jackal, while sleeping.           -  [16]"
            .to_owned();
        let mut scores = crate::testkit::grid(&list);
        for row in &mut scores.cells[3..5] {
            for cell in row.iter_mut().take(79) {
                cell.rev = true;
            }
        }
        let mut play = play(vec![
            map("", &room, 2, 3),
            map("You die...--More--", &room, 0, 18),
            map(
                "Do you want your possessions identified? [ynq] (n)",
                &room,
                0,
                51,
            ),
            view(&tombstone, 17, 8),
            view(&closing, 23, 8),
            View::parse(&scores, Pos::new(5, 0)),
        ]);
        let reply = play.send(b"F", &[], &[]);
        assert_eq!(reply.code, ExitCode::Over, "{}", reply.text);
        assert_eq!(sent(&play), ["F", " ", "n", " ", " "]);
        let death = "killed by a jackal, while sleeping";
        let end = reply.facts.end.as_ref().unwrap();
        assert_eq!(end.death.as_deref(), Some(death));
        assert!(
            has_line(&reply.text, &format!("end: {death}")),
            "{}",
            reply.text
        );
        assert!(!reply.text.contains("msg:   |"), "{}", reply.text);
        assert!(has_line(
            &reply.text,
            "answered: \"Do you want your possessions identified? [ynq] (n)\" \u{2190} n"
        ));
        let reply = play.go(">", Ground::Dry, None, &SquareSet::new());
        assert_eq!(reply.code, ExitCode::Over);
        assert!(
            reply
                .text
                .starts_with("go >: the game is over; nothing was sent")
        );
        assert!(has_line(&reply.text, &format!("end: {death}")));
        assert_eq!(play.term.sent.len(), 5);
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
            reply
                .facts
                .here
                .as_deref()
                .unwrap()
                .ends_with("Something is written here in the dust.; You read: \"Elbereth\"."),
            "{}",
            reply.text
        );
        play.look();
        assert_eq!(sent(&play), [":"]);
        play.term.views = vec![room(4, 402, ""), room(4, 402, second)];
        play.memory.hold_inventory(Some(402));
        let reply = play.look();
        assert_eq!(sent(&play), [":", ":"]);
        let here = reply.facts.here.unwrap();
        assert!(
            here.ends_with("You read: \"Elbe?eth\".") && !here.contains("Elbereth"),
            "{here}"
        );
    }

    #[test]
    fn an_object_picked_up_underfoot_is_looked_at_again() {
        let room = |turn: u32, message: &str| {
            let status = format!("Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:{turn}");
            let rows = screen(
                message,
                &[" ------ ", " |..@.| ", " ------ "],
                STATUS1,
                &status,
            );
            View::parse(&crate::testkit::grid(&rows), Pos::new(2, 4))
        };
        let mut play = play(vec![room(400, "")]);
        play.memory.ingest(&room(400, "You see here a dagger."));
        let reply = play.look();
        assert!(sent(&play).is_empty());
        assert!(reply.facts.here.unwrap().contains("dagger"));
        // Picking it up takes no turn for a fast hero, so the turn alone would not prompt a new look.
        play.term.views = vec![
            room(400, "a - a dagger."),
            room(400, "You see no objects here."),
        ];
        let reply = play.look();
        assert_eq!(sent(&play), [":"]);
        let here = reply.facts.here.unwrap();
        assert!(!here.contains("dagger"), "{here}");
    }

    #[test]
    fn a_monster_arriving_stops_a_rest_short_and_is_named_with_its_square() {
        let mut play = play(vec![start().view()]);
        play.last = Some(exec::search(20));
        let outcome = Outcome {
            stop: Stop::Changed,
            changed: exec::Changed::ENTITIES,
            arrival: Some(exec::Arrival {
                glyph: 'd',
                pos: Pos::new(8, 41),
                dist: 9,
            }),
            steps: 3,
            turns: Some(3),
            ..Outcome::default()
        };
        let report = play.report("rest 20", &outcome);
        assert!(!report.done);
        assert_eq!(
            report.stopped,
            ["a monster came into view or next to the hero (d 41,8)"]
        );
        let mut near = exec::search(20);
        near.near = Some(3);
        play.last = Some(near);
        let outcome = Outcome {
            arrival: Some(exec::Arrival {
                glyph: 'd',
                pos: Pos::new(8, 41),
                dist: 2,
            }),
            ..outcome
        };
        let report = play.report("rest --near 3 20", &outcome);
        assert_eq!(
            report.stopped,
            ["a monster came within 3 squares of the hero (d 41,8, 2 away)"]
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
        let reply = play.rest(5, None);
        assert_eq!(reply.code, ExitCode::Short, "{}", reply.text);
        assert!(
            reply.text.starts_with(
                "rest 5: 1 search, 0 turns, 1 took no time (the game said \"The jackal bites!\"); \
                 stopped: the status line changed, a message appeared"
            ),
            "{}",
            reply.text
        );
        let calm = [" ------- ", " |..@..| ", " |.....| ", " ------- "];
        let arrived = [" ------- ", " |..@..| ", " |....d| ", " ------- "];
        let mut arrival = Play::new(ScriptedTerm::new(vec![
            map("", &calm, 2, 4),
            map("", &arrived, 2, 4),
        ]));
        assert_eq!(arrival.rest(5, None).code, ExitCode::Short);
    }

    #[test]
    fn a_rest_reports_a_hit_on_its_last_search() {
        let mut play = play(vec![
            jackal("", 12, 400),
            jackal("", 12, 401),
            jackal("The jackal bites!", 9, 402),
        ]);
        let reply = play.rest(2, None);
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
        let mut play = play(vec![map("", &room, 2, 4)]);
        let reply = play.rest(5, None);
        assert_eq!(reply.code, ExitCode::Failed, "{}", reply.text);
        assert!(
            reply.text.starts_with(
                "rest 5: 3 searches, 0 turns, 3 took no time (screen unchanged); stopped: no time passed"
            ),
            "{}",
            reply.text
        );
        let report = reply.facts.report.unwrap();
        assert_eq!(report.no_time, Some(3));
        assert_eq!(
            report.no_time_why,
            [NoTime {
                searches: 3,
                why: NoTimeWhy::Screen,
                text: None
            }]
        );
    }

    #[test]
    fn a_monster_drawn_blank_on_remembered_ground_is_listed_named_and_named_beside_the_hero() {
        let floor = [" ------- ", " |..@..| ", " |.....| ", " ------- "];
        let ghost = [" ------- ", " |..@ .| ", " |.....| ", " ------- "];
        let mut play = play(vec![
            map("", &ghost, 2, 4),
            map("Pick a monster, object or location.", &ghost, 2, 4),
            map("         ghost (Jay's ghost)", &ghost, 2, 4),
        ]);
        play.memory.ingest(&map("", &floor, 2, 4));
        let reply = play.look();
        assert_eq!(sent(&play), [";", "@l.", ":"]);
        assert!(
            has_line(
                &reply.text,
                "monsters: Jay's ghost (shown blank) 5,2 adjacent l"
            ),
            "{}",
            reply.text
        );
        // The game refuses a search beside it; the rest names it.
        let refused = "You already found a monster.  Use 'm' prefix to force another search.";
        play.term.views.push(map(refused, &ghost, 2, 4));
        let reply = play.rest(5, None);
        assert!(
            reply.text.lines().next().is_some_and(|line| line.ends_with(
                "stopped: no time passed; beside the hero: Jay's ghost (shown blank) 5,2"
            )),
            "{}",
            reply.text
        );
        // A screen that blanks more remembered squares than monsters could, as after the map was forgotten,
        // shows none.
        let gone = [" ------- ", " |  @  | ", " |     | ", " ------- "];
        let mut forgot = Play::new(ScriptedTerm::new(vec![map("", &gone, 2, 4)]));
        forgot.memory.ingest(&map("", &floor, 2, 4));
        assert!(!forgot.show().text.contains("monsters:"));
    }

    #[test]
    fn a_refused_search_says_what_the_game_said_and_a_repeat_that_it_showed_nothing() {
        // The game answers the first search with its refusal and the next ones with nothing, the message line
        // cleared, the turn counter where it was.
        let said = "You already found a monster.  Use 'm' prefix to force another search.";
        let room = [" ------- ", " |..@d.| ", " ------- "];
        let mut play = play(vec![map("", &room, 2, 4), map(said, &room, 2, 4)]);
        let reply = play.rest(5, None);
        assert_eq!(reply.code, ExitCode::Failed, "{}", reply.text);
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "rest 5: 1 search, 0 turns, 1 took no time (the game said \"You already found a monster.  \
                 Use 'm' prefix to force another search.\"); stopped: no time passed; beside the hero: d 5,2"
            )
        );
        play.term.views.push(map("", &room, 2, 4));
        let reply = play.rest(5, None);
        assert_eq!(reply.code, ExitCode::Failed, "{}", reply.text);
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "rest 5: 4 searches, 0 turns, 4 took no time (1 turn counter unchanged, no message or prompt; \
                 3 screen unchanged); stopped: no time passed; beside the hero: d 5,2"
            )
        );
        let report = reply.facts.report.unwrap();
        assert_eq!(report.no_time, Some(4));
        assert_eq!(
            report
                .no_time_why
                .iter()
                .map(|a| (a.searches, a.why))
                .collect::<Vec<_>>(),
            [(1, NoTimeWhy::Turn), (3, NoTimeWhy::Screen)]
        );
    }

    #[test]
    fn a_rest_where_the_turn_counter_is_not_shown_claims_no_time_either_way() {
        let room = [" ------- ", " |..@..| ", " ------- "];
        let untimed = "Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2";
        let mut play = play(vec![view(&screen("", &room, STATUS1, untimed), 2, 4)]);
        let reply = play.rest(5, None);
        assert!(
            reply.text.starts_with(
                "rest 5: 3 searches, turn counter not shown; stopped: nothing changed"
            ),
            "{}",
            reply.text
        );
        let report = reply.facts.report.unwrap();
        assert_eq!((report.turns, report.no_time), (None, None));
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
        let reply = play.send(b"20s", &[], &[]);
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
        play.term.failures = vec![Some(LinkError::Held("waiting to start".to_owned()))];
        let reply = play.send(b"s", &[], &[]);
        assert_eq!(reply.code, ExitCode::Held);
        assert!(reply.text.starts_with("send: "), "{}", reply.text);
        assert!(play.term.sent.is_empty());
    }

    /// The corridor east of a hero at 3,2: walked up to `hero`, with a branch north-east of 5,2 once the hero
    /// has reached it, and `extra` drawn on row 1.
    fn corridor(hero: u8, extra: &str) -> View {
        let mut row: Vec<char> = "   ".chars().collect();
        row.extend(std::iter::repeat_n('#', usize::from(hero) - 3));
        row.extend("@##".chars());
        let row: String = row.into_iter().collect();
        let mut above: Vec<char> = format!("{extra:<12}").chars().collect();
        if hero >= 5 {
            above[6] = '#';
        }
        let above: String = above.into_iter().collect();
        map("", &[above.trim_end(), &row], 2, hero)
    }

    #[test]
    fn explore_walks_leg_after_leg_until_a_stop_of_its_own() {
        // The first leg ends past the branch, the way a walk ends on its own; the second goes on until a jackal
        // comes into view.
        let mut play = self::play(vec![
            corridor(3, ""),
            corridor(4, ""),
            corridor(5, ""),
            corridor(6, ""),
            corridor(7, "           d"),
        ]);
        play.show();
        let reply = play.explore(5, None);
        assert_eq!(sent(&play).iter().filter(|keys| *keys == "ml").count(), 4);
        assert_eq!(reply.code, ExitCode::Short);
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "explore 5: 2 legs, 4 steps, 0 turns, now at 7,2; stopped: a monster came into view or next to the hero (d 11,1)"
            )
        );
        assert_eq!(reply.facts.report.unwrap().legs, Some(2));

        // Out of legs after one that stopped where a walk stops on its own, it is done.
        let views = (3..=7).map(|hero| corridor(hero, "")).collect();
        let mut play = self::play(views);
        play.show();
        let reply = play.explore(1, None);
        assert_eq!(reply.code, ExitCode::Done);
        assert_eq!(
            reply.text.lines().next(),
            Some("explore 1: 1 leg, 3 steps, 0 turns, now at 6,2; done")
        );
    }

    #[test]
    fn explore_stops_for_down_stairs_it_had_not_seen_and_at_the_end_of_the_frontier() {
        let mut play = self::play(vec![
            corridor(3, ""),
            corridor(4, ""),
            corridor(5, "         >"),
        ]);
        play.show();
        let reply = play.explore(5, None);
        assert_eq!(reply.code, ExitCode::Short);
        assert!(
            reply
                .text
                .starts_with("explore 5: 1 leg, 2 steps, 0 turns, now at 5,2; stopped: down stairs came into view"),
            "{}",
            reply.text
        );

        let room = [" -----", " |...|", " |.@.+", " |...|", " -----"];
        let mut play = self::play(vec![map("", &room, 3, 3)]);
        play.show();
        let reply = play.explore(5, None);
        assert_eq!(reply.code, ExitCode::Failed);
        assert_eq!(
            reply.text,
            "no reachable known square borders unexplored ones"
        );
        assert!(play.term.sent.is_empty());
    }

    /// The hero at 3,2 in a lit room with `east` drawn beside it at 4,2, `hp` hit points of 16, and `top` as the
    /// message line; the cursor waits after `top` when it asks something, else on the hero.
    fn fighting(top: &str, east: char, hp: u32) -> View {
        let row = format!(" |.@{east}.|");
        let status = format!("Dlvl:3 $:0 HP:{hp}(16) Pw:2(2) AC:6 Xp:2 T:400");
        let rows = screen(
            top,
            &[" ------", &row, " |....|", " ------"],
            STATUS1,
            &status,
        );
        if top.ends_with('?') || top.ends_with(')') {
            view(&rows, 0, top.len() as u8 + 1)
        } else {
            view(&rows, 2, 3)
        }
    }

    /// How many attacks `play` sent.
    fn attacks(play: &Play<ScriptedTerm>) -> usize {
        play.term
            .sent
            .iter()
            .filter(|keys| keys.as_slice() == b"Fl")
            .count()
    }

    #[test]
    fn fight_attacks_while_its_monster_is_shown_and_stops_at_the_hp_floor() {
        let mut play = self::play(vec![
            fighting("", 'd', 12),
            fighting("You hit the jackal.", 'd', 12),
            fighting("You kill the jackal!", '.', 11),
        ]);
        play.show();
        let reply = play.fight(b'l', 5, 4);
        assert_eq!(attacks(&play), 2);
        assert_eq!(reply.code, ExitCode::Short);
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "fight l 5 4: 2 attacks, 0 turns, now at 3,2; stopped: no monster is shown at 4,2"
            )
        );
        assert!(has_line(&reply.text, "msg: You hit the jackal."));
        assert!(has_line(&reply.text, "msg: You kill the jackal!"));

        let mut play = self::play(vec![
            fighting("", 'd', 12),
            fighting("The jackal bites!", 'd', 6),
        ]);
        play.show();
        let reply = play.fight(b'l', 5, 6);
        assert_eq!(attacks(&play), 1);
        assert!(
            reply.text.starts_with(
                "fight l 5 6: 1 attack, 0 turns, now at 3,2; stopped: HP 6 of 16, at or below 6"
            ),
            "{}",
            reply.text
        );

        let mut play = self::play(vec![
            fighting("", 'd', 12),
            fighting("You miss the jackal.", 'd', 12),
        ]);
        play.show();
        let reply = play.fight(b'l', 3, 0);
        assert_eq!((reply.code, attacks(&play)), (ExitCode::Done, 3));
        assert!(
            reply.text.lines().next().unwrap().ends_with("; done"),
            "{}",
            reply.text
        );
    }

    #[test]
    fn fight_stops_at_a_prompt_or_another_monster_and_refuses_to_start_without_a_target() {
        let mut play = self::play(vec![
            fighting("", 'd', 12),
            fighting("Really attack the jackal? [yn] (n)", 'd', 12),
        ]);
        play.show();
        let reply = play.fight(b'l', 5, 4);
        assert_eq!(
            (reply.code, sent(&play)),
            (ExitCode::Short, vec!["Fl".to_owned()])
        );
        assert_eq!(
            reply.facts.prompt.as_deref(),
            Some("Really attack the jackal? [yn] (n)")
        );
        assert!(
            reply
                .text
                .lines()
                .next()
                .unwrap()
                .ends_with("stopped: a prompt opened"),
            "{}",
            reply.text
        );

        let mut play = self::play(vec![
            fighting("", 'd', 12),
            fighting("You hit the jackal.", 'j', 12),
        ]);
        play.show();
        let reply = play.fight(b'l', 5, 4);
        assert!(
            reply
                .text
                .lines()
                .next()
                .unwrap()
                .ends_with("stopped: another monster is shown at 4,2"),
            "{}",
            reply.text
        );

        let mut play = self::play(vec![fighting("", '.', 12)]);
        play.show();
        let reply = play.fight(b'l', 5, 4);
        assert_eq!(reply.code, ExitCode::Failed);
        assert!(
            reply.text.contains("no monster is shown at 4,2; send Fl"),
            "{}",
            reply.text
        );
        let reply = play.fight(b'h', 5, 4);
        assert!(
            reply.text.contains("no monster is shown at 2,2"),
            "{}",
            reply.text
        );

        let mut play = self::play(vec![fighting("", 'd', 4)]);
        play.show();
        let reply = play.fight(b'l', 5, 4);
        assert_eq!(reply.code, ExitCode::Failed);
        assert_eq!(reply.text, "HP 4 of 16, at or below 4; no attack was made");
        assert!(play.term.sent.is_empty());
    }

    #[test]
    fn a_guarded_prompt_stops_the_keys_before_it() {
        let views = || {
            vec![
                fighting("", 'd', 12),
                fighting("In what direction?", 'd', 12),
                fighting("Really attack the jackal? [yn] (n)", 'd', 12),
                fighting("You hit the jackal.", 'd', 12),
            ]
        };
        let guard = ["Really attack".to_owned(), "Really quit".to_owned()];
        let mut play = self::play(views());
        play.show();
        let reply = play.send(b"Fly", &guard, &[]);
        assert_eq!(sent(&play), ["F", "l"]);
        assert_eq!(reply.code, ExitCode::Short);
        assert_eq!(
            reply.text.lines().next(),
            Some(
                "send: 2 keys, 0 turns; stopped: the guarded prompt \"Really attack the jackal? [yn] (n)\" opened; 1 key was not sent"
            )
        );
        assert_eq!(
            reply.facts.prompt.as_deref(),
            Some("Really attack the jackal? [yn] (n)")
        );
        // Unguarded, the same keys answer it.
        let mut play = self::play(views());
        play.show();
        let reply = play.send(b"Fly", &[], &[]);
        assert_eq!(
            (reply.code, sent(&play)[..3].join("")),
            (ExitCode::Done, "Fly".to_owned())
        );
    }

    #[test]
    fn a_look_reads_the_inventory_once_a_turn() {
        let start = Data::load("seed4242-start").view();
        let menu = Data::load("seed4242-inventory").view();
        let mut play = Play::new(ScriptedTerm::new(vec![start.clone(), menu, start.clone()]));
        let reply = play.look();
        assert_eq!(sent(&play)[..2], ["i", "\u{1b}"]);
        let inventory = reply.facts.inventory.unwrap();
        assert_eq!(inventory.turn, Some(1));
        assert!(
            inventory
                .lines
                .iter()
                .any(|line| line == "c - an uncursed stethoscope")
        );
        assert!(!reply.text.contains("msg: "), "{}", reply.text);
        let before = play.term.sent.len();
        play.look();
        assert!(
            play.term.sent[before..]
                .iter()
                .all(|keys| keys.as_slice() != b"i")
        );
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
