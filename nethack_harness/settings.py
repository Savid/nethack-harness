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
    "sturdy_hp": 25,        # a hero with less max HP than this, or AC worse than sturdy_ac, is fragile
    "sturdy_ac": 6,
    "fragile_lead": 1,      # a fragile hero descends no deeper than XL + this (+1 at risk=high)
    "cap_lift": 120,        # seconds on a level after which the depth lead no longer blocks descending
    "potions": 1,           # quaff known healing potions in emergencies
    "spells": 1,            # cast healing in emergencies when the hero knows it
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
    "pickup_food": 1,       # pick up known-safe food the hero steps on
    "ranged": 1,            # fire the quivered missiles (f) at hostiles approaching in a line
    "multi_quiet": 0.12,    # seconds of silence that end a multi-turn command (count, travel, run)
    "quiet": 0.06,          # seconds of terminal silence that end a key send
    "last_prayer": -1,      # set to the turn of a prayer made by hand
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


ALIASES = {"xl_lead": "lead"}     # older names keep working


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
    CFG.update(pending)


def validate(sets):
    """Check every k=v before anything changes; returns the coerced values or raises ValueError."""
    pending = {}
    for k, v in (sets or {}).items():         # validate everything before changing anything
        k = ALIASES.get(k, k)
        if k not in DEFAULTS:
            raise ValueError("unknown setting %s (known: %s)" % (k, ", ".join(sorted(DEFAULTS))))
        if k == "risk" and v not in RISK:
            raise ValueError("risk must be low, normal or high")
        if k == "effort" and v not in EFFORT:
            raise ValueError("effort must be off, low, medium or high")
        if k == "mines" and v not in ("auto", "allow", "avoid", "escalate"):
            raise ValueError("mines must be auto, allow, avoid or escalate")
        try:
            value = coerce(k, v)
        except (TypeError, ValueError):
            raise ValueError("bad value for %s: %r" % (k, v))
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
    return " ".join("%s=%s" % (k, CFG[k]) for k in DEFAULTS if k != "last_prayer")
