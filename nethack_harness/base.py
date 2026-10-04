"""Shared small types: actions, hard escalations, sentinels."""


GAME_OVER = "game_over"
RACE_MONSTER = {"dwarvish": "dwarf", "gnomish": "gnome", "elven": "elf", "orcish": "orc", "human": "human"}


class Act:
    def __init__(self, key, desc, keys, kind, prior=0.0, target=None):
        self.key, self.desc, self.keys, self.kind, self.prior, self.target = key, desc, keys, kind, prior, target
        self.door = None           # goto_door: the locked door this trip is for

    def __repr__(self):
        return "Act(%s %.1f)" % (self.key, self.prior)


class Hard(Exception):
    """An escalation that budgets and calm windows never suppress."""
