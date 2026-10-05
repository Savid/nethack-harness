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


def argument_facts(observation, action):
    facts = action.as_dict()
    spec = TOOLS.get(action.kind)
    if not spec:
        return facts
    parts = action.id.split(":")
    variant = next((v for v in spec.variants if v.name in parts[1:]), None)
    facts["modifier"] = {"name": variant.name, "description": variant.description} if variant else None
    if spec.directions and parts[-1] in spec.directions:
        key = parts[-1]
        facts.update(direction=dict(DIRECTION_NAMES, **{".": "self", "<": "up", ">": "down"})[key],
                     direction_key=key, origin=observation.get("hero", {}).get("position"))
        origin, target = facts["origin"], facts["target"]
        if origin is not None and target is not None:
            facts["delta"] = [end - start for start, end in zip(origin, target)]
        adjacent = next((square for square in observation.get("adjacent", [])
                         if square["direction"] == key and square["position"] == target), None)
        if adjacent is not None:
            facts["target_observation"] = adjacent
    return facts


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
        instructions += (" Each request is independent; use the supplied state and choices."
                         " objective_progress records attempts since this objective scope began; use their results"
                         " to assess whether the objective already warrants pause. Earlier history may belong to other scopes."
                         " Choose pause for caller review if the evidence or offered choices are insufficient"
                         " to select an action for the objective, or the recorded actions show a loop without progress.")
    body = {"state": {"objective": objective, "observation": observation},
            "questions": {"action": {"type": "choice", "instructions": instructions + " Objective: " + objective,
                                      "criteria": criteria}}}
    body["state"]["decision"] = {"stage": stage, "tool": tool}
    body["state"]["caller_context"] = caller_context or {}
    if stage == "arguments":
        body["state"]["argument_facts"] = {
            action.id: argument_facts(observation, action) for action in actions if action.id in criteria}
    if not direct:
        body["state"]["navigation"] = {
            "source": "offered routes through remembered terrain; not a guarantee of passage",
            "destinations": [{"action": action.id, "description": action.description,
                              "position": position(action.target), "known_path_length": len(action.route),
                              "next_position": position(action.route[0]), "max_steps": action.steps}
                             for action in actions if action.kind in ("travel", "explore") and action.route]}
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
