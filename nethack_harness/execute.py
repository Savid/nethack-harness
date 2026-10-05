"""Execute a selected action and stop at observable boundaries."""
import re
import time
from collections import Counter

from .base import Paused
from .knowledge import DIRS, MON
from .level import FEATURES, position
from .perceive import fingerprint, phase
from .transport import Closed, Held
from .tools import TOOLS


class Boundary(Exception):
    def __init__(self, reason):
        self.reason = reason


def interrupt_signature(view, observer, terrain):
    """Observed state whose change ends a bounded action; terrain is "all", "features" or None."""
    level = observer.current
    known = ()
    if level and terrain:
        cells = sorted(level.terrain.items()) if terrain == "all" else sorted(
            (p, ch) for p, ch in level.terrain.items() if ch in FEATURES or ch == "+")
        known = (tuple(cells), tuple((tuple(cell["position"]), cell["colour"]) for cell in level.structural_obstacles()),
                 frozenset(level.open_doors))
    return (tuple(sorted((key, value) for key, value in view.st.items() if key != "turn")),
            tuple(view.cond), view.engulfed, (level.id, known) if level else None)


def monsters(view, observer):
    """Visible monster appearances, and those adjacent to the hero, counted by glyph and colour.

    A monster the terminal highlights as the hero's pet is not counted, so a following pet never arrives."""
    visible, adjacent = Counter(), Counter()
    hero = view.hero
    for entity in observer.entities(view):
        if entity["kind"] not in ("monster", "unseen") or entity["pet_highlight"]:
            continue
        key = (entity["glyph"], entity["colour"], entity["bright"])
        visible[key] += 1
        r, c = (x - 1 for x in entity["position"])
        if hero and max(abs(r - hero[0]), abs(c - hero[1])) <= 1:
            adjacent[key] += 1
    return visible, adjacent


def discovery(p, ch, level, known, known_colours, known_seen):
    """Whether remembered terrain at p is a room or feature found since exploration began.

    A new or changed feature or closed door counts, as does a coloured structure. Other terrain counts only
    where no glyph had ever shown: floor uncovered by a moving monster or redrawn in another colour once out
    of sight was already known, and corridor and wall squares are followed rather than reported."""
    if known.get(p) == ch and known_colours.get(p) == level.colours.get(p):
        return False
    if ch in FEATURES or ch == "+":
        return known.get(p) != ch
    if ch == "#":
        return not level.corridor(p)
    return ch not in "|-" and p not in known_seen


def arrived(start, now):
    """A monster appeared or came adjacent; monsters already in view moving about do not count."""
    return any(now[0][key] > start[0][key] for key in now[0]) or any(now[1][key] > start[1][key] for key in now[1])


class Executor:
    def __init__(self, term, observer, record_send, record_result):
        self.term, self.observer, self.record_send = term, observer, record_send
        self.record_result = record_result
        self.keys = []
        self.frames = []
        self.cancelled = lambda: False

    def send(self, keys, source="action"):
        expected = fingerprint(self.term.view())
        number = None

        def before_send(view):
            nonlocal number
            if self.cancelled():
                raise Boundary("caller_interrupt")
            if view.ended:
                raise Boundary("game_over")
            if fingerprint(view) != expected:
                raise Boundary("observation_changed")
            number = self.record_send(keys, source)
            self.keys.append({"keys": keys, "source": source})
            if not self.action_started:
                self.observer.begin(self.action, self.before)
                self.action_started = True

        self.term.send(keys, before_send=before_send)
        self.record_result(number, "completed")
        view = self.term.view()
        self.observer.ingest(view)
        self.frames.append({"status": dict(view.st), "phase": phase(view), "message": view.msg})
        return view

    def pages(self, view):
        for _ in range(32):
            if phase(view) != "more":
                return view
            view = self.send(" ", "protocol")
        raise Paused("pagination did not finish after 32 pages")

    def query(self, view):
        for _ in range(32):
            view = self.pages(view)
            if phase(view) != "menu":
                return view
            match = re.search(r"\((\d+) of (\d+)\)", view.text_screen())
            key = ">" if match and int(match.group(1)) < int(match.group(2)) else "\x1b"
            view = self.send(key, "protocol")
        raise Paused("information menu did not finish after 32 pages")

    def snapshot(self, reason, error=None):
        turn0, turn1 = self.before.st.get("turn"), self.term.view().st.get("turn")
        result = {"reason": reason, "steps": self.steps, "keys": self.keys, "frames": self.frames,
                  "elapsed_turns": turn1 - turn0 if turn0 is not None and turn1 is not None else None,
                  "elapsed_seconds": time.monotonic() - self.started}
        if error:
            result["error"] = error
        return result

    def run(self, action, expected, cancelled=lambda: False):
        self.keys, self.frames = [], []
        self.steps, self.started = 0, time.monotonic()
        self.before, self.cancelled = self.term.view(), cancelled
        self.action = action
        self.action_started = False
        try:
            result = self._run(action, expected)
        except Boundary as e:
            result = self.snapshot(e.reason)
        except Held:
            result = self.snapshot("input_held", "game input is temporarily unavailable")
        except Closed:
            result = self.snapshot("terminal_closed", "terminal connection ended")
        except Paused as e:
            result = self.snapshot("execution_paused", str(e))
        except Exception as e:
            result = self.snapshot("execution_error", type(e).__name__)
        if self.action_started:
            after = self.term.view()
            event = {"action": action.id, "reason": result["reason"], "steps": result["steps"],
                     "turn": after.st.get("turn"), "elapsed_turns": result["elapsed_turns"],
                     "target": position(action.target), "position_before": position(self.before.hero),
                     "position_after": position(after.hero)}
            if action.kind == "input":
                event.update(keys=action.keys, description=action.description)
            self.observer.history.append(event)
        return result

    def _run(self, action, expected):
        self.term.poll()
        before = self.term.view()
        self.before = before
        if fingerprint(before) != expected:
            return self.snapshot("observation_changed")
        if before.ended:
            return self.snapshot("game_over")
        if action.kind == "pause":
            return self.snapshot("requested_pause")
        if self.cancelled():
            return self.snapshot("caller_interrupt")
        exploring = action.kind == "explore"
        terrain = None if exploring else "features" if action.kind == "travel" else "all"
        signature = interrupt_signature(before, self.observer, terrain)
        presence = monsters(before, self.observer)
        known = dict(self.observer.current.terrain) if exploring else {}
        known_colours = dict(self.observer.current.colours) if exploring else {}
        known_doors = set(self.observer.current.open_doors) if exploring else set()
        known_seen = set(self.observer.current.seen) if exploring else set()
        covered = set(known)
        reason = "completed"
        changed_fields = []
        view = before
        for step in range(action.steps):
            if self.cancelled():
                reason = "caller_interrupt"
                break
            keys = action.keys
            origin = view.hero
            if action.kind in ("travel", "explore"):
                if step < len(action.route):
                    target = action.route[step]
                else:
                    options = self.observer.current.continuations(origin, covered)
                    if len(options) != 1:
                        reason = "branch_discovered" if options else "exploration_boundary"
                        break
                    target = options[0]
                delta = target[0] - origin[0], target[1] - origin[1]
                direction = next((key for key, d in DIRS.items() if d == delta), None)
                if direction is None:
                    reason = "route_changed"
                    break
                # The prefix moves without pickup or attacks, but it also skips the game's own check before
                # a guarded square, so a step onto one is sent plain and any confirmation goes to the engine.
                # Into the highlighted pet the prefix would only bump it; a plain step swaps places with it,
                # as the game's own travel does.
                plain = self.observer.current.guarded(target) or view.pet(*target) and view.ch(*target) in MON
                keys = ("" if plain else "m") + direction
            prior = view
            view = self.send(keys, "protocol" if action.kind in ("continue", "redraw") else "action")
            self.steps += 1
            if getattr(self.term, "redraw_needed", False):
                view = self.send("\x12", "protocol")
            view = self.pages(view)
            for index, (expected_phase, followup_keys) in enumerate(action.followups):
                if phase(view) != expected_phase:
                    return self.snapshot("game_over" if view.ended else "unexpected_prompt")
                view = self.send(followup_keys)
                if action.kind == "inspect" and index + 1 == len(action.followups):
                    self.observer.remember_look(action.target, before, view)
                view = self.pages(view)
            spec = TOOLS.get(action.kind)
            if spec and spec.query:
                view = self.query(view)
            if action.kind == "look_here" and before.hero:
                self.observer.remember_look(before.hero, before, view, source="look_here")
            if action.kind == "search" and self.observer.current and origin and not prior.engulfed:
                self.observer.current.searches[origin] = self.observer.current.searches.get(origin, 0) + 1
            if view.ended:
                reason = "game_over"
                break
            if phase(view) != "play":
                reason = "prompt"
                break
            if action.kind in ("travel", "explore") and view.hero != target:
                reason = "no_observed_effect" if fingerprint(view) == fingerprint(prior) else "movement_interrupted"
                break
            if exploring:
                covered.add(view.hero)
                level = self.observer.current
                discovered = [p for p, ch in level.terrain.items()
                              if discovery(p, ch, level, known, known_colours, known_seen)]
                if discovered or level.open_doors - known_doors:
                    reason = "feature_discovered"
                    break
            if action.kind in ("move", "attack", "open", "close", "kick", "ascend", "descend", "wait", "search") and \
                    fingerprint(view) == fingerprint(prior):
                reason = "no_observed_effect"
                break
            if exploring or step + 1 < action.steps:
                current_signature = interrupt_signature(view, self.observer, terrain)
                changed_fields = [name for name, old, new in zip(
                    ("status", "conditions", "engulfed", "terrain"), signature, current_signature) if old != new]
                if arrived(presence, monsters(view, self.observer)):
                    changed_fields.append("entities")
                if view.msg and view.msg != before.msg:
                    changed_fields.append("message")
                if changed_fields:
                    reason = "observation_changed"
                    break
        else:
            if exploring:
                reason = "step_limit"
        result = self.snapshot(reason)
        if changed_fields:
            result["changed_fields"] = changed_fields
        if exploring:
            result["position"] = position(view.hero)
            result["new_terrain"] = [{"position": position(p), "glyph": ch}
                                     for p, ch in sorted(self.observer.current.terrain.items()) if known.get(p) != ch]
        return result
