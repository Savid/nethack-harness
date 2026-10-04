"""Tunables. Every key can be changed at runtime with `--set k=v`; `--mode` sets the keys modes own."""
import re

DEFAULTS = {
    "mode": "descend",      # descend | explore | careful (careful = risk low)
    "risk": "normal",       # low | normal | high: scales the HP gates and the depth lead below
    "descend_hp": None,     # take stairs only at or above this HP fraction (None: from risk)
    "rest_hp": None,        # rest below this HP fraction when nothing is in view (None: from risk)
    "hp_escalate": None,    # escalate below this HP fraction under attack with no safe prayer (None: from risk)
    "elbereth_hp": None,    # engrave Elbereth below this HP fraction when threatened (None: from risk)
    "lead": None,           # do not descend deeper than XL + lead (None: by role, adjusted by risk)
    "mines": "auto",        # auto (allow for gnome/dwarf heroes, else avoid) | allow | avoid | escalate
    "dig": 0,               # 1: dig down with a pick-axe or mattock instead of hunting for stairs
    "avoid": "",            # regex of monster names never to melee
    "effort": "medium",     # decision effort: off (rules only) | low | medium | high (see EFFORT)
    "danger_max": 0.8,      # escalate when the model's danger is above this and it disagrees with the rules
    "p_min": 0.35,          # escalate in a risky spot when the model's normalised confidence is below this
    "esc_gap": 60,          # seconds between model-triggered escalations (hard ones are never throttled)
    "calm": 8,              # decisions after a resume before model-triggered escalations may fire
    "stall": 60,            # escalate after this many decisions without new squares or depth
    "stall_turns": 800,     # ...or this many game turns
    "decide_timeout": 4.0,  # seconds per decision call; slower answers trip the breaker
    "breaker": 20,          # seconds of rules-only play after a failed or slow decision call
    "slow_ms": 1500,        # trip the breaker when the median of the last 5 calls is slower than this
    "search_budget": 150,   # search turns per level before moving down the escape ladder
    "sturdy_hp": 10,        # fragile: max HP below sturdy_hp + sturdy_hp_per_xl * XL, or AC worse than sturdy_ac
    "sturdy_hp_per_xl": 4,
    "sturdy_ac": 7,
    "fragile_lead": 1,      # pace: no deeper than XL + this until pace_xl (always, while fragile); +1 at risk=high
    "pace_xl": 4,           # ...from this XL a sturdy hero may go one level deeper (XL + fragile_lead + 1)
    "cap_lift": 120,        # seconds on a level after which the depth lead no longer blocks descending
    "potions": 1,           # quaff known healing potions in emergencies
    "spells": 1,            # cast known spells: healing when hurt, force bolt at dangerous foes and blockers
    "elbereth": 1,          # engrave Elbereth in emergencies
    "trapdoors": 1,         # use known trap doors and holes as free descents
    "probe": 1,             # ask the game where the stairs are (travel prompt) when none are visible
    "mapping": 1,           # read a known magic mapping scroll when a level runs out of options
    "briefing": 1,          # pause once at start with role, kit and capabilities
    "branch_points": 1,     # pause once at each branch point: Mines entry, a trap door or hole, a depth jump,
                            # a level with two down staircases
    "stall_secs": 45,       # escalate after this many seconds without new squares or depth
    "hp_drop": 0.25,        # escalate when HP falls by this fraction of max within 5 turns
    "hp_drop_min": 5,       # ...and by at least this many points (one bite at low max HP is not news)
    "milestone": "off",     # off | depth | xl | both: pause once at each new deepest level / XL while healthy
    "milestone_hp": 0.67,   # ...only at or above this HP fraction with no hostile in view (otherwise later)
    "milestone_from": 1,    # ...and only for depths at least this deep
    "fight_handoff": "ladder",  # losing fast: "ladder" runs the crisis ladder first; "escalate" hands over at once
    "crisis_turns": 12,     # turns the crisis ladder has before a still-falling HP is handed over
    "fight_question": 1,    # 1: in a crisis, a close call between ladder steps goes to the decision model
    "pickup_food": 1,       # pick up known-safe food the hero steps on
    "ranged": 1,            # fire the quivered missiles (f) at hostiles approaching in a line
    "multi_quiet": 0.12,    # seconds of silence that end a multi-turn command (count, travel, run)
    "quiet": 0.06,          # seconds of terminal silence that end a key send
    "last_prayer": -1,      # set to the turn of a prayer made by hand
    "time_left": -1,        # seconds of play left from now (the outer loop's clock); counted on a monotonic clock
    "endgame_secs": 180,    # in the last this-many seconds of time_left: no depth cap, descend at HP >= 50%
    "tiebreak_seed": -1,    # reseed the loop's tie-breaking choices (a copy then explores differently)
    "auto": 0,              # 1: log escalations and play on without pausing (benchmarks only)
    "report": "compact",    # compact (status, near monsters, messages, a map crop) | full (everything + screen)
    "pause_on": "all,-branch_point,-oscillating",   # benign notices the loop handles itself do not pause      # which escalation codes pause: all | code,code | all,-code (help escalations)
}
RISK = {
    "low": {"descend_hp": 0.85, "rest_hp": 0.85, "hp_escalate": 0.5, "elbereth_hp": 0.5, "lead": -1},
    "normal": {"descend_hp": 0.7, "rest_hp": 0.75, "hp_escalate": 0.34, "elbereth_hp": 0.34, "lead": 0},
    "high": {"descend_hp": 0.5, "rest_hp": 0.6, "hp_escalate": 0.25, "elbereth_hp": 0.25, "lead": 2},
}
# What each decision-effort level means: when to call the model, state size, answer reuse, hook gating.
EFFORT = {
    "off": {"ask": "never", "state": "none", "cache": 0, "ungated_hooks": "never"},
    "low": {"ask": "danger", "state": "compact", "cache": 20, "ungated_hooks": "with_calls"},
    "medium": {"ask": "risky", "state": "full", "cache": 6, "ungated_hooks": "with_calls"},
    "high": {"ask": "contested", "state": "full", "cache": 0, "ungated_hooks": "always"},
}
MODES = {
    "descend": {"mode": "descend", "risk": "normal"},
    "explore": {"mode": "explore", "risk": "normal"},
    "careful": {"mode": "careful", "risk": "low", "danger_max": 0.65, "p_min": 0.45, "esc_gap": 30},
}
CFG = dict(DEFAULTS)


def val(key):
    """A tunable's effective value: explicit setting, else derived from the risk level."""
    v = CFG[key]
    if v is None:
        v = RISK.get(CFG["risk"], RISK["normal"])[key]
    return v


def coerce(key, value):
    default = DEFAULTS[key]
    if default is None:
        if value in (None, "", "none", "None", "auto"):
            return None
        return int(value) if key == "lead" else finite(float(value))
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError("%s wants on/off" % key)
    if isinstance(default, float):
        return finite(float(value))
    if isinstance(default, int) and str(value).strip().lower() in ("on", "off", "true", "false", "yes", "no"):
        return int(str(value).strip().lower() in ("on", "true", "yes"))
    return type(default)(value)


def finite(x):
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError("not a finite number")
    return x


# Allowed values, by key: discrete choices, on/off flags, fractions and signed numbers. Everything else numeric
# must be zero or more.
CHOICES = {"report": ("compact", "full"), "mines": ("auto", "allow", "avoid", "escalate"), "milestone": ("off", "depth", "xl", "both"),
           "fight_handoff": ("ladder", "escalate"), "mapping": (0, 1, 2)}
FLAGS = ("fight_question", "dig", "potions", "spells", "elbereth", "trapdoors", "probe", "briefing", "branch_points", "pickup_food",
         "ranged", "auto")
FRACTIONS = ("descend_hp", "rest_hp", "hp_escalate", "elbereth_hp", "danger_max", "p_min", "hp_drop", "milestone_hp")
SIGNED = ("lead", "fragile_lead", "last_prayer", "tiebreak_seed", "time_left")


MODE_KEYS = sorted({k for m in MODES.values() for k in m})


def apply_settings(mode=None, sets=None):
    """A mode resets only the settings modes own (mode, risk and the escalation thresholds) and then applies its
    values; everything else (mines, avoid, dig, effort, ...) is kept. Explicit sets come last."""
    pending = validate(sets)
    if mode:
        if mode not in MODES:
            raise ValueError("unknown mode %s (known: %s)" % (mode, ", ".join(MODES)))
        for k in MODE_KEYS:
            CFG[k] = DEFAULTS[k]
        CFG.update(MODES[mode])
    merged = dict(CFG, **pending)
    if merged["rest_hp"] is not None and merged["rest_hp"] < 0.4 and merged["hp_escalate"] == 0 and \
            merged["crisis_turns"] > 50:
        raise ValueError("these settings remove every HP floor at once (rest_hp < 0.4, hp_escalate=0, "
                         "crisis_turns > 50); keep at least one")
    CFG.update(pending)


def validate(sets):
    """Check every k=v before anything changes; returns the coerced values or raises ValueError."""
    pending = {}
    choices = dict(CHOICES, risk=tuple(RISK), effort=tuple(EFFORT), **{k: (0, 1) for k in FLAGS})
    for k, v in (sets or {}).items():         # validate everything before changing anything
        if k == "mode":
            raise ValueError("use --mode descend|explore|careful to change the mode")
        if k not in DEFAULTS:
            raise ValueError("unknown setting %s (known: %s)" % (k, ", ".join(sorted(DEFAULTS))))
        try:
            value = coerce(k, v)
        except (TypeError, ValueError):
            raise ValueError("bad value for %s: %r" % (k, v))
        if k in choices and value not in choices[k]:
            raise ValueError("%s must be one of %s" % (k, ", ".join(str(x) for x in choices[k])))
        if isinstance(value, (int, float)) and not isinstance(value, bool) and k not in choices:
            if k in FRACTIONS and not 0 <= value <= 1:
                raise ValueError("%s is a fraction between 0 and 1" % k)
            if k not in SIGNED and k not in FRACTIONS and value < 0:
                raise ValueError("%s must be zero or more" % k)
        if k == "pause_on":
            from .escalation import parse_pause_on
            parse_pause_on(value)
        if k == "avoid" and value:
            try:
                re.compile(value)
            except re.error as e:
                raise ValueError("avoid is not a valid regex: %s" % e)
        pending[k] = value
    return pending


def effort():
    return EFFORT.get(CFG["effort"], EFFORT["medium"])


def describe():
    return " ".join("%s=%s" % (k, CFG[k]) for k in DEFAULTS
                    if k not in ("last_prayer", "tiebreak_seed", "time_left"))
