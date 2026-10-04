"""What the decision model is shown and asked."""
import time

from . import knowledge as K
from .decide import builtin_questions, normalize
from .hooks import HOOK_API
from .level import compass
from .settings import CFG


class ModelView:
    def state(self, v, c):
        h = c["hero"]
        crop = ["".join("P" if (r, x) != h and 1 <= r <= 21 and v.ch(r, x) in K.MON and v.pet(r, x) else v.ch(r, x)
                        for x in range(h[1] - 8, h[1] + 9)) for r in range(h[0] - 4, h[0] + 5)]
        s = {"goal": "Get as deep as possible in NetHack without dying (mode: %s, risk: %s)." % (
                CFG["mode"], CFG["risk"]),
             "hero": {"Dlvl": c["dl"], "HP": "%d of %d" % (c["hp"], c["hpmax"]), "HP_percent": int(100 * c["hpf"]),
                      "AC": v.st.get("ac"), "XL": c["xl"], "turn": c["turn"], "status": v.cond or ["normal"],
                      "who": v.title, "role": self.role},
             "hostile_monsters": ["%s, %d squares %s" % (x["name"], x["dist"], compass(h, x["pos"]))
                                  for x in c["hostiles"][:6]] or ["none in view"],
             "standing_on": {">": "down stairs", "<": "up stairs"}.get(c["under"], "floor"),
             "messages": list(self.msgs)[-3:],
             "recent_actions": [x["text"].split(":")[0] for x in self.hist if x["kind"] == "act"][-8:],
             "map_around_hero": crop,
             "map_legend": "@ hero (center), P pet, letters monsters, . floor, # corridor, | - walls, + door, "
                           "> down stairs, < up stairs, blank unknown"}
        if c["peace"]:
            s["peaceful_monsters"] = [x["name"] for x in c["peace"][:4]]
        if c["obst"]:
            s["never_melee"] = [x["name"] for x in c["obst"][:4]]
        if self.directive:
            s["orders"] = self.directive
        s["screen"] = v.text_screen()
        return s

    def facts(self, v, c, new_level=False):
        return {"api": HOOK_API, "dlvl": c["dl"], "hp": c["hp"], "hpmax": c["hpmax"], "hp_percent": int(100 * c["hpf"]),
                "xl": c["xl"], "turn": c["turn"], "conditions": list(v.cond), "new_level": new_level,
                "hostiles": [h["name"] for h in c["hostiles"]], "standing_on": c["under"], "role": self.role,
                "race": self.race, "messages": list(self.msgs)[-3:], "decisions": self.decisions, "keys": self.keys,
                "mode": CFG["mode"], "risk": CFG["risk"], "orders": self.directive, "screen": v.text_screen(),
                "state": self.state(v, c)}

    def ask(self, v, c, acts, extra=None, size="full"):
        questions = builtin_questions(acts, self.rng) if acts else {}
        questions.update(extra or {})
        state = self.state(v, c)
        if size == "compact":
            for k in ("screen", "recent_actions", "map_legend"):
                state.pop(k, None)
        started = time.monotonic()
        res = self.decide({"state": state, "questions": questions}, timeout=CFG["decide_timeout"])
        took = time.monotonic() - started
        self.calls += 1
        self.mtime += took
        return normalize(res.get("answers")), took
