//! The compact text view and its machine form, and the cache of farlook names.
//!
//! Text parts, in order: the report line; the two status rows with spaces squashed; `msg:` lines; for a prompt,
//! `prompt:`, `cursor:` and a `keys:` hint; for any other non-map screen, `screen (MODE):` and its rows; the map
//! with a column ruler and row numbers; then `you: X,Y  here: …`, `monsters:`, `seen:`, `items:`, `frontier:`,
//! `inventory:`, `answered:` and `end:`. Coordinates print as `x,y`: x the terminal column, y the terminal row.

use std::fmt::Write;

use crate::glyphs::feature_name;
use crate::level::{Level, Near, Paths, door_sides};
use crate::observe::{Entity, EntityKind, Memory};
use crate::screen::{Phase, View, strip_more};
use crate::types::{
    Answered, End, Facts, Frontier, Inventory, MAP_BOTTOM, MAP_TOP, Monster, Place, Pos, ROWS,
    Report, SquareSet,
};

/// Most monsters named by farlook in one look.
pub const FARLOOKS: usize = 8;
/// Most entries listed under `seen:`, `items:` and `frontier:` before `+N more`.
pub const LISTED: usize = 6;
/// The refusal for a map drawn with non-ASCII symbols.
pub const ASCII: &str = "the map holds non-ASCII symbols: the harness reads the default ASCII symbol set, so turn \
                         off DECgraphics, IBMgraphics and other symsets";

/// A farlook name kept for one monster.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Named {
    /// The level it was seen on.
    pub level: u16,
    /// Its square.
    pub pos: Pos,
    /// Its glyph.
    pub glyph: char,
    /// Its colour.
    pub shade: crate::glyphs::Shade,
    /// Highlighted as the pet.
    pub pet: bool,
    /// The game's description.
    pub name: String,
    /// The turn it was looked at.
    pub turn: u32,
}

impl Named {
    fn matches(&self, level: u16, entity: &Entity) -> bool {
        self.level == level
            && self.pos == entity.pos
            && self.glyph == entity.glyph
            && self.shade == entity.shade
            && self.pet == entity.pet
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
    /// The cached name and look turn for `entity` on `level`.
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

    /// Caches a name for `entity`.
    pub fn insert(&mut self, level: u16, entity: &Entity, name: &str, turn: u32) {
        if let Some(named) = self
            .entries
            .iter_mut()
            .find(|named| named.matches(level, entity))
        {
            named.name.clear();
            named.name.push_str(name);
            named.turn = turn;
            return;
        }
        self.entries.push(Named {
            level,
            pos: entity.pos,
            glyph: entity.glyph,
            shade: entity.shade,
            pet: entity.pet,
            name: name.to_owned(),
            turn,
        });
    }

    /// The monsters in `entities` with no cached name, nearest to `hero` first, at most `FARLOOKS`, written into
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

/// A remembered place, or one a `go` target names: its square, glyph, label and the squares a walk to it ends on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Landmark {
    /// The square it is on.
    pub pos: Pos,
    /// Its glyph; `None` for a chosen square or frontier.
    pub glyph: Option<char>,
    /// What it is, such as `down stairs`, `door`, `item` or `frontier`.
    pub label: String,
    /// Squares a walk to it may end on.
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
            out.push(Landmark {
                pos: p,
                glyph: Some(ch),
                label: "door".to_owned(),
                goals: door_sides(p),
            });
        } else if level.open_doors.contains(p) {
            out.push(Landmark::at(p, Some(ch), "open door"));
        }
    }
    for entity in entities {
        match entity.kind {
            EntityKind::Object => out.push(Landmark::at(
                entity.pos,
                Some(entity.glyph),
                if entity.statue { "statue" } else { "item" },
            )),
            EntityKind::DoorOrObject => out.push(Landmark::at(
                entity.pos,
                Some(entity.glyph),
                "door or object",
            )),
            EntityKind::Monster | EntityKind::Unseen => {}
        }
    }
}

/// Everything a view is rendered from.
pub struct Scene<'a> {
    /// The screen.
    pub view: &'a View,
    /// What is remembered.
    pub memory: &'a Memory,
    /// Cached farlook names.
    pub names: &'a Names,
    /// Messages the game wrote since the last reply.
    pub fresh: &'a [String],
    /// How the command went, for `send`, `go` and `rest`.
    pub report: Option<&'a Report>,
    /// Prompts the keys passed.
    pub answered: &'a [Answered],
}

/// One `seen:` or `items:` entry while it is sorted: unreachable last, then by route length and square.
struct Listed {
    unreachable: bool,
    steps: u32,
    place: Place,
    text: String,
}

/// Renders views, reusing its route and entity buffers.
#[derive(Default)]
pub struct Renderer {
    paths: Paths,
    terrain_paths: Paths,
    entities: Vec<Entity>,
    line: String,
    landmarks: Vec<Landmark>,
    seen: Vec<Listed>,
    items: Vec<Listed>,
    edges: Vec<(Pos, u32)>,
    occupied: SquareSet,
    listed_items: SquareSet,
    listed_level: Option<u16>,
}

/// Whether a phase is a prompt drawn over the map.
fn over_map(phase: Phase) -> bool {
    matches!(
        phase,
        Phase::Choice
            | Phase::Text
            | Phase::Direction
            | Phase::Item
            | Phase::Spell
            | Phase::Position
    )
}

/// Starts a new line in `out` unless it is empty.
fn newline(out: &mut String) {
    if !out.is_empty() {
        out.push('\n');
    }
}

/// Appends `text` with runs of spaces squashed to one and the ends trimmed.
fn squash(text: &str, out: &mut String) {
    for (i, word) in text.split_whitespace().enumerate() {
        if i > 0 {
            out.push(' ');
        }
        out.push_str(word);
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
        let mut text = String::with_capacity(4096);
        let mut facts = Facts::default();
        self.write(scene, &mut text, &mut facts)?;
        Ok((text, facts))
    }

    fn write(&mut self, scene: &Scene, text: &mut String, facts: &mut Facts) -> Result<(), String> {
        let view = scene.view;
        let phase = view.phase();
        let map_shown = phase == Phase::Play || over_map(phase);
        if map_shown
            && view.rows[MAP_TOP as usize..=MAP_BOTTOM as usize]
                .iter()
                .any(|row| !row.is_ascii())
        {
            return Err(ASCII.to_owned());
        }
        self.status_facts(view, facts);
        if let Some(report) = scene.report {
            if report.error.is_some() || report.verb != "send" {
                report_line(report, view.hero, text);
            }
            facts.report = Some(report.clone());
        }
        if view.status.hp.is_some() {
            newline(text);
            squash(&view.rows[ROWS - 1], text);
            newline(text);
            squash(&view.rows[ROWS - 2], text);
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
            newline(text);
            text.push_str("msg: ");
            text.push_str(message);
            facts.msgs.push(message.clone());
        }
        self.line.clear();
        let hinted = hint(view, phase, &mut self.line);
        if !map_shown {
            newline(text);
            let _ = write!(text, "screen ({}):", phase.name());
            let rows = if view.status.hp.is_some() {
                &view.rows[..ROWS - 2]
            } else {
                &view.rows[..]
            };
            for row in rows.iter().filter(|row| !row.trim().is_empty()) {
                newline(text);
                text.push_str(row.trim_end());
            }
            if hinted {
                newline(text);
                text.push_str(&self.line);
            }
            if phase == Phase::Ended && view.yn.is_some() || phase == Phase::More {
                facts.prompt = Some(prompt);
            }
            Self::tail(scene, text, facts, false);
            return Ok(());
        }
        if phase != Phase::Play {
            newline(text);
            text.push_str("prompt: ");
            text.push_str(&prompt);
            if phase == Phase::Position && view.cursor.on_map() {
                newline(text);
                let _ = write!(text, "cursor: {}", view.cursor);
            }
            if hinted {
                newline(text);
                text.push_str(&self.line);
            }
            facts.prompt = Some(prompt);
        }
        map_lines(view, text);
        if phase == Phase::Play
            && let Some(hero) = view.hero
        {
            facts.pos = Some(hero);
            if view.engulfed {
                newline(text);
                let _ = write!(text, "you: {hero}, engulfed");
            } else if let Some(level) = scene.memory.current() {
                self.surroundings(scene, level, hero, text, facts);
            }
        }
        Self::tail(scene, text, facts, phase == Phase::Play && !view.engulfed);
        Ok(())
    }

    /// The status-line facts.
    fn status_facts(&self, view: &View, facts: &mut Facts) {
        let status = &view.status;
        facts.dlvl = status.dlvl;
        facts.turn = status.turn;
        facts.hp = status.hp;
        facts.hpmax = status.hpmax;
        facts.xl = status.xl;
        facts.ac = status.ac;
        facts.hunger = view.cond.hunger().map(str::to_owned);
    }

    /// The parts after the map: `inventory:` on the map view, then `answered:` and `end:`.
    fn tail(scene: &Scene, text: &mut String, facts: &mut Facts, map: bool) {
        if let Some(inventory) = scene.memory.inventory() {
            if map && !inventory.lines.is_empty() {
                inventory_line(inventory, text);
            }
            facts.inventory = Some(inventory.clone());
        }
        if !scene.answered.is_empty() {
            newline(text);
            text.push_str("answered: ");
            for (i, answered) in scene.answered.iter().enumerate() {
                if i > 0 {
                    text.push_str("; ");
                }
                if answered.key.is_empty() {
                    let _ = write!(text, "({})", answered.prompt);
                    continue;
                }
                let _ = write!(text, "\"{}\" \u{2190} {}", answered.prompt, answered.key);
                if answered.text_entry {
                    text.push_str(" (typed into a text prompt)");
                }
            }
            facts.answered.extend_from_slice(scene.answered);
        }
        if let Some(end) = scene.memory.end() {
            end_lines(end, text);
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
        let view = scene.view;
        scene.memory.entities(view, &mut self.entities);
        self.occupied.clear();
        for entity in &self.entities {
            if matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) && !entity.pet {
                self.occupied.insert(entity.pos);
            }
        }
        level.paths(hero, &self.occupied, &mut self.paths);
        level.paths(hero, &SquareSet::new(), &mut self.terrain_paths);

        self.line.clear();
        here(level, hero, &mut self.line);
        newline(text);
        let _ = write!(text, "you: {hero}  here: {}", self.line);
        facts.here = Some(self.line.clone());

        self.monsters(scene, level.id, hero, text, facts);

        landmarks(level, &self.entities, &mut self.landmarks);
        self.seen.clear();
        self.items.clear();
        self.listed_items.clear();
        self.listed_level = Some(level.id);
        for landmark in &self.landmarks {
            if !landmark.is_item() {
                match landmark.glyph {
                    Some('>') if facts.stairs_down.is_none() => {
                        facts.stairs_down = Some(landmark.pos)
                    }
                    Some('<') if facts.stairs_up.is_none() => facts.stairs_up = Some(landmark.pos),
                    _ => {}
                }
            }
            if landmark.pos == hero {
                continue;
            }
            let steps = landmark
                .goals
                .as_slice()
                .iter()
                .filter_map(|&goal| self.paths.distance(goal))
                .min();
            let blocked = steps.is_none()
                && landmark
                    .goals
                    .as_slice()
                    .iter()
                    .any(|&goal| self.terrain_paths.reaches(goal));
            let glyph = landmark.glyph.unwrap_or(' ');
            let item = landmark.label == "item";
            let mut entry = String::new();
            entry.push(glyph);
            if !item {
                entry.push(' ');
                entry.push_str(&landmark.label);
            }
            let _ = write!(entry, " {} ", landmark.pos);
            if let Some(key) = hero_neighbour(hero, landmark.pos) {
                let _ = write!(entry, "(adjacent {})", key as char);
            } else {
                match steps {
                    Some(n) => {
                        let _ = write!(entry, "({n})");
                    }
                    None if blocked => entry.push_str("(blocked)"),
                    None => entry.push_str("(no route)"),
                }
            }
            let listed = Listed {
                unreachable: steps.is_none(),
                steps: steps.unwrap_or(0),
                place: Place {
                    glyph,
                    label: if item {
                        String::new()
                    } else {
                        landmark.label.clone()
                    },
                    x: landmark.pos.x,
                    y: landmark.pos.y,
                    steps,
                    blocked,
                },
                text: entry,
            };
            if landmark.is_item() {
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
            if entries.is_empty() {
                continue;
            }
            entries.sort_by(|a, b| {
                (a.unreachable, a.steps, a.place.y, a.place.x, &a.text).cmp(&(
                    b.unreachable,
                    b.steps,
                    b.place.y,
                    b.place.x,
                    &b.text,
                ))
            });
            newline(text);
            let _ = write!(text, "{title}: ");
            for (i, entry) in entries.iter().take(LISTED).enumerate() {
                if i > 0 {
                    text.push_str("; ");
                }
                text.push_str(&entry.text);
            }
            if entries.len() > LISTED {
                let _ = write!(text, "; +{} more", entries.len() - LISTED);
            }
            out.extend(entries.drain(..).map(|entry| entry.place));
        }

        level.frontier_targets(&self.paths, &mut self.edges);
        if !self.edges.is_empty() {
            newline(text);
            text.push_str("frontier: ");
            for (i, &(p, steps)) in self.edges.iter().take(LISTED).enumerate() {
                if i > 0 {
                    text.push_str("; ");
                }
                let _ = write!(text, "{p} ({steps})");
            }
            if self.edges.len() > LISTED {
                let _ = write!(text, "; +{} more", self.edges.len() - LISTED);
            }
            facts
                .frontier
                .extend(self.edges.iter().map(|&(p, steps)| Frontier {
                    x: p.x,
                    y: p.y,
                    steps,
                }));
        }
    }

    /// `monsters:`: monsters and unseen presences, nearest first, with cached farlook names.
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
        self.entities
            .sort_by_key(|entity| entity.pos.chebyshev(hero));
        let mut first = true;
        for entity in &self.entities {
            if !matches!(entity.kind, EntityKind::Monster | EntityKind::Unseen) {
                continue;
            }
            let named = scene.names.get(level, entity);
            self.line.clear();
            match named {
                Some((name, _)) => self.line.push_str(name),
                None if entity.kind == EntityKind::Unseen => self.line.push_str("unseen"),
                None => {}
            }
            let looked = named.and_then(|(_, looked)| (Some(looked) != turn).then_some(looked));
            if let Some(looked) = looked {
                let _ = write!(self.line, " (looked T:{looked})");
            }
            if entity.pet && !self.line.contains("tame") {
                if !self.line.is_empty() {
                    self.line.push(' ');
                }
                self.line.push_str("pet");
            }
            newline_or(text, &mut first, "monsters: ", "; ");
            text.push(entity.glyph);
            if !self.line.is_empty() {
                text.push(' ');
                text.push_str(&self.line);
            }
            let _ = write!(text, " {}", entity.pos);
            match hero_neighbour(hero, entity.pos) {
                Some(key) => {
                    let _ = write!(text, " adjacent {}", key as char);
                }
                None => {
                    let _ = write!(text, " {} away", entity.pos.chebyshev(hero));
                }
            }
            facts.monsters.push(Monster {
                name: named.map(|(name, _)| name.to_owned()),
                glyph: entity.glyph,
                x: entity.pos.x,
                y: entity.pos.y,
                dist: entity.pos.chebyshev(hero),
                looked_turn: looked,
                pet: entity.pet,
            });
        }
    }
}

/// Starts the first entry of a list line with `title`, later ones with `separator`.
fn newline_or(text: &mut String, first: &mut bool, title: &str, separator: &str) {
    if *first {
        newline(text);
        text.push_str(title);
        *first = false;
    } else {
        text.push_str(separator);
    }
}

/// The direction key from `hero` to `p` when they are neighbours.
fn hero_neighbour(hero: Pos, p: Pos) -> Option<u8> {
    hero.neighbours().find(|&(_, q)| q == p).map(|(key, _)| key)
}

/// `inventory: (T:N) a - …; b - …`: the last inventory menu the game showed and the turn it showed it.
fn inventory_line(inventory: &Inventory, text: &mut String) {
    newline(text);
    text.push_str("inventory: ");
    if let Some(turn) = inventory.turn {
        let _ = write!(text, "(T:{turn}) ");
    }
    for (i, line) in inventory.lines.iter().enumerate() {
        if i > 0 {
            text.push_str("; ");
        }
        text.push_str(line);
    }
}

/// `end:` lines: the death line, then every other end-of-game line, verbatim.
fn end_lines(end: &End, text: &mut String) {
    if let Some(death) = &end.death {
        newline(text);
        text.push_str("end: ");
        text.push_str(death);
    }
    for line in &end.lines {
        if end.death.as_ref() == Some(line) {
            continue;
        }
        newline(text);
        text.push_str("end: ");
        text.push_str(line);
    }
}

/// The report line, such as `go >: 5 steps, 5 turns, now at 40,7; done` or
/// `rest 20: 3 searches, 3 turns; stopped: a monster came into view or next to the hero (d 41,8)`.
pub fn report_line(report: &Report, hero: Option<Pos>, out: &mut String) {
    newline(out);
    out.push_str(&report.verb);
    out.push_str(": ");
    if let Some(error) = &report.error {
        out.push_str(error);
        return;
    }
    let steps = i64::from(report.steps);
    if report.verb.starts_with("rest") {
        plural(steps, "search", out);
        if let Some(turns) = report.turns {
            out.push_str(", ");
            plural(turns, "turn", out);
        }
    } else {
        plural(steps, "step", out);
        if let Some(turns) = report.turns {
            out.push_str(", ");
            plural(turns, "turn", out);
        }
        if let Some(hero) = hero {
            let _ = write!(out, ", now at {hero}");
        }
    }
    if report.done {
        out.push_str("; done");
    } else {
        out.push_str("; stopped: ");
        for (i, reason) in report.stopped.iter().enumerate() {
            if i > 0 {
                out.push_str(", ");
            }
            out.push_str(reason);
        }
    }
}

/// The `keys:` hint for a prompt: the answers the screen itself offers.
pub fn hint(view: &View, phase: Phase, out: &mut String) -> bool {
    let start = out.len();
    match phase {
        Phase::More => out.push_str("keys: ' ' continues"),
        Phase::Choice => {
            out.push_str("keys: ");
            push_spaced(view.yn.as_deref().unwrap_or(""), out);
            let msg = view.msg.trim_end();
            let mut tail = msg.chars().rev();
            if let (Some(')'), Some(default), Some('(')) = (tail.next(), tail.next(), tail.next()) {
                out.push_str("; default ");
                out.push(default);
            }
            out.push_str("; \\e cancels");
        }
        Phase::Item | Phase::Spell => {
            out.push_str("keys: one of ");
            match bracketed(&view.msg) {
                Some(choices) => out.push_str(choices),
                None => out.push_str("the listed letters"),
            }
            out.push_str("; \\e cancels");
        }
        Phase::Direction => {
            out.push_str("keys: a direction ");
            push_directions(out);
            out.push_str(", < up, > down, . self; \\e cancels");
        }
        Phase::Text => out.push_str("keys: type the text, then \\r; \\e cancels"),
        Phase::Position => {
            out.push_str("keys: move the cursor with ");
            push_directions(out);
            out.push_str(", then . or , to pick; \\e cancels");
        }
        Phase::Menu => {
            let mut entries = Vec::new();
            view.menu_entries(&mut entries);
            out.push_str("keys: ");
            if entries.is_empty() {
                out.push_str("\\r closes");
            } else {
                for (i, entry) in entries.iter().enumerate() {
                    if i > 0 {
                        out.push(' ');
                    }
                    out.push(entry.key);
                }
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
                push_spaced(yn, out);
            }
        }
        Phase::Play | Phase::Unknown => {}
    }
    out.len() > start
}

/// Appends the characters of `text` separated by spaces.
fn push_spaced(text: &str, out: &mut String) {
    for (i, ch) in text.chars().enumerate() {
        if i > 0 {
            out.push(' ');
        }
        out.push(ch);
    }
}

/// Appends the eight direction keys separated by spaces.
fn push_directions(out: &mut String) {
    for (i, &(key, _, _)) in crate::types::DIRS.iter().enumerate() {
        if i > 0 {
            out.push(' ');
        }
        out.push(key as char);
    }
}

/// The first `[…]` in `msg`.
fn bracketed(msg: &str) -> Option<&str> {
    let open = msg.find('[')?;
    let close = msg[open..].find(']')?;
    Some(&msg[open..=open + close])
}

/// Appends the column ruler for columns `left..right`: a row of labels at the first column and every tenth one
/// that fits, then a row of unit digits.
fn ruler(left: usize, right: usize, out: &mut String) {
    let mut labels = [b' '; crate::types::COLS + 2];
    let width = right - left + 2;
    let mut digits = [0u8; 2];
    for c in left..right {
        let label = format_number(c, &mut digits);
        if c != left && c % 10 != 0 {
            continue;
        }
        let at = c - left;
        let from = at.saturating_sub(1);
        let to = (at + label.len()).min(width);
        if labels[from..to].iter().all(|&b| b == b' ') {
            labels[at..at + label.len()].copy_from_slice(label);
        }
    }
    newline(out);
    out.push_str("   ");
    let labels = &labels[..width];
    let end = labels.iter().rposition(|&b| b != b' ').map_or(0, |i| i + 1);
    out.extend(labels[..end].iter().map(|&b| b as char));
    newline(out);
    out.push_str("   ");
    out.extend((left..right).map(|c| (b'0' + (c % 10) as u8) as char));
}

/// The decimal digits of `n` (below 100) in `buf`.
fn format_number(n: usize, buf: &mut [u8; 2]) -> &[u8] {
    if n >= 10 {
        buf[0] = b'0' + (n / 10 % 10) as u8;
        buf[1] = b'0' + (n % 10) as u8;
        &buf[..]
    } else {
        buf[0] = b'0' + n as u8;
        &buf[..1]
    }
}

/// The map rows with a column ruler and row numbers, cut to the columns in use.
pub fn map_lines(view: &View, out: &mut String) {
    let rows = MAP_TOP as usize..=MAP_BOTTOM as usize;
    let mut left = usize::MAX;
    let mut right = 0;
    for r in rows.clone() {
        let text = view.rows[r].trim_end();
        if text.trim_start().is_empty() {
            continue;
        }
        left = left.min(text.len() - text.trim_start().len());
        right = right.max(text.chars().count());
    }
    if left == usize::MAX {
        return;
    }
    ruler(left, right.max(left), out);
    for r in rows {
        let text = view.rows[r].trim_end();
        if text.trim_start().is_empty() {
            continue;
        }
        newline(out);
        let _ = write!(out, "{r:>2} {}", text.get(left..).unwrap_or(""));
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
