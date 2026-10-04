"""The decision model's say: when to ask it, what its answer may change, and the rules-only breaker."""
import time

from .decide import Unhealthy, confidence
from .hooks import HookError
from .base import Hard
from .settings import CFG, effort


class Arbiter:
    def fetch_answers(self, v, c, acts, near, contested, facts):
        """Ask the decision model (and due hook questions) when the effort level says so; reuse an answer for
        an unchanged situation; trip the rules-only breaker on errors, malformed or slow answers.
        Returns (answers, seconds taken, an escalation reason or None, whether the answer is fresh)."""
        dl, hpf = c["dl"], c["hpf"]
        reason = None
        eff = effort()
        risky = bool(near) or c["hit"] or hpf < 0.5
        healthy = time.time() >= self.breaker_until
        builtin = self.decide is not None and healthy and {
            "never": False, "danger": contested and (bool([h for h in near if h["dist"] <= 1]) or hpf < 0.5),
            "risky": contested and risky, "contested": contested}.get(eff["ask"], contested)
        due = {}
        if self.decide is not None and healthy and eff["ask"] != "never":
            ungated = eff["ungated_hooks"] == "always" or (eff["ungated_hooks"] == "with_calls" and builtin)
            try:
                due = self.hooks.due(facts, ungated)
            except HookError as e:
                raise Hard(str(e))
        sig = (dl, c["hero"], tuple(a.key for a in acts), c["hp"] // 3, tuple(h["name"] for h in near[:3]),
               self.directive)
        ans, took, fresh = {}, 0, False
        if builtin and not due and self.cache and self.cache[0] == sig and \
                self.decisions - self.cache[1] <= eff["cache"]:
            ans = self.cache[2]          # same situation as a moment ago: reuse the answer
            self.reused += 1
        elif builtin or due:
            fresh = True
            try:
                ans, took = self.ask(v, c, acts if builtin else None, due, eff["state"])
                if builtin and not ("act" in ans and isinstance(ans["act"].get("probabilities"), dict)
                                    and any(a.key in ans["act"]["probabilities"] for a in acts)):
                    raise Unhealthy("malformed answer: no usable probabilities for act")
                if builtin and not all(isinstance(x, (int, float)) and 0.0 <= x <= 1.0
                                       for x in ans["act"]["probabilities"].values()):
                    raise Unhealthy("malformed answer: act probabilities must be numbers in [0, 1]")
                for x in ans.values():
                    for k in ("noul", "confidence"):
                        if k in x and not (0.0 <= x[k] <= 1.0):     # NaN fails this too
                            raise Unhealthy("malformed answer: %s out of range" % k)
                if builtin:
                    self.cache = (sig, self.decisions, ans)
                if due:
                    self.hooks.answered([k for k in due if k in ans], self.decisions)
                    if any(k in ans for k in due):
                        self.new_level_pending.discard(dl)
                self.latencies.append(took)
                slow = sorted(self.latencies)[len(self.latencies) // 2] if len(self.latencies) >= 5 else 0
                if took > CFG["decide_timeout"] or slow > CFG["slow_ms"] / 1000:
                    self.latencies.clear()
                    raise Unhealthy("slow answers (last %.1fs, median %.1fs)" % (took, slow))
            except Exception as e:     # endpoint trouble: play on the rules for a while
                self.breaker_until, self.breaker_trips = time.time() + CFG["breaker"], self.breaker_trips + 1
                self.note("model_error", "%s; rules only for %ds" % (str(e)[:160], CFG["breaker"]))
                self.last_model_error = str(e)[:160]
                ans = {}
                if self.breaker_trips in (3, 10) or (self.directive and self.breaker_trips == 1):
                    reason = "decision endpoint degraded: rules only (%d failures, last: %s)%s" % (
                        self.breaker_trips, self.last_model_error,
                        "; your orders cannot be applied while it is down" if self.directive else "")
        return ans, took, reason, fresh

    def weigh(self, c, acts, ans, took, reason):
        """What the model's answer may change: with orders set it may pick among the rules' safe options; its
        danger may escalate. Returns (chosen action, info for the log, escalation reason or None)."""
        hpf, top, chosen = c["hpf"], acts[0], acts[0]
        probs = {k: x for k, x in ans["act"]["probabilities"].items() if any(a.key == k for a in acts)}
        ranked = sorted(probs.items(), key=lambda kv: -kv[1])
        conf = confidence(probs)
        danger = min(1.0, max(0.0, ans.get("danger", {}).get("noul", 0.0)))
        pick = next(a for a in acts if a.key == ranked[0][0])
        info = {"src": "model", "p": round(ranked[0][1], 3), "conf": round(conf, 3), "danger": round(danger, 3),
                "ms": int(took * 1000), "rule": top.key, "agree": pick is top,
                "top": [(k, round(x, 3)) for k, x in ranked[:4]]}
        # The model may choose only among options the rules allow, and never an unsafe melee.
        margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0)
        if pick is not top:
            self.disagreements += 1
        vetoed = pick.prior <= -2 or "(Gnomish Mines)" in pick.desc and self.mines_policy() == "avoid" or \
            (pick.kind == "wait" and not c["threats"]) or \
            top.kind in ("pray", "quaff", "cast", "elbereth", "flee")
        crisis_call = CFG["fight_question"] and self.crisis_active(c) and top.prior - acts[1].prior < 1 \
            if len(acts) > 1 else False
        if (self.directive or crisis_call) and conf >= 0.5 and margin >= 0.15 and not vetoed:
            if pick is not top:
                self.overrides += 1
            chosen = pick
        budget_ok = self.clock() - self.last_model_esc >= CFG["esc_gap"] and self.decisions >= self.calm_until
        if budget_ok and danger > CFG["danger_max"] and (pick is not top or hpf < 0.5):
            reason = "danger %.2f (rules want %s, model wants %s %.2f)" % (danger, top.key, pick.key,
                                                                          ranked[0][1])
        elif budget_ok and danger > 0.5 and hpf < 0.5 and conf < CFG["p_min"]:
            reason = "uncertain in a risky spot: %s" % ", ".join("%s %.2f" % kv for kv in ranked[:3])
        if reason:
            self.last_model_esc = self.clock()
        return chosen, info, reason
