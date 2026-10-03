import os
import unittest

from helpers import nh, screen, view

K = nh.knowledge


class FakeTerm:
    """Records sends; always shows the same normal screen."""

    def __init__(self, lines=None, cursor=(2, 3)):
        self.sent = []
        self.v = view(lines or screen("", [" ---- ", " |.@.| ", " ---- "]), cursor)

    def send(self, keys):
        self.sent.append(keys)

    def view(self):
        return self.v


class PromptTableTest(unittest.TestCase):
    def test_fixed_answers(self):
        self.assertEqual(K.prompt_answer("Really step onto that trap door? [yn] (n)"), "y")
        self.assertEqual(K.prompt_answer("Really attack the watchman? [yn] (n)"), "ESC")
        self.assertEqual(K.prompt_answer("Beware, there will be no return! Still climb? [yn] (n)"), "n")
        self.assertEqual(K.prompt_answer("Really step onto that bear trap? [yn] (n)"), "n")
        self.assertEqual(K.prompt_answer("Do you want your possessions identified? [ynq] (n)"), "GAME_OVER")
        self.assertIsNone(K.prompt_answer("Do you want to frobnicate? [yn] (n)"))

    def test_unknown_prompt_escapes_then_escalates(self):
        p = nh.Pilot(FakeTerm(), None)
        v = view(screen("Do you want to frobnicate? [yn] (n)"), (0, 40))
        self.assertIsNone(p.answer(v))
        self.assertEqual(p.term.sent, ["\x1b"])
        with self.assertRaises(nh.policy.Hard):
            p.answer(v)

    def test_pray_prompt_only_when_praying(self):
        p = nh.Pilot(FakeTerm(), None)
        v = view(screen("Are you sure you want to pray? [yn] (n)"), (0, 40))
        p.answer(v)
        p.praying = True
        p.answer(v)
        self.assertEqual(p.term.sent, ["n", "y"])


class TablesTest(unittest.TestCase):
    def test_colour_normalisation(self):
        self.assertEqual(K.colour("brown", True), ("brown", True))
        self.assertEqual(K.colour("brightgreen", False), ("green", True))
        self.assertEqual(K.colour("default", False), ("gray", False))

    def test_never_melee_by_symbol_and_name(self):
        self.assertTrue(K.never_melee("e", "blue", False))
        self.assertTrue(K.never_melee("F", "green", False))          # green mold
        self.assertIsNone(K.never_melee("F", "green", True))          # lichen
        self.assertTrue(K.never_melee("c", "brown", True))           # cockatrice
        self.assertTrue(K.never_melee("x", "gray", False, "floating eye"))
        self.assertIsNone(K.never_melee("d", "brown", False, "jackal"))
        self.assertEqual(K.threat_xl("q", "brown", False, "rothe"), 4)
        self.assertEqual(K.threat_xl("a", "blue", False), 99)


class PrayerTest(unittest.TestCase):
    def test_timing_and_failure(self):
        p = nh.Pilot(FakeTerm(), None)
        self.assertFalse(p.prayer_safe(100))
        self.assertTrue(p.prayer_safe(150))
        p.last_prayer = 1000
        self.assertFalse(p.prayer_safe(1500))
        self.assertTrue(p.prayer_safe(1900))
        p.message("You feel that Mitra is displeased.", p.term.view())
        self.assertTrue(p.prayer_broken)
        self.assertFalse(p.prayer_safe(9000))

    def test_shop_and_boulder_messages(self):
        p = nh.Pilot(FakeTerm(), None)
        v = p.term.view()
        p.message("Welcome to Asidonhopo's general store!", v)
        self.assertTrue(p.lv[3].shop and p.lv[3].no_kick)
        p.last_try = {"kind": "push", "hero": (2, 3), "key": "push_l"}
        p.message("You try to move the boulder, but in vain.", v)
        self.assertTrue(p.lv[3].banned((2, 3), "push_l", 0))


class SettingsTest(unittest.TestCase):
    def tearDown(self):
        nh.apply_settings("descend")

    def test_modes_reset_everything(self):
        nh.apply_settings("careful")
        self.assertEqual(nh.settings.val("descend_hp"), 0.85)
        nh.apply_settings("descend")
        self.assertEqual(nh.settings.val("descend_hp"), 0.7)
        self.assertEqual(nh.CFG["danger_max"], 0.8)

    def test_sets_and_validation(self):
        nh.apply_settings(None, {"risk": "high", "effort": "low", "lead": "3", "dig": "1"})
        self.assertEqual((nh.CFG["risk"], nh.CFG["effort"], nh.CFG["lead"], nh.CFG["dig"]), ("high", "low", 3, 1))
        with self.assertRaises(ValueError):
            nh.apply_settings(None, {"effort": "extreme"})
        with self.assertRaises(ValueError):
            nh.apply_settings(None, {"nonsense": "1"})

    def test_confidence_normalisation(self):
        self.assertAlmostEqual(nh.confidence({"a": 0.5, "b": 0.5}), 0.0)
        self.assertAlmostEqual(nh.confidence({"a": 1.0, "b": 0.0}), 1.0)
        self.assertAlmostEqual(nh.confidence({"a": 0.4, "b": 0.2, "c": 0.2, "d": 0.2}), 0.2)


class CharacterTest(unittest.TestCase):
    def test_attributes_line(self):
        m = K.ATTRIBUTES.search("  You are a Rhizotomist, a level 1 male gnomish Healer.")
        self.assertEqual((m.group(2), m.group(3)), ("gnomish", "Healer"))

    def test_mines_policy_and_lead(self):
        p = nh.Pilot(FakeTerm(), None)
        p.race, p.role = "gnomish", "Healer"
        self.assertEqual(p.mines_policy(), "allow")
        self.assertEqual(p.lead(), 2)
        p.race, p.role = "human", "Valkyrie"
        self.assertEqual(p.mines_policy(), "avoid")
        self.assertEqual(p.lead(), 4)


if __name__ == "__main__":
    unittest.main()
