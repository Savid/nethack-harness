"""Executable choices with explicit targets and bounded durations."""
from dataclasses import dataclass
import string

from .knowledge import DIRECTION_NAMES, MON
from .level import FEATURES, neighbours, position, cursor_keys
from .perceive import parse_menu_entries, phase
from .tools import PAUSE, TOOLS

WITHHELD = "modifier withheld toward remembered %s, so the game's own refusal or confirmation applies"


@dataclass(frozen=True)
class Action:
    id: str
    description: str
    kind: str
    keys: str = ""
    steps: int = 1
    target: tuple = None
    route: tuple = ()
    followups: tuple = ()
    subject: str = ""
    frontier: bool = False
    withheld: str = ""

    @property
    def tool(self):
        spec = TOOLS.get(self.kind)
        return spec.group or self.kind if spec else self.kind

    def as_dict(self):
        result = {"id": self.id, "description": self.description, "kind": self.kind,
                  "tool": self.tool, "max_steps": self.steps, "target": position(self.target)}
        if self.subject:
            result["subject"] = self.subject
        return result


def input_action(key, description=None):
    return Action("input:%02x" % ord(key), description or "Type %r" % key, "input", key)


def expand_letters(text):
    chars = set()
    i = 0
    while i < len(text):
        if i + 2 < len(text) and text[i].isalpha() and text[i + 1] == "-" and text[i + 2].isalpha():
            chars.update(chr(n) for n in range(ord(text[i]), ord(text[i + 2]) + 1))
            i += 3
        else:
            if text[i] in string.ascii_letters + string.digits + string.punctuation:
                chars.add(text[i])
            i += 1
    return chars


def catalogue(view, observer, max_steps):
    mode = phase(view)
    if mode == "ended":
        return []
    if mode == "more":
        return [Action("continue", "Display the next page", "continue", " ")]
    pause = Action("pause", PAUSE, "pause", steps=0)
    if mode == "unknown":
        return [pause]
    if mode != "play":
        labels = {}
        if mode == "choice":
            labels = {ch: "Answer %s" % ch for ch in view.yn}
        elif mode in ("item", "spell"):
            entries = observer.inventory if mode == "item" else observer.spells
            letters = view.obj if mode == "item" else view.spell
            labels = {ch: entries.get(ch, "Choose %s" % ch) for ch in expand_letters(letters)}
            labels.update({"?": "List applicable choices", "*": "List all choices"})
            if mode == "item":
                labels.update({ch: "Type quantity digit %s before selecting an item" % ch for ch in string.digits})
        elif mode == "direction":
            labels = dict(DIRECTION_NAMES, **{".": "self", "<": "up", ">": "down"})
        elif mode == "menu":
            labels = {" ": "Advance or accept this menu", "\r": "Accept selections", ">": "Next page",
                      "<": "Previous page", "^": "First page", "|": "Last page",
                      ".": "Select all entries", "-": "Deselect all entries", "@": "Invert all selections",
                      ",": "Select this page", "\\": "Deselect this page", "~": "Invert this page",
                      ":": "Search menu entries", "{": "Scroll menu left", "}": "Scroll menu right"}
            labels.update({ch: "Type quantity digit %s before selecting an entry" % ch for ch in string.digits})
            labels.update({entry["key"]: entry["text"] + " (selection: " + entry["selection"] + ")"
                           for entry in parse_menu_entries(view)})
        elif mode == "position":
            labels = {key: "Move cursor " + name for key, name in DIRECTION_NAMES.items()}
            labels.update({".": "Confirm this position", ",": "Select with a quick description",
                           ";": "Select this position once", ":": "Select with a detailed description",
                           "@": "Move cursor to hero", "?": "Show targeting help",
                           "m": "Next monster", "M": "Previous monster", "o": "Next object", "O": "Previous object",
                           "d": "Next door", "D": "Previous door", "x": "Next unexplored location",
                           "X": "Previous unexplored location", "v": "Next valid target", "V": "Previous valid target"})
        else:
            labels = {ch: "Type %r" % ch for ch in string.printable if ch in string.ascii_letters + string.digits + string.punctuation + " "}
            labels.update({"\r": "Submit text", "\x08": "Delete previous character"})
        labels["\x1b"] = "Cancel this prompt"
        return [input_action(key, desc) for key, desc in sorted(labels.items())] + [pause]
    actions = []
    adjacent = dict(neighbours(view.hero))
    level = None if view.engulfed else observer.current
    for tool in TOOLS.values():
        if not tool.keys and not tool.directions:
            continue
        for variant in (None,) + tool.variants:
            suffix = ":" + variant.name if variant else ""
            prefix = variant.prefix if variant else ""
            description = tool.description + ("; " + variant.description if variant else "")
            if tool.directions:
                for key in tool.directions:
                    target = adjacent.get(key, view.hero if key in ".<>" else None)
                    if target is None:
                        continue
                    direction = dict(DIRECTION_NAMES, **{".": "self", "<": "up", ">": "down"})[key]
                    withheld = ""
                    if variant and variant.skips_guards and level and level.guarded(target):
                        withheld = WITHHELD % level.guarded(target)
                    label = "%s: %s at %s" % (description + ("; " + withheld if withheld else ""),
                                              direction, position(target))
                    if tool.name == "move":
                        notes = [repr(view.ch(*target))]
                        if view.pet(*target) and view.ch(*target) in MON:
                            notes.append("highlighted pet")
                        if level and level.terrain.get(target) in FEATURES:
                            notes.append("remembered " + level.kind(target))
                        label += " (glyph %s)" % ", ".join(notes)
                    checked = tool.name in ("open", "close", "kick")
                    actions.append(Action(tool.name + suffix + ":" + key, label, tool.name,
                                          ("" if withheld else prefix) + tool.keys + ("" if checked else key),
                                          target=target, followups=(("direction", key),) if checked else (),
                                          withheld=withheld))
            else:
                for count in sorted({1, max_steps}) if tool.repeat else (1,):
                    label = description + (" for up to %d turns" % count if tool.repeat else "")
                    identifier = tool.name + suffix + (":%d" % count if tool.repeat else "")
                    actions.append(Action(identifier, label, tool.name, prefix + tool.keys, count))
    if view.engulfed:
        return actions + [pause]
    entities = observer.entities(view)
    occupied = {tuple(x - 1 for x in e["position"]) for e in entities if e["kind"] in ("monster", "unseen")}
    previous = level.paths(view.hero, occupied)
    targets = level.targets(previous)
    inspections = {p: "remembered " + level.kind(p) for p, ch in level.terrain.items() if ch in FEATURES}
    inspections[view.hero] = "the hero's square"
    for entity in entities:
        p = tuple(x - 1 for x in entity["position"])
        inspections[p] = entity["glyph"]
        if p in previous and entity["kind"] == "object":
            targets[p] = "object %s" % entity["glyph"]
    for p, description in sorted(inspections.items()):
        pos = position(p)
        actions.append(Action("inspect:%d,%d" % tuple(pos), "Inspect %s at %s" % (description, pos), "inspect",
                              ";", target=p, followups=(("position", cursor_keys(view.hero, p)[1:]),)))
    for target, description in sorted(targets.items()):
        if level.route(previous, target):
            actions.append(travel(level, previous, target, description, max_steps))
            if description == "corridor endpoint":
                actions.append(explore(level, previous, target, max_steps))
    return actions + [pause]


def travel(level, previous, target, description, max_steps):
    """Bounded movement along the known route to target; `previous` is from `level.paths`."""
    route = level.route(previous, target)
    pos = position(target)
    return Action("travel:%d,%d" % tuple(pos), "Move toward %s at %s along %d known squares, at most %d steps" % (
                      description, pos, len(route), max_steps),
                  "travel", steps=min(max_steps, len(route)), target=target, route=tuple(route),
                  subject=description, frontier=level.frontier(target))


def explore(level, previous, target, max_steps):
    """Travel to a corridor endpoint, then follow the corridor newly revealed beyond it."""
    pos = position(target)
    return Action("explore:%d,%d" % tuple(pos), "Explore the corridor beyond %s for at most %d steps; stop at a "
                  "branch, room, feature or encounter" % (pos, max_steps),
                  "explore", steps=max_steps, target=target, route=tuple(level.route(previous, target)),
                  subject="corridor endpoint", frontier=level.frontier(target))
