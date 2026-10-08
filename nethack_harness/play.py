"""Direct play for a caller that makes its own decisions: a compact text view and bounded commands.

Coordinates are x,y as printed: x is the terminal column, y the terminal row."""
import re

from .actions import Action, explore, travel
from .knowledge import DIRECTION_NAMES
from .level import FEATURES, cursor_keys, neighbours, on_map, position
from .perceive import fingerprint, parse_menu_entries, phase

FARLOOKS = 8
LISTED = 6
PROMPTS = ("choice", "text", "direction", "item", "spell", "position")
PROMPT_OPEN = "a prompt is open: answer or cancel it with send first"
ASCII = ("the map holds non-ASCII symbols: the harness reads the default ASCII symbol set, "
         "so turn off DECgraphics, IBMgraphics and other symsets")
FIELDS = {"status": "the status line changed", "conditions": "a condition changed", "engulfed": "the hero was engulfed",
          "terrain": "new terrain came into view", "entities": "a monster came into view or next to the hero",
          "message": "a message appeared", "hp_full": "HP is full"}
STOPS = {"step_limit": "it reached its step limit", "prompt": "a prompt opened", "unexpected_prompt": "a prompt opened",
         "movement_interrupted": "the hero did not arrive where expected", "no_observed_effect": "nothing changed",
         "route_changed": "the hero left the route", "feature_discovered": "new terrain came into view",
         "branch_discovered": "the corridor branches", "exploration_boundary": "the corridor ends",
         "game_over": "the game ended", "caller_interrupt": "it was interrupted",
         "observation_changed": "the screen changed"}


def xy(p):
    return "%d,%d" % (p[1], p[0])


def chebyshev(p, q):
    return max(abs(p[0] - q[0]), abs(p[1] - q[1]))


def square(entity):
    return tuple(x - 1 for x in entity["position"])


def squash(row):
    return " ".join(row.split())


def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "es" if word.endswith("h") else "s")


def messages(frames):
    """Message lines the game wrote while a command ran, including text windows the harness paged through."""
    out = []
    for frame in frames:
        if frame.get("window"):
            texts = [line.strip() for line in frame["window"] if line.strip()]
            out += texts[:1] + ["  " + text for text in texts[1:]]
        elif frame["phase"] != "menu" and not frame.get("message_unchanged"):
            text = frame["message"].replace("--More--", "").strip()
            if text:
                out.append(text)
    return out


def ruler(left, right):
    labels, units = [" "] * (right - left + 2), []
    for c in range(left, right):
        units.append(str(c % 10))
        label = str(c)
        if (c == left or c % 10 == 0) and all(ch == " " for ch in labels[max(0, c - left - 1):c - left + len(label)]):
            labels[c - left:c - left + len(label)] = label
    return ["   " + "".join(labels).rstrip(), "   " + "".join(units)]


def map_lines(view):
    rows = [(r, view.rows[r].rstrip()) for r in range(1, 22)]
    rows = [(r, text) for r, text in rows if text.strip()]
    if not rows:
        return []
    left = min(len(text) - len(text.lstrip()) for _, text in rows)
    right = max(len(text) for _, text in rows)
    return ruler(left, right) + ["%2d %s" % (r, text[left:]) for r, text in rows]


def hint(view, mode):
    """The answers the screen itself offers."""
    if mode == "more":
        return "keys: ' ' continues"
    if mode == "choice":
        default = re.search(r"\((.)\)\s*$", view.msg)
        return "keys: %s%s; \\e cancels" % (" ".join(view.yn), "; default " + default[1] if default else "")
    if mode in ("item", "spell"):
        choices = re.search(r"\[[^\]]*\]", view.msg)
        return "keys: one of %s; \\e cancels" % (choices[0] if choices else "the listed letters")
    if mode == "direction":
        return "keys: a direction %s, < up, > down, . self; \\e cancels" % " ".join(DIRECTION_NAMES)
    if mode == "text":
        return "keys: type the text, then \\r; \\e cancels"
    if mode == "position":
        return "keys: move the cursor with %s, then . or , to pick; \\e cancels" % " ".join(DIRECTION_NAMES)
    if mode == "menu":
        entries = "".join(entry["key"] for entry in parse_menu_entries(view))
        paged = re.search(r"\((\d+) of (\d+)\)", view.text_screen())
        parts = ["%s select, \\r accepts" % " ".join(entries) if entries else "\\r closes"]
        if paged and int(paged[1]) < int(paged[2]):
            parts.append("> next page")
        return "keys: %s; \\e cancels" % ", ".join(parts)
    if mode == "ended" and view.yn:
        return "keys: %s" % " ".join(view.yn)
    return None


class Play:
    def __init__(self, session):
        self.session = session
        self.fresh = []
        self.reported = 0
        self.names = {}
        self.where = None
        self.items = set()

    @property
    def observer(self):
        return self.session.observer

    @property
    def term(self):
        return self.session.term

    def send(self, keys, cancelled=lambda: False):
        report, reason, _ = self.run(Action("manual", "Caller-supplied keys", "manual", keys), "send", cancelled)
        return report, reason, False

    def rest(self, turns, cancelled=lambda: False):
        view = self.term.view()
        if phase(view) != "play":
            raise ValueError(PROMPT_OPEN)
        report, reason, outcome = self.run(Action("search:%d" % turns, "Search here up to %d times" % turns,
                                                  "search", "s", turns), "rest %d" % turns, cancelled)
        refused = outcome and outcome.get("elapsed_turns") == 0 and outcome["steps"] and (
            outcome["steps"] > 1 or outcome["reason"] != "completed")
        return report, reason, refused

    def go(self, target, cancelled=lambda: False):
        view = self.term.view()
        if phase(view) != "play" or view.engulfed:
            raise ValueError(PROMPT_OPEN if phase(view) != "play" else "go is unavailable while engulfed")
        level = self.observer.current
        occupied = self.occupied(view)
        previous = level.paths(view.hero, occupied)
        word = target.strip().lower()
        places = self.places(target, view, level, previous)
        routes = sorted((len(level.route(previous, goal)), goal, label) for _, _, label, goals in places
                        for goal in goals if goal in previous)
        if routes and not routes[0][0] and any(length for length, _, _ in routes):
            routes = [route for route in routes if route[0]]
        if not routes:
            raise ValueError(self.unreachable(target, word, view, level, places, occupied))
        length, goal, label = routes[0]
        if not length:
            raise ValueError("already at %s" % xy(goal))
        steps = self.session.settings.max_action_steps
        if word == "frontier" and level.corridor(goal) and len(level.corridor_links(goal)) <= 1:
            action = explore(level, previous, goal, steps)
        else:
            action = travel(level, previous, goal, label, steps)
        report, reason, _ = self.run(action, "go " + target, cancelled, fingerprint(view))
        return report, reason, False

    def run(self, action, label, cancelled, expected=None):
        last = self.session.last
        reason = self.session.step(manual=action, expected=expected, cancelled=cancelled)
        if self.session.last is last:
            return "%s: the game is over; nothing was sent" % label, reason, None
        outcome = self.session.last["outcome"]
        self.fresh = messages(outcome.get("frames", []))
        self.reported = self.observer.message_total
        return self.report(label, action, outcome), reason, outcome

    def report(self, label, action, outcome):
        reason = outcome["reason"]
        if outcome.get("error"):
            return "%s: %s" % (label, outcome["error"])
        if action.kind == "manual":
            return None
        hero = self.term.view().hero
        turns = outcome.get("elapsed_turns")
        if action.kind == "search":
            done = plural(outcome["steps"], "search")
            if turns == 0 and outcome["steps"]:
                done += ", no time passed"
            elif turns is not None:
                done += ", " + plural(turns, "turn")
        else:
            done = plural(outcome["steps"], "step")
            if turns is not None:
                done += ", " + plural(turns, "turn")
            if hero:
                done += ", now at " + xy(hero)
        if reason == "completed":
            if action.kind == "search" or hero == action.target:
                return "%s: %s; done" % (label, done)
            why = "it reached its step limit, %d squares short" % (len(action.route) - outcome["steps"])
        elif reason == "observation_changed" and outcome.get("changed_fields"):
            why = ", ".join(FIELDS.get(field, field) for field in outcome["changed_fields"])
        else:
            why = STOPS.get(reason, reason)
        return "%s: %s; stopped: %s" % (label, done, why)

    def occupied(self, view):
        """Squares monsters block. A route step into the highlighted pet swaps places with it, so it blocks none."""
        return {square(entity) for entity in self.observer.entities(view)
                if entity["kind"] in ("monster", "unseen") and not entity["pet_highlight"]}

    def places(self, target, view, level, previous):
        """Landmarks matching a go target: x,y, a map glyph, a landmark kind, or frontier."""
        match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", target)
        if match:
            p = int(match[2]), int(match[1])
            if not on_map(p):
                raise ValueError("%s is off the map" % target)
            return [(p, "", "chosen square", (p,))]
        word = target.strip().lower()
        if word == "frontier":
            return [(p, "", "frontier", (p,)) for p, _ in self.frontier(level, previous)]
        found = [place for place in self.landmarks(view, level)
                 if word in (place[1], place[2]) or word in place[2].split() and place[2] != "open door"]
        if not found:
            raise ValueError("no remembered %r on this level; use x,y, <, >, frontier or a kind listed under seen" %
                             target)
        return found

    def unreachable(self, target, word, view, level, places, occupied):
        """Why no known route reaches the target, from remembered facts."""
        terrain = level.paths(view.hero)
        blockers = sorted({q for _, _, _, goals in places for goal in goals if goal in terrain
                           for q in level.route(terrain, goal) if q in occupied})
        if blockers:
            return "the known route to %s is blocked by a monster at %s" % (
                target, " or ".join(xy(q) for q in blockers[:3]))
        if len(places) == 1 and places[0][2] == "chosen square":
            p = places[0][0]
            if level.terrain.get(p) == "+":
                return "%s is a door; go door walks beside it" % target
            if p not in level.terrain and p not in level.object_squares:
                return "%s has not been seen" % target
            return "no known route to %s" % target
        if word != "frontier":
            return "no known route to %s" % target
        facts = ["no reachable known square borders unexplored ones"]
        doors = []
        for p, _, label, goals in self.landmarks(view, level):
            lengths = [len(level.route(terrain, goal)) for goal in goals if goal in terrain]
            if label == "door":
                doors.append("%s (%s)" % (xy(p), min(lengths) if lengths else "no route"))
        if doors:
            facts.append("doors: " + ", ".join(doors[:LISTED]))
        edges = [p for p in terrain if self.edge(level, p)]
        blocking = sorted({q for p in edges for q in level.route(terrain, p) if q in occupied})
        blocking += sorted(q for q in occupied if q not in level.terrain and q not in blocking)
        if blocking:
            facts.append("monsters in the way or on unexplored squares: " + ", ".join(xy(q) for q in blocking[:3]))
        boulders = [e for e in self.observer.entities(view) if e["kind"] == "object" and e["glyph"] in "0`"]
        if boulders:
            facts.append("boulders: " + ", ".join("%s %s" % (e["glyph"], xy(square(e))) for e in boulders[:LISTED]))
        ends = sorted(p for p in level.visits if level.corridor(p) and len(level.corridor_links(p)) <= 1)
        if ends:
            facts.append("corridor ends: " + ", ".join(xy(p) for p in ends[:LISTED]))
        return "; ".join(facts)

    def landmarks(self, view, level):
        """(square, glyph, label, squares to travel to) for remembered features, doors and items."""
        out = []
        for p, ch in sorted(level.terrain.items()):
            if ch in FEATURES:
                out.append((p, ch, level.kind(p), (p,)))
            elif ch == "+":
                out.append((p, ch, "door", tuple(q for key, q in neighbours(p) if key in "hjkl")))
            elif p in level.open_doors:
                out.append((p, ch, "open door", (p,)))
        out += [(p, "", kind, (p,)) for (p, kind) in sorted(level.landmark_messages)]
        for entity in self.observer.entities(view):
            if entity["kind"] == "object":
                p = square(entity)
                out.append((p, entity["glyph"], "statue" if entity.get("statue") else "item", (p,)))
        return out

    @staticmethod
    def edge(level, p):
        """A square beside never-seen ones. Rock beside a corridor never shows, so a corridor square counts only
        at an end of the known corridor."""
        return level.frontier(p) and not (level.corridor(p) and len(level.corridor_links(p)) > 1)

    def frontier(self, level, previous):
        """For each connected group of reachable edge squares, its farthest square, nearest group first."""
        edge = {p for p in previous if self.edge(level, p)}
        distance = {p: len(level.route(previous, p)) for p in edge}
        groups = []
        while edge:
            stack, group = [edge.pop()], []
            while stack:
                p = stack.pop()
                group.append(p)
                for _, q in neighbours(p):
                    if q in edge:
                        edge.discard(q)
                        stack.append(q)
            nearest = min(distance[p] for p in group)
            far = max(group, key=lambda p: (distance[p], p))
            groups.append((nearest, far, distance[far]))
        return [(p, length) for _, p, length in sorted(groups)]

    def plain(self, view):
        """Whether the game is verifiably waiting for a command: no prompt, the cursor on the hero, status shown.

        Naming sends keys, so it happens only then, and only in sessions without a decision engine."""
        prompts = getattr(self.term, "prompts", None)
        return (self.session.engine is None and phase(view) == "play" and not view.engulfed and view.hero
                and tuple(view.cursor) == view.hero and view.ch(*view.hero) == "@"
                and not (prompts and prompts.position)
                and all(view.st.get(key) is not None for key in ("hp", "turn", "location")))

    def protocol(self, action):
        """Run a free information command; True when it completed without spending game time."""
        self.session.step(protocol=action)
        outcome = self.session.last["outcome"]
        if phase(self.term.view()) == "position":
            self.session.step(protocol=Action("input:1b", "Cancel the farlook", "input", "\x1b"))
        return outcome["reason"] == "completed" and not outcome.get("elapsed_turns")

    def farlook(self, view, p):
        """The game's own farlook description of the monster at p, or None."""
        self.observer.current.inspections.pop(p, None)
        action = Action("inspect:%d,%d" % tuple(position(p)), "Inspect %s at %s" % (view.ch(*p), position(p)),
                        "inspect", ";", target=p, followups=(("position", cursor_keys(view.hero, p)[1:]),))
        done = self.protocol(action)
        seen = self.observer.current.inspections.get(p)
        if done and seen and seen["glyph"] == view.ch(*p) and seen["colour"] == view.col(*p):
            return seen["description"]
        return None

    def name_monsters(self, view):
        """Name each monster in view by farlook. A name is kept while the monster stays on its square with the same
        glyph and colour at every view; a monster that moved or arrived is looked at again."""
        level, turn = self.observer.current, view.st.get("turn")
        names, budget = {}, FARLOOKS
        monsters = [e for e in self.observer.entities(view) if e["kind"] == "monster" and e["glyph"] != "~"]
        for entity in sorted(monsters, key=lambda e: chebyshev(square(e), view.hero)):
            key = (level.id, square(entity), entity["glyph"], entity["colour"], entity["bright"],
                   entity["pet_highlight"])
            named = self.names.get(key)
            if named is None and budget and self.plain(self.term.view()):
                budget -= 1
                name = self.farlook(view, square(entity))
                if self.term.view().st.get("turn") != turn:
                    break
                named = (name, turn) if name else None
            if named:
                names[key] = named
        self.names = names

    def look_underfoot(self, view):
        """Ask the game what is underfoot (`:`, no game time) after the hero arrives on a square whose terrain was
        never seen or where an object lay."""
        level, hero, turn = self.observer.current, view.hero, view.st.get("turn")
        arrived = self.where != (level.id, hero)
        self.where = (level.id, hero)
        known = level.underfoot.get(hero)
        if not arrived or known and known["observed_turn"] == turn or not (
                hero in level.inferred_floor or hero in self.items):
            return
        if self.protocol(Action("look_here", "Inspect the square underfoot", "look_here", ":")):
            level.underfoot[hero] = {"notes": messages(self.session.last["outcome"].get("frames", [])),
                                     "observed_turn": turn}

    def view(self, report=None):
        """The compact view: status, new messages, any prompt, the map and what is remembered around it."""
        self.session.observe()
        if self.observer.message_total > self.reported:
            fresh = min(self.observer.message_total - self.reported, len(self.observer.messages))
            self.fresh += [message["text"].replace("--More--", "").strip()
                           for message in list(self.observer.messages)[-fresh:]]
        view = self.term.view()
        if self.plain(view):
            self.name_monsters(view)
            if self.plain(self.term.view()):
                self.look_underfoot(self.term.view())
            view = self.term.view()
        else:
            self.names = {}
        self.session.observe()
        self.reported = self.observer.message_total
        mode = phase(view)
        if mode in ("play",) + PROMPTS and any(ord(ch) > 127 for row in view.rows[1:22] for ch in row):
            raise ValueError(ASCII)
        lines = [report] if report else []
        if view.st.get("hp") is not None:
            lines += [squash(view.rows[23]), squash(view.rows[22])]
        shown = view.msg.replace("--More--", "").strip()
        lines += ["msg: " + text for text in self.fresh if text != shown or mode == "play"]
        self.fresh = []
        keys = hint(view, mode)
        if mode not in ("play",) + PROMPTS:
            screen = view.rows[:22] if view.st.get("hp") is not None else view.rows
            lines.append("screen (%s):" % ("game over" if mode == "ended" else mode))
            lines += [row.rstrip() for row in screen if row.strip()]
            return "\n".join(lines + ([keys] if keys else []))
        if mode != "play":
            lines.append("prompt: " + shown)
            if mode == "position" and 1 <= view.cursor[0] <= 21:
                lines.append("cursor: " + xy(view.cursor))
            lines.append(keys)
        lines += map_lines(view)
        if mode != "play":
            return "\n".join(lines)
        if view.engulfed:
            lines.append("you: %s, engulfed" % xy(view.hero))
            return "\n".join(lines)
        lines += self.surroundings(view)
        return "\n".join(lines)

    def underfoot(self, level, hero):
        ground = level.terrain.get(hero)
        parts = []
        if ground is not None and hero not in level.inferred_floor:
            name = level.kind(hero) if ground in FEATURES else "corridor" if level.corridor(hero) else \
                "open door" if hero in level.open_doors else ""
            parts.append((ground + " " + name).strip())
        known = level.underfoot.get(hero)
        if known:
            parts += [note for note in known["notes"] if note]
        return "; ".join(parts) or "unknown"

    def surroundings(self, view):
        level, hero = self.observer.current, view.hero
        previous = level.paths(hero, self.occupied(view))
        terrain = level.paths(hero)
        around = {q: key for key, q in neighbours(hero)}

        def where(p, goals):
            if p in around:
                return "(adjacent %s)" % around[p]
            lengths = [len(level.route(previous, goal)) for goal in goals if goal in previous]
            if lengths:
                return "(%d)" % min(lengths) if min(lengths) or p != hero else "(here)"
            return "(blocked)" if any(goal in terrain for goal in goals) else "(no route)"

        lines = ["you: %s  here: %s" % (xy(hero), self.underfoot(level, hero))]
        shown = []
        for entity in sorted(self.observer.entities(view), key=lambda e: chebyshev(square(e), hero)):
            if entity["kind"] == "object":
                continue
            p = square(entity)
            key = (level.id, p, entity["glyph"], entity["colour"], entity["bright"], entity["pet_highlight"])
            name, looked = self.names.get(key, (None, None))
            label = name or ("unseen" if entity["kind"] == "unseen" else "")
            if name and looked != view.st.get("turn"):
                label += " (looked T:%d)" % looked
            if entity["pet_highlight"] and "tame" not in label:
                label = (label + " pet").strip()
            near = "adjacent %s" % around[p] if p in around else "%d away" % chebyshev(p, hero)
            shown.append(" ".join(part for part in (entity["glyph"], label, xy(p), near) if part))
        if shown:
            lines.append("monsters: " + "; ".join(shown))
        features, items = [], []
        self.items = set()
        for p, glyph, label, goals in self.landmarks(view, level):
            if p == hero:
                continue
            text = " ".join(part for part in (glyph, label if label != "item" else "", xy(p), where(p, goals))
                            if part)
            lengths = [len(level.route(previous, goal)) for goal in goals if goal in previous]
            entry = (not lengths, min(lengths) if lengths else 0, p, text)
            if label in ("item", "statue"):
                self.items.add(p)
                items.append(entry)
            else:
                features.append(entry)
        for title, entries in (("seen", features), ("items", items)):
            if entries:
                entries.sort()
                more = len(entries) - LISTED
                lines.append("%s: %s%s" % (title, "; ".join(entry[3] for entry in entries[:LISTED]),
                                           "; +%d more" % more if more > 0 else ""))
        edges = self.frontier(level, previous)
        if edges:
            more = len(edges) - LISTED
            lines.append("frontier: %s%s" % ("; ".join("%s (%d)" % (xy(p), length) for p, length in edges[:LISTED]),
                                             "; +%d more" % more if more > 0 else ""))
        return lines
