"""Structured observations from terminal output and previously observed facts."""
import hashlib
import json
import re
import string
from collections import deque

from .knowledge import ITEMS, MON, WARNING
from .level import Level, neighbours, position
from .tools import TOOLS


MENU_ENTRY = re.compile(r"^\s*([!-~])\s+([-+*#])\s+(.+)")


def menu_rows(view):
    left, end = 0, len(view.rows)
    for index, row in enumerate(view.rows):
        footer = re.search(r"\((?:end|\d+ of \d+)\)\s*$", row)
        if footer:
            # TTY corner windows leave the map to their left. The footer follows
            # the same one-column inset as the menu entries.
            left, end = max(0, footer.start() - 1), index + 1
            break
    yield from (row[left:] for row in view.rows[:end])


def parse_menu_entries(view):
    entries = []
    for row in menu_rows(view):
        match = MENU_ENTRY.search(row)
        if match and match[3].strip():
            key, marker, text = match.groups()
            entries.append({"key": key, "text": text.strip(),
                            "selection": "partial" if marker == "#" else "all" if marker in "+*" else "none"})
    return entries


def fingerprint(view):
    data = [view.rows, view.fgs, view.bolds, view.revs, view.cursor]
    return hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def phase(view):
    if view.ended:
        return "ended"
    if view.more:
        return "more"
    if view.direction:
        return "direction"
    if view.obj is not None:
        return "item"
    if view.spell is not None:
        return "spell"
    if view.yn:
        return "choice"
    if view.asking and view.msg:
        return "text"
    if view.menu:
        return "menu"
    if view.getpos:
        return "position"
    return "play" if view.normal else "unknown"


class Observer:
    def __init__(self):
        self.levels = []
        self.current = None
        self.previous = None
        self.messages = deque(maxlen=20)
        self.history = deque(maxlen=8)
        self.inventory = {}
        self.spells = {}
        self.query_turns = {}
        self.query_complete = {}
        self.attributes = []
        self.overview = []
        self.reading = None
        self.read_items = {}
        self.read_lines = []
        self.reading_turn = None
        self.read_pages = set()
        self.read_total = None
        self.read_seen_pages = set()
        self.prayers = []
        self.edges = {}
        self.arrivals = {}
        self.last_action = None
        self.last_play_message = None
        self.game_result = None

    def begin(self, action, view):
        self.last_action = (action, self.current, view.hero)
        tool = TOOLS.get(action.kind)
        if tool and tool.query:
            self.reading = tool.query
            self.read_items = {}
            self.read_lines = []
            self.reading_turn = view.st.get("turn")
            self.read_pages = set()
            self.read_total = None
            self.read_seen_pages = set()
            self.query_complete[self.reading] = False
        if action.kind == "pray":
            self.prayers.append({"turn": view.st.get("turn"), "event": "prayer requested"})

    def ingest(self, view):
        if view.ended and (self.game_result is None or view.result):
            self.game_result = {"outcome": view.result or "unknown", "observed_turn": view.st.get("turn"),
                                "source": "terminal end-of-game text", "message": view.msg}
        new_message = view.msg and (not self.messages or self.messages[-1]["text"] != view.msg)
        if new_message:
            self.messages.append({"turn": view.st.get("turn"), "text": view.msg})
        mode = phase(view)
        if self.reading and mode in ("menu", "more"):
            self.query_turns[self.reading] = self.reading_turn
            marker = re.search(r"\((\d+) of (\d+)\)", view.text_screen())
            if marker:
                self.read_pages.add(int(marker[1]))
                self.read_total = int(marker[2])
            elif "(end)" in view.text_screen():
                self.read_pages.add(1)
                self.read_total = 1
            self.query_complete[self.reading] = (self.read_total is not None and
                self.read_pages == set(range(1, self.read_total + 1)))
            lines = tuple(menu_rows(view))
            page = int(marker[1]) if marker else lines
            if page not in self.read_seen_pages:
                self.read_seen_pages.add(page)
                self.read_lines.extend(line.rstrip() for line in lines if line.strip())
            for entry in parse_menu_entries(view):
                if self.reading == "spells" and entry["key"] not in string.ascii_letters:
                    continue
                self.read_items[entry["key"]] = entry["text"]
            if self.reading == "inventory":
                self.inventory = dict(self.read_items)
            elif self.reading == "spells":
                self.spells = dict(self.read_items)
            elif self.reading == "attributes":
                self.attributes = list(self.read_lines)
            elif self.reading == "overview":
                self.overview = list(self.read_lines)
        if self.reading and mode == "play":
            if self.reading == "inventory" and "not carrying anything" in view.msg:
                self.inventory = {}
                self.query_turns["inventory"] = self.reading_turn
                self.query_complete["inventory"] = True
            self.reading = None
        if mode != "play":
            return
        label = view.st.get("location")
        changed = self.current is None or self.current.label != label
        if changed:
            edge = None
            if self.last_action:
                action, old, origin = self.last_action
                if old and origin and action.kind in ("ascend", "descend"):
                    edge = (old.id, origin, action.kind)
            known = self.edges.get(edge)
            if known is None and edge:
                reverse = "ascend" if edge[2] == "descend" else "descend"
                for traversed, destination in self.edges.items():
                    if destination is not self.last_action[1] or traversed[2] != reverse:
                        continue
                    candidate = next(lv for lv in self.levels if lv.id == traversed[0])
                    if (self.arrivals[traversed] == edge[1] and traversed[1] == view.hero and
                            candidate.label == label):
                        known = candidate
                        break
            if known:
                self.current = known
            else:
                self.current = Level("level-%d" % (len(self.levels) + 1), label)
                self.levels.append(self.current)
            if edge:
                self.edges[edge] = self.current
                self.arrivals[edge] = view.hero
        if view.engulfed:
            self.last_play_message = view.msg
            return
        self.current.observe(view)
        for word, glyph in (("up", "<"), ("down", ">")):
            if re.search(r"(?:staircase|ladder) %s here" % word, view.msg, re.I):
                self.current.remember_terrain(view.hero, glyph, view.st.get("turn"))
        if view.msg != self.last_play_message and view.hero:
            evidence = {"text": view.msg, "observed_turn": view.st.get("turn")}
            if (re.search(r"Welcome(?: again)? to .+!", view.msg) or
                    re.search(r"This shop (?:seems to be|is) (?:deserted|untended)\.", view.msg)):
                self.current.landmark_messages[(view.hero, "shop entry")] = dict(
                    evidence, source="entry message", extent="message location; shop boundary and current contents unknown")
            if re.search(r"You feel a strange vibration (?:under|beneath) ", view.msg):
                self.current.landmark_messages[(view.hero, "vibrating square observation")] = dict(
                    evidence, source="underfoot message", extent="message location; current feature not reverified")
        self.last_play_message = view.msg
        signature = (self.current.id, view.hero, view.st.get("turn"))
        if signature != self.previous:
            self.current.visits[view.hero] = self.current.visits.get(view.hero, 0) + 1
            self.previous = signature

    def remember_look(self, target, before, after, source="inspect"):
        if phase(after) != "play" or before.engulfed or after.engulfed:
            return
        parts = re.findall(r"\(([^()]*)\)", after.msg)
        text = (parts[-1] if parts and source == "inspect" else after.msg).strip()
        if text and self.current:
            self.current.inspections[target] = {
                "description": text, "observed_turn": before.st.get("turn"),
                "glyph": before.ch(*target), "colour": before.col(*target), "source": source}

    def entities(self, view):
        entities = []
        if not view.normal or view.engulfed:
            return entities
        for r in range(1, 22):
            for c in range(80):
                p, ch = (r, c), view.ch(r, c)
                if p == view.hero or ch not in MON | ITEMS | WARNING | {"I", "0", "`"}:
                    continue
                colour, bright = view.col(r, c)
                kind = "monster" if ch in MON else "unseen" if ch in WARNING | {"I"} else "object"
                entry = {"position": position(p), "glyph": ch, "colour": colour, "bright": bright,
                         "kind": kind, "pet_highlight": view.pet(r, c)}
                looked = self.current.inspections.get(p) if self.current else None
                if (looked and looked["observed_turn"] == view.st.get("turn") and
                        looked["glyph"] == ch and looked["colour"] == (colour, bright)):
                    entry["last_inspection"] = looked
                entities.append(entry)
        return entities

    def observation(self, view):
        self.ingest(view)
        mode = phase(view)
        out = {"fingerprint": fingerprint(view), "phase": mode,
               "game_result": self.game_result,
               "coordinates": {"base": 1, "map_origin": [2, 1], "map_size": [21, 80]}, "hero": dict(view.st, position=position(view.hero),
               title=view.title, conditions=list(view.cond), engulfed=view.engulfed),
               "messages": list(self.messages), "entities": self.entities(view),
               "inventory": dict(items=self.inventory, **self.query_metadata("inventory", view)),
               "spells": dict(items=self.spells, **self.query_metadata("spells", view)),
               "attributes": dict(lines=self.attributes, **self.query_metadata("attributes", view)),
               "dungeon_overview": dict(lines=self.overview, **self.query_metadata("overview", view)),
               "prayers": list(self.prayers), "recent_actions": self.recent_actions(),
               "repetition": self.repetition()}
        if mode == "play":
            occupied = {tuple(x - 1 for x in entity["position"]) for entity in out["entities"]
                        if entity["kind"] in ("monster", "unseen")}
            out["navigation_obstacles"] = {
                "source": "map navigation unavailable while engulfed" if view.engulfed else
                          "observed occupied squares exclude routes; terrain paths do not guarantee passage",
                "occupied_squares": [position(p) for p in sorted(occupied)],
                "unavailable_destinations": [] if view.engulfed else self.current.blocked_targets(view.hero, occupied)}
            out["underfoot"] = {"position": position(view.hero),
                                "remembered_terrain": None if view.engulfed else self.current.terrain.get(view.hero),
                                "basis": "unavailable while engulfed" if view.engulfed else
                                "inferred floor placeholder" if view.hero in self.current.inferred_floor
                                else "observed map or underfoot message"}
            out["adjacent"] = [] if view.engulfed else [{"direction": key, "position": position(p), "glyph": view.ch(*p),
                                "colour": view.fg(*p), "remembered_terrain": self.current.terrain.get(p)}
                               for key, p in neighbours(view.hero)]
            out["map"] = [row.rstrip() for row in view.rows[1:22]]
            out["map_context"] = "engulfed overlay" if view.engulfed else "dungeon"
            out["level"] = {"id": self.current.id, "label": self.current.label,
                            "known_terrain": self.current.rows(),
                            "structural_obstacles": self.current.structural_obstacles(),
                            "inferred_floor": [position(p) for p in sorted(self.current.inferred_floor)],
                            "inspections": [dict(value, position=position(p))
                                            for p, value in sorted(self.current.inspections.items())],
                            "visits": [position(p) + [n] for p, n in sorted(self.current.visits.items())],
                            "searches": [position(p) + [n] for p, n in sorted(self.current.searches.items())]}
            out["known_levels"] = [{"id": lv.id, "label": lv.label,
                                    "landmarks": lv.landmarks(),
                                    "connections": [{"position": position(p), "action": action, "destination": dest.id}
                                                    for (origin, p, action), dest in sorted(self.edges.items())
                                                    if origin == lv.id]}
                                   for lv in self.levels]
        else:
            out["level"] = {"id": self.current.id, "label": self.current.label} if self.current else None
            out["screen"] = view.text_screen()
            out["prompt"] = view.msg
            out["menu_entries"] = parse_menu_entries(view)
        return out

    def query_metadata(self, name, view):
        turn = self.query_turns.get(name)
        now = view.st.get("turn")
        return {"observed_turn": turn, "age_turns": now - turn if now is not None and turn is not None else None,
                "complete": self.query_complete.get(name, False), "source": name + " query",
                "freshness": "last observed snapshot; intervening actions may have changed it"}

    def recent_actions(self):
        events, previous = [], None
        for event in self.history:
            if event == previous:
                events[-1]["repetitions"] = events[-1].get("repetitions", 1) + 1
            else:
                events.append(dict(event))
            previous = event
        return events

    def repetition(self):
        if not self.history:
            return None
        last = self.history[-1]
        tail = []
        for entry in reversed(self.history):
            if entry["action"] != last["action"]:
                break
            tail.append(entry)
        turns = [entry["elapsed_turns"] for entry in tail]
        return {"action": last["action"], "consecutive_in_recent_history": len(tail),
                "elapsed_turns": sum(turns) if all(t is not None for t in turns) else None,
                "position_unchanged": all(entry["position_before"] == entry["position_after"] for entry in tail)}
