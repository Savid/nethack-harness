import unittest

from helpers import Case, Endpoint, FakeGame, facts, nh, screen, view  # noqa: F401


class ViewTest(Case):
    def test_status_and_hero(self):
        rows = [" ---- ", " |.@.| ", " ---- "]
        v = view(screen("", rows), (2, 3))
        self.assertTrue(v.normal)
        self.assertEqual(v.hero, (2, 3))
        self.assertEqual(v.st["dlvl"], 3)
        self.assertEqual((v.st["hp"], v.st["hpmax"], v.st["turn"]), (12, 16, 400))

    def test_prompts(self):
        self.assertTrue(view(screen("You hit the jackal.--More--"), (0, 30)).more)
        v = view(screen("Really attack the gnome? [yn] (n)"), (0, 30))
        self.assertEqual(v.yn, "yn")
        v = view(screen("What do you want to eat? [fg or ?*]"), (0, 30))
        self.assertEqual((v.obj, v.yn), ("fg", None))
        self.assertFalse(v.normal)


class ScreenRobustnessTest(Case):
    def test_short_condition_forms(self):
        v = view(screen("", status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400 TermIll Ston Blnd Trap"), (0, 0))
        self.assertEqual(set(v.cond), {"TermIll", "Stone", "Blind", "Trapped"})

    def test_engravings_are_not_death(self):
        v = view(screen('You read: "They say that you will be killed by an exploding tin.  You die..."'), (0, 0))
        self.assertFalse(v.dead)
        self.assertTrue(view(screen("Do you want your possessions identified? [ynq] (n)"), (0, 50)).dead)

    def test_pet_fights_are_not_hits(self):
        self.assertIsNone(nh.knowledge.HIT.search("Your little dog bites the jackal."))
        self.assertIsNotNone(nh.knowledge.HIT.search("The jackal bites!"))


if __name__ == "__main__":
    unittest.main()
