"""A fight going badly: the crisis ladder, retreat and verified Elbereth."""
from . import knowledge as K
from .level import cheb, compass, nbrs, travel
from .settings import CFG
from .base import Act


class Crisis:
    def crisis_active(self, c):
        cr = self.crisis
        return bool(cr) and cr["dl"] == c["dl"] and c["turn"] <= cr["until"]

    def retreat_act(self, v, c):
        """BotHack-style retreat: stairs within 8 steps along a path no threat is next to (then take them), else
        a step to a neighbouring square next to fewer threats, preferring corridors and doorways."""
        hero, lv, dist = c["hero"], c["lv"], c["dist"]
        threats = [h["pos"] for h in c["hostiles"] if h["dist"] <= 3]
        exits = [(lv.up, "<")] if lv.up and c["dl"] > 1 else []
        exits += [(p, ">") for p, kind in lv.downs.items() if kind == "main" and self.descend_ok(dict(c, hpf=1.0))]
        for p, key in sorted(exits, key=lambda e: dist.get(e[0], 999)):
            if p == hero or dist.get(p, 999) > 8:
                continue
            route = lv.route(v, hero, p) or []
            if route and all(cheb(q, t) > 1 for q in route[1:] for t in threats) and \
                    not any(lv.cell(v, q)[0] == "+" for q in route):
                return Act("retreat", "Retreat to the %s stairs %d steps %s and take them" % (
                    "up" if key == "<" else "down", len(route), compass(hero, p)), travel(hero, p), "retreat",
                    8.8, p)
        here = sum(cheb(hero, t) == 1 for t in threats)
        best = None
        for k, q in nbrs(hero):
            if dist.get(q) != 1 or v.ch(*q) in K.MON or q in lv.blocked:
                continue
            near = sum(cheb(q, t) == 1 for t in threats)
            exposed = sum(1 for _, n in nbrs(q) if dist.get(n) is not None)
            if near < here and (best is None or (near, exposed) < best[0]):
                best = ((near, exposed), k, q)
        if best:
            (near, exposed), k, q = best
            return Act("retreat", "Step %s away from the attackers (next to %d instead of %d%s)" % (
                K.DN[k], near, here, ", a narrow spot" if exposed <= 2 else ""), k, "retreat", 8.6, q)
        return None

    def crisis_ladder(self, v, c, acts):
        """A fight going badly: the standard bot ladder, in order. Pray, quaff, stairs underfoot, Elbereth,
        retreat, then fight. Returns the ladder steps plus the fighting moves; empty means the ladder is done."""
        hero, th = c["hero"], c["threats"]
        ladder = []
        heal = self.items(K.HEALING.pattern)
        if c["can_pray"] and c["trouble"]:
            # Hungry with no food: the prayer is the next meal, so a healing potion goes first when there is one
            pr = 9.35 if CFG["potions"] and heal and c["hpf"] < 0.5 and self.prayer_is_food(c) else 9.6
            ladder.append(Act("pray", "Pray (in serious trouble and the prayer timeout looks safe)", "", "pray", pr))
        if CFG["potions"] and heal and c["hpf"] < 0.5:
            ladder.append(Act("quaff", "Quaff the %s" % heal[0][1], "q" + heal[0][0], "quaff", 9.4))
        heal_spell = self.spell("heal", v)
        if heal_spell and c["hpf"] < 0.5:
            ladder.append(Act("cast_heal", "Cast %s on yourself" % heal_spell[1], "Z%s." % heal_spell[0], "cast",
                              9.3))
        cure = self.cure_act(v, c)
        if cure:
            ladder.append(Act(cure.key, cure.desc, cure.keys, cure.kind, 9.25))
        if c["under"] == "<" and c["dl"] > 1:
            ladder.append(Act("flee_up", "Escape up the stairs you stand on", "<", "flee", 9.2))
        elif c["under"] == ">" and self.descend_ok(dict(c, hpf=1.0)) and \
                not (c["lv"].downs.get(hero) == "branch" and self.mines_policy() == "avoid"):
            ladder.append(Act("descend", "Escape down the stairs you stand on", ">", "descend", 9.2))
        adjacent = [h for h in th if h["dist"] == 1]
        if CFG["elbereth"] and adjacent and self.elbereth_useful(v, c, adjacent):
            ladder.append(Act("elbereth", "Engrave Elbereth in the dust (verified after engraving)", "", "elbereth",
                              9.0))
        if c["on_elbereth"] and not c["ranged"]:       # (a hit since engraving already voids on_elbereth)
            ladder.append(Act("rest_s", "Rest one turn on the verified Elbereth", "ms", "rest", 8.9))
        if c["ranged"]:
            ladder += [Act(a.key, a.desc, a.keys, a.kind, 9.1, a.target) for a in self.leave_line(v, c)]
        retreat = self.retreat_act(v, c)
        if retreat:
            ladder.append(retreat)
        fight = [a for a in acts if a.kind in ("attack", "fire", "throw", "zap")]
        if c["blind"] and c["hpf"] < 0.5:
            # blind and hurt: an unseen marker may be a long-gone monster or something worse; waste no turns on it
            fight = [a for a in fight if not (a.target and (v.ch(*a.target) == "I" or v.ch(*a.target) in K.WARNING))]
        if c["incoming"] >= c["hp"]:
            # they can kill the hero before it acts again: a one-turn step (engrave, rest, a walk away) is no
            # safer than fighting; only prayer, potions and stairs underfoot beat a blow that may end it
            for a in ladder:
                if a.kind in ("elbereth", "rest", "retreat") or a.key.startswith("leave_line"):
                    a.prior = 5.5
            for a in fight:
                h = next((h for h in c["hostiles"] if h["pos"] == a.target), None)
                a.prior = 6.5 if h and h["threat"] <= c["xl"] else 5.8
        tried = self.crisis["tried"] if self.crisis else []
        for a in ladder:         # a step tried twice in this crisis without ending it goes behind the others
            if a.key != "rest_s" and tried.count(a.key) >= 2:
                a.prior -= 6
        return sorted(ladder + fight, key=lambda a: -a.prior)

    def engrave_elbereth(self, c):
        """Engrave, read it back, and engrave once more if this build's dust errors misspelt it."""
        for attempt in (1, 2):
            self.flow("E-", until=8)
            text = self.read_engraving()
            if text.lower() == "elbereth":
                self.elbereth_at = (c["dl"], c["hero"], c["turn"])
                self.note("elbereth", "engraved and verified" + (" on the second try" if attempt == 2 else ""))
                return True
            self.note("elbereth", "engraving reads %r, not Elbereth" % text[:30])
        self.elbereth_at, self.elbereth_failed = None, (c["dl"], c["hero"], c["turn"])
        return False
