"""Answer some escalations without pausing (load with --plugin examples/auto_answers.py).

on_escalation(facts, esc) sees every escalation the outer loop may automate (safety codes such as low_hp
always pause). Return None to pause as usual, {"continue": True} to play on, or {"plan": [...]} to queue
plan items and play on.
"""
API = 1


def on_escalation(facts, esc):
    text = esc["text"]
    if esc["code"] == "alarm" and "lycanthropy" in text and "prayer: safe" in text:
        return {"plan": ["goal:pray"]}          # a safe prayer cures lycanthropy
    if esc["code"] == "branch_point" and "trap door" in text:
        return {"continue": True}               # let the loop use the free descent
    return None
