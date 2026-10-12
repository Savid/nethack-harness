"""System One choices over tools and their concrete arguments."""
import email.utils
import http.client
import json
import socket
import time
import urllib.error
import urllib.request

from .base import Paused
from .knowledge import DIRECTION_NAMES
from .level import position
from .tools import PAUSE, TOOLS, tool_description

RESPONSE_LIMIT = 1024 * 1024
# Statuses and connection failures that a later attempt can outlive: a full queue, a gateway or engine restarting.
TRANSIENT_STATUS = frozenset((429, 502, 503, 529))
TRANSIENT_ERRORS = (ConnectionRefusedError, ConnectionResetError, TimeoutError, socket.timeout)
BACKOFF = (0.5, 8.0)


class DecisionError(Paused):
    def __init__(self, message, response=None, latency=None, retries=()):
        super().__init__(message)
        self.response, self.latency, self.retries = response, latency, list(retries)


class Failure(Exception):
    """One failed request: its message, response evidence and whether a later attempt may succeed."""

    def __init__(self, message, response=None, status=None, transient=False, retry_after=None):
        super().__init__(message)
        self.response, self.status, self.transient, self.retry_after = response, status, transient, retry_after


def retry_after(headers):
    """Seconds from a Retry-After header, given as seconds or as an HTTP date; None when absent or unreadable."""
    value = (headers or {}).get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def reject_constant(value):
    raise ValueError("non-finite JSON number: " + value)


ARGUMENTS = "If action is %s, choose its concrete arguments."
GUIDANCE = ("Decide from this state alone; observation.history lists this objective's earlier attempts and their "
            "results. Prefer a choice that has not already failed to make progress.")
POSITIONS = "Positions are x,y: terminal column, then row."
REQUEST_ATTEMPTS = 8
REQUEST_MESSAGES = 8
KEPT_MESSAGES = 2
NEAREST_DESTINATIONS = 24
BYTES_PER_TOKEN = 2
DIRECTIONS = dict(DIRECTION_NAMES, **{".": "here", "<": "up", ">": "down"})


def xy_text(point):
    return "%d,%d" % tuple(point)


def visit_counts(observation):
    return {tuple(entry[:2]): entry[2] for entry in (observation.get("level") or {}).get("visits", ())}


def destination_text(action, visits):
    """A destination in one line: what it is, its known route length, whether it is a frontier square, how often the
    hero has stood there, and where a travel bound stops short of it."""
    steps = len(action.route)
    parts = [action.subject, "%d step%s" % (steps, "" if steps == 1 else "s")]
    if action.frontier:
        parts.append("frontier")
    visited = visits.get(tuple(position(action.target)), 0)
    if visited:
        parts.append("visited %d" % visited)
    if action.kind == "travel" and steps > action.steps:
        parts.append("stops after %d" % action.steps)
    return ", ".join(parts)


def square_text(square):
    """An adjacent square in one line: direction, position, glyph, colour, the highlighted pet and remembered terrain."""
    parts = [DIRECTIONS[square["direction"]], xy_text(square["position"]), repr(square["glyph"])]
    if square["colour"] != "default":
        parts.append(square["colour"])
    if square.get("pet_highlight"):
        parts.append("pet")
    if square.get("remembered_feature"):
        parts.append("remembered " + square["remembered_feature"])
    elif square["remembered_terrain"] not in (None, square["glyph"]):
        parts.append("remembered %r" % square["remembered_terrain"])
    return " ".join(parts)


def entity_text(entity):
    parts = [entity["glyph"], entity["colour"] + (" bright" if entity["bright"] else ""), xy_text(entity["position"]),
             entity["kind"]]
    if entity["pet_highlight"]:
        parts.append("pet")
    if entity.get("statue"):
        parts.append("statue")
    if entity.get("last_inspection"):
        parts.append("looked: " + entity["last_inspection"]["description"])
    return " ".join(parts)


def attempt_text(attempt):
    """One attempt in one line: the action, how it ended, its steps and turns, and where the hero went."""
    action, result = attempt["action"], attempt.get("result") or {}
    text = action["id"]
    if action["kind"] == "input":
        text += " (%s)" % action["description"]
    reason = result.get("reason", "pending")
    if result.get("changed_fields"):
        reason += " (%s)" % ", ".join(result["changed_fields"])
    text += ": %s, %s steps" % (reason, result.get("steps", "?"))
    if result.get("elapsed_turns") is not None:
        text += ", %d turns" % result["elapsed_turns"]
    before, after = attempt.get("before") or {}, attempt.get("after") or {}
    if before.get("position") and after.get("position"):
        text += (", stayed at %s" % xy_text(before["position"]) if before["position"] == after["position"] else
                 ", %s to %s" % (xy_text(before["position"]), xy_text(after["position"])))
    if after.get("level") and after["level"] != before.get("level"):
        text += ", now on " + after["level"]
    if attempt["source"] != "engine":
        text += " (%s)" % attempt["source"]
    if result.get("error"):
        text += ": " + result["error"]
    return text


def history(observation):
    """This scope's attempts, the latest of them in one line each, and the caller budgets that remain."""
    progress = observation.get("objective_progress")
    if not progress:
        return None
    out = {"attempts": progress["scope_attempts"],
           "recent": [attempt_text(a) for a in progress["recent_attempts"][-REQUEST_ATTEMPTS:]]}
    if progress.get("attempts_remaining") is not None:
        out["attempts_remaining"] = progress["attempts_remaining"]
    budget = observation.get("review_budget") or {}
    if budget.get("calls_remaining") is not None:
        out["calls_remaining"] = budget["calls_remaining"]
    return out


def snapshot_summary(snapshot, field):
    return {field: snapshot[field], "age_turns": snapshot["age_turns"], "complete": snapshot["complete"]}


def request_observation(observation):
    """The observation as sent to the engine: its facts once each, in compact lines, without map memory the screen
    already shows, values that say nothing, or more than the latest messages and attempts."""
    o = observation
    out = {"phase": o.get("phase")}
    if o.get("game_result"):
        out["game_result"] = o["game_result"]
    hero = {key: value for key, value in (o.get("hero") or {}).items()
            if value is not None and value is not False and value != [] and key != "position"}
    if (o.get("hero") or {}).get("position"):
        hero["position"] = xy_text(o["hero"]["position"])
    out["hero"] = hero
    out["messages"] = [("T%s: " % m["turn"] if m.get("turn") is not None else "") + m["text"]
                       for m in (o.get("messages") or [])[-REQUEST_MESSAGES:]]
    for key in ("prompt", "screen", "menu_entries", "map"):
        if key in o:
            out[key] = o[key]
    if o.get("map_context", "dungeon") != "dungeon":
        out["map_context"] = o["map_context"]
    if o.get("entities"):
        out["entities"] = [entity_text(entity) for entity in o["entities"]]
    if o.get("underfoot"):
        underfoot = o["underfoot"]
        out["underfoot"] = (underfoot["remembered_terrain"] or "unknown") + (
            " (inferred floor)" if underfoot.get("inferred") else "")
    if o.get("adjacent"):
        out["adjacent"] = {square["direction"]: square_text(square) for square in o["adjacent"]}
    level = o.get("level")
    if level:
        out["level"] = {"id": level.get("id"), "label": level.get("label")}
        looks = {}
        for look in sorted(level.get("inspections") or [], key=lambda look: look["observed_turn"] or 0):
            looks[look["description"]] = look
        if looks:
            out["level"]["inspections"] = ["T%s %s: %s" % (look["observed_turn"], xy_text(look["position"]),
                                                           look["description"])
                                           for look in sorted(looks.values(), key=lambda look: look["position"])]
        if level.get("searches"):
            out["level"]["searches"] = {"%d,%d" % tuple(entry[:2]): entry[2] for entry in level["searches"]}
        if level.get("structural_obstacles"):
            out["level"]["structural_obstacles"] = ["%s %s #" % (xy_text(cell["position"]), cell["colour"])
                                                    for cell in level["structural_obstacles"]]
    known = []
    for entry in o.get("known_levels") or []:
        summary = {"id": entry["id"], "label": entry["label"]}
        if entry.get("landmarks"):
            summary["landmarks"] = ["%s %s" % (xy_text(mark["position"]), mark["kind"]) for mark in entry["landmarks"]]
        if entry.get("connections"):
            summary["connections"] = ["%s %s to %s" % (xy_text(link["position"]), link["action"], link["destination"])
                                      for link in entry["connections"]]
        if len(summary) > 2:
            known.append(summary)
    if known:
        out["known_levels"] = known
    blocked = (o.get("navigation_obstacles") or {}).get("unavailable_destinations")
    if blocked:
        out["blocked_destinations"] = ["%s %s, %d steps, occupied %s" % (
            xy_text(entry["position"]), entry["description"], entry["terrain_path_length"],
            " ".join(xy_text(p) for p in entry["occupied_on_terrain_path"])) for entry in blocked]
    for key, field in (("inventory", "items"), ("spells", "items"), ("attributes", "lines"), ("dungeon_overview", "lines")):
        if key in o and (o[key][field] or o[key]["observed_turn"] is not None):
            out[key] = snapshot_summary(o[key], field)
    if o.get("prayers"):
        out["prayers"] = o["prayers"]
    attempts = history(o)
    if attempts:
        out["history"] = attempts
    return out


def choice_text(action, described=()):
    """What tells an argument choice apart from the others of its tool: its modifier, its bound, and its direction
    unless observation.adjacent describes the square under that key (`described`). Destinations are described once,
    in state.navigation under the same action ID, so a plain move toward a described square needs no words."""
    spec = TOOLS.get(action.kind)
    if action.kind in ("travel", "explore"):
        return ""
    parts = action.id.split(":")
    variant = next((v for v in spec.variants if v.name in parts[1:]), None) if spec else None
    text = [variant.description] if variant else []
    if spec and spec.directions and parts[-1] in spec.directions:
        if parts[-1] not in described:
            text.insert(0, DIRECTIONS[parts[-1]])
    elif spec and spec.repeat:
        text.append("up to %d turn%s" % (action.steps, "" if action.steps == 1 else "s"))
    elif not text:
        text = [action.description]
    if action.withheld:
        text.append(action.withheld)
    return "; ".join(text)


def described_directions(observation):
    return {square["direction"] for square in observation.get("adjacent") or []}


def estimated_tokens(body):
    """A conservative token count for a request: one token for every two bytes of its JSON."""
    return -(-len(json.dumps(body, allow_nan=False).encode()) // BYTES_PER_TOKEN)


def omit_other_levels(observation, actions, tool):
    current = (observation.get("level") or {}).get("id")
    known = observation.get("known_levels") or []
    if not any(level.get("landmarks") for level in known if level["id"] != current):
        return None
    known = [level if level["id"] == current else {key: value for key, value in level.items() if key != "landmarks"}
             for level in known]
    return dict(observation, known_levels=known), actions, "landmarks of other levels"


def omit_older_messages(observation, actions, tool):
    messages = (observation.get("messages") or [])[-REQUEST_MESSAGES:]
    if len(messages) <= KEPT_MESSAGES:
        return None
    return dict(observation, messages=messages[-KEPT_MESSAGES:]), actions, \
        "messages before the latest %d" % KEPT_MESSAGES


def omit_far_destinations(observation, actions, tool):
    targets = list(dict.fromkeys(target for _, target in sorted(
        (len(action.route), action.target) for action in actions if action.kind in ("travel", "explore"))))
    if len(targets) <= NEAREST_DESTINATIONS:
        return None
    near = set(targets[:NEAREST_DESTINATIONS])
    actions = [action for action in actions if action.kind not in ("travel", "explore") or action.target in near]
    return observation, actions, "%d destinations beyond the %d nearest" % (len(targets) - len(near),
                                                                           NEAREST_DESTINATIONS)


# What a request over its token budget leaves out, in this order, until it fits.
OMISSIONS = (omit_other_levels, omit_older_messages, omit_far_destinations)


# The named-command tool offers dozens of rarely used commands; its question is asked once it has been chosen.
SEPARATE = ("command",)


def argument_options(actions):
    """The offered actions of every tool that has several, by tool in catalogue order, but those asked separately."""
    options = {}
    for action in actions:
        if action.kind != "pause" and action.tool not in SEPARATE:
            options.setdefault(action.tool, []).append(action)
    return {tool: listed for tool, listed in options.items() if len(listed) > 1}


def request_body(observation, actions, objective, model=None, tool=None, caller_context=None, max_tokens=None):
    """One stateless decision request, within max_tokens when that is given.

    In normal play it asks the tool question and, for each offered tool with several actions, which of them to
    use if that tool is chosen: the engine answers all of them together and the answer for the chosen tool is
    used. These argument questions take the room the budget leaves, in catalogue order; the arguments of a chosen
    tool whose question did not fit are asked in a request of their own. A request for one tool's arguments asks
    only that, and one at a prompt asks for the answer.

    A request over the budget leaves out, in order, other levels' landmarks, older messages and destinations
    beyond the nearest; `state.omitted` lists what it left out."""
    omitted = []
    body = build_body(observation, actions, objective, model, tool, caller_context, omitted)
    for omit in OMISSIONS:
        if max_tokens is None or estimated_tokens(body) <= max_tokens:
            break
        trimmed = omit(observation, actions, tool)
        if trimmed:
            observation, actions, note = trimmed
            omitted.append(note)
            body = build_body(observation, actions, objective, model, tool, caller_context, omitted)
    if body["state"]["decision"]["stage"] == "tool":
        described = described_directions(observation)
        for name, options in argument_options(actions).items():
            body["questions"][name] = {"type": "choice", "instructions": ARGUMENTS % name, "criteria": dict(
                {action.id: choice_text(action, described) for action in options}, pause=PAUSE)}
            if max_tokens is not None and estimated_tokens(body) > max_tokens:
                del body["questions"][name]
    return body


def build_body(observation, actions, objective, model, tool, caller_context, omitted):
    direct = observation.get("phase") != "play"
    stage = "arguments" if tool else "input" if direct else "tool"
    if stage == "tool":
        criteria = {action.tool: tool_description(action.tool, action.description) for action in actions}
        instructions = "Choose the tool to use next."
    elif stage == "arguments":
        described = described_directions(observation)
        criteria = {action.id: choice_text(action, described) for action in actions
                    if action.tool == tool or action.kind == "pause"}
        instructions = "Selected tool: %s. Choose its concrete arguments." % tool
    else:
        criteria = {action.id: action.description for action in actions}
        instructions = "Answer the current prompt."
    if "pause" in criteria:
        instructions += " " + GUIDANCE
    state = {"decision": {"stage": stage, "tool": tool}, "objective": objective}
    if caller_context:
        state["caller_context"] = caller_context
    if omitted:
        state["omitted"] = list(omitted)
    routes = [action for action in actions if action.kind in ("travel", "explore") and action.route and (
        stage == "tool" or action.tool == tool)]
    if routes:
        visits = visit_counts(observation)
        state["navigation"] = {"destinations": {action.id: destination_text(action, visits) for action in routes}}
    state["observation"] = request_observation(observation)
    body = {"state": state, "questions": {"action": {
        "type": "choice", "instructions": "%s %s Objective: %s" % (instructions, POSITIONS, objective),
        "criteria": criteria}}}
    if model:
        body["model"] = model
    return body


def offered(answer, questions, name):
    choice = answer["answers"][name]["choice"]
    if not isinstance(choice, str) or choice not in questions[name]["criteria"]:
        raise ValueError(name)
    return choice


class Engine:
    def __init__(self, url, model=None, key=None, timeout=10):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("decision endpoint must be an HTTP(S) URL")
        self.url, self.model, self.key, self.timeout = url, model, key, timeout

    def post(self, body, timeout):
        """One request: the parsed response, or a Failure."""
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, body, headers), timeout=timeout) as response:
                raw = response.read(RESPONSE_LIMIT + 1)
        except urllib.error.HTTPError as e:
            message, answer = "decision endpoint returned HTTP %d" % e.code, None
            try:
                with e:
                    raw = e.read(RESPONSE_LIMIT + 1)
                answer = raw[:RESPONSE_LIMIT].decode("utf-8", "replace")
                if len(raw) <= RESPONSE_LIMIT:
                    try:
                        answer = json.loads(answer, parse_constant=reject_constant)
                    except ValueError:
                        pass
                else:
                    message += " (response truncated after 1 MiB)"
            except OSError:
                pass
            raise Failure(message, answer, e.code, e.code in TRANSIENT_STATUS, retry_after(e.headers)) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) and isinstance(e.reason, Exception) else e
            raise Failure("decision request failed (%s)" % type(reason).__name__,
                          transient=isinstance(reason, TRANSIENT_ERRORS)) from None
        if len(raw) > RESPONSE_LIMIT:
            raise Failure("decision response exceeds 1 MiB")
        try:
            return json.loads(raw, parse_constant=reject_constant)
        except ValueError as e:
            raise Failure("decision request failed (%s)" % type(e).__name__, raw.decode("utf-8", "replace")) from None

    def ask(self, request):
        """Send request, retrying transient failures with backoff and any Retry-After until the decision timeout.

        Returns the response, the seconds taken and the failed attempts before it; raises DecisionError with them."""
        body = json.dumps(request, allow_nan=False).encode()
        started = time.monotonic()
        deadline, wait, retries = started + self.timeout, BACKOFF[0], []
        while True:
            attempted = time.monotonic()
            try:
                return self.post(body, max(0.001, deadline - attempted)), time.monotonic() - started, retries
            except Failure as failure:
                now = time.monotonic()
                pause = max(wait, failure.retry_after or 0)
                if not failure.transient or now + pause >= deadline:
                    raise DecisionError(str(failure), failure.response, now - started, retries) from None
                retries.append({"error": str(failure), "status": failure.status, "response": failure.response,
                                "latency": now - attempted, "retry_after": failure.retry_after, "waited": pause})
                time.sleep(pause)
                wait = min(wait * 2, BACKOFF[1])

    def choose(self, request):
        """The answer to the action question and, when the request asked about the chosen tool's arguments, the
        answer to that question, keyed by question. Answers about tools not chosen are only recorded."""
        answer, latency, retries = self.ask(request)
        questions = request["questions"]
        try:
            choices = {"action": offered(answer, questions, "action")}
        except (KeyError, TypeError, ValueError):
            raise DecisionError("decision response must contain answers.action.choice naming an offered action",
                                answer, latency, retries) from None
        tool = choices["action"]
        if tool != "action" and tool in questions:
            try:
                choices[tool] = offered(answer, questions, tool)
            except (KeyError, TypeError, ValueError):
                raise DecisionError("decision response must contain answers.%s.choice naming an offered %s action"
                                    % (tool, tool), answer, latency, retries) from None
        return choices, answer, latency, retries
