"""Play by hand while the loop is paused: guarded key batches."""
import re
import time

from .knowledge import ALARM as K_ALARM, MON


def monsters_near(v, reach=7):
    """Non-pet monster glyphs within reach of the hero."""
    hero = v.hero
    if not hero:
        return 0
    return sum(1 for r in range(max(1, hero[0] - reach), min(22, hero[0] + reach + 1))
               for col in range(max(0, hero[1] - reach), min(80, hero[1] + reach + 1))
               if (r, col) != hero and v.rows[r][col] in MON and not v.pet(r, col))


def dismiss_more(term, note):
    """Dismiss --More-- prompts, recording their text; returns it."""
    for _ in range(5):
        w = term.view()
        if not w.more:
            break
        note.append(w.msg.replace("--More--", "").strip()[:160])
        term.send(b" ")
    return note


def guarded_repeat(term, p, keys, c, beat=None):
    """Send KEYS up to c["times"] times while paused; stop at the first sign of trouble. Returns the reply.
    Ordinary --More-- prompts are dismissed and read; alarming ones stop the batch."""
    stop_on = re.compile(c["stop_on"]) if c.get("stop_on") else None
    term.poll()
    seen = dismiss_more(term, [])
    v = term.view()
    if v.prompt:
        return "repeat: nothing sent: a prompt is open (%s); answer it first\n%s\n" % (v.msg[:120], v.text_screen())
    done, why, started = 0, "done", time.time()
    for _ in range(c["times"]):
        if beat:
            beat()                       # a long batch must not look like a stuck loop
        if time.time() - started > 45:
            why = "time limit (45 s) reached"
            break
        before = term.view()
        hp0, mon0, dl0 = before.st.get("hp"), monsters_near(before), before.st.get("dlvl")
        p.record(keys, "hand")
        term.send(keys)
        done += 1
        msgs = [term.view().msg.replace("--More--", "").strip()]
        alarm = next((m for m in msgs if K_ALARM.search(m)), None)
        if not alarm:
            msgs = dismiss_more(term, msgs)
            alarm = next((m for m in msgs if K_ALARM.search(m)), None)
        text = "  ".join(m for m in msgs if m)
        seen += [m for m in msgs if m]
        v = term.view()
        for m in msgs:
            if m:
                p.message(m, v)          # the loop learns from what happened (locked doors, kills, prayers)
        hp, hpmax = v.st.get("hp"), max(1, v.st.get("hpmax") or 1)
        falling = hp is not None and hp0 is not None and hp < hp0
        if v.dead:
            why = "the hero died"
        elif v.asking and not v.prompt and v.msg:
            term.send(b"\x1b")          # an open question the keys left behind (a Count:, a text prompt)
            why = "a prompt was left open and escaped: %s" % v.msg[:80]
        elif (v.st.get("turn"), v.rows) == (before.st.get("turn"), before.rows):
            why = "nothing changed (the game refused or ignored the keys)"
        elif alarm:
            why = "alarming message: %s" % alarm[:120]
        elif v.more or v.prompt:
            why = "a prompt is open: %s" % v.msg[:120]
        elif falling and not c.get("allow_loss"):
            why = "HP fell %d -> %d" % (hp0, hp)
        elif falling and hp / hpmax < c["stop_hp"]:
            why = "HP %d/%d is below %.2f and falling" % (hp, hpmax, c["stop_hp"])
        elif monsters_near(v) > mon0:
            why = "a new monster came into view"
        elif v.st.get("dlvl") != dl0:
            why = "the level changed"
        elif re.search(r"You attack thin air|You kill|is killed|You destroy|There is nothing here", text):
            why = "the target is gone (%s)" % text[:80]
        elif stop_on and stop_on.search(text):
            why = "message matched: %s" % text[:120]
        else:
            continue
        break
    p.note("manual", "repeat %r x%d (%s)" % (keys[:40], done, why))
    return "repeat: sent %d of %d (%s)\nmessages: %s\n%s\n" % (
        done, c["times"], why, " / ".join(seen[-6:]) or "-", term.view().text_screen())
