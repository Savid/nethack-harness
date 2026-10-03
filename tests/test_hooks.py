import json
import os
import tempfile
import unittest

from helpers import Endpoint, FakeGame, facts, nh, screen, view  # noqa: F401


class HookTest(unittest.TestCase):
    def write(self, text, suffix):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    def test_declarative_rules_gates_and_cooldown(self):
        path = self.write(json.dumps({"questions": [
            {"key": "shop", "question": {"type": "noul", "instructions": "Is a shopkeeper visible?"},
             "escalate_when": {"noul_gte": 0.8}, "cooldown": 5},
            {"key": "level", "question": {"type": "choice", "instructions": "Kind of level?",
                                          "criteria": {"normal": "ordinary", "special": "special"}},
             "escalate_when": {"choice_in": ["special"], "min_confidence": 0.6}, "when": {"new_level": True}}]}),
            ".json")
        hooks = nh.Hooks()
        self.assertEqual(hooks.load_questions(path), ["shop", "level"])
        self.assertEqual(set(hooks.due(facts(1))), {"shop"})
        self.assertEqual(set(hooks.due(facts(2, new_level=True))), {"shop", "level"})
        reason, keys = hooks.judge(facts(2), {"shop": {"noul": 0.9}})
        self.assertTrue(reason.startswith("hook:shop"))
        self.assertNotIn("shop", hooks.due(facts(4)))          # cooling down
        self.assertIn("shop", hooks.due(facts(8)))
        reason, _ = hooks.judge(facts(9), {"level": {"choice": "special", "confidence": 0.7}})
        self.assertTrue(reason.startswith("hook:level"))
        self.assertEqual(hooks.judge(facts(10), {"level": {"choice": "special", "confidence": 0.5}}), (None, None))
        hooks.disabled.add("shop")
        self.assertNotIn("shop", hooks.due(facts(30)))

    def test_bad_question_is_rejected(self):
        path = self.write(json.dumps([{"key": "x", "question": {"type": "choice"}, "escalate_when": {"noul_gte": 1}}]),
                          ".json")
        with self.assertRaises(nh.HookError):
            nh.Hooks().load_questions(path)

    def test_plugin_questions_actions_escalations_and_errors(self):
        path = self.write(
            "API = 1\n"
            "def extra_questions(facts):\n"
            "    return {'deep': {'type': 'noul', 'instructions': 'Is this level deep?'}} if facts['dlvl'] > 1 else {}\n"
            "def on_answers(facts, answers):\n"
            "    if answers.get('deep', {}).get('noul', 0) > 0.5:\n"
            "        return {'escalate': 'deep level'}\n"
            "    if facts['hp'] == 1:\n"
            "        return {'action': 's'}\n"
            "    if facts['hp'] == 0:\n"
            "        raise ValueError('boom')\n", ".py")
        hooks = nh.Hooks()
        name = hooks.load_plugin(path)
        due = hooks.due(facts())
        self.assertEqual(list(due), [name + ".deep"])
        reason, _ = hooks.judge(facts(), {name + ".deep": {"noul": 0.9}})
        self.assertEqual(reason, "hook:%s deep level" % name)
        self.assertEqual(hooks.judge(dict(facts(), hp=1), {}), (None, "s"))
        with self.assertRaises(nh.HookError):
            hooks.judge(dict(facts(), hp=0), {})


if __name__ == "__main__":
    unittest.main()
