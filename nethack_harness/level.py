"""Observed terrain and shortest paths through known squares."""
import heapq
import re
from itertools import count

from .knowledge import DIRS, ITEMS, MON, WARNING, trap_kind


FEATURES = {"<": "up stairs", ">": "down stairs", "^": "trap", "_": "altar",
            "{": "fountain", "}": "water or lava", "\\": "throne"}
LIQUIDS = {"red": "lava", "blue": "water"}
# Remembered water or lava is not ground: routes, like the game's own travel, never cross it.
PASSABLE = ".#<>^{_\\"
REFUSED_STEPS = 64


def on_map(p):
    return 1 <= p[0] <= 21 and 0 <= p[1] < 80


def position(p):
    """The [x, y] of a (row, column) square: x is the terminal column, y the terminal row, as the view prints them."""
    return [p[1], p[0]] if p is not None else None


def square(xy):
    """The (row, column) square of an [x, y] position."""
    return xy[1], xy[0]


def xy(p):
    return "%d,%d" % (p[1], p[0])


def neighbours(p):
    for key, (dr, dc) in DIRS.items():
        q = p[0] + dr, p[1] + dc
        if on_map(q):
            yield key, q


def door_line(view, r, c):
    """Whether the square sits in a wall line, as a door does; a spellbook, also drawn +, lies on a floor."""
    return view.ch(r, c - 1) == "-" or view.ch(r, c + 1) == "-" or view.ch(r - 1, c) == "|" or view.ch(r + 1, c) == "|"


DOOR, OBJECT, DOOR_OR_OBJECT = "door", "object", "door or object"


def plus_kind(view, terrain, r, c):
    """What a + at (r, c) is: a closed door and a spellbook are both drawn +.

    A door sits in a wall line, so a + with a wall glyph continuing its line, or remembered as a door, is a door.
    One on remembered ground, or with floor beside it both across and along, is in no wall line: an object. The screen
    cannot tell the rest, such as a + at a corridor's end whose walls have not been seen."""
    remembered = terrain.get((r, c))
    if door_line(view, r, c) or remembered == "+":
        return DOOR
    if remembered is not None or ("." in (view.ch(r - 1, c), view.ch(r + 1, c)) and
                                  "." in (view.ch(r, c - 1), view.ch(r, c + 1))):
        return OBJECT
    return DOOR_OR_OBJECT


def cursor_keys(start, target):
    dr, dc = target[0] - start[0], target[1] - start[1]
    return ";@" + ("l" * dc if dc > 0 else "h" * -dc) + ("j" * dr if dr > 0 else "k" * -dr) + "."


class Level:
    def __init__(self, name, label):
        self.id, self.label = name, label
        self.terrain = {}
        self.colours = {}
        self.inferred_floor = set()
        self.seen = set()
        self.object_squares = set()
        # Squares showing a + that may be a closed door or an object: routes neither enter nor leave them diagonally.
        self.maybe_doors = set()
        self.open_doors = set()
        self.visits = {}
        self.searches = {}
        self.feature_turns = {}
        self.inspections = {}
        self.trap_kinds = {}
        # Statues are drawn as the monster they depict; a farlook that named one keeps its square an object.
        self.statues = {}
        self.underfoot = {}
        # Steps that left the hero in place without a turn passing, as (square, direction key), oldest first.
        self.refused = {}

    def remember_terrain(self, p, ch, turn, colour=None):
        previous = self.terrain.get(p)
        self.terrain[p] = ch
        if colour is not None:
            self.colours[p] = colour
        elif previous != ch:
            self.colours.pop(p, None)
        self.inferred_floor.discard(p)
        if ch in FEATURES:
            self.feature_turns[p] = turn
        else:
            self.feature_turns.pop(p, None)

    def observe(self, view):
        blind = "Blind" in view.cond
        for r in range(1, 22):
            for c in range(80):
                p, ch = (r, c), view.ch(r, c)
                if p == view.hero or ch == " " or ch in WARNING or ch == "I":
                    continue
                statue = self.statues.get(p) == (ch, view.col(r, c))
                if ch in MON and not statue:
                    # A monster sensed while blind does not show its square.
                    if not blind:
                        self.seen.add(p)
                    continue
                self.seen.add(p)
                plus = plus_kind(view, self.terrain, r, c) if ch == "+" else None
                if plus == DOOR_OR_OBJECT:
                    self.maybe_doors.add(p)
                else:
                    self.maybe_doors.discard(p)
                if ch in ITEMS or statue or plus in (OBJECT, DOOR_OR_OBJECT):
                    if p not in self.terrain:
                        self.object_squares.add(p)
                    continue
                self.object_squares.discard(p)
                self.remember_terrain(p, ch, view.st.get("turn"), view.col(r, c)[0])
                if ch in "|-" and view.fg(r, c) in ("brown", "yellow"):
                    self.open_doors.add(p)
                else:
                    self.open_doors.discard(p)
        if view.hero:
            self.seen.add(view.hero)
            self.object_squares.discard(view.hero)
            if view.hero not in self.terrain:
                self.terrain[view.hero] = "."
                self.inferred_floor.add(view.hero)

    def corridor(self, p):
        return self.terrain.get(p) == "#" and self.colours.get(p) not in ("green", "cyan")

    def structural_obstacles(self):
        return [{"position": position(p), "glyph": "#", "colour": self.colours[p],
                 "source": "remembered terminal terrain"}
                for p, ch in sorted(self.terrain.items()) if ch == "#" and not self.corridor(p)]

    def kind(self, p):
        """The remembered feature at p; a liquid is named by its terminal colour when that identifies it."""
        ch = self.terrain.get(p)
        if ch == "}":
            return LIQUIDS.get(self.colours.get(p), FEATURES[ch])
        if ch == "^":
            return self.trap_kinds.get(p, FEATURES[ch])
        return FEATURES.get(ch, ch)

    def note_underfoot(self, p, notes, turn):
        """Keep what the game said about the hero's square, and the stairs or trap it names."""
        self.underfoot[p] = {"notes": list(notes), "observed_turn": turn}
        for note in notes:
            stairs = re.search(r"\b(?:staircase|ladder) (up|down)\b", note)
            if stairs and re.search(r"\bhere\.?$", note):
                self.remember_terrain(p, "<" if stairs[1] == "up" else ">", turn)
            trap = trap_kind(note)
            if trap:
                self.remember_terrain(p, "^", turn)
                self.trap_kinds[p] = trap

    def refuse(self, p, key):
        """Keep the step from p in direction key out of routes, forgetting the oldest beyond REFUSED_STEPS."""
        self.refused.pop((p, key), None)
        self.refused[(p, key)] = True
        while len(self.refused) > REFUSED_STEPS:
            del self.refused[next(iter(self.refused))]

    def guarded(self, p):
        """The name of remembered terrain at p that the game checks before a plain move enters it, or None.

        It refuses a step into known water or lava, and asks before one onto a known trap or into a visible gas
        cloud, which is drawn as a coloured #. The movement prefix that suppresses autopickup skips these checks."""
        ch = self.terrain.get(p)
        if ch in ("}", "^"):
            return self.kind(p)
        if ch == "#" and not self.corridor(p):
            return "%s #" % self.colours[p]
        return None

    def passable(self, p):
        # Objects lie on passable ground, so a square seen only under an object can be routed through.
        return self.terrain.get(p, " ") in PASSABLE or p in self.open_doors or p in self.object_squares

    def linked(self, p, q, key, connects):
        """Cardinal neighbours connect; a diagonal step connects only where no cardinal path joins them."""
        if key in "hjkl":
            return True
        return not (connects((p[0], q[1])) or connects((q[0], p[1])))

    def corridor_links(self, p):
        def connects(q):
            return (self.terrain.get(q, " ") in PASSABLE + "+" or q in self.open_doors) and (
                self.terrain.get(q) != "#" or self.corridor(q))
        return [q for key, q in neighbours(p) if connects(q) and (
            key in "hjkl" or self.corridor(q) and self.corridor(p) and self.linked(p, q, key, connects))]

    def continuations(self, p, covered):
        """Corridor squares beyond p that were not covered, cardinal ones first."""
        options = [q for key, q in neighbours(p) if key in "hjkl" and q not in covered and self.corridor(q)]
        if options:
            return options
        return [q for key, q in neighbours(p) if key in "yubn" and q not in covered and self.corridor(q)
                and self.linked(p, q, key, self.corridor)]

    def paths(self, start, occupied=()):
        """Shortest routes that take the fewest refused steps, then cross the fewest remembered traps: like the
        game's own travel, a route crosses a trap only where no other known route exists, and it takes a step
        the game refused only where no route avoids one."""
        previous, cost = {start: None}, {start: (0, 0, 0)}
        order = count()
        queue = [(0, 0, 0, next(order), start)]
        blocked = set(occupied) - {start}
        while queue:
            refused, traps, length, _, p = heapq.heappop(queue)
            if (refused, traps, length) != cost[p]:
                continue
            for key, q in neighbours(p):
                if q in blocked or not self.passable(q):
                    continue
                if self.terrain.get(q) == "#" and not self.corridor(q):
                    continue
                if key in "yubn" and ({p, q} & self.open_doors or {p, q} & self.maybe_doors):
                    continue
                step = (refused + ((p, key) in self.refused), traps + (self.terrain.get(q) == "^"), length + 1)
                if q in cost and cost[q] <= step:
                    continue
                cost[q], previous[q] = step, p
                heapq.heappush(queue, step + (next(order), q))
        return previous

    @staticmethod
    def route(previous, target):
        if target not in previous:
            return None
        path = []
        while previous[target] is not None:
            path.append(target)
            target = previous[target]
        return path[::-1]

    def targets(self, previous):
        out = {}
        for p, ch in self.terrain.items():
            if p not in previous:
                continue
            if ch in FEATURES:
                out[p] = self.kind(p)
            elif self.corridor(p):
                exits = self.corridor_links(p)
                if len(exits) <= 1:
                    out[p] = "corridor endpoint"
                elif len(exits) > 2:
                    out[p] = "corridor junction"
            elif self.frontier(p):
                out[p] = "edge of known terrain"
        for door, ch in sorted(self.terrain.items()):
            if ch != "+":
                continue
            for key, p in neighbours(door):
                if key in "hjkl" and p in previous:
                    label = "square beside closed door at %s" % xy(door)
                    out[p] = out[p] + "; " + label if p in out else label
        return out

    def unknown(self, q):
        """A square that never showed a glyph and that the hero never stood beside.

        Standing on a square shows all of its neighbours; one still blank afterwards is solid rock."""
        return q not in self.seen and not any(n in self.visits for _, n in neighbours(q))

    def frontier(self, p):
        """A square the hero has not stood on with an unknown square beside it in a cardinal direction.

        The game digs corridors one orthogonal step at a time, so the blank square diagonally past a corridor
        bend that a route cut across is no way on."""
        return p not in self.visits and any(self.unknown(q) for key, q in neighbours(p) if key in "hjkl")

    def rows(self):
        return ["".join(self.terrain.get((r, c), " ") for c in range(80)).rstrip() for r in range(1, 22)]

    def blocked_targets(self, start, occupied):
        terrain_paths = self.paths(start)
        available = self.paths(start, occupied)
        blocked = []
        for target, description in sorted(self.targets(terrain_paths).items()):
            if target in available:
                continue
            route = self.route(terrain_paths, target)
            blocked.append({"position": position(target), "description": description,
                            "terrain_path_length": len(route),
                            "occupied_on_terrain_path": [position(p) for p in route if p in occupied]})
        return blocked

    def landmarks(self):
        return [{"position": position(p), "kind": self.kind(p), "glyph": ch,
                 "observed_turn": self.feature_turns.get(p), "source": "terminal map or underfoot message"}
                for p, ch in sorted(self.terrain.items()) if ch in FEATURES]
