"""Escalation codes: one registry for what every pause means, how it is throttled and whether it may be silenced.

Every reason the loop gives maps to exactly one code here (classify). Reports, status and hook facts carry the
code; dedupe and `pause_on` work on codes, never on wording. `help escalations` is generated from this table.
"""
import re


class Spec:
    def __init__(self, code, pattern, doc, example, window=1, silenceable=True):
        self.code, self.rx, self.doc, self.example = code, re.compile(pattern), doc, example
        self.window = window                 # turns an unchanged repeat waits (same place, same level)
        self.silenceable = silenceable       # pause_on may turn it off; safety-critical ones always pause


REGISTRY = [
    Spec("briefing", r"^briefing$", "the start-up briefing", "briefing"),
    Spec("game_over", r"^game_over$", "the game ended", "game_over", silenceable=False),
    Spec("milestone", r"^milestone: ", "a new deepest level or XL (milestone setting)",
         "milestone: new deepest Dlvl 6 (XL 4, HP 33/35, T1450; down stairs known: no)"),
    Spec("branch_point", r"^branch point: ", "Mines entry, trap door or hole, two down staircases",
         "branch point: two down staircases on Dlvl 4"),
    Spec("depth_jump", r"^depth jump: ", "fell or jumped two or more levels, or well below XL",
         "depth jump: Dlvl 3 -> 6 at XL 2"),
    Spec("endgame", r"^endgame: ", "the last endgame_secs of time_left: depth caps lifted",
         "endgame: 170 s left: depth caps lifted ..."),
    Spec("depth_gate", r"^depth gate: ", "explored level, depth cap holds", "depth gate: Dlvl 2 is explored ..."),
    Spec("losing_fast", r"^losing fast: (?!the crisis|HP still)", "HP dropping fast (fight_handoff=escalate)",
         "losing fast: HP 7/16, down 6 in 5 turns (jackal)", silenceable=False),
    Spec("crisis_exhausted", r"^losing fast: the crisis ladder is exhausted", "the crisis ladder has nothing left",
         "losing fast: the crisis ladder is exhausted at HP 5/16 (...; tried: elbereth, retreat)",
         silenceable=False),
    Spec("crisis_falling", r"^losing fast: HP still falling", "HP still falling after the crisis ladder",
         "losing fast: HP still falling after the crisis ladder, 12 -> 6/20 (...)", silenceable=False),
    Spec("surrounded", r"^surrounded: ", "three or more adjacent hostiles", "surrounded: 3 adjacent hostiles ...",
         silenceable=False),
    Spec("low_hp", r"^low HP ", "low HP under attack and no safe remedy left",
         "low HP 4/16 with jackal near and no safe prayer, potion or Elbereth (prayer: fails ...)",
         silenceable=False),
    Spec("danger", r"^danger \d", "the decision model sees danger the rules do not",
         "danger 0.85 (rules want explore, model wants move_h 0.60)"),
    Spec("uncertain", r"^uncertain in a risky spot", "the decision model is unsure in a risky spot",
         "uncertain in a risky spot: attack_h 0.40, move_l 0.35"),
    Spec("hunger", r"^(?:Weak|Fainting|Fainted) from hunger|^Hungry with no food", "hunger with no remedy",
         "Weak from hunger, no food, no safe prayer"),
    Spec("swarm", r"^swarm: ", "left a swarm of fast, poisonous attackers by the up stairs",
         "swarm: 4 killer bee, poisonous and fast, on Dlvl 6; left by the up stairs ..."),
    Spec("camped", r"^camped: ", "stair ping-pong: a monster camps the arrival of a deeper level",
         "camped: the Dlvl 5 arrival is camped by dwarf at 12,40 (Dlvl 4 <-> 5 4 times) ..."),
    Spec("oscillating", r"^oscillating: ", "a loop between two squares, actions or levels",
         "oscillating: explore / wait_blocked at 8,63 / 8,71, 12 times ..."),
    Spec("stalled", r"^stalled: ", "no new squares or depth for a while", "stalled: no new squares or depth ...",
         window=150),
    Spec("level_exhausted", r"^level exhausted: ", "no frontier, stairs, search budget or tools left",
         "level exhausted: no frontier, stairs, search budget or tools left (150 search turns)", window=150),
    Spec("alarm", r"^alarming message: ", "a message that needs judgment (stoning, lycanthropy, theft...)",
         "alarming message: You feel feverish."),
    Spec("stoning", r"^stoning ", "turning to stone with prayer unsafe", "stoning (...) and prayer is not safe ...",
         silenceable=False),
    Spec("unknown_prompt", r"^unknown (?:text )?prompt|^(?:text )?prompt needs you", "a question the loop "
         "will not answer", "unknown text prompt (nothing sent): To what level ...", silenceable=False),
    Spec("screen", r"^unrecognised screen", "a screen the loop cannot read", "unrecognised screen; ...",
         silenceable=False),
    Spec("frozen", r"^frozen: |^engulfed for |^blind for ", "turns not passing, a long engulf or blindness",
         "frozen: 16 actions without the turn counter moving"),
    Spec("plan", r"^goal:|^plan goal|^plan replay|^unknown plan item", "a plan item could not run",
         "goal:retreat: no stairs within 8 steps ..."),
    Spec("hook", r"^hook[: ]", "a hook question or plugin asked for a pause, or failed", "hook:shop (yes 0.91)"),
    Spec("model_degraded", r"^decision endpoint degraded", "the decision endpoint keeps failing",
         "decision endpoint degraded: rules only (3 failures, last: timed out)"),
    Spec("request", r"^paused on request", "pause was asked for", "paused on request", silenceable=False),
    Spec("problem", r"^(?:resume|setup) problem: |^state dir not writable|^inner loop error",
         "a bad setting, plugin, file or internal error", "resume problem: avoid is not a valid regex",
         silenceable=False),
]
BY_CODE = {s.code: s for s in REGISTRY}


def classify(text):
    """The code for an escalation reason ("other" if nothing matches; a test keeps that from happening)."""
    for s in REGISTRY:
        if s.rx.search(text or ""):
            return s.code
    return "other"


def parse_pause_on(value):
    """pause_on: "all" (default), or codes to pause on, or "all,-code,-code" to silence some. Codes that are
    not silenceable always pause. Returns the set of codes that pause."""
    items = [x.strip() for x in str(value or "all").split(",") if x.strip()]
    unknown = [x.lstrip("-") for x in items if x.lstrip("-") not in BY_CODE and x not in ("all", "-all")]
    if unknown:
        raise ValueError("unknown escalation code(s) %s (see help escalations)" % ", ".join(unknown))
    on = set(BY_CODE) if not items or items[0] == "all" else set()
    for x in items:
        if x.startswith("-"):
            on.discard(x[1:])
        elif x != "all":
            on.add(x)
    return on | {s.code for s in REGISTRY if not s.silenceable}


def help_text():
    rows = ["ESCALATION CODES (status, reports and hook facts carry them; --set pause_on=all,-milestone silences)"]
    for s in REGISTRY:
        rows.append("  %-16s %s%s" % (s.code, s.doc, "" if s.silenceable else " [always pauses]"))
    rows.append("  pause_on: all (default) | code,code,... | all,-code,...; silenced codes are logged and play goes "
                "on. An on_escalation plugin function may also answer: None | {\"continue\": true} | "
                "{\"plan\": [items]} (help plugins).")
    return "\n".join(rows)
