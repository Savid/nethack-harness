"""Outside strategies: declarative questions and plugin modules (hook API 1)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import threading

HOOK_API = 1
RULES = ("noul_gte", "noul_lte", "score_gte", "score_lte", "choice_in", "min_confidence")


class HookError(Exception):
    pass


def check_question(key, q):
    if not isinstance(q, dict) or q.get("type") not in ("choice", "noul", "score"):
        raise HookError("question %r needs type choice, noul or score" % key)
    crit = q.get("criteria")
    if q["type"] == "choice" and not (isinstance(crit, dict) and 0 < len(crit) <= 255 and
                                      all(isinstance(v, str) for v in crit.values())):
        raise HookError("choice question %r needs a criteria map of 1-255 descriptions" % key)
    if q["type"] == "score" and not (isinstance(crit, list) and 2 <= len(crit) <= 10):
        raise HookError("score question %r needs an ordered criteria list of 2-10 levels" % key)
    if q["type"] == "noul" and crit is not None and not (isinstance(crit, dict) and set(crit) <= {"true", "false"}):
        raise HookError("noul question %r: criteria may only hold true and false" % key)
    if not isinstance(q.get("instructions", ""), str):
        raise HookError("question %r: instructions must be text" % key)
    return {k: v for k, v in q.items() if k in ("type", "instructions", "criteria")}


def check_rule(key, rule):
    if not isinstance(rule, dict) or not rule or set(rule) - set(RULES):
        raise HookError("hook %s: escalate_when needs one or more of %s" % (key, ", ".join(RULES)))
    for k, v in rule.items():
        if k == "choice_in":
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise HookError("hook %s: choice_in must be a list of option keys" % key)
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            raise HookError("hook %s: %s must be a number" % (key, k))
    return rule


def rule_fires(rule, answer):
    """Does an escalate_when rule match one answer? All given conditions must hold."""
    if not answer:
        return False
    checks = []
    if "noul_gte" in rule:
        checks.append(answer.get("noul", -1) >= rule["noul_gte"])
    if "noul_lte" in rule:
        checks.append(answer.get("noul", 2) <= rule["noul_lte"])
    if "score_gte" in rule:
        checks.append(answer.get("score", -1e9) >= rule["score_gte"])
    if "score_lte" in rule:
        checks.append(answer.get("score", 1e9) <= rule["score_lte"])
    if "choice_in" in rule:
        checks.append(answer.get("choice") in rule["choice_in"])
    if "min_confidence" in rule:
        checks.append(answer.get("confidence", 0) >= rule["min_confidence"])
    return bool(checks) and all(checks)


class Hooks:
    """Outside strategies, without the inner loop knowing what they are for.

    Declarative questions (JSON): each entry is merged into the step's batched decision call when due and
    pauses the loop (reason "hook:KEY") when its escalate_when rule matches:
      {"key": "shop", "question": {"type": "noul", "instructions": "..."},
       "escalate_when": {"noul_gte": 0.8}, "when": {"every": 10} | {"new_level": true}, "cooldown": 50}
    Plugins (Python files) may define API = 1 and any of:
      extra_questions(facts) -> {key: question}
      on_answers(facts, answers) -> None | {"escalate": reason} | {"action": keys}
      on_resume(facts, orders) -> None
    Plugin errors pause the loop with reason "hook error" instead of crashing it.
    """

    def __init__(self):
        self.questions, self.plugins, self.disabled = {}, {}, set()
        self.fired, self.asked, self.errors = {}, {}, {}
        self.log_path = None          # where plugin output goes (the daemon sets STATE/hooks.log)

    def load_questions(self, path):
        with open(path) as f:
            data = json.load(f)
        entries = data.get("questions", []) if isinstance(data, dict) else data
        if not isinstance(entries, list):
            raise HookError("expected a list of questions")
        loaded = {}
        for e in entries:
            if not isinstance(e, dict):
                raise HookError("each question entry must be an object")
            key = str(e.get("key") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", key) or key in ("act", "danger"):
                raise HookError("bad hook key %r" % key)
            when = e.get("when") or {}
            if not isinstance(when, dict) or set(when) - {"every", "new_level"} or \
                    ("every" in when and not (isinstance(when["every"], int) and when["every"] > 0)):
                raise HookError("hook %s: when must be {\"every\": N} or {\"new_level\": true}" % key)
            cooldown = e.get("cooldown", 0)
            if not isinstance(cooldown, int) or cooldown < 0:
                raise HookError("hook %s: cooldown must be a whole number of decisions" % key)
            loaded[key] = {"question": check_question(key, e.get("question")),
                           "rule": check_rule(key, e.get("escalate_when")), "when": when, "cooldown": cooldown}
        self.questions.update(loaded)          # all or nothing
        return list(self.questions)

    def load_plugin(self, path):
        name = re.sub(r"\W", "_", os.path.splitext(os.path.basename(path))[0])
        spec = importlib.util.spec_from_file_location("nethack_harness_plugin_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if getattr(module, "API", HOOK_API) != HOOK_API:
            raise HookError("plugin %s wants hook API %s; this is %d" % (name, module.API, HOOK_API))
        self.plugins[name] = module
        return name

    def enabled(self, key):
        return key not in self.disabled

    def due(self, facts, ungated=True):
        """Questions to ask this decision: {wire key: question}. Hooks without a `when` gate are included only
        when `ungated` is true (the effort level decides: always, or only when a call happens anyway)."""
        out = {}
        n = facts["decisions"]
        for key, h in self.questions.items():
            if not self.enabled(key) or n - self.fired.get(key, -10 ** 9) <= h["cooldown"]:
                continue
            when = h["when"]
            if not when and not ungated:
                continue
            if when.get("new_level") and not facts["new_level"]:
                continue
            if when.get("every") and n - self.asked.get(key, -10 ** 9) < int(when["every"]):
                continue
            out[key] = h["question"]
        for name, mod in self.plugins.items():
            if self.enabled(name) and hasattr(mod, "extra_questions") and ungated:
                for k, q in (self.call(name, "extra_questions", facts) or {}).items():
                    out["%s.%s" % (name, k)] = check_question(k, q)
        return out

    def answered(self, keys, n):
        """Record that these questions got answers (a failed call leaves new_level hooks due)."""
        for key in keys:
            self.asked[key] = n

    def judge(self, facts, answers):
        """Returns (reason, keys): an escalation reason and/or keys a plugin wants sent instead."""
        n = facts["decisions"]
        for key, h in self.questions.items():
            if key in answers and rule_fires(h["rule"], answers[key]):
                self.fired[key] = n
                return "hook:%s %s" % (key, describe(answers[key])), None
        for name, mod in self.plugins.items():
            if not self.enabled(name) or not hasattr(mod, "on_answers"):
                continue
            own = {k.split(".", 1)[1]: v for k, v in answers.items() if k.startswith(name + ".")}
            own.update({k: v for k, v in answers.items() if k in ("act", "danger")})
            out = self.call(name, "on_answers", facts, own) or {}
            if out.get("escalate"):
                return "hook:%s %s" % (name, str(out["escalate"])[:200]), None
            if out.get("action"):
                return None, str(out["action"])
        return None, None

    def resumed(self, facts, orders):
        for name, mod in self.plugins.items():
            if self.enabled(name) and hasattr(mod, "on_resume"):
                self.call(name, "on_resume", facts, orders)

    def call(self, name, fn, *args):
        """Run one plugin function under a time limit, with its output sent to a capped log. A plugin that
        overruns, or fails twice, is disabled (and the error says so) so it cannot stall or flood the loop."""
        box = {}

        def run():
            try:
                with contextlib.redirect_stdout(Sink(self.log_path)), contextlib.redirect_stderr(Sink(self.log_path)):
                    box["out"] = getattr(self.plugins[name], fn)(*args)
            except BaseException as e:       # noqa: B902 - a plugin may raise anything, even SystemExit
                box["err"] = e

        t = threading.Thread(target=run, name="hook-" + name, daemon=True)
        t.start()
        t.join(HOOK_SECS)
        if t.is_alive():
            self.disabled.add(name)
            raise HookError("hook %s.%s ran over %ds; plugin disabled (re-enable with --enable %s)" % (
                name, fn, HOOK_SECS, name))
        if "err" in box:
            if isinstance(box["err"], KeyboardInterrupt):
                raise box["err"]
            self.errors[name] = self.errors.get(name, 0) + 1
            off = self.errors[name] >= 2
            if off:
                self.disabled.add(name)
                self.errors[name] = 0
            e = box["err"]
            raise HookError("hook error in %s.%s: %s: %s%s" % (name, fn, type(e).__name__, str(e)[:300],
                                                               "; plugin disabled after 2 errors" if off else ""))
        out = box.get("out")
        try:
            if fn == "extra_questions" and out is not None and not isinstance(out, dict):
                raise HookError("hook error in %s.extra_questions: must return a dict" % name)
            if fn == "on_answers" and out is not None:
                if not isinstance(out, dict):
                    raise HookError("hook error in %s.on_answers: must return None or a dict" % name)
                if len(str(out.get("action", ""))) > 1024:
                    raise HookError("hook error in %s.on_answers: action longer than 1024 keys" % name)
        except HookError:
            self.errors[name] = self.errors.get(name, 0) + 1
            if self.errors[name] >= 2:
                self.disabled.add(name)
                self.errors[name] = 0
            raise
        return out


HOOK_SECS = 5
LOG_CAP = 1 << 20


class Sink(io.TextIOBase):
    """Plugin print output: appended to a log file until it reaches LOG_CAP bytes, then dropped."""

    def __init__(self, path):
        self.path = path

    def writable(self):
        return True

    def write(self, text):
        if not self.path:
            return len(text)
        try:
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            if size < LOG_CAP:
                with open(self.path, "a") as f:
                    f.write(text[:LOG_CAP])
        except OSError:
            pass
        return len(text)


def describe(answer):
    if "noul" in answer:
        return "(yes %.2f)" % answer["noul"]
    if "choice" in answer:
        return "(%s %.2f)" % (answer["choice"], answer.get("confidence", 0))
    if "score" in answer:
        return "(score %.2f)" % answer["score"]
    return ""
