"""Tunables. Every key can be changed at runtime with `--set k=v`; `--mode` resets them to the mode's values."""

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
    "search_budget": 150,   # search turns per level before moving down the escape ladder
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
    "pickup_food": 1,       # pick up known-safe food the hero steps on
    "ranged": 1,            # fire the quivered missiles (f) at hostiles approaching in a line
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
        return int(value) if key == "lead" else float(value)
    return type(default)(value)


def apply_settings(mode=None, sets=None):
    """A mode resets every tunable to its default, then applies the mode; explicit sets come last."""
    if mode:
        if mode not in MODES:
            raise ValueError("unknown mode %s (known: %s)" % (mode, ", ".join(MODES)))
        keep = CFG["last_prayer"]
        CFG.clear()
        CFG.update(DEFAULTS)
        CFG.update(MODES[mode])
        CFG["last_prayer"] = keep
    for k, v in (sets or {}).items():
        if k not in DEFAULTS:
            raise ValueError("unknown setting %s (known: %s)" % (k, ", ".join(sorted(DEFAULTS))))
        if k == "risk" and v not in RISK:
            raise ValueError("risk must be low, normal or high")
        if k == "effort" and v not in EFFORT:
            raise ValueError("effort must be off, low, medium or high")
        if k == "mines" and v not in ("auto", "allow", "avoid", "escalate"):
            raise ValueError("mines must be auto, allow, avoid or escalate")
        CFG[k] = coerce(k, v)


def effort():
    return EFFORT.get(CFG["effort"], EFFORT["medium"])


def describe():
    return " ".join("%s=%s" % (k, CFG[k]) for k in DEFAULTS if k != "last_prayer")
