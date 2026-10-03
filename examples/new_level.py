"""Example plugin: pause once on every new dungeon level that looks special, and rest by
searching when HP is low and nothing is in view. Load with --plugin examples/new_level.py."""
API = 1


def extra_questions(facts):
    if facts["new_level"]:
        return {"special": {"type": "noul", "instructions": "Does this dungeon level look unusual or special "
                                                            "(a shop, an altar, a big room, a maze)?"}}
    return {}


def on_answers(facts, answers):
    if answers.get("special", {}).get("noul", 0) >= 0.7:
        return {"escalate": "new level looks special (Dlvl %d)" % facts["dlvl"]}
    if facts["hp_percent"] < 30 and not facts["hostiles"]:
        return {"action": "20s"}
    return None


def on_resume(facts, orders):
    pass
