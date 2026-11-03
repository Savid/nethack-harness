//! The compact text view and its machine form, and the cache of farlook names.
//!
//! Text parts, in order: the report line; the two status rows with spaces squashed; `msg:` lines; for a prompt,
//! `prompt:`, `cursor:` and a `keys:` hint; for any other non-map screen, `screen (MODE):` and its rows; the map
//! with a column ruler and row numbers; then `you: X,Y  here: …`, `monsters:`, `seen:`, `items:`, `frontier:`,
//! `inventory:`, `answered:` and `end:`. Coordinates print as `x,y`: x the terminal column, y the terminal row.

use std::fmt::{Arguments, Write};

use crate::glyphs::feature_name;
use crate::level::{Level, Near, Paths, door_sides};
use crate::observe::{Entity, EntityKind, Memory};
use crate::screen::{Phase, View, strip_more};
use crate::types::{
    Answered, COLS, DIRS, Facts, Frontier, MAP_BOTTOM, MAP_TOP, Monster, Place, Pos, ROWS, Report,
    SquareSet,
};

/// Most monsters named by farlook in one look.
pub const FARLOOKS: usize = 8;
/// Most entries listed under `seen:`, `items:` and `frontier:` before `+N more`.
pub const LISTED: usize = 6;
/// The refusal for a map drawn with non-ASCII symbols.
pub const ASCII: &str = "the map holds non-ASCII symbols: the harness reads the default ASCII symbol set, so turn \
                         off DECgraphics, IBMgraphics and other symsets";

/// A farlook name kept for one monster, with the level it was seen on and the turn it was looked at.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Named {
    level: u16,
    entity: Entity,
    name: String,
    turn: u32,
}

impl Named {
    fn matches(&self, level: u16, entity: &Entity) -> bool {
        let named = &self.entity;
        self.level == level
            && (named.pos, named.glyph, named.shade, named.pet)
                == (entity.pos, entity.glyph, entity.shade, entity.pet)
    }
}

/// Farlook names by (level, square, glyph, colour, pet highlight). A name lasts while its monster stays on its
/// square looking the same; only monsters in view are kept, so the cache never outgrows the screen.
#[derive(Clone, Debug, Default)]
pub struct Names {
    entries: Vec<Named>,
}

/// Whether farlook names `entity`: a monster other than a long worm's tail.
fn nameable(entity: &Entity) -> bool {
    entity.kind == EntityKind::Monster && entity.glyph != '~'
}

impl Names {
    /// The kept name and look turn for `entity` on `level`.
    pub fn get(&self, level: u16, entity: &Entity) -> Option<(&str, u32)> {
        self.entries
            .iter()
            .find(|named| named.matches(level, entity))
            .map(|named| (named.name.as_str(), named.turn))
    }

    /// Keeps only the names whose monster is still in `entities` on `level`.
    pub fn retain_visible(&mut self, level: u16, entities: &[Entity]) {
        self.entries.retain(|named| {
            entities
                .iter()
                .any(|entity| nameable(entity) && named.matches(level, entity))
        });
    }

    pub fn insert(&mut self, level: u16, entity: &Entity, name: &str, turn: u32) {
        self.entries.retain(|named| !named.matches(level, entity));
        self.entries.push(Named {
            level,
            entity: *entity,
            name: name.to_owned(),
            turn,
        });
    }

    /// The monsters in `entities` with no kept name, nearest to `hero` first, at most `FARLOOKS`, written into
    /// `out`.
    pub fn unnamed(&self, level: u16, hero: Pos, entities: &[Entity], out: &mut Vec<Entity>) {
        out.clear();
        out.extend(
            entities
                .iter()
                .filter(|entity| nameable(entity) && self.get(level, entity).is_none())
                .copied(),
        );
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

/// One `seen:` or `items:` entry while it is sorted: unreachable last, then by route length and square.
struct Listed {
    place: Place,
    text: String,
}

/// Routes from the hero: the entities in view, the squares monsters and unseen things other than the pet hold,
/// the routes around them, and the routes over terrain alone, searched only when asked for.
#[derive(Default)]
pub struct Routes {
    pub entities: Vec<Entity>,
    pub occupied: SquareSet,
    pub paths: Paths,
    terrain: Paths,
    terrain_from: Option<(u16, Pos)>,
}

impl Routes {
    /// Takes the entities of `view` and routes from its hero around them. A route step into the highlighted pet
    /// swaps places with it, so the pet blocks nothing.
    pub fn from_hero(&mut self, memory: &Memory, view: &View) {
        memory.entities(view, &mut self.entities);
        self.occupied.clear();
        for entity in &self.entities {
            if matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) && !entity.pet {
                self.occupied.insert(entity.pos);
            }
        }
        self.terrain_from = None;
        if let (Some(level), Some(hero)) = (memory.current(), view.hero) {
            level.paths(hero, &self.occupied, &mut self.paths);
        }
    }

    /// Routes from `hero` over `level`'s terrain as if no monster stood anywhere, searched once per hero square.
    pub fn terrain(&mut self, level: &Level, hero: Pos) -> &Paths {
        if self.terrain_from != Some((level.id, hero)) {
            level.paths(hero, &SquareSet::new(), &mut self.terrain);
            self.terrain_from = Some((level.id, hero));
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

/// Appends each of `items`, written by `write`, with `separator` between them.
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
    /// The squares the last map view listed under `items:`, with the level they were on.
    pub fn listed_items(&self) -> Option<(u16, &SquareSet)> {
        self.listed_level.map(|level| (level, &self.listed_items))
    }

    /// The text view and its machine form. Fails with `ASCII` when the map or a prompt screen holds non-ASCII
    /// symbols.
    pub fn render(&mut self, scene: &Scene) -> Result<(String, Facts), String> {
        let view = scene.view;
        let phase = view.phase();
        let map_shown = matches!(
            phase,
            Phase::Play
                | Phase::Choice
                | Phase::Text
                | Phase::Direction
                | Phase::Item
                | Phase::Spell
                | Phase::Position
        );
        if map_shown
            && view.rows[MAP_TOP as usize..=MAP_BOTTOM as usize]
                .iter()
                .any(|row| !row.is_ascii())
        {
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
        self.line.clear();
        let hinted = hint(view, phase, &mut self.line);
        if !map_shown {
            line(&mut text, format_args!("screen ({}):", phase.name()));
            let rows = &view.rows[..if status.hp.is_some() { ROWS - 2 } else { ROWS }];
            for row in rows.iter().filter(|row| !row.trim().is_empty()) {
                line(&mut text, format_args!("{}", row.trim_end()));
            }
            if hinted {
                line(&mut text, format_args!("{}", self.line));
            }
            if phase == Phase::Ended && view.yn.is_some() || phase == Phase::More {
                facts.prompt = Some(prompt);
            }
            Self::tail(scene, &mut text, &mut facts, false);
            return Ok((text, facts));
        }
        if phase != Phase::Play {
            line(&mut text, format_args!("prompt: {prompt}"));
            if phase == Phase::Position && view.cursor.on_map() {
                line(&mut text, format_args!("cursor: {}", view.cursor));
            }
            if hinted {
                line(&mut text, format_args!("{}", self.line));
            }
            facts.prompt = Some(prompt);
        }
        map_lines(view, &mut text);
        if phase == Phase::Play
            && let Some(hero) = view.hero
        {
            facts.pos = Some(hero);
            if view.engulfed {
                line(&mut text, format_args!("you: {hero}, engulfed"));
            } else if let Some(level) = scene.memory.current() {
                self.surroundings(scene, level, hero, &mut text, &mut facts);
            }
        }
        let map = phase == Phase::Play && !view.engulfed;
        Self::tail(scene, &mut text, &mut facts, map);
        Ok((text, facts))
    }

    /// The parts after the map: `inventory:` on the map view, then `answered:` and `end:`.
    fn tail(scene: &Scene, text: &mut String, facts: &mut Facts, map: bool) {
        if let Some(inventory) = scene.memory.inventory() {
            if map && !inventory.lines.is_empty() {
                line(text, format_args!("inventory: "));
                if let Some(turn) = inventory.turn {
                    let _ = write!(text, "(T:{turn}) ");
                }
                join(text, "; ", &inventory.lines, |text, item| {
                    text.push_str(item)
                });
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
            let rest = end
                .lines
                .iter()
                .filter(|line| end.death.as_ref() != Some(*line));
            for death in end.death.iter().chain(rest) {
                line(text, format_args!("end: {death}"));
            }
            facts.end = Some(end.clone());
        }
    }

    /// `you:`, `monsters:`, `seen:`, `items:` and `frontier:` for the map view.
    fn surroundings(
        &mut self,
        scene: &Scene,
        level: &Level,
        hero: Pos,
        text: &mut String,
        facts: &mut Facts,
    ) {
        self.routes.from_hero(scene.memory, scene.view);
        self.line.clear();
        here(level, hero, &mut self.line);
        line(text, format_args!("you: {hero}  here: {}", self.line));
        facts.here = Some(self.line.clone());

        self.monsters(scene, level.id, hero, text, facts);

        landmarks(level, &self.routes.entities, &mut self.landmarks);
        self.seen.clear();
        self.items.clear();
        self.listed_items.clear();
        self.listed_level = Some(level.id);
        for landmark in &self.landmarks {
            let item = landmark.is_item();
            match landmark.glyph {
                Some('>') if !item && facts.stairs_down.is_none() => {
                    facts.stairs_down = Some(landmark.pos)
                }
                Some('<') if !item && facts.stairs_up.is_none() => {
                    facts.stairs_up = Some(landmark.pos)
                }
                _ => {}
            }
            if landmark.pos == hero {
                continue;
            }
            let goals = landmark.goals.as_slice();
            let steps = goals
                .iter()
                .filter_map(|&goal| self.routes.paths.distance(goal))
                .min();
            let blocked = steps.is_none() && {
                let terrain = self.routes.terrain(level, hero);
                goals.iter().any(|&goal| terrain.reaches(goal))
            };
            let glyph = landmark.glyph.unwrap_or(' ');
            let plain = landmark.label == "item";
            let label = if plain { "" } else { landmark.label.as_str() };
            let mut entry = String::from(glyph);
            if !plain {
                let _ = write!(entry, " {label}");
            }
            let _ = match (hero.direction_to(landmark.pos), steps) {
                (Some(key), _) => write!(entry, " {} (adjacent {})", landmark.pos, key as char),
                (None, Some(n)) => write!(entry, " {} ({n})", landmark.pos),
                (None, None) if blocked => write!(entry, " {} (blocked)", landmark.pos),
                (None, None) => write!(entry, " {} (no route)", landmark.pos),
            };
            let listed = Listed {
                place: Place {
                    glyph,
                    label: label.to_owned(),
                    x: landmark.pos.x,
                    y: landmark.pos.y,
                    steps,
                    blocked,
                },
                text: entry,
            };
            if item {
                self.listed_items.insert(landmark.pos);
                self.items.push(listed);
            } else {
                self.seen.push(listed);
            }
        }
        for (title, entries, out) in [
            ("seen", &mut self.seen, &mut facts.seen),
            ("items", &mut self.items, &mut facts.items),
        ] {
            entries.sort_by(|a, b| {
                let key =
                    |e: &Listed| (e.place.steps.is_none(), e.place.steps, e.place.y, e.place.x);
                key(a).cmp(&key(b)).then_with(|| a.text.cmp(&b.text))
            });
            listing(text, title, entries, |text, entry| {
                text.push_str(&entry.text)
            });
            out.extend(entries.drain(..).map(|entry| entry.place));
        }

        level.frontier_targets(&self.routes.paths, &mut self.edges);
        listing(text, "frontier", &self.edges, |text, (p, steps)| {
            let _ = write!(text, "{p} ({steps})");
        });
        let frontier = self.edges.iter().map(|&(p, steps)| Frontier {
            x: p.x,
            y: p.y,
            steps,
        });
        facts.frontier.extend(frontier);
    }

    /// `monsters:`: monsters and unseen presences, nearest first, with kept farlook names.
    fn monsters(
        &mut self,
        scene: &Scene,
        level: u16,
        hero: Pos,
        text: &mut String,
        facts: &mut Facts,
    ) {
        let turn = scene.view.status.turn;
        // A stable sort keeps row-then-column order among monsters at the same distance.
        self.routes
            .entities
            .sort_by_key(|entity| entity.pos.chebyshev(hero));
        let mut first = true;
        for entity in &self.routes.entities {
            if !matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) {
                continue;
            }
            let named = scene.names.get(level, entity);
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
            if first {
                line(text, format_args!("monsters: "));
                first = false;
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
    }
}

/// The report line, such as `go >: 5 steps, 5 turns, now at 40,7; done` or
/// `rest 20: 3 searches, 3 turns; stopped: a monster came into view or next to the hero (d 41,8)`.
pub fn report_line(report: &Report, hero: Option<Pos>, out: &mut String) {
    line(out, format_args!("{}: ", report.verb));
    if let Some(error) = &report.error {
        out.push_str(error);
        return;
    }
    let rest = report.verb.starts_with("rest");
    plural(
        i64::from(report.steps),
        if rest { "search" } else { "step" },
        out,
    );
    if let Some(turns) = report.turns {
        out.push_str(", ");
        plural(turns, "turn", out);
    }
    if let Some(hero) = hero.filter(|_| !rest) {
        let _ = write!(out, ", now at {hero}");
    }
    if report.done {
        out.push_str("; done");
    } else {
        out.push_str("; stopped: ");
        join(out, ", ", &report.stopped, |out, reason| {
            out.push_str(reason)
        });
    }
}

/// The `keys:` hint for a prompt: the answers the screen itself offers.
pub fn hint(view: &View, phase: Phase, out: &mut String) -> bool {
    let start = out.len();
    let spaced = |out: &mut String, keys: &mut dyn Iterator<Item = char>| {
        join(out, " ", keys, String::push);
    };
    let directions =
        |out: &mut String| spaced(out, &mut DIRS.iter().map(|&(key, _, _)| key as char));
    match phase {
        Phase::More => out.push_str("keys: ' ' continues"),
        Phase::Choice => {
            out.push_str("keys: ");
            spaced(out, &mut view.yn.as_deref().unwrap_or("").chars());
            let mut tail = view.msg.trim_end().chars().rev();
            if let (Some(')'), Some(default), Some('(')) = (tail.next(), tail.next(), tail.next()) {
                let _ = write!(out, "; default {default}");
            }
            out.push_str("; \\e cancels");
        }
        Phase::Item | Phase::Spell => {
            let open = view.msg.find('[');
            let close = open.and_then(|open| view.msg[open..].find(']').map(|close| open + close));
            let choices = match (open, close) {
                (Some(open), Some(close)) => &view.msg[open..=close],
                _ => "the listed letters",
            };
            let _ = write!(out, "keys: one of {choices}; \\e cancels");
        }
        Phase::Direction => {
            out.push_str("keys: a direction ");
            directions(out);
            out.push_str(", < up, > down, . self; \\e cancels");
        }
        Phase::Text => out.push_str("keys: type the text, then \\r; \\e cancels"),
        Phase::Position => {
            out.push_str("keys: move the cursor with ");
            directions(out);
            out.push_str(", then . or , to pick; \\e cancels");
        }
        Phase::Menu => {
            let mut entries = Vec::new();
            view.menu_entries(&mut entries);
            out.push_str("keys: ");
            if entries.is_empty() {
                out.push_str("\\r closes");
            } else {
                spaced(out, &mut entries.iter().map(|entry| entry.key));
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
                spaced(out, &mut yn.chars());
            }
        }
        Phase::Play | Phase::Unknown => {}
    }
    out.len() > start
}

/// Appends the column ruler for columns `left..right`: a row of labels at the first column and every tenth one
/// that fits, then a row of unit digits.
fn ruler(left: usize, right: usize, out: &mut String) {
    let mut labels = [b' '; COLS + 2];
    let width = right - left + 2;
    for c in (left..right).filter(|&c| c == left || c % 10 == 0) {
        // Columns are below 100, so a label is one or two digits.
        let digits = [b'0' + (c / 10) as u8, b'0' + (c % 10) as u8];
        let label = &digits[usize::from(c < 10)..];
        let at = c - left;
        let to = (at + label.len()).min(width);
        if labels[at.saturating_sub(1)..to].iter().all(|&b| b == b' ') {
            labels[at..at + label.len()].copy_from_slice(label);
        }
    }
    let labels = &labels[..width];
    let end = labels.iter().rposition(|&b| b != b' ').map_or(0, |i| i + 1);
    line(out, format_args!("   "));
    out.extend(labels[..end].iter().map(|&b| b as char));
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
    let Some(left) = rows()
        .map(|(_, text)| text.len() - text.trim_start().len())
        .min()
    else {
        return;
    };
    let right = rows()
        .map(|(_, text)| text.chars().count())
        .max()
        .unwrap_or(0);
    ruler(left, right.max(left), out);
    for (r, text) in rows() {
        line(
            out,
            format_args!("{r:>2} {}", text.get(left..).unwrap_or("")),
        );
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
            level.kind(hero)
        } else if level.corridor(hero) {
            Some("corridor")
        } else if level.open_doors.contains(hero) {
            Some("open door")
        } else if level.engravings.contains(hero) {
            Some("engraving")
        } else {
            None
        };
        if let Some(name) = name.filter(|name| !name.is_empty()) {
            out.push(' ');
            out.push_str(name);
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
    fn hints_offer_the_choices_on_screen() {
        let eat = view(
            &screen("What do you want to eat? [k or ?*]", &[], STATUS1, STATUS2),
            0,
            35,
        );
        let mut out = String::new();
        assert!(hint(&eat, eat.phase(), &mut out));
        assert_eq!(out, "keys: one of [k or ?*]; \\e cancels");
        let ask = view(
            &screen("Really attack the guard? [yn] (n)", &[], STATUS1, STATUS2),
            0,
            34,
        );
        out.clear();
        assert!(hint(&ask, Phase::Choice, &mut out));
        assert_eq!(out, "keys: y n; default n; \\e cancels");
        out.clear();
        assert!(hint(&ask, Phase::Direction, &mut out));
        assert_eq!(
            out,
            "keys: a direction h j k l y u b n, < up, > down, . self; \\e cancels"
        );
        out.clear();
        assert!(!hint(&ask, Phase::Play, &mut out));
        assert!(out.is_empty());
    }
}
