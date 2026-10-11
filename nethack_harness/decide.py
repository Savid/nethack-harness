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
from .tools import TOOLS, tool_description

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


GUIDANCE = ("Decide from this state alone; caller_context and objective_progress list earlier attempts and their"
            " results. Prefer a choice that has not already failed to make progress.")
OMITTED_LEVEL = ("known_terrain", "visits", "inferred_floor")
REQUEST_ATTEMPTS = 8


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
        if action.withheld:
            facts["modifier"]["withheld"] = action.withheld
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
            facts["target_observation"] = {name: adjacent[name] for name in (
                "glyph", "colour", "remembered_terrain", "remembered_feature", "pet_highlight") if name in adjacent}
    return facts


def snapshot_summary(snapshot, field):
    return {field: snapshot[field], "age_turns": snapshot["age_turns"], "complete": snapshot["complete"]}


def point_summary(point):
    return {key: value for key, value in point.items() if key != "fingerprint"} if point else point


def attempt_summary(attempt):
    out = {key: value for key, value in attempt.items() if key != "decision"}
    for key in ("before", "after"):
        if key in out:
            out[key] = point_summary(out[key])
    return out


def progress_summary(progress):
    """Recent scoped attempts without screen hashes or record numbers."""
    attempts = progress["recent_attempts"][-REQUEST_ATTEMPTS:]
    return dict(progress, start=point_summary(progress["start"]),
                recent_attempts=[attempt_summary(attempt) for attempt in attempts],
                omitted_attempts=progress["omitted_attempts"] + len(progress["recent_attempts"]) - len(attempts))


def request_observation(observation):
    """The observation as sent to the engine: complete facts, without map memory the screen already shows.

    Attempt and movement histories are shortened to their most recent entries; the decision log keeps them whole."""
    out = {key: value for key, value in observation.items() if key != "fingerprint"}
    if out.get("objective_progress"):
        out["objective_progress"] = progress_summary(out["objective_progress"])
    if out.get("navigation_progress"):
        events = out["navigation_progress"]["recent_actions"][-REQUEST_ATTEMPTS:]
        out["navigation_progress"] = dict(out["navigation_progress"], recent_actions=[
            {key: value for key, value in event.items() if key != "decision"} for event in events])
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
            "frontier": ("the hero has not stood there, and a square beside it in a cardinal direction never "
                         "showed a glyph and was never beside the hero"),
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
        answer, latency, retries = self.ask(request)
        try:
            choice = answer["answers"]["action"]["choice"]
            if not isinstance(choice, str) or choice not in request["questions"]["action"]["criteria"]:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise DecisionError("decision response must contain answers.action.choice naming an offered action",
                                answer, latency, retries) from None
        return choice, answer, latency, retries
