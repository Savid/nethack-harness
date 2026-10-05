"""System One choices over tools and their concrete arguments."""
import json
import time
import urllib.error
import urllib.request

from .base import Paused
from .knowledge import DIRECTION_NAMES
from .level import position
from .tools import TOOLS, tool_description


class DecisionError(Paused):
    def __init__(self, message, response=None, latency=None):
        super().__init__(message)
        self.response, self.latency = response, latency


def reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


GUIDANCE = ("Decide from this state alone; caller_context and objective_progress list earlier attempts and their"
            " results. Prefer a choice that has not already failed to make progress.")
OMITTED_LEVEL = ("known_terrain", "visits", "inferred_floor")


def visit_counts(observation):
    return {tuple(entry[:2]): entry[2] for entry in (observation.get("level") or {}).get("visits", ())}


def destination(action, visits):
    target = position(action.target)
    return {"kind": action.subject, "position": target, "path_length": len(action.route),
            "next_position": position(action.route[0]), "max_steps": action.steps, "visits": visits.get(tuple(target), 0),
            "frontier": action.frontier}


def argument_facts(observation, action, visits):
    facts = {"target": position(action.target), "max_steps": action.steps}
    spec = TOOLS.get(action.kind)
    if action.route:
        facts.update(destination(action, visits))
    if not spec:
        return facts
    parts = action.id.split(":")
    variant = next((v for v in spec.variants if v.name in parts[1:]), None)
    if variant:
        facts["modifier"] = {"name": variant.name, "description": variant.description}
    if spec.directions and parts[-1] in spec.directions:
        key = parts[-1]
        origin = observation.get("hero", {}).get("position")
        facts.update(direction=dict(DIRECTION_NAMES, **{".": "self", "<": "up", ">": "down"})[key],
                     direction_key=key, origin=origin)
        if origin is not None and facts["target"] is not None:
            facts["delta"] = [end - start for start, end in zip(origin, facts["target"])]
        adjacent = next((square for square in observation.get("adjacent", [])
                         if square["direction"] == key and square["position"] == facts["target"]), None)
        if adjacent is not None:
            facts["target_observation"] = {name: adjacent[name] for name in ("glyph", "colour", "remembered_terrain")}
    return facts


def snapshot_summary(snapshot, field):
    return {field: snapshot[field], "age_turns": snapshot["age_turns"], "complete": snapshot["complete"]}


def request_observation(observation):
    """The observation as sent to the engine: complete facts, without map memory the screen already shows."""
    out = {key: value for key, value in observation.items() if key != "fingerprint"}
    if "messages" in out:
        out["messages"] = out["messages"][-8:]
    for key, field in (("inventory", "items"), ("spells", "items"), ("attributes", "lines"), ("dungeon_overview", "lines")):
        if key not in out:
            continue
        if out[key][field] or out[key]["observed_turn"] is not None:
            out[key] = snapshot_summary(out[key], field)
        else:
            del out[key]
    level = out.get("level")
    if level and "known_terrain" in level:
        out["level"] = {key: value for key, value in level.items() if key not in OMITTED_LEVEL and value != []}
    return out


def request_body(observation, actions, objective, model=None, tool=None, caller_context=None):
    direct = observation.get("phase") != "play"
    stage = "arguments" if tool else "input" if direct else "tool"
    if stage == "tool":
        criteria = {action.tool: tool_description(action.tool, action.description) for action in actions}
        instructions = "Choose the tool to use next."
    else:
        criteria = {action.id: action.description for action in actions
                    if tool is None or action.tool == tool or action.kind == "pause"}
        instructions = "Selected tool: %s. Choose its concrete arguments." % tool if tool else "Answer the current prompt."
    if "pause" in criteria:
        instructions += " " + GUIDANCE
    visits = visit_counts(observation)
    state = {"decision": {"stage": stage, "tool": tool}, "objective": objective, "caller_context": caller_context or {}}
    if stage == "tool":
        state["navigation"] = {
            "source": "offered routes through remembered terrain; not a guarantee of passage",
            "frontier": "the hero has not stood there and it borders squares that never showed a glyph",
            "destinations": [dict(action=action.id, **destination(action, visits))
                             for action in actions if action.kind in ("travel", "explore") and action.route]}
    elif stage == "arguments":
        state["argument_facts"] = {action.id: argument_facts(observation, action, visits)
                                   for action in actions if action.id in criteria}
    state["observation"] = request_observation(observation)
    body = {"state": state, "questions": {"action": {"type": "choice", "instructions": instructions + " Objective: " + objective,
                                                      "criteria": criteria}}}
    if model:
        body["model"] = model
    return body


class Engine:
    def __init__(self, url, model=None, key=None, timeout=10):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("decision endpoint must be an HTTP(S) URL")
        self.url, self.model, self.key, self.timeout = url, model, key, timeout

    def choose(self, request):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        started = time.monotonic()
        answer = None

        def failure(message):
            return DecisionError(message, answer, time.monotonic() - started)

        try:
            req = urllib.request.Request(self.url, json.dumps(request, allow_nan=False).encode(), headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise failure("decision response exceeds 1 MiB")
                answer = raw.decode("utf-8", "replace")
                answer = json.loads(raw, parse_constant=reject_constant)
        except urllib.error.HTTPError as e:
            message = "decision endpoint returned HTTP %d" % e.code
            try:
                with e:
                    raw = e.read(1024 * 1024 + 1)
                answer = raw[:1024 * 1024].decode("utf-8", "replace")
                if len(raw) <= 1024 * 1024:
                    try:
                        answer = json.loads(answer, parse_constant=reject_constant)
                    except ValueError:
                        pass
                else:
                    message += " (response truncated after 1 MiB)"
            except OSError:
                pass
            raise failure(message) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise failure("decision request failed (%s)" % type(e).__name__) from None
        try:
            choice = answer["answers"]["action"]["choice"]
            if not isinstance(choice, str) or choice not in request["questions"]["action"]["criteria"]:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise failure("decision response must contain answers.action.choice naming an offered action") from None
        return choice, answer, time.monotonic() - started
