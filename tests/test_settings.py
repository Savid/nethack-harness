from unittest import TestCase
from nethack_harness.settings import Settings


class SettingsTest(TestCase):
    def test_execution_limits_are_validated(self):
        for values in ({"quiet": float("nan")}, {"decision_timeout": 0}, {"max_action_steps": 65},
                       {"max_action_steps": 0}, {"max_action_steps": True}, {"objective": " "},
                       {"review_after_calls": -1}, {"review_after_calls": True}, {"review_after_calls": 1.5},
                       {"max_action_attempts": -1}, {"max_action_attempts": True}, {"max_action_attempts": 1.5},
                       {"caller_context": []}, {"caller_context": {"unknown": float("nan")}}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Settings(**values)
        settings = Settings(objective="Explore", max_action_steps=3)
        self.assertEqual(settings.as_dict()["max_action_steps"], 3)
