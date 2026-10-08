from unittest import TestCase
import unittest

from helpers import screen, view
from nethack_harness.perceive import phase
from nethack_harness.screen import PromptContext


class ViewTest(TestCase):
    def test_quest_and_endgame_status_labels(self):
        for location in ("Home 1", "Home 6", "Earth", "Air", "Fire", "Water", "Astral Plane", "Fort Ludios", "Tutorial:1"):
            with self.subTest(location=location):
                v = view(screen("", [" ---- ", " |.@.| ", " ---- "],
                                status2=location + " $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400"))
                self.assertEqual(v.st["location"], location)
                self.assertTrue(v.normal)
        v = view(screen(status1="Fire the Evoker St:8 Dx:11 Co:15 In:18 Wi:11 Ch:12 Chaotic"))
        self.assertEqual(v.st["location"], "Dlvl:3")

    def test_status_and_hero(self):
        rows = [" ---- ", " |.@.| ", " ---- "]
        v = view(screen("", rows), (2, 3))
        self.assertTrue(v.normal)
        self.assertEqual(v.hero, (2, 3))
        self.assertEqual(v.st["dlvl"], 3)
        self.assertEqual((v.st["hp"], v.st["hpmax"], v.st["turn"]), (12, 16, 400))
        self.assertEqual([v.st[key] for key in ("strength", "dex", "con", "int", "wis", "cha", "alignment")],
                         ["16", 12, 14, 9, 10, 8, "Lawful"])

    def test_exceptional_strength_is_preserved(self):
        for strength in ("18/50", "18/**", "25"):
            v = view(screen(status1="Hero the Warrior St:%s Dx:12 Co:14 In:9 Wi:10 Ch:8 Neutral" % strength))
            self.assertEqual(v.st["strength"], strength)

    def test_prompts(self):
        self.assertTrue(view(screen("You hit the jackal.--More--"), (0, 30)).more)
        v = view(screen("Really attack the gnome? [yn] (n)"), (0, 30))
        self.assertEqual(v.yn, "yn")
        v = view(screen("What do you want to eat? [fg or ?*]"), (0, 30))
        self.assertEqual((v.obj, v.yn), ("fg", None))
        self.assertFalse(v.normal)

    def test_farlook_cursor_is_not_the_hero(self):
        v = view(screen("Move cursor to a monster, object or location:"), (2, 3))
        self.assertTrue(v.getpos)
        self.assertFalse(v.normal)
        self.assertIsNone(v.hero)

    def test_source_position_prompts(self):
        for message in ("(For instructions type a '?')", "Move cursor to the desired position:",
                        "Move cursor to the spot to hit:", "Where do you want to cast the spell?"):
            with self.subTest(message=message):
                v = view(screen(message), (2, 3))
                self.assertEqual(phase(v), "position")
                self.assertIsNone(v.hero)

    def test_spell_and_custom_direction_prompts(self):
        v = view(screen("Cast which spell? [a-c *?]"), (0, 28))
        self.assertEqual((phase(v), v.spell), ("spell", "a-c *?"))
        for message in ("Talk to whom? (in what direction)", "At whom? (in what direction)", "Open where? [.>]"):
            self.assertEqual(phase(view(screen(message), (0, len(message)))), "direction")

    def test_wrapped_text_remains_input(self):
        lines = screen("What do you want to engrave? " + "x" * 52, ["xxx"])
        v = view(lines, (1, 3))
        self.assertEqual(phase(v), "text")
        self.assertIsNone(v.hero)

    def test_message_split_at_a_space_continues_on_the_next_row(self):
        first = "(The seed chose your character; your role, race, gender and alignment options"
        v = view(screen(first, ["were ignored.)--More--", "", " ---- ", " |.@.| "]), (1, 22))
        self.assertEqual(v.msg, first + " were ignored.)--More--")
        self.assertEqual(phase(v), "more")
        corner = view(screen(" " * 31 + "There is a staircase up out of the dungeon here.",
                             ["", " " * 31 + "Things that are here:", " " * 31 + "--More--"]), (3, 39))
        self.assertEqual(corner.msg, "There is a staircase up out of the dungeon here.")

    def test_continuation_with_dots_or_more_is_part_of_the_message(self):
        for first, rest in (('You read: "They say that a gnome with a wand of digging can do wonders, but so',
                             "can you...--More--"),
                            ("You hear the footsteps of a guard on patrol.  You see here a piece of the",
                             "piece.--More--")):
            with self.subTest(rest=rest):
                v = view(screen(first, [rest, "", " ---- ", " |.@.| "]), (1, len(rest)))
                self.assertEqual(v.msg, first + " " + rest)
                self.assertEqual(v.message_rows, 2)

    def test_menu_search_reads_text_above_existing_menu(self):
        v = view(screen("Search for:", [" a - a dagger", " (end)"]), (0, 12))
        self.assertTrue(v.menu)
        self.assertEqual(phase(v), "text")


class PromptContextTest(TestCase):
    def start(self):
        context = PromptContext()
        v = context.observe(view(screen("Move cursor to the desired position:")))
        return context, v

    def test_descriptions_keep_targeting_until_confirmation(self):
        context, v = self.start()
        context.before_input(v, "l")
        described = context.observe(view(screen("floor of a room [03,05]"), (2, 4)))
        self.assertEqual(phase(described), "position")
        self.assertIsNone(described.hero)
        self.assertFalse(described.normal)
        context.before_input(described, ".")
        self.assertEqual(phase(context.observe(view())), "play")

    def test_nested_windows_do_not_end_targeting(self):
        for lines, cursor, key in ((screen("Targeting instructions --More--"), (0, 37), " "),
                                   (screen("Choose a target", [" a - goblin", " (end)"]), (2, 6), "\x1b")):
            context, _ = self.start()
            nested = context.observe(view(lines, cursor))
            context.before_input(nested, key)
            self.assertEqual(phase(context.observe(view(screen("floor of a room [03,05]"), (2, 4)))), "position")

    def test_full_height_help_preserves_targeting_without_status(self):
        for footer, key in (("--More--", " "), ("(end)", "\x1b")):
            context, _ = self.start()
            lines = ["Targeting instructions".ljust(80)] * 23 + [footer.ljust(80)]
            nested = context.observe(view(lines, (23, len(footer))))
            self.assertIsNone(nested.st.get("location"))
            self.assertIsNone(nested.st.get("turn"))
            context.before_input(nested, key)
            restored = context.observe(view(screen("floor of a room [03,05]"), (2, 4)))
            self.assertEqual(phase(restored), "position")
            self.assertIsNone(restored.hero)

    def test_selection_and_changed_turn_end_targeting(self):
        for key in ".,;:\x1b":
            context, v = self.start()
            context.before_input(v, key)
            self.assertEqual(phase(context.observe(view())), "play")
        context, _ = self.start()
        changed = view(screen("", [" ---- ", " |.@.| ", " ---- "],
                              status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:401"))
        self.assertEqual(phase(context.observe(changed)), "play")

    def test_ordinary_description_never_starts_targeting(self):
        v = view(screen("floor of a room [03,05]", [" ---- ", " |.@.| ", " ---- "]))
        self.assertEqual(phase(PromptContext().observe(v)), "play")


class ScreenRobustnessTest(TestCase):
    def test_death_message_allows_life_saving_pagination(self):
        self.assertEqual(phase(view(screen("You die...--More--"), (0, 20))), "more")
        self.assertFalse(view(screen("You die..."), (0, 10)).ended)

    def test_explicit_ascension_is_an_observed_terminal_result(self):
        for message in ("You ascend to the status of Demigod...--More--",
                        "You ascend to the status of Demigoddess...",
                        "Goodbye Jane Doe the Demigoddess..."):
            v = view(screen(message))
            self.assertEqual(phase(v), "ended")
            self.assertEqual(v.result, "ascended")
        self.assertIsNone(view(screen('You read: "You ascend to the status of Demigod..."')).result)

    def test_short_condition_forms(self):
        v = view(screen("", status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400 TermIll Ston Blnd Trap"), (0, 0))
        self.assertEqual(set(v.cond), {"TermIll", "Stone", "Blind", "Trapped"})

    def test_engravings_are_not_death(self):
        v = view(screen('You read: "They say that you will be killed by an exploding tin.  You die..."'), (0, 0))
        self.assertFalse(v.ended)
        self.assertTrue(view(screen("Do you want your possessions identified? [ynq] (n)"), (0, 50)).ended)

    def test_all_role_farewells_end_play(self):
        for farewell in ("Goodbye", "Fare thee well", "Sayonara", "Aloha", "Farvel"):
            self.assertEqual(phase(view(screen(farewell + " Jane Doe the Demigoddess..."))), "ended")
        self.assertFalse(view(screen('You read: "Goodbye Hero the Valkyrie..."')).ended)



if __name__ == "__main__":
    unittest.main()
