"""Food rules: safe corpses, the order pack food is eaten in, floor and tin prompts, kills that leave corpses,
prayer for hunger and the hunger escalation."""
import random

from helpers import Case, nh, screen, view

F = nh.food
K = nh.knowledge

ROOM = [" ---------- ", " |........| ", " |........| ", " |........| ", " ---------- "]
STATUS1 = "Hero the Stripling   St:16 Dx:12 Co:14 In:9 Wi:10 Ch:8 Lawful"


def status2(hungry="", turn=1000, hp="16(16)", pw="2(2)"):
    return "Dlvl:3 $:0 HP:%s Pw:%s AC:6 Xp:2 T:%d %s" % (hp, pw, turn, hungry)


class ScriptTerm:
    """Shows one screen after another: each send moves to the next screen (the last one stays)."""

    def __init__(self, views):
        self.views, self.sent = list(views), []

    def view(self):
        return self.views[0]

    def send(self, keys):
        self.sent.append(keys)
        if len(self.views) > 1:
            self.views.pop(0)

    def settle(self, *a):
        pass

    def poll(self, *a):
        return False


def map_view(top="", hungry="", turn=1000, rows=None, cursor=(3, 4), **kw):
    return view(screen(top, rows or ROOM, status1=STATUS1, status2=status2(hungry, turn, **kw)), cursor)


def prompt(text):
    return view(screen(text, ROOM, status1=STATUS1, status2=status2()), (0, len(text) + 1))


def pilot(views, role="Valkyrie", race="human"):
    p = nh.Pilot(ScriptTerm(views), None)
    p.options = p.briefed = True
    p.inv_turn, p.inv_complete = 10 ** 9, True
    p.rng = random.Random(0)
    p.role, p.race = role, race
    p.lookup = lambda pos: None
    return p


def ranked(p):
    v = p.term.view()
    p.branch_dl = v.st.get("dlvl")
    p.view()
    c = p.context(v)
    return p.actions(v, c), c


class CorpseSafetyTest(Case):
    def test_hazards_follow_the_monster_facts(self):
        self.assertIsNone(F.corpse_hazard("jackal"))
        self.assertIsNone(F.corpse_hazard("newt"))
        self.assertEqual(F.corpse_hazard("cockatrice"), "dangerous to eat")
        self.assertEqual(F.corpse_hazard("chickatrice"), "dangerous to eat")
        self.assertEqual(F.corpse_hazard("werejackal"), "dangerous to eat")
        self.assertEqual(F.corpse_hazard("acid blob"), "acidic")
        self.assertEqual(F.corpse_hazard("kobold"), "poisonous")
        self.assertIsNone(F.corpse_hazard("kobold", role="Barbarian"))          # poison resistant from the start
        self.assertIsNone(F.corpse_hazard("kobold", race="orcish"))
        self.assertIsNone(F.corpse_hazard("kobold", poison_res=True))
        self.assertEqual(F.corpse_hazard("human", race="human"), "cannibalism")
        self.assertIsNone(F.corpse_hazard("human", role="Caveman", race="human"))
        self.assertIsNone(F.corpse_hazard("dwarf", race="human"))
        self.assertEqual(F.corpse_hazard("dwarf", race="dwarvish"), "cannibalism")
        self.assertEqual(F.corpse_hazard("little dog"), "a domestic animal (aggravates monsters)")
        self.assertEqual(F.corpse_hazard("pony"), "a domestic animal (aggravates monsters)")
        self.assertEqual(F.corpse_hazard("giant bat"), "dangerous to eat")
        self.assertEqual(F.corpse_hazard("lizard"), None)
        self.assertEqual(F.corpse_hazard("lichen", role="Monk"), None)
        self.assertEqual(F.corpse_hazard("jackal", role="Monk"), "meat (a Monk's alignment suffers)")
        self.assertEqual(F.corpse_hazard("frobnitz"), "unknown monster")
        self.assertEqual(F.corpse_hazard("kobold zombie"), "dangerous to eat")

    def test_corpse_names(self):
        self.assertEqual(F.corpse_name("a jackal corpse"), "jackal")
        self.assertEqual(F.corpse_name("2 uncursed partly eaten newt corpses"), "newt")
        self.assertEqual(F.corpse_name("a partly eaten gnome lord corpse"), "gnome lord")
        self.assertIsNone(F.corpse_name("a food ration"))

    def test_freshness(self):
        p = pilot([map_view()])
        self.assertTrue(p.corpse_ok("jackal", 10))
        self.assertFalse(p.corpse_ok("jackal", F.FRESH_TURNS))
        self.assertFalse(p.corpse_ok("jackal", None))           # a corpse of unknown age
        self.assertTrue(p.corpse_ok("lichen", None))            # lichens never rot
        self.assertFalse(p.corpse_ok("cockatrice", 1))


class PackFoodTest(Case):
    PACK = {"a": ("a +1 long sword (weapon in hand)", "Weapons"),
            "f": ("2 uncursed food rations", "Comestibles"),
            "g": ("an uncursed lembas wafer", "Comestibles"),
            "h": ("3 uncursed apples", "Comestibles"),
            "i": ("a K-ration", "Comestibles"),
            "j": ("an egg", "Comestibles"),
            "k": ("a lizard corpse", "Comestibles"),
            "l": ("a tin opener", "Tools"),
            "m": ("a jackal corpse", "Comestibles"),
            "n": ("a fortune cookie (unpaid, 7 zorkmids)", "Comestibles"),
            "o": ("a partly eaten food ration", "Comestibles")}

    def test_order_cheap_first_rations_kept_cures_last(self):
        p = pilot([map_view()])
        p.inv = dict(self.PACK)
        self.assertEqual([k for k, _ in p.food_letters()], ["o", "h", "f", "i", "g", "k"])
        self.assertEqual(p.food_choice("Hungry")[0], "o")
        del p.inv["o"], p.inv["h"], p.inv["f"]
        self.assertEqual(p.food_choice("Hungry")[0], "i")        # the kept rations only when nothing else
        del p.inv["i"], p.inv["g"]
        self.assertIsNone(p.food_choice("Hungry"))               # the lizard is kept
        self.assertIsNone(p.food_choice("Weak", can_pray=True))  # pray rather than eat it
        self.assertEqual(p.food_choice("Weak")[0], "k")
        self.assertIsNone(p.food_choice(None))

    def test_fruit_is_thrown_only_when_spare(self):
        p = pilot([map_view()])
        p.inv = {"h": ("10 uncursed apples", "Comestibles"), "c": ("10 uncursed carrots", "Comestibles")}
        self.assertIsNone(p.spare_missile())                       # 1000 nutrition: never below the reserve
        p.inv["f"] = ("2 food rations", "Comestibles")
        self.assertEqual(p.spare_missile()[0], "c")

    def test_eating_from_the_pack_refuses_floor_food(self):
        p = pilot([map_view(hungry="Hungry"),
                   prompt("There is a jackal corpse here; eat it? [ynq] (n)"),
                   prompt("What do you want to eat? [fh or ?*]"),
                   map_view(hungry="")])
        p.inv = {"f": ("a food ration", "Comestibles"), "h": ("2 apples", "Comestibles")}
        p.eat_pack({"hungry": "Hungry", "can_pray": False, "turn": 1000})
        self.assertEqual(p.term.sent, ["e", "n", "h"])
        self.assertEqual(p.inv_turn, -2)


class FloorPromptTest(Case):
    def kill(self, p, name, turn, pos=(3, 5)):
        p.last_act = nh.Act("attack_l", "attack", "Fl", "attack", 4, pos)
        p.message("You kill the %s!" % name, map_view(turn=turn))

    def test_kills_are_remembered_and_eaten_fresh(self):
        p = pilot([map_view(turn=1000)])
        self.kill(p, "jackal", 990)
        self.assertEqual(p.kills, [(3, (3, 5), "jackal", 990)])
        p.eating_corpse, p.eating_at = True, (3, 5)
        p.last_st = {"dlvl": 3, "turn": 1000, "xl": 2}
        self.assertEqual(p.floor_food_answer("There is a jackal corpse here; eat it? [ynq] (n)"), "y")
        self.assertEqual(p.floor_food_answer("There is a partly eaten jackal corpse here; eat it? [ynq] (n)"), "y")
        self.assertEqual(p.floor_food_answer("There is a cursed jackal corpse here; eat it? [ynq] (n)"), "n")
        self.assertEqual(p.floor_food_answer("There is a newt corpse here; eat it? [ynq] (n)"), "n")  # not ours
        self.assertEqual(p.floor_food_answer("There is a food ration here; eat it? [ynq] (n)"), "n")
        p.last_st["turn"] = 990 + F.FRESH_TURNS
        self.assertEqual(p.floor_food_answer("There is a jackal corpse here; eat it? [ynq] (n)"), "n")  # too old
        p.eating_corpse = False
        p.last_st["turn"] = 1000
        self.assertEqual(p.floor_food_answer("There is a jackal corpse here; eat it? [ynq] (n)"), "n")

    def test_pets_and_unseen_kills_leave_no_memory(self):
        p = pilot([map_view()])
        p.last_act = nh.Act("attack_l", "attack", "Fl", "attack", 4, (3, 5))
        p.message("You kill poor Fido!", map_view())
        p.last_act = nh.Act("explore", "explore", "_", "explore", 2, (3, 5))
        p.message("You kill the newt!", map_view())
        self.assertEqual(p.kills, [])

    def test_eat_floor_skips_a_cockatrice_and_eats_the_jackal(self):
        p = pilot([map_view(),
                   prompt("There is a cockatrice corpse here; eat it? [ynq] (n)"),
                   prompt("There is a jackal corpse here; eat it? [ynq] (n)"),
                   view(screen("This jackal corpse tastes terrible!--More--", ROOM, STATUS1, status2()), (0, 45)),
                   map_view(hungry="")])
        p.kills = [(3, (3, 4), "cockatrice", 995), (3, (3, 4), "jackal", 995)]
        p.last_st = {"dlvl": 3, "turn": 1000, "xl": 2}
        p.eat_floor({"hero": (3, 4), "dl": 3})
        self.assertEqual(p.term.sent, ["e", "n", "y", " "])
        self.assertEqual(p.kills, [])                       # eaten: forgotten
        self.assertFalse(p.eating_corpse)

    def test_an_interrupted_meal_is_resumed_later(self):
        p = pilot([map_view(),
                   prompt("There is a jackal corpse here; eat it? [ynq] (n)"),
                   view(screen("You stop eating the jackal corpse.--More--", ROOM, STATUS1, status2()), (0, 40)),
                   map_view(hungry="Hungry")])
        p.kills = [(3, (3, 4), "jackal", 995)]
        p.last_st = {"dlvl": 3, "turn": 1000, "xl": 2}
        p.eat_floor({"hero": (3, 4), "dl": 3})
        self.assertEqual(p.kills, [(3, (3, 4), "jackal", 995)])

    def test_nothing_safe_here_cancels_the_pack_prompt(self):
        p = pilot([map_view(),
                   prompt("There is a kobold corpse here; eat it? [ynq] (n)"),
                   prompt("What do you want to eat? [f or ?*]"),
                   map_view()])
        p.kills = [(3, (3, 4), "kobold", 995)]
        p.last_st = {"dlvl": 3, "turn": 1000, "xl": 2}
        p.eat_floor({"hero": (3, 4), "dl": 3})
        self.assertEqual(p.term.sent, ["e", "n", "\x1b"])

    def test_prompt_table(self):
        self.assertEqual(K.prompt_answer("There are 2 newt corpses here; eat one? [ynq] (n)"), "CORPSE")
        self.assertEqual(K.prompt_answer("Continue eating? [yn] (n)"), "n")
        self.assertEqual(K.prompt_answer("It smells like newts.  Eat it? [yn] (n)"), "TIN")
        self.assertEqual(K.prompt_answer("Eat it? [yn] (n)"), "TIN")

    def test_tins(self):
        p = pilot([map_view()])
        self.assertEqual(p.tin_answer("It smells like newts.  Eat it? [yn] (n)"), "y")
        self.assertEqual(p.tin_answer("It smells like cockatrices.  Eat it? [yn] (n)"), "n")
        self.assertEqual(p.tin_answer("It smells like kobolds.  Eat it? [yn] (n)"), "n")
        p.message("It smells like jackals.", map_view())
        self.assertEqual(p.tin_answer("Eat it? [yn] (n)"), "y")
        p.message("It contains spinach.", map_view())
        self.assertEqual(p.tin_answer("Eat it? [yn] (n)"), "y")

    def test_poison_resistance_is_learned(self):
        p = pilot([map_view()])
        self.assertFalse(p.corpse_ok("kobold", 1))
        p.message("You feel healthy.", map_view())
        self.assertTrue(p.corpse_ok("kobold", 1))


class HungerActionsTest(Case):
    def test_a_fresh_corpse_is_eaten_before_moving_on(self):
        p = pilot([map_view(turn=1000)])
        p.kills = [(3, (3, 4), "jackal", 995)]
        acts, _ = ranked(p)
        self.assertEqual(acts[0].key, "eat_corpse")
        p = pilot([map_view(turn=1000, hungry="Satiated")])
        p.kills = [(3, (3, 4), "jackal", 995)]
        self.assertNotIn("eat_corpse", [a.key for a in ranked(p)[0]])   # never eat while Satiated
        p = pilot([map_view(turn=1000)])
        p.kills = [(3, (3, 4), "jackal", 995)]
        p.inv = {"f": ("2 food rations", "Comestibles")}
        self.assertNotIn("eat_corpse", [a.key for a in ranked(p)[0]])   # a full pack: not worth the risk
        p.term.views = [map_view(turn=1000, hungry="Hungry")]
        self.assertEqual(ranked(p)[0][0].key, "eat_corpse")             # hungry: the corpse saves the pack

    def test_hungry_walks_to_a_nearby_corpse(self):
        rows = [" ---------- ", " |........| ", " |..@...%| ", " |........| ", " ---------- "]
        p = pilot([map_view(turn=1000, hungry="Hungry", rows=rows)])
        p.kills = [(3, (3, 8), "newt", 990), (3, (2, 6), "jackal", 995)]
        acts, _ = ranked(p)
        self.assertEqual(acts[0].key, "goto_corpse")
        self.assertEqual(acts[0].target, (3, 8))
        self.assertEqual(p.kills, [(3, (3, 8), "newt", 990)])     # the jackal left no corpse: forgotten

    def test_hungry_eats_from_the_pack(self):
        p = pilot([map_view(hungry="Hungry")])
        p.inv = {"f": ("a food ration", "Comestibles"), "h": ("2 apples", "Comestibles")}
        acts, _ = ranked(p)
        self.assertEqual(acts[0].key, "eat")
        self.assertIn("apples", acts[0].desc)

    def test_weak_with_no_food_prays_when_safe(self):
        p = pilot([map_view(hungry="Weak", turn=2000)])
        p.last_prayer = 1000
        acts, c = ranked(p)
        self.assertEqual(acts[0].key, "pray")
        self.assertIsNone(p.hunger_reason(c, acts))

    def test_hunger_escalates_once_and_only_without_remedy(self):
        p = pilot([map_view(hungry="Hungry", turn=1000)])
        p.last_prayer = 600                          # safe again from T1500: after Fainting
        acts, c = ranked(p)
        key, reason = p.hunger_reason(c, acts)
        self.assertTrue(reason.startswith("Hungry with no food"), reason)
        self.assertEqual(nh.escalation.classify(reason), "hunger")
        p.hunger_noted = key
        self.assertIsNone(p.hunger_reason(c, acts))  # once per state
        p.term.views = [map_view(hungry="Fainting", turn=1000)]
        acts, c = ranked(p)
        p.hunger_noted = p.hunger_reason(c, acts)[0]
        p.term.views = [map_view(hungry="Fainted", turn=1001)]
        acts, c = ranked(p)
        self.assertIsNone(p.hunger_reason(c, acts))  # Fainted and Fainting are one state
        p.term.views = [map_view(hungry="Hungry", turn=1000)]
        acts, c = ranked(p)
        p.last_prayer = 200                          # safe from T1100: before Fainting, so the loop will pray
        self.assertIsNone(p.hunger_reason(c, acts))
        p.last_prayer = 600
        p.inv = {"h": ("an apple", "Comestibles")}
        self.assertIsNone(p.hunger_reason(c, ranked(p)[0]))

    def test_starving_prays_as_a_last_resort(self):
        p = pilot([map_view(hungry="Fainting", turn=1000)])
        p.last_prayer = 700
        acts, c = ranked(p)
        self.assertNotIn("pray", [a.key for a in acts])    # Con 14: starves at about T1240
        p.last_prayer = 500
        self.assertEqual(ranked(p)[0][0].key, "pray")       # 500 turns since the last prayer: likely to work
        p.last_prayer = 700
        p.term.views = [map_view(hungry="Fainting", turn=1190)]
        acts, c = ranked(p)
        self.assertEqual(acts[0].key, "pray")
        p.prayer_broken = True
        self.assertNotIn("pray", [a.key for a in ranked(p)[0]])

    def test_status_reads_con_and_int(self):
        v = map_view()
        self.assertEqual((v.st["con"], v.st["int"]), (14, 9))


class SpellHungerTest(Case):
    def test_trivial_adjacent_monsters_are_meleed_not_bolted(self):
        rows = [" ---------- ", " |........| ", " |..@d....| ", " |........| ", " ---------- "]
        for name, zap in (("jackal", False), ("soldier ant", True)):
            p = pilot([view(screen("", rows, STATUS1, status2(pw="10(10)")), (3, 4))], role="Wizard")
            p.spells = {"force bolt": ("a", 1, 0)}
            p.lookup = lambda pos, name=name: name if pos == (3, 5) else None
            keys = [a.key for a in ranked(p)[0]]
            self.assertEqual("zap_l" in keys, zap, (name, keys))


class PrayerIsFoodTest(Case):
    def test_hungry_with_no_food_quaffs_before_praying(self):
        rows = [" ---------- ", " |........| ", " |..@d....| ", " |........| ", " ---------- "]
        for hungry, inv, first in (("Hungry", {}, "quaff"), ("", {}, "pray"),
                                   ("Hungry", {"f": ("a food ration", "Comestibles")}, "pray")):
            p = pilot([view(screen("", rows, STATUS1, status2(hungry, hp="3(16)")), (3, 4))])
            p.inv = dict(inv, d=("2 uncursed potions of healing", "Potions"))
            p.lookup = lambda pos: "jackal" if pos == (3, 5) else None
            acts, c = ranked(p)
            self.assertEqual(acts[0].key, first, (hungry, inv, [a.key for a in acts[:3]]))


class CalmMealTest(Case):
    def test_no_meal_with_a_hostile_in_view_unless_weak(self):
        rows = [" ---------- ", " |........| ", " |..@....d| ", " |........| ", " ---------- "]
        for hungry, expect in (("", False), ("Weak", True)):
            p = pilot([view(screen("", rows, STATUS1, status2(hungry)), (3, 4))])
            p.kills = [(3, (3, 4), "jackal", 995)]
            p.lookup = lambda pos: "jackal" if pos == (3, 9) else None
            self.assertEqual("eat_corpse" in [a.key for a in ranked(p)[0]], expect, hungry)
