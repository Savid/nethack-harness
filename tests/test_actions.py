from unittest import TestCase
from helpers import screen, view
from nethack_harness.actions import catalogue
from nethack_harness.perceive import Observer


class ActionCatalogueTest(TestCase):
    def test_coloured_structures_do_not_become_corridor_routes(self):
        for colour in ("green", "cyan"):
            with self.subTest(colour=colour):
                observer = Observer()
                initial = view(screen("", ["", "   @#>"]), (2, 3))
                initial.fgs[2][4] = colour
                state = observer.observation(initial)
                self.assertEqual(state["level"]["structural_obstacles"], [
                    {"position": [3, 5], "glyph": "#", "colour": colour, "source": "remembered terminal terrain"}])
                choices = {a.id for a in catalogue(initial, observer, 8)}
                self.assertNotIn("travel:3,6", choices)
                self.assertIn("move:l", choices)
                obscured = view(screen("", ["", "   @ >"]), (2, 3))
                observer.observation(obscured)
                self.assertNotIn("travel:3,6", {a.id for a in catalogue(obscured, observer, 8)})
                corridor = view(screen("", ["", "   @#>"]), (2, 3))
                observer.observation(corridor)
                self.assertEqual(observer.current.structural_obstacles(), [])
                route = next(a for a in catalogue(corridor, observer, 8) if a.id == "travel:3,6")
                self.assertEqual(route.route, ((2, 4), (2, 5)))

    def test_occupied_corridor_explains_unavailable_destinations_until_it_clears(self):
        observer = Observer()
        first = view(screen("", ["", " >#####@"]), (2, 7))
        observer.observation(first)
        blocked = view(screen("", ["", " >####f@"]), (2, 7))
        observation = observer.observation(blocked)
        self.assertEqual(observation["navigation_obstacles"]["unavailable_destinations"], [
            {"position": [3, 2], "description": "down stairs", "terrain_path_length": 6,
             "occupied_on_terrain_path": [[3, 7]]}])
        self.assertNotIn("travel:3,2", {a.id for a in catalogue(blocked, observer, 8)})
        clear = observer.observation(first)
        self.assertEqual(clear["navigation_obstacles"]["unavailable_destinations"], [])
        self.assertIn("travel:3,2", {a.id for a in catalogue(first, observer, 8)})

    def test_occupied_square_does_not_hide_a_destination_with_an_alternative_route(self):
        observer = Observer()
        observer.observation(view(screen("", [" ------- ", " |@.>..| ", " |.....| ", " ------- "]), (2, 2)))
        occupied = view(screen("", [" ------- ", " |@f>..| ", " |.....| ", " ------- "]), (2, 2))
        observation = observer.observation(occupied)
        blocked = observation["navigation_obstacles"]["unavailable_destinations"]
        self.assertNotIn([3, 5], [entry["position"] for entry in blocked])
        travel = next(action for action in catalogue(occupied, observer, 8) if action.id == "travel:3,5")
        self.assertEqual(travel.route[-1], (2, 4))
        self.assertNotIn((2, 3), travel.route)

    def choices(self, v):
        observer = Observer()
        observer.observation(v)
        return {a.id: a for a in catalogue(v, observer, 8)}

    def test_inspection_includes_terrain_features_and_the_hero_square(self):
        choices = self.choices(view(screen("", [" -------- ", " |.@_^.>| ", " -------- "])))
        for name in ("inspect:3,4", "inspect:3,5", "inspect:3,6", "inspect:3,8"):
            self.assertIn(name, choices)
            self.assertEqual(choices[name].followups[0][0], "position")
        self.assertIn("altar", choices["inspect:3,5"].description)
        self.assertIn("trap", choices["inspect:3,6"].description)

    def test_targets_are_explicit_even_at_low_health(self):
        v = view(screen("", [" ------- ", " |.@e..| ", " ------- "],
                        status2="Dlvl:8 $:0 HP:1(16) Pw:2(2) AC:6 Xp:1 T:400"))
        choices = self.choices(v)
        self.assertEqual(choices["attack:l"].target, (2, 4))
        self.assertEqual(choices["attack:l"].steps, 1)
        self.assertIn("descend", choices)
        self.assertIn("pray", choices)
        self.assertEqual(choices["wait:8"].steps, 8)

    def test_travel_routes_have_named_targets_and_step_bounds(self):
        v = view(screen("", [" ----------- ", " |.@......>| ", " ----------- "]))
        choices = self.choices(v)
        travel = choices["travel:3,11"]
        self.assertEqual(travel.target, (2, 10))
        self.assertEqual(travel.route[-1], travel.target)
        self.assertLessEqual(travel.steps, 8)

    def test_travel_reaches_door_approach_and_retains_final_step(self):
        rows = [" --------- ", " |.@.....| ", " |....*..| ", " ----+---- "]
        choices = self.choices(view(screen("", rows)))
        target = choices["travel:4,6"]
        self.assertEqual(target.target, (3, 5))
        self.assertIn("closed door at [5, 6]", target.description)
        rows[1] = " |.......| "
        rows[2] = " |..@....| "
        near = self.choices(view(screen("", rows), (3, 4)))
        self.assertEqual(near["travel:4,6"].route, ((3, 5),))
        self.assertEqual(near["travel:4,6"].steps, 1)

    def test_prompt_choices_preserve_game_answers(self):
        v = view(screen("Really attack the peaceful gnome? [yn] (n)"), (0, 40))
        choices = self.choices(v)
        self.assertEqual(choices["input:79"].keys, "y")
        self.assertEqual(choices["input:6e"].keys, "n")

    def test_exploration_names_a_corridor_frontier_and_bound(self):
        choices = self.choices(view(screen("", ["", "   @##"])))
        action = choices["explore:3,6"]
        self.assertEqual((action.kind, action.target, action.steps), ("explore", (2, 5), 8))
        self.assertEqual(action.route, ((2, 4), (2, 5)))
        self.assertFalse(any(k.startswith("explore:") for k in self.choices(view())))

    def test_mapped_corridors_offer_endpoints_and_junctions(self):
        rows = ["", "   @############", "         #", "         #"]
        choices = self.choices(view(screen("", rows)))
        targets = {a.target for a in choices.values() if a.kind == "travel"}
        self.assertEqual(targets, {(2, 9), (2, 15), (4, 9)})
        self.assertIn("junction", choices["travel:3,10"].description)
        self.assertIn("explore:3,16", choices)

    def test_item_ranges_and_text_entry(self):
        v = view(screen("What do you want to eat? [a-c or ?*]"), (0, 40))
        choices = self.choices(v)
        for key in "abc?*":
            self.assertIn("input:%02x" % ord(key), choices)
        v = view(screen("What do you want to write in the dust?"), (0, 40))
        choices = self.choices(v)
        self.assertEqual(choices["input:45"].keys, "E")
        self.assertEqual(choices["input:0d"].keys, "\r")

    def test_game_over_has_no_actions(self):
        self.assertEqual(self.choices(view(screen("Do you want your possessions identified? [ynq] (n)"), (0, 10))), {})

    def test_directional_commands_wait_for_the_requested_prompt(self):
        choices = self.choices(view())
        for kind, launcher in (("open", "o"), ("close", "c"), ("kick", "\x04")):
            for direction in "ykuhlbjn":
                action = choices[kind + ":" + direction]
                self.assertEqual(action.keys, launcher)
                self.assertEqual(action.followups, (("direction", direction),))
        self.assertEqual(choices["open:."].followups, (("direction", "."),))
        self.assertEqual(choices["move:no_pickup:h"].keys, "mh")

    def test_command_modifiers_have_distinct_mechanics(self):
        choices = self.choices(view())
        for name, keys in (("quaff:inventory", "mq"), ("search:force:8", "ms"),
                           ("loot:saddle", "m#loot\r"), ("descend:no_pickup", "m>")):
            self.assertEqual(choices[name].keys, keys)
        self.assertEqual(choices["search:force:8"].steps, 8)
        self.assertIn("safe-wait", choices["search:force:8"].description)
        self.assertIn("saddle", choices["loot:saddle"].description)

    def test_quantities_and_visible_menu_selectors_take_precedence(self):
        choices = self.choices(view(screen("What do you want to drop? [a-c or ?*]"), (0, 40)))
        self.assertEqual(choices["input:32"].keys, "2")
        rows = [": - look inside", "1 - special option", "a # 5 arrows", "(end)"]
        choices = self.choices(view(screen("Choose objects", rows), (4, 5)))
        self.assertIn("look inside", choices["input:3a"].description)
        self.assertIn("special option", choices["input:31"].description)
        self.assertIn("partial", choices["input:61"].description)
        self.assertIn("quantity", choices["input:32"].description)
        self.assertIn("Select all", choices["input:2e"].description)

    def test_spell_choices_use_observed_spell_labels(self):
        observer = Observer()
        observer.spells = {"a": "force bolt", "b": "healing"}
        v = view(screen("Cast which spell? [a-b *?]"), (0, 29))
        choices = {a.id: a for a in catalogue(v, observer, 8)}
        self.assertEqual(choices["input:61"].description, "force bolt")
        self.assertEqual(choices["input:62"].description, "healing")
