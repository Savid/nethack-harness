from unittest import TestCase
import json
from pathlib import Path

from helpers import screen, view
from nethack_harness.actions import Action, catalogue
from nethack_harness.perceive import Observer, fingerprint, parse_menu_entries
from nethack_harness.screen import PromptContext, View


class ObservationTest(TestCase):
    def test_engulfed_overlay_preserves_memory_without_inventing_spatial_facts(self):
        normal = view(screen("", [" ------- ", " |.@.>| ", " ------- "]))
        swallowed = view(screen("You are swallowed!", ["  /-\\", "  |@|", "  \\-/"],
                               status2="Dlvl:3 $:0 HP:10(16) Pw:2(2) AC:6 Xp:2 T:401"))
        self.assertTrue(swallowed.engulfed)
        for prime in (False, True):
            with self.subTest(previous_map=prime):
                observer = Observer()
                initial = observer.observation(normal) if prime else None
                state = observer.observation(swallowed)
                self.assertEqual(state["entities"], [])
                self.assertEqual(state["adjacent"], [])
                self.assertIsNone(state["underfoot"]["remembered_terrain"])
                self.assertEqual(state["hero"]["hp"], 10)
                self.assertEqual(state["map_context"], "engulfed overlay")
                self.assertIn("You are swallowed!", state["messages"][-1]["text"])
                if prime:
                    self.assertEqual(state["level"], initial["level"])
                    self.assertEqual(state["known_levels"], initial["known_levels"])
                else:
                    self.assertEqual(observer.current.terrain, {})
                    self.assertEqual(state["known_levels"][0]["landmarks"], [])
                actions = catalogue(swallowed, observer, 8)
                self.assertFalse(any(a.kind in ("inspect", "travel", "explore") for a in actions))
                self.assertTrue({"attack", "quaff", "cast", "pause"}.issubset({a.tool for a in actions}))
                observer.remember_look(normal.hero, swallowed, normal, source="look_here")
                self.assertEqual(observer.current.inspections, {})
                restored = observer.observation(normal)
                self.assertEqual(restored["map_context"], "dungeon")
                self.assertIn("travel:3,6", {a.id for a in catalogue(normal, observer, 8)})

    def test_vibrating_square_message_and_underfoot_details_are_retained(self):
        observer = Observer()
        initial = view(screen("You feel a strange vibration under your feet.", [" ---- ", " |.@.| ", " ---- "]))
        observer.observation(initial)
        state = observer.observation(view())
        landmark = state["known_levels"][0]["landmarks"][0]
        self.assertEqual(landmark["kind"], "vibrating square observation")
        self.assertEqual(landmark["position"], [3, 4])
        after = view(screen("There is a high altar to Tyr (lawful) here.", [" ---- ", " |.@.| ", " ---- "]))
        observer.remember_look(after.hero, initial, after, source="look_here")
        state = observer.observation(view())
        self.assertEqual(state["level"]["inspections"][0]["description"], after.msg)

    def test_overview_retains_ordered_repeated_lines_across_pages(self):
        observer, initial = Observer(), view()
        observer.observation(initial)
        action = next(a for a in catalogue(initial, observer, 8) if a.id == "overview")
        observer.begin(action, initial)
        first = view(screen("The Dungeons of Doom: levels 1 to 4", ["  Level 3:", " (1 of 2)"]))
        second = view(screen("The Gnomish Mines: levels 3 to 5", ["  Level 3:", " (2 of 2)"]))
        observer.observation(first)
        observer.observation(first)
        state = observer.observation(second)
        lines = state["dungeon_overview"]["lines"]
        self.assertEqual(lines.count("  Level 3:"), 2)
        self.assertLess(lines.index("The Dungeons of Doom: levels 1 to 4"),
                        lines.index("The Gnomish Mines: levels 3 to 5"))

    def test_ascension_result_is_retained_through_end_disclosure(self):
        observer = Observer()
        observer.observation(view(screen("You ascend to the status of Demigoddess...--More--")))
        state = observer.observation(view(screen("Do you want your possessions identified? [ynq] (n)"), (0, 50)))
        self.assertEqual(state["game_result"]["outcome"], "ascended")
        self.assertIn("Demigoddess", state["game_result"]["message"])

    def test_elemental_planes_and_quest_floors_do_not_share_terrain_memory(self):
        observer = Observer()
        labels = ("Home 1", "Home 2", "Earth", "Air", "Fire", "Water", "Astral Plane")
        for location in labels:
            state = observer.observation(view(screen("", [" ---- ", " |.@.| ", " ---- "],
                status2=location + " $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:400")))
            self.assertEqual(state["level"]["label"], location)
        self.assertEqual([lv.label for lv in observer.levels], list(labels))

    def test_unknown_underfoot_is_labelled_inferred_until_observed(self):
        observer = Observer()
        initial = view()
        state = observer.observation(initial)
        self.assertEqual(state["underfoot"]["basis"], "inferred floor placeholder")
        self.assertEqual(state["level"]["inferred_floor"], [[3, 4]])
        state = observer.observation(view(screen("There is a staircase down here.",
                                                 [" ---- ", " |.@.| ", " ---- "])))
        self.assertEqual(state["underfoot"]["remembered_terrain"], ">")
        self.assertEqual(state["underfoot"]["basis"], "observed map or underfoot message")
        self.assertEqual(state["level"]["inferred_floor"], [])

    def test_level_identity_remains_present_at_prompts(self):
        observer = Observer()
        prompt = view(screen("Really attack the peaceful gnome? [yn] (n)"), (0, 40))
        self.assertIsNone(observer.observation(prompt)["level"])
        observer.observation(view())
        self.assertEqual(observer.observation(prompt)["level"], {"id": "level-1", "label": "Dlvl:3"})

    def test_inspection_records_observed_description_and_turn(self):
        observer = Observer()
        before = view(screen("", [" ------- ", " |.@e..| ", " ------- "]))
        observer.observation(before)
        after = view(screen("e  a floating eye (floating eye)", [" ------- ", " |.@e..| ", " ------- "]))
        observer.remember_look((2, 4), before, after)
        state = observer.observation(after)
        monster = state["entities"][0]
        self.assertEqual(monster["position"], [3, 5])
        self.assertEqual(monster["last_inspection"]["description"], "floating eye")
        self.assertEqual(monster["last_inspection"]["observed_turn"], 400)
        self.assertEqual(monster["last_inspection"]["source"], "inspect")

    def test_unknown_identification_remains_an_observation(self):
        state = Observer().observation(view(screen("", [" ------- ", " |.@e..| ", " ------- "])))
        self.assertEqual(state["entities"][0]["glyph"], "e")
        self.assertEqual(state["hero"]["hp"], 12)

    def test_interrupted_inspection_does_not_record_prompt_as_description(self):
        observer = Observer()
        before = view(screen("", [" ------- ", " |.@e..| ", " ------- "]))
        observer.observation(before)
        after = view(screen("Pick a monster, object or location.--More--"), (0, 50))
        observer.remember_look((2, 4), before, after)
        self.assertEqual(observer.current.inspections, {})

    def test_old_inspection_survives_without_identifying_a_later_monster(self):
        observer = Observer()
        rows = [" ------- ", " |.@e..| ", " ------- "]
        before = view(screen("", rows))
        observer.observation(before)
        after = view(screen("e  a floating eye (floating eye)", rows))
        observer.remember_look((2, 4), before, after)
        later = view(screen("", rows, status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:401"))
        state = observer.observation(later)
        self.assertNotIn("last_inspection", state["entities"][0])
        self.assertEqual(state["level"]["inspections"][0]["description"], "floating eye")
        self.assertEqual(state["level"]["inspections"][0]["observed_turn"], 400)

    def test_landmarks_and_shop_evidence_survive_history_and_level_changes(self):
        observer = Observer()
        initial = view(screen('"Hello, Hero! Welcome to Izchak\'s lighting store!"',
                              [" -------- ", " |.@{_.>| ", " -------- "]))
        state = observer.observation(initial)
        kinds = {entry["kind"] for entry in state["known_levels"][0]["landmarks"]}
        self.assertEqual(kinds, {"shop entry", "fountain", "altar", "down stairs"})
        action = next(a for a in catalogue(initial, observer, 8) if a.id == "descend")
        observer.begin(action, initial)
        down = view(screen("", [" ---- ", " |.@.| ", " ---- "], status2="Dlvl:4 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:401"))
        state = observer.observation(down)
        self.assertEqual(state["known_levels"][1]["connections"], [])
        for index in range(30):
            observer.observation(view(screen("Message %d" % index, [" ---- ", " |.@.| ", " ---- "],
                status2="Dlvl:4 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:402")))
        action = next(a for a in catalogue(down, observer, 8) if a.id == "ascend")
        observer.begin(action, down)
        back = view(screen("", [" -------- ", " |.@._.>| ", " -------- "]))
        state = observer.observation(back)
        known = state["known_levels"][0]
        self.assertEqual(state["level"]["id"], known["id"])
        kinds = {entry["kind"] for entry in known["landmarks"]}
        self.assertNotIn("fountain", kinds)
        shop = next(entry for entry in known["landmarks"] if entry["kind"] == "shop entry")
        self.assertEqual(shop["observed_turn"], 400)
        self.assertEqual(shop["position"], [3, 4])
        self.assertEqual(known["connections"][0]["destination"], "level-2")
        self.assertEqual(Observer().levels, [])

    def test_caller_typed_stair_keys_return_to_the_remembered_level(self):
        observer = Observer()
        upper = view(screen("", [" -------- ", " |..{.@>| ", " -------- "]), (2, 6))
        observer.observation(upper)
        observer.begin(Action("manual", "Caller-supplied keys", "manual", ">"), upper)
        lower = view(screen("", [" ---- ", " |.@<| ", " ---- "], status2="Dlvl:4 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:401"))
        observer.observation(lower)
        observer.begin(Action("manual", "Caller-supplied keys", "manual", "<"), lower)
        state = observer.observation(view(screen("", [" -------- ", " |..{.@>| ", " -------- "],
                                                 status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:402"), (2, 6)))
        self.assertEqual(state["level"]["id"], "level-1")
        self.assertEqual([level["id"] for level in state["known_levels"]], ["level-1", "level-2"])

    def test_query_completeness_and_age_do_not_claim_partial_inventory_is_complete(self):
        observer, initial = Observer(), view()
        observer.observation(initial)
        action = next(a for a in catalogue(initial, observer, 8) if a.id == "inventory")
        observer.begin(action, initial)
        page1 = view(screen("Inventory", [" a - a dagger", " (1 of 2)"]))
        page2 = view(screen("Inventory", [" b - an apple", " (2 of 2)"]))
        state = observer.observation(page1)
        self.assertFalse(state["inventory"]["complete"])
        state = observer.observation(page2)
        self.assertTrue(state["inventory"]["complete"])
        self.assertEqual(state["inventory"]["items"], {"a": "a dagger", "b": "an apple"})
        later = view(screen("", [" ---- ", " |.@.| ", " ---- "], status2="Dlvl:3 $:0 HP:12(16) Pw:2(2) AC:6 Xp:2 T:420"))
        state = observer.observation(later)
        self.assertEqual(state["inventory"]["age_turns"], 20)
        observer.begin(action, later)
        observer.observation(page1)
        state = observer.observation(later)
        self.assertFalse(state["inventory"]["complete"])
        observer.begin(action, later)
        state = observer.observation(view(screen("You are not carrying anything.", [" ---- ", " |.@.| ", " ---- "])))
        self.assertTrue(state["inventory"]["complete"])
        self.assertEqual(state["inventory"]["items"], {})

    def test_recorded_screens_produce_serializable_observations(self):
        directory = Path(__file__).parent / "fixtures" / "screens"
        files = list(directory.glob('*.json'))
        self.assertGreater(len(files), 20)
        for path in files:
            with self.subTest(screen=path.name):
                v = View(**json.loads(path.read_text()))
                state = Observer().observation(v)
                json.dumps(state, allow_nan=False)
                self.assertEqual(state["hero"].get("hp"), v.st.get("hp"))
                self.assertEqual(len(state["fingerprint"]), 64)

    def test_fingerprint_changes_with_colour(self):
        a, b = view(), view()
        b.fgs[2][3] = "red"
        self.assertNotEqual(fingerprint(a), fingerprint(b))

    def test_menu_selectors_and_selection_state(self):
        v = view(screen("Choose", [" : - Look inside", " 1 - First option", " a # 5 arrows",
                                   " b * a dagger", " c + a wand", " (end)"]))
        entries = parse_menu_entries(v)
        self.assertEqual([entry["key"] for entry in entries], [":", "1", "a", "b", "c"])
        self.assertEqual([entry["selection"] for entry in entries], ["none", "none", "partial", "all", "all"])
        self.assertEqual(entries[2]["text"], "5 arrows")
        self.assertEqual(Observer().observation(v)["menu_entries"], entries)

    def test_side_inventory_window_ignores_map_left_of_entries(self):
        observer, initial = Observer(), view()
        observer.observation(initial)
        action = next(a for a in catalogue(initial, observer, 8) if a.id == "inventory")
        observer.begin(action, initial)
        menu = [("".ljust(24) + "Armor"), ("".ljust(24) + "a - a pair of gloves"),
                ("    ------".ljust(24) + "b - a robe"), ("    |.@..|".ljust(24) + "Comestibles"),
                ("    |....|".ljust(24) + "e - 3 food rations"),
                (" z - map fragment".ljust(24) + "f - 6 apples"), ("".ljust(24) + "(end)")]
        state = observer.observation(view(screen(menu[0], menu[1:])))
        self.assertTrue(state["inventory"]["complete"])
        self.assertEqual(state["inventory"]["items"],
                         {"a": "a pair of gloves", "b": "a robe", "e": "3 food rations", "f": "6 apples"})
        self.assertEqual([entry["key"] for entry in state["menu_entries"]], ["a", "b", "e", "f"])

    def test_map_below_menu_footer_is_not_parsed_as_choices(self):
        v = view(screen("Tip: Farlooking or selecting a map location",
                        ["Help text", " (end)", " # # corridor", " a - map artifact"]))
        self.assertEqual(parse_menu_entries(v), [])

    def test_spell_memory_does_not_include_menu_operations(self):
        observer = Observer()
        initial = view()
        observer.observation(initial)
        action = next(a for a in catalogue(initial, observer, 8) if a.id == "spells")
        observer.begin(action, initial)
        menu = view(screen("Currently known spells", [" a - force bolt", " + - [sort spells]", " (end)"]))
        state = observer.observation(menu)
        self.assertEqual(state["spells"]["items"], {"a": "force bolt"})
        self.assertEqual([e["key"] for e in state["menu_entries"]], ["a", "+"])

    def test_targeting_cursor_does_not_update_map_or_visits(self):
        observer, context = Observer(), PromptContext()
        observer.observation(context.observe(view()))
        terrain = dict(observer.current.terrain)
        visits = dict(observer.current.visits)
        target = context.observe(view(screen("Move cursor to the desired position:")))
        observer.observation(target)
        context.before_input(target, "l")
        target = context.observe(view(screen("floor of a room [03,05]"), (2, 4)))
        state = observer.observation(target)
        self.assertEqual(state["phase"], "position")
        self.assertIsNone(state["hero"]["position"])
        self.assertEqual(observer.current.terrain, terrain)
        self.assertEqual(observer.current.visits, visits)
