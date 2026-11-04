//! The compact text view and its machine form, and the cache of farlook names.
//!
//! Text parts, in order: the report line; the two status rows with spaces squashed; `msg:` lines; for a prompt,
//! `prompt:`, `cursor:` and a `keys:` hint; for any other non-map screen, `screen (MODE):` and its rows; the map
//! with a column ruler and row numbers; then `you: X,Y  here: …`, `monsters:`, `seen:`, `items:`, `frontier:`,
//! `inventory:`, `answered:` and `end:`. Coordinates print as `x,y`: x the terminal column, y the terminal row.

use std::fmt::{Arguments, Write};

use crate::glyphs::{Shade, feature_name};
use crate::level::{Ground, Level, Near, Paths, door_sides};
use crate::observe::{Entity, EntityKind, Memory};
use crate::screen::{Phase, View, strip_more};
use crate::types::{
    Answered, COLS, Facts, Frontier, MAP_BOTTOM, MAP_TOP, Monster, NoTime, NoTimeWhy, Place, Pos,
    ROWS, Report, SquareSet,
};

/// Most monsters named by farlook in one look.
pub const FARLOOKS: usize = 8;
/// Most entries listed under `items:` and `frontier:` before `+N more`.
pub const LISTED: usize = 6;
/// Most squares of one kind listed under `seen:` before `+N more` of that kind.
pub const PER_KIND: usize = 3;
/// The refusal for a map drawn with non-ASCII symbols.
pub const ASCII: &str = "the map holds non-ASCII symbols: the harness reads the default ASCII symbol set, so turn \
                         off DECgraphics, IBMgraphics and other symsets";

/// What a farlook name is kept under: the level, and the monster's square, glyph, colour and pet highlight.
type Key = (u16, Pos, char, Shade, bool);

fn key_of(level: u16, entity: &Entity) -> Key {
    (level, entity.pos, entity.glyph, entity.shade, entity.pet)
}

/// Farlook names, each with its key and the turn it was looked at. A name lasts while its monster stays on its
/// square looking the same; only monsters in view are kept, so the cache never outgrows the screen.
#[derive(Default)]
pub struct Names {
    entries: Vec<(Key, String, u32)>,
}

/// Whether farlook names `entity`: a monster other than a long worm's tail.
fn nameable(entity: &Entity) -> bool {
    entity.kind == EntityKind::Monster && entity.glyph != '~'
}

impl Names {
    /// The kept name and look turn for `entity` on `level`.
    pub fn get(&self, level: u16, entity: &Entity) -> Option<(&str, u32)> {
        let key = key_of(level, entity);
        let (_, name, turn) = self.entries.iter().find(|(k, ..)| *k == key)?;
        Some((name, *turn))
    }

    /// Keeps only the names whose monster is still in `entities` on `level`.
    pub fn retain_visible(&mut self, level: u16, entities: &[Entity]) {
        self.entries.retain(|(key, ..)| {
            entities
                .iter()
                .any(|entity| nameable(entity) && key_of(level, entity) == *key)
        });
    }

    pub fn insert(&mut self, level: u16, entity: &Entity, name: &str, turn: u32) {
        let key = key_of(level, entity);
        self.entries.retain(|(k, ..)| *k != key);
        self.entries.push((key, name.to_owned(), turn));
    }

    /// The unnamed monsters in `entities`, nearest to `hero` first, at most `FARLOOKS`, written into `out`.
    pub fn unnamed(&self, level: u16, hero: Pos, entities: &[Entity], out: &mut Vec<Entity>) {
        out.clear();
        out.extend_from_slice(entities);
        out.retain(|entity| nameable(entity) && self.get(level, entity).is_none());
        out.sort_by_key(|entity| entity.pos.chebyshev(hero));
        out.truncate(FARLOOKS);
    }
}

/// A remembered place, or one a `go` target names: its square, glyph (`None` for a chosen square or frontier),
/// what it is (`down stairs`, `door`, `item`, `frontier`, …) and the squares a walk to it may end on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Landmark {
    pub pos: Pos,
    pub glyph: Option<char>,
    pub label: String,
    pub goals: Near,
}

impl Landmark {
    /// A landmark a walk ends on itself.
    pub fn at(pos: Pos, glyph: Option<char>, label: &str) -> Landmark {
        let mut goals = Near::default();
        goals.push(pos);
        Landmark {
            pos,
            glyph,
            label: label.to_owned(),
            goals,
        }
    }

    /// A closed door, which a walk reaches from a square beside it.
    pub fn door(pos: Pos) -> Landmark {
        Landmark {
            goals: door_sides(pos),
            ..Landmark::at(pos, Some('+'), "door")
        }
    }

    /// Whether it is listed under `items:` rather than `seen:`.
    pub fn is_item(&self) -> bool {
        self.label == "item" || self.label == "statue"
    }
}

/// Remembered features, doors and items on `level`, with the objects among `entities`, row then column, written
/// into `out`. They are what `seen:` and `items:` list and what `go` can name.
pub fn landmarks(level: &Level, entities: &[Entity], out: &mut Vec<Landmark>) {
    out.clear();
    for (p, ch) in level.known() {
        if let Some(name) = feature_name(ch) {
            out.push(Landmark::at(p, Some(ch), level.kind(p).unwrap_or(name)));
        } else if ch == '+' {
            out.push(Landmark::door(p));
        } else if level.open_doors.contains(p) {
            out.push(Landmark::at(p, Some(ch), "open door"));
        }
    }
    for entity in entities {
        let label = match entity.kind {
            EntityKind::Object if entity.statue => "statue",
            EntityKind::Object => "item",
            EntityKind::DoorOrObject => "door or object",
            EntityKind::Monster | EntityKind::Unseen => continue,
        };
        out.push(Landmark::at(entity.pos, Some(entity.glyph), label));
    }
}

/// Everything a view is rendered from: the screen, memory, the kept farlook names, the messages written since the
/// last reply, how the command went (for `send`, `go` and `rest`) and the prompts its keys passed.
pub struct Scene<'a> {
    pub view: &'a View,
    pub memory: &'a Memory,
    pub names: &'a Names,
    pub fresh: &'a [String],
    pub report: Option<&'a Report>,
    pub answered: &'a [Answered],
}

/// One `seen:` or `items:` entry while it is sorted: unreachable last, then by route length and square. `at` is its
/// square and route, such as `32,7 (adjacent j)` or `40,4 (8)`.
struct Listed {
    place: Place,
    at: String,
}

impl Listed {
    /// Whether `other` is the same kind: the same glyph and label.
    fn same_kind(&self, other: &Listed) -> bool {
        self.place.glyph == other.place.glyph && self.place.label == other.place.label
    }
}

/// Routes from the hero over `ground`: the entities in view, the squares monsters and unseen things other than the
/// pet hold, the routes around them, and the routes over terrain alone, searched only when asked for.
#[derive(Default)]
pub struct Routes {
    pub entities: Vec<Entity>,
    pub occupied: SquareSet,
    pub paths: Paths,
    ground: Ground,
    terrain: Paths,
    terrain_from: Option<(u16, Pos, Ground)>,
}

impl Routes {
    /// Takes the entities of `view` and routes from its hero around them over `ground`. A route step into the
    /// highlighted pet swaps places with it, so the pet blocks nothing.
    pub fn from_hero(&mut self, memory: &Memory, view: &View, ground: Ground) {
        memory.entities(view, &mut self.entities);
        self.occupied.clear();
        for entity in &self.entities {
            if matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) && !entity.pet {
                self.occupied.insert(entity.pos);
            }
        }
        self.terrain_from = None;
        self.ground = ground;
        if let (Some(level), Some(hero)) = (memory.current(), view.hero) {
            level.paths(hero, &self.occupied, ground, &mut self.paths);
        }
    }

    /// Routes from `hero` over `level`'s terrain, on the ground of the last search from the hero, as if no monster
    /// stood anywhere; searched once per hero square.
    pub fn terrain(&mut self, level: &Level, hero: Pos) -> &Paths {
        let from = Some((level.id, hero, self.ground));
        if self.terrain_from != from {
            level.paths(hero, &SquareSet::new(), self.ground, &mut self.terrain);
            self.terrain_from = from;
        }
        &self.terrain
    }
}

/// Renders views, reusing its route and entity buffers.
#[derive(Default)]
pub struct Renderer {
    /// The routes the last map view was rendered with.
    pub routes: Routes,
    line: String,
    landmarks: Vec<Landmark>,
    seen: Vec<Listed>,
    items: Vec<Listed>,
    /// The kinds under `seen:` while they are listed: the index of each kind's first entry and its count.
    kinds: Vec<(usize, usize)>,
    edges: Vec<(Pos, u32)>,
    listed_items: SquareSet,
    listed_level: Option<u16>,
}

/// Starts a new line in `out`, unless it is empty, and writes `line` on it.
fn line(out: &mut String, line: Arguments) {
    if !out.is_empty() {
        out.push('\n');
    }
    let _ = out.write_fmt(line);
}

fn join<T>(
    out: &mut String,
    separator: &str,
    items: impl IntoIterator<Item = T>,
    mut write: impl FnMut(&mut String, T),
) {
    for (i, item) in items.into_iter().enumerate() {
        if i > 0 {
            out.push_str(separator);
        }
        write(out, item);
    }
}

/// A `title:` line listing the first `LISTED` of `items`, then `+N more`; nothing when there are none.
fn listing<T>(out: &mut String, title: &str, items: &[T], write: impl FnMut(&mut String, &T)) {
    if items.is_empty() {
        return;
    }
    line(out, format_args!("{title}: "));
    join(out, "; ", items.iter().take(LISTED), write);
    if items.len() > LISTED {
        let _ = write!(out, "; +{} more", items.len() - LISTED);
    }
}

/// Appends `count word`, with the plural `-es` after `h` and `-s` otherwise.
fn plural(count: i64, word: &str, out: &mut String) {
    let _ = write!(out, "{count} {word}");
    if count != 1 {
        out.push_str(if word.ends_with('h') { "es" } else { "s" });
    }
}

impl Renderer {
    /// Whether the last map view listed `p` on `level` under `items:`.
    pub fn listed_item(&self, level: u16, p: Pos) -> bool {
        self.listed_level == Some(level) && self.listed_items.contains(p)
    }

    /// The text view and its machine form. Fails with `ASCII` when a shown map holds non-ASCII symbols.
    pub fn render(&mut self, scene: &Scene) -> Result<(String, Facts), String> {
        let view = scene.view;
        let phase = view.phase();
        // Pages, menus, end-of-game and unrecognised screens print in full rather than as a map.
        let map_shown = !matches!(
            phase,
            Phase::More | Phase::Menu | Phase::Ended | Phase::Unknown
        );
        let map_rows = &view.rows[MAP_TOP as usize..=MAP_BOTTOM as usize];
        if map_shown && map_rows.iter().any(|row| !row.is_ascii()) {
            return Err(ASCII.to_owned());
        }
        let mut text = String::with_capacity(4096);
        let status = &view.status;
        let mut facts = Facts {
            dlvl: status.dlvl,
            turn: status.turn,
            hp: status.hp,
            hpmax: status.hpmax,
            xl: status.xl,
            ac: status.ac,
            hunger: view.cond.hunger().map(str::to_owned),
            ..Facts::default()
        };
        if let Some(report) = scene.report {
            if report.error.is_some() || report.verb != "send" {
                report_line(report, view.hero, &mut text);
            }
            facts.report = Some(report.clone());
        }
        if status.hp.is_some() {
            for row in [&view.rows[ROWS - 1], &view.rows[ROWS - 2]] {
                line(&mut text, format_args!(""));
                join(&mut text, " ", row.split_whitespace(), String::push_str);
            }
        }
        let prompt = strip_more(&view.msg).trim().to_owned();
        for message in scene.fresh {
            // A prompt's text, and on a screen printed in full any line it shows, would repeat what follows.
            if message.is_empty()
                || phase != Phase::Play && *message == prompt
                || !map_shown && view.rows.iter().any(|row| row.trim() == message.trim())
            {
                continue;
            }
            line(&mut text, format_args!("msg: {message}"));
            facts.msgs.push(message.clone());
        }
        if !map_shown {
            line(&mut text, format_args!("screen ({}):", phase.name()));
            let rows = &view.rows[..if status.hp.is_some() { ROWS - 2 } else { ROWS }];
            for row in rows.iter().filter(|row| !row.trim().is_empty()) {
                line(&mut text, format_args!("{}", row.trim_end()));
            }
            if phase == Phase::More || phase == Phase::Ended && view.yn.is_some() {
                facts.prompt = Some(prompt);
            }
        } else if phase != Phase::Play {
            line(&mut text, format_args!("prompt: {prompt}"));
            if phase == Phase::Position && view.cursor.on_map() {
                line(&mut text, format_args!("cursor: {}", view.cursor));
            }
            facts.prompt = Some(prompt);
        }
        self.line.clear();
        hint(view, phase, &mut self.line);
        if !self.line.is_empty() {
            line(&mut text, format_args!("{}", self.line));
        }
        if map_shown {
            map_lines(view, &mut text);
        }
        if let Some(hero) = view.hero.filter(|_| phase == Phase::Play) {
            facts.pos = Some(hero);
            if view.engulfed {
                line(&mut text, format_args!("you: {hero}, engulfed"));
            } else {
                self.surroundings(scene, &mut text, &mut facts);
            }
        }
        let map = phase == Phase::Play && !view.engulfed;
        tail(scene, map, &mut text, &mut facts);
        Ok((text, facts))
    }

    /// `you:`, `monsters:` (nearest first, with kept farlook names) and the places for the map view.
    fn surroundings(&mut self, scene: &Scene, text: &mut String, facts: &mut Facts) {
        let (Some(level), Some(hero)) = (scene.memory.current(), scene.view.hero) else {
            return;
        };
        self.routes.from_hero(scene.memory, scene.view, Ground::Dry);
        self.line.clear();
        here(level, hero, &mut self.line);
        line(text, format_args!("you: {hero}  here: {}", self.line));
        facts.here = Some(self.line.clone());
        let turn = scene.view.status.turn;
        // A stable sort keeps row-then-column order among monsters at the same distance.
        let entities = &mut self.routes.entities;
        entities.sort_by_key(|entity| entity.pos.chebyshev(hero));
        for entity in entities.iter() {
            if !matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) {
                continue;
            }
            let named = scene.names.get(level.id, entity);
            let looked = named.and_then(|(_, looked)| (Some(looked) != turn).then_some(looked));
            self.line.clear();
            match named {
                Some((name, _)) => self.line.push_str(name),
                None if entity.kind == EntityKind::Unseen => self.line.push_str("unseen"),
                None => {}
            }
            if let Some(looked) = looked {
                let _ = write!(self.line, " (looked T:{looked})");
            }
            if entity.pet && !self.line.contains("tame") {
                if !self.line.is_empty() {
                    self.line.push(' ');
                }
                self.line.push_str("pet");
            }
            if facts.monsters.is_empty() {
                line(text, format_args!("monsters: "));
            } else {
                text.push_str("; ");
            }
            text.push(entity.glyph);
            if !self.line.is_empty() {
                let _ = write!(text, " {}", self.line);
            }
            let dist = entity.pos.chebyshev(hero);
            let _ = match hero.direction_to(entity.pos) {
                Some(key) => write!(text, " {} adjacent {}", entity.pos, key as char),
                None => write!(text, " {} {dist} away", entity.pos),
            };
            facts.monsters.push(Monster {
                name: named.map(|(name, _)| name.to_owned()),
                glyph: entity.glyph,
                x: entity.pos.x,
                y: entity.pos.y,
                dist,
                looked_turn: looked,
                pet: entity.pet,
            });
        }
        self.places(level, hero, text, facts);
    }

    /// `seen:`, `items:` and `frontier:` for the map view.
    fn places(&mut self, level: &Level, hero: Pos, text: &mut String, facts: &mut Facts) {
        landmarks(level, &self.routes.entities, &mut self.landmarks);
        self.listed_items.clear();
        self.listed_level = Some(level.id);
        for landmark in &self.landmarks {
            let item = landmark.is_item();
            if !item && landmark.glyph == Some('>') {
                facts.stairs_down.get_or_insert(landmark.pos);
            }
            if !item && landmark.glyph == Some('<') {
                facts.stairs_up.get_or_insert(landmark.pos);
            }
            if landmark.pos == hero {
                continue;
            }
            let goals = landmark.goals.as_slice();
            let paths = &self.routes.paths;
            let steps = goals.iter().filter_map(|&goal| paths.distance(goal)).min();
            let blocked = steps.is_none() && {
                let terrain = self.routes.terrain(level, hero);
                goals.iter().any(|&goal| terrain.reaches(goal))
            };
            let glyph = landmark.glyph.unwrap_or(' ');
            let plain = landmark.label == "item";
            let label = if plain { "" } else { landmark.label.as_str() };
            let mut at = String::new();
            let _ = match (hero.direction_to(landmark.pos), steps) {
                (Some(key), _) => write!(at, "{} (adjacent {})", landmark.pos, key as char),
                (None, Some(n)) => write!(at, "{} ({n})", landmark.pos),
                (None, None) if blocked => write!(at, "{} (blocked)", landmark.pos),
                (None, None) => write!(at, "{} (no route)", landmark.pos),
            };
            let place = Place {
                glyph,
                label: label.to_owned(),
                x: landmark.pos.x,
                y: landmark.pos.y,
                steps,
                blocked,
            };
            let listed = Listed { place, at };
            if item {
                self.listed_items.insert(landmark.pos);
                self.items.push(listed);
            } else {
                self.seen.push(listed);
            }
        }
        let order = |e: &Listed| {
            let place = &e.place;
            (place.steps.is_none(), place.steps, place.y, place.x)
        };
        for entries in [&mut self.seen, &mut self.items] {
            entries.sort_by(|a, b| {
                order(a).cmp(&order(b)).then_with(|| {
                    (a.place.glyph, &a.place.label).cmp(&(b.place.glyph, &b.place.label))
                })
            });
        }
        by_kind(text, &self.seen, &mut self.kinds);
        listing(text, "items", &self.items, |out, entry| {
            out.push(entry.place.glyph);
            if !entry.place.label.is_empty() {
                let _ = write!(out, " {}", entry.place.label);
            }
            let _ = write!(out, " {}", entry.at);
        });
        facts
            .seen
            .extend(self.seen.drain(..).map(|entry| entry.place));
        facts
            .items
            .extend(self.items.drain(..).map(|entry| entry.place));

        level.frontier_targets(&self.routes.paths, &mut self.edges);
        listing(text, "frontier", &self.edges, |text, (p, steps)| {
            let _ = write!(text, "{p} ({steps})");
        });
        for &(p, steps) in &self.edges {
            let (x, y) = (p.x, p.y);
            facts.frontier.push(Frontier { x, y, steps });
        }
    }
}

/// The `seen:` line for `entries`, sorted nearest by route first: grouped by kind, the kinds with fewest squares
/// first (as many by their nearest square), each with its nearest `PER_KIND` squares and then `+N more` of that
/// kind. Every kind is listed, so a kind with few squares never hides behind a kind with many. `kinds` is scratch.
fn by_kind(out: &mut String, entries: &[Listed], kinds: &mut Vec<(usize, usize)>) {
    if entries.is_empty() {
        return;
    }
    kinds.clear();
    for (i, entry) in entries.iter().enumerate() {
        match kinds
            .iter_mut()
            .find(|(first, _)| entries[*first].same_kind(entry))
        {
            Some((_, count)) => *count += 1,
            None => kinds.push((i, 1)),
        }
    }
    kinds.sort_unstable_by_key(|&(first, count)| (count, first));
    line(out, format_args!("seen: "));
    join(out, "; ", kinds.iter(), |out, &(first, count)| {
        let kind = &entries[first];
        let _ = write!(out, "{} {}", kind.place.glyph, kind.place.label);
        let squares = entries[first..].iter().filter(|e| kind.same_kind(e));
        for (n, entry) in squares.take(PER_KIND).enumerate() {
            out.push_str(if n == 0 { " " } else { ", " });
            out.push_str(&entry.at);
        }
        if count > PER_KIND {
            let _ = write!(out, ", +{} more", count - PER_KIND);
        }
    });
}

/// The parts after the map: `inventory:` on the map view, then `answered:` and `end:`.
fn tail(scene: &Scene, map: bool, text: &mut String, facts: &mut Facts) {
    if let Some(inventory) = scene.memory.inventory() {
        if map && !inventory.lines.is_empty() {
            line(text, format_args!("inventory: "));
            if let Some(turn) = inventory.turn {
                let _ = write!(text, "(T:{turn}) ");
            }
            join(text, "; ", &inventory.lines, |out, s| out.push_str(s));
        }
        facts.inventory = Some(inventory.clone());
    }
    if !scene.answered.is_empty() {
        line(text, format_args!("answered: "));
        join(text, "; ", scene.answered, |text, answered| {
            if answered.key.is_empty() {
                let _ = write!(text, "({})", answered.prompt);
                return;
            }
            let _ = write!(text, "\"{}\" \u{2190} {}", answered.prompt, answered.key);
            if answered.text_entry {
                text.push_str(" (typed into a text prompt)");
            }
        });
        facts.answered.extend_from_slice(scene.answered);
    }
    if let Some(end) = scene.memory.end() {
        let death = end.death.as_ref();
        let rest = end.lines.iter().filter(|&line| Some(line) != death);
        for shown in death.into_iter().chain(rest) {
            line(text, format_args!("end: {shown}"));
        }
        facts.end = Some(end.clone());
    }
}

/// The report line, such as `go >: 5 steps, 5 turns, now at 40,7; done` or
/// `rest 20: 3 searches, 3 turns; stopped: a monster came into view or next to the hero (d 41,8)`. A rest says
/// when the turn counter was not shown, and how many searches took no time with what the screen showed for them:
/// `rest 20: 20 searches, 15 turns, 5 took no time (turn counter unchanged, no message or prompt); done`.
pub fn report_line(report: &Report, hero: Option<Pos>, out: &mut String) {
    line(out, format_args!("{}: ", report.verb));
    if let Some(error) = &report.error {
        out.push_str(error);
        return;
    }
    let rest = report.verb.starts_with("rest");
    let unit = if rest { "search" } else { "step" };
    plural(report.steps.into(), unit, out);
    if let Some(wet) = report.wet {
        let _ = write!(out, ", {wet} wet");
    }
    if let Some(turns) = report.turns {
        out.push_str(", ");
        plural(turns, "turn", out);
    } else if rest {
        out.push_str(", turn counter not shown");
    }
    if let Some(searches) = report.no_time.filter(|&n| n > 0) {
        no_time(searches, &report.no_time_why, out);
    }
    if let Some(hero) = hero.filter(|_| !rest) {
        let _ = write!(out, ", now at {hero}");
    }
    if report.done {
        out.push_str("; done");
    } else {
        out.push_str("; stopped: ");
        join(out, ", ", &report.stopped, |out, why| out.push_str(why));
    }
}

/// `, N took no time (…)`: what the screen showed with the searches that took no time, each distinct answer after
/// its count unless one answer covers them all, and the searches beyond the listed answers as `N more`.
fn no_time(searches: u32, answers: &[NoTime], out: &mut String) {
    let _ = write!(out, ", {searches} took no time (");
    let whole = matches!(answers, [answer] if answer.searches == searches);
    let listed: u32 = answers.iter().map(|answer| answer.searches).sum();
    join(out, "; ", answers, |out, answer| {
        if !whole {
            let _ = write!(out, "{} ", answer.searches);
        }
        let text = answer.text.as_deref().unwrap_or_default();
        let _ = match answer.why {
            NoTimeWhy::Message => write!(out, "the game said \"{text}\""),
            NoTimeWhy::Prompt => write!(out, "prompt \"{text}\""),
            NoTimeWhy::Screen => write!(out, "screen unchanged"),
            NoTimeWhy::Turn => write!(out, "turn counter unchanged, no message or prompt"),
        };
    });
    if listed < searches {
        let sep = if answers.is_empty() { "" } else { "; " };
        let _ = write!(out, "{sep}{} more", searches - listed);
    }
    out.push(')');
}

/// The `keys:` hint for a prompt, if any: the answers the screen itself offers.
pub fn hint(view: &View, phase: Phase, out: &mut String) {
    match phase {
        Phase::More => out.push_str("keys: ' ' continues"),
        Phase::Choice => {
            let yn = view.yn.as_deref().unwrap_or("");
            out.push_str("keys: ");
            join(out, " ", yn.chars(), String::push);
            let mut tail = view.msg.trim_end().chars().rev();
            if let (Some(')'), Some(default), Some('(')) = (tail.next(), tail.next(), tail.next()) {
                let _ = write!(out, "; default {default}");
            }
            out.push_str("; \\e cancels");
        }
        Phase::Item | Phase::Spell => {
            let (_, after) = view.msg.split_once('[').unwrap_or_default();
            let _ = match after.split_once(']') {
                Some((letters, _)) => write!(out, "keys: one of [{letters}]; \\e cancels"),
                None => write!(out, "keys: one of the listed letters; \\e cancels"),
            };
        }
        Phase::Direction => {
            out.push_str("keys: a direction h j k l y u b n, < up, > down, . self; \\e cancels")
        }
        Phase::Text => out.push_str("keys: type the text, then \\r; \\e cancels"),
        Phase::Position => out.push_str(
            "keys: move the cursor with h j k l y u b n, then . or , to pick; \\e cancels",
        ),
        Phase::Menu => {
            let mut entries = Vec::new();
            view.menu_entries(&mut entries);
            out.push_str("keys: ");
            if entries.is_empty() {
                out.push_str("\\r closes");
            } else {
                let keys = entries.iter().map(|entry| entry.key);
                join(out, " ", keys, String::push);
                out.push_str(" select, \\r accepts");
            }
            if view.menu_page().is_some_and(|(page, pages)| page < pages) {
                out.push_str(", > next page");
            }
            out.push_str("; \\e cancels");
        }
        Phase::Ended => {
            if let Some(yn) = view.yn.as_deref() {
                out.push_str("keys: ");
                join(out, " ", yn.chars(), String::push);
            }
        }
        Phase::Play | Phase::Unknown => {}
    }
}

/// Appends the column ruler for columns `left..right`: a row of labels at the first column and every tenth one
/// that fits, then a row of unit digits.
fn ruler(left: usize, right: usize, out: &mut String) {
    line(out, format_args!("   "));
    let start = out.len();
    for c in (left..right).filter(|&c| c == left || c % 10 == 0) {
        let at = start + c - left;
        // A label after the first needs a blank column before it.
        if c == left || out.len() < at {
            out.extend(std::iter::repeat_n(' ', at - out.len()));
            let _ = write!(out, "{c}");
        }
    }
    line(out, format_args!("   "));
    out.extend((left..right).map(|c| (b'0' + (c % 10) as u8) as char));
}

/// The map rows with a column ruler and row numbers, cut to the columns in use.
pub fn map_lines(view: &View, out: &mut String) {
    let rows = || {
        (MAP_TOP as usize..=MAP_BOTTOM as usize)
            .map(|r| (r, view.rows[r].trim_end()))
            .filter(|(_, text)| !text.trim_start().is_empty())
    };
    let (mut left, mut right) = (COLS, 0);
    for (_, text) in rows() {
        left = left.min(text.len() - text.trim_start().len());
        right = right.max(text.chars().count());
    }
    if right == 0 {
        return;
    }
    ruler(left, right.max(left), out);
    for (r, text) in rows() {
        let row = text.get(left..).unwrap_or("");
        line(out, format_args!("{r:>2} {row}"));
    }
}

/// The `here:` text for the hero's square: remembered terrain and what the game said there.
pub fn here(level: &Level, hero: Pos, out: &mut String) {
    let start = out.len();
    if let Some(ground) = level.terrain(hero)
        && !level.inferred_floor.contains(hero)
    {
        out.push(ground);
        let name = if feature_name(ground).is_some() {
            level.kind(hero).unwrap_or("")
        } else if level.corridor(hero) {
            "corridor"
        } else if level.open_doors.contains(hero) {
            "open door"
        } else if level.engravings.contains(hero) {
            "engraving"
        } else {
            ""
        };
        if !name.is_empty() {
            let _ = write!(out, " {name}");
        }
    }
    if let Some(underfoot) = level.underfoot.get(&hero) {
        for note in underfoot.notes.iter().filter(|note| !note.is_empty()) {
            if out.len() > start {
                out.push_str("; ");
            }
            out.push_str(note);
        }
    }
    if out.len() == start {
        out.push_str("unknown");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::glyphs::{Base, Shade};
    use crate::testkit::{STATUS1, STATUS2, screen, view};

    fn monster(y: u8, x: u8, glyph: char, base: Base) -> Entity {
        Entity {
            pos: Pos::new(y, x),
            glyph,
            shade: Shade {
                base,
                bright: false,
            },
            kind: EntityKind::Monster,
            pet: false,
            statue: false,
        }
    }

    #[test]
    fn names_last_while_the_monster_stays_and_looks_the_same() {
        let jackal = monster(6, 36, 'd', Base::Brown);
        let newt = monster(5, 30, ':', Base::Brown);
        let mut names = Names::default();
        names.insert(1, &jackal, "jackal", 7);
        assert_eq!(names.get(1, &jackal), Some(("jackal", 7)));
        assert_eq!(names.get(2, &jackal), None);
        let recoloured = Entity {
            shade: Shade::GRAY,
            ..jackal
        };
        assert_eq!(names.get(1, &recoloured), None);
        names.retain_visible(1, &[recoloured, newt]);
        assert_eq!(names.get(1, &jackal), None);
        names.insert(1, &newt, "newt", 8);
        names.insert(1, &newt, "newt", 9);
        assert_eq!(names.get(1, &newt), Some(("newt", 9)));
        names.retain_visible(1, &[newt]);
        assert_eq!(names.get(1, &newt), Some(("newt", 9)));
    }

    #[test]
    fn unnamed_monsters_come_nearest_first_and_at_most_farlooks() {
        let hero = Pos::new(10, 40);
        let mut entities: Vec<Entity> = (0..12)
            .map(|i| monster(10, 70 - i * 2, 'd', Base::Red))
            .collect();
        entities.push(Entity {
            kind: EntityKind::Object,
            ..monster(10, 41, '%', Base::Red)
        });
        entities.push(monster(10, 42, '~', Base::Red));
        let mut names = Names::default();
        names.insert(1, &entities[11], "jackal", 1);
        let mut out = Vec::new();
        names.unnamed(1, hero, &entities, &mut out);
        assert_eq!(out.len(), FARLOOKS);
        assert_eq!(out[0].pos, Pos::new(10, 50));
        assert!(
            out.windows(2)
                .all(|w| w[0].pos.chebyshev(hero) <= w[1].pos.chebyshev(hero))
        );
    }

    #[test]
    fn report_lines_count_steps_turns_and_reasons() {
        let mut out = String::new();
        let go = Report {
            verb: "go >".to_owned(),
            done: true,
            steps: 5,
            turns: Some(5),
            ..Report::default()
        };
        report_line(&go, Some(Pos::new(7, 40)), &mut out);
        assert_eq!(out, "go >: 5 steps, 5 turns, now at 40,7; done");
        out.clear();
        let wet = Report {
            verb: "go --wet >".to_owned(),
            stopped: vec!["wet step 41,7->42,7 refused".to_owned()],
            steps: 2,
            wet: Some(1),
            turns: Some(1),
            ..Report::default()
        };
        report_line(&wet, Some(Pos::new(7, 41)), &mut out);
        assert_eq!(
            out,
            "go --wet >: 2 steps, 1 wet, 1 turn, now at 41,7; stopped: wet step 41,7->42,7 refused"
        );
        out.clear();
        let rest = Report {
            verb: "rest 20".to_owned(),
            stopped: vec!["a monster came into view or next to the hero (d 41,8)".to_owned()],
            steps: 3,
            turns: Some(3),
            ..Report::default()
        };
        report_line(&rest, Some(Pos::new(7, 40)), &mut out);
        assert_eq!(
            out,
            "rest 20: 3 searches, 3 turns; stopped: a monster came into view or next to the hero (d 41,8)"
        );
        out.clear();
        let idle = Report {
            verb: "rest 3".to_owned(),
            stopped: vec!["a message appeared".to_owned(), "HP is full".to_owned()],
            steps: 1,
            turns: Some(0),
            ..Report::default()
        };
        report_line(&idle, None, &mut out);
        assert_eq!(
            out,
            "rest 3: 1 search, 0 turns; stopped: a message appeared, HP is full"
        );
        out.clear();
        let closed = Report {
            verb: "go frontier".to_owned(),
            error: Some("the terminal is closed".to_owned()),
            ..Report::default()
        };
        report_line(&closed, None, &mut out);
        assert_eq!(out, "go frontier: the terminal is closed");
    }

    #[test]
    fn a_rest_says_which_searches_took_no_time_and_what_the_screen_showed() {
        let answer = |searches, why, text: Option<&str>| NoTime {
            searches,
            why,
            text: text.map(str::to_owned),
        };
        let mut out = String::new();
        let fast = Report {
            verb: "rest 20".to_owned(),
            done: true,
            steps: 20,
            turns: Some(15),
            no_time: Some(5),
            no_time_why: vec![answer(5, NoTimeWhy::Turn, None)],
            ..Report::default()
        };
        report_line(&fast, None, &mut out);
        assert_eq!(
            out,
            "rest 20: 20 searches, 15 turns, 5 took no time (turn counter unchanged, no message or prompt); done"
        );
        let json = serde_json::to_value(&fast).unwrap();
        assert_eq!(json["no_time"], 5);
        assert_eq!(
            json["no_time_why"],
            serde_json::json!([{"searches": 5, "why": "turn counter unchanged"}])
        );
        out.clear();
        let timed = Report {
            no_time: Some(0),
            no_time_why: Vec::new(),
            turns: Some(20),
            ..fast
        };
        report_line(&timed, None, &mut out);
        assert_eq!(out, "rest 20: 20 searches, 20 turns; done");
        let json = serde_json::to_value(&timed).unwrap();
        assert_eq!(json["no_time"], 0);
        assert!(json.get("no_time_why").is_none(), "{json}");
        out.clear();
        let mixed = Report {
            verb: "rest 9".to_owned(),
            stopped: vec!["a prompt opened".to_owned()],
            steps: 9,
            turns: Some(2),
            no_time: Some(7),
            no_time_why: vec![
                answer(3, NoTimeWhy::Screen, None),
                answer(1, NoTimeWhy::Message, Some("You hear a door open.")),
                answer(
                    1,
                    NoTimeWhy::Prompt,
                    Some("Really attack the gnome? [yn] (n)"),
                ),
            ],
            ..Report::default()
        };
        report_line(&mixed, None, &mut out);
        assert_eq!(
            out,
            "rest 9: 9 searches, 2 turns, 7 took no time (3 screen unchanged; 1 the game said \"You hear a door \
             open.\"; 1 prompt \"Really attack the gnome? [yn] (n)\"; 2 more); stopped: a prompt opened"
        );
        out.clear();
        let unshown = Report {
            verb: "rest 3".to_owned(),
            done: true,
            steps: 3,
            ..Report::default()
        };
        report_line(&unshown, None, &mut out);
        assert_eq!(out, "rest 3: 3 searches, turn counter not shown; done");
        out.clear();
        let walk = Report {
            verb: "go >".to_owned(),
            done: true,
            steps: 2,
            ..Report::default()
        };
        report_line(&walk, Some(Pos::new(7, 40)), &mut out);
        assert_eq!(out, "go >: 2 steps, now at 40,7; done");
    }

    #[test]
    fn the_ruler_labels_the_first_column_and_every_tenth_that_fits() {
        let mut out = String::new();
        ruler(35, 78, &mut out);
        assert_eq!(
            out,
            "   35   40        50        60        70\n   5678901234567890123456789012345678901234567"
        );
        out.clear();
        ruler(9, 12, &mut out);
        assert_eq!(out, "   9\n   901");
        out.clear();
        ruler(1, 9, &mut out);
        assert_eq!(out, "   1\n   12345678");
    }

    #[test]
    fn the_view_lists_answered_prompts_and_the_inventory() {
        let rows = screen("", &[" ------ ", " |.@..| ", " ------ "], STATUS1, STATUS2);
        let shown = view(&rows, 2, 3);
        let mut memory = Memory::default();
        memory.ingest(&shown);
        let answered = [
            Answered {
                prompt: "Do you want to add to the current engraving?".to_owned(),
                key: "n".to_owned(),
                text_entry: false,
            },
            Answered {
                prompt: "What do you want to write in the dust here?".to_owned(),
                key: "E".to_owned(),
                text_entry: true,
            },
        ];
        let names = Names::default();
        let scene = Scene {
            view: &shown,
            memory: &memory,
            names: &names,
            fresh: &[],
            report: None,
            answered: &answered,
        };
        let (text, facts) = Renderer::default().render(&scene).unwrap();
        assert_eq!(
            text.lines().last(),
            Some(
                "answered: \"Do you want to add to the current engraving?\" \u{2190} n; \
                 \"What do you want to write in the dust here?\" \u{2190} E (typed into a text prompt)"
            )
        );
        assert_eq!(facts.answered.len(), 2);
        assert!(text.lines().any(|line| line == "you: 3,2  here: unknown"));
    }

    #[test]
    fn seen_lists_every_kind_rarest_first_however_many_squares_one_kind_has() {
        // A lake of 19 rows of 40 water squares beside the hero, two fountains, and down stairs beyond the lake.
        let mut rows = vec![String::new(); 21];
        for (y, row) in rows.iter_mut().enumerate().skip(1) {
            *row = format!("  {}", "}".repeat(40));
            if y == 10 {
                row.push_str(" >");
            }
        }
        rows[0] = " {".to_owned();
        rows[1].replace_range(1..2, "@");
        rows[20] = " {".to_owned();
        let rows: Vec<&str> = rows.iter().map(String::as_str).collect();
        let mut grid = crate::testkit::grid(&screen("", &rows, STATUS1, STATUS2));
        for cell in grid
            .cells
            .iter_mut()
            .flatten()
            .filter(|cell| cell.ch == '}')
        {
            cell.fg = crate::types::Colour::Blue;
        }
        let shown = View::parse(&grid, Pos::new(2, 1));
        let mut memory = Memory::default();
        memory.ingest(&shown);
        let names = Names::default();
        let scene = Scene {
            view: &shown,
            memory: &memory,
            names: &names,
            fresh: &[],
            report: None,
            answered: &[],
        };
        let (text, facts) = Renderer::default().render(&scene).unwrap();
        let seen = text.lines().find(|l| l.starts_with("seen: ")).unwrap();
        assert_eq!(
            seen,
            "seen: > down stairs 43,11 (no route); { fountain 1,1 (adjacent k), 1,21 (no route); \
             } water 2,2 (adjacent l), 3,2 (no route), 4,2 (no route), +757 more"
        );
        // The machine form keeps every square, nearest by route first.
        assert_eq!(facts.seen.len(), 763);
        assert_eq!(
            facts.seen.iter().filter(|p| p.label == "water").count(),
            760
        );
        assert_eq!(facts.seen[0].label, "fountain");
    }

    #[test]
    fn hints_offer_the_choices_on_screen() {
        let eat = view(
            &screen("What do you want to eat? [k or ?*]", &[], STATUS1, STATUS2),
            0,
            35,
        );
        let mut out = String::new();
        hint(&eat, eat.phase(), &mut out);
        assert_eq!(out, "keys: one of [k or ?*]; \\e cancels");
        let ask = view(
            &screen("Really attack the guard? [yn] (n)", &[], STATUS1, STATUS2),
            0,
            34,
        );
        out.clear();
        hint(&ask, Phase::Choice, &mut out);
        assert_eq!(out, "keys: y n; default n; \\e cancels");
        out.clear();
        hint(&ask, Phase::Direction, &mut out);
        assert_eq!(
            out,
            "keys: a direction h j k l y u b n, < up, > down, . self; \\e cancels"
        );
        out.clear();
        hint(&ask, Phase::Play, &mut out);
        assert!(out.is_empty());
    }
}
