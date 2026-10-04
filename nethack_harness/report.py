"""What the outer loop reads: escalation reports, the startup briefing and status."""
import json
import re
import time

from . import knowledge as K
from .escalation import classify
from .level import compass, pos1
from .settings import CFG, describe, effort, val

RESUME_HINT = ("resume [--directive TEXT] [--mode descend|explore|careful] [--set k=v] [--plan ITEM] "
               "[--questions F] [--plugin F] [--enable/--disable KEY] --timeout S   (help: capability map)")


def capabilities(p):
    kit = p.kit()
    caps = ["explore (travel, corridor runs)", "stairs and trap doors (travel+descend in one send)",
            "stairs probe via the travel prompt" + ("" if CFG["probe"] else " [off]"),
            "fight by rules (never-melee and threat tables)", "rest and search with count prefixes",
            "pray when the game's trouble rule holds and the timeout is safe",
            "quaff healing potions" + ("" if CFG["potions"] and kit["healing"] else " [none known]" if CFG["potions"]
                                       else " [off]"),
            "Elbereth" + ("" if CFG["elbereth"] else " [off]"),
            "dig down: --set dig=1" + (" [ON]" if CFG["dig"] else "") + ("" if kit["dig"] else " [no digging tool]")]
    for name, (letter, level, fail) in sorted(p.spells.items(), key=lambda kv: kv[1][0]):
        use = K.SPELLS.get(name, (None, None))[1]
        caps.append("spell %s (Z%s, %d%% fail)%s" % (name, letter, fail, "" if not use else
                                                      " cast %s" % ("at foes in a line" if use == "attack"
                                                                    else "on yourself when hurt") +
                                                      ("" if CFG["spells"] else " [off]")))
    return caps


def depth_line(p, st):
    xl, hpmax, ac = st.get("xl", 1), st.get("hpmax", 1), st.get("ac", 10)
    cap, _, how = p.depth_limits(xl, hpmax, ac)
    return "%s start: the loop descends to Dlvl %d at most for now (%s); cap_lift never lifts the pace for a " \
        "fragile hero before XL 3" % ("fragile" if p.fragile(hpmax, ac, xl) else "sturdy", cap, how)


def suggestions(p):
    kit, out = p.kit(), []
    st = p.term.view().st
    out.append("--set mines=%s (race %s; auto picks %s)" % (p.mines_policy(), p.race, p.mines_policy()))
    fragile = p.fragile(st.get("hpmax", 1), st.get("ac", 10), st.get("xl", 1))
    weak = p.role in K.WEAK_ROLES
    why = ", ".join(x for x in ("few HP or poor AC for the level" if fragile else "",
                                "a weak melee role" if weak else "") if x) or "sturdy start"
    out.append("--set risk=%s (%s); effort=medium is the default, effort=low saves decision calls" % (
        "low" if fragile or weak else "high" if p.role in K.STRONG_ROLES else "normal", why))
    for rx, advice in K.KIT_ADVICE:
        hit = p.items(rx)
        if hit:
            out.append("%s (%s): %s" % (hit[0][1][:40], hit[0][0], advice))
    wielded = next((t for k, t in p.items() if "wielded" in K.item_state(t)), "")
    if kit["dig"] and ("bullwhip" in wielded or not wielded):
        letter = p.items("|".join(K.DIG_TOOLS))[0][0]
        out.append("fight with the digging tool rather than %s: --plan 'keys:w%s'" % (wielded or "bare hands", letter))
    if kit["dig"] and not CFG["dig"]:
        out.append("you carry a digging tool: --set dig=1 descends by digging (fast, skips levels' contents; you "
                   "land mid-level with no up stairs nearby, and the depth cap still applies)")
    if not kit["food"]:
        out.append("no food in the pack: prayer fixes Weak once every ~900 turns; fresh corpses are eaten")
    return out


def briefing(p):
    v = p.term.view()
    kit = p.kit()
    lines = ["BRIEFING: you are a %s %s %s (title %s), Dlvl %s, HP %s/%s, AC %s, T %s." % (
        p.align or "?", p.race or "?", p.role or "?", v.title, v.st.get("dlvl"), v.st.get("hp"), v.st.get("hpmax"),
        v.st.get("ac"), v.st.get("turn"))]
    for k in ("food", "healing", "dig", "mapping", "wands", "ranged", "spellbooks"):
        if kit[k]:
            lines.append("kit %s: %s" % (k, "; ".join(kit[k])[:240]))
    lines.append("capabilities: " + " | ".join(capabilities(p)))
    lines.append("settings: " + describe() + " | effective: descend_hp=%.2f rest_hp=%.2f hp_escalate=%.2f lead=%d" % (
        val("descend_hp"), val("rest_hp"), val("hp_escalate"), p.lead()))
    lines.append("depth: " + depth_line(p, v.st))
    lines.append("hooks: " + hooks_line(p))
    lines.append("suggested: " + " ; ".join(suggestions(p)))
    lines.append("Set a plan now with resume (or just resume to play with these defaults).")
    return "\n".join(lines)


def hooks_line(p):
    h = p.hooks
    names = ["%s%s" % (k, " [off]" if k in h.disabled else "") for k in list(h.questions) + list(h.plugins)]
    return ", ".join(names) or "none"


def summary(p, reason):
    """The escalation report: compact by default (report=compact), or with everything (report=full)."""
    if reason != "briefing" and CFG["report"] == "compact":
        return compact(p, reason)
    return full(p, reason)


def crop(v, rows=11, cols=21):
    """The map around the hero, with its top-left corner as 1-based ROW,COL."""
    hr, hc = v.hero or (11, 40)
    top = min(max(1, hr - rows // 2), 22 - rows)
    left = min(max(0, hc - cols // 2), 80 - cols)
    lines = [v.rows[r][left:left + cols].rstrip() for r in range(top, top + rows)]
    while lines and not lines[0].strip():
        lines.pop(0)
        top += 1
    while lines and not lines[-1].strip():
        lines.pop()
    return top, left, lines


def compact(p, reason):
    v = p.term.view()
    st = v.st if v.st.get("dlvl") is not None else p.last_st
    out = ["ESCALATION [%s]: %s" % (classify(reason), reason),
           "Dlvl %s HP %s/%s AC %s XL %s T %s%s | prayer: %s%s" % (
               st.get("dlvl"), st.get("hp"), st.get("hpmax"), st.get("ac"), st.get("xl"), st.get("turn"),
               (" " + " ".join(v.cond)) if v.cond else "", p.prayer_band(st.get("turn") or 0),
               " | time left %s" % time_left_text(p) if p.time_budget else "")]
    near = sorted(p.hostiles + p.obst, key=lambda h: h["dist"])[:5]
    if near:
        out.append("near: " + "; ".join("%s%s %d %s at %s" % (
            h["name"], " [never melee]" if h in p.obst else "", h["dist"], compass(v.hero or h["pos"], h["pos"]),
            pos1(h["pos"])) for h in near))
    msgs = [m for _, m in list(p.msg_log)[-4:]]
    if msgs:
        out.append("messages: " + " | ".join(m[:100] for m in msgs))
    if p.pending:
        acts, i = p.pending
        out.append("model: %s (danger %.2f)" % (", ".join("%s %.2f" % kv for kv in i["top"][:3]), i["danger"]))
    top, left, lines = crop(v)
    if lines:
        out.append("map from %d,%d (@ = you):" % (top + 1, left + 1))
        out += lines
    lv = p.lv.get(st.get("dlvl") if p.branch == "main" else (p.branch, st.get("dlvl")))
    food = p.food_letters()
    extra = ["food: %s" % (", ".join(t for _, t in food[:2]) or ("none" if p.inv_complete else "unknown"))]
    if lv and lv.downs:
        extra.append("stairs down: " + ", ".join(pos1(q) + ("" if k == "main" else " (Mines)")
                                                 for q, k in lv.downs.items()))
    if p.plan:
        extra.append("plan: " + " | ".join(p.plan))
    out.append(" | ".join(extra))
    out.append("next: resume [--plan ITEM] [--set k=v] [--directive TEXT]; screen for the full screen; "
               "help brief")
    return "\n".join(out)


def full(p, reason):
    v = p.term.view()
    if reason == "briefing":
        return briefing(p) + "\n" + footer(p) + "\n--- screen ---\n" + v.text_screen()
    st, lp = (v.st if v.st.get("dlvl") is not None else p.last_st), p.last_prayer   # game over: last known
    out = ["ESCALATION: " + reason,
           "Dlvl %s HP %s/%s AC %s XL %s T %s %s | %s %s | last prayer %s%s | mode %s risk %s effort %s | orders: %s" % (
               st.get("dlvl"), st.get("hp"), st.get("hpmax"), st.get("ac"), st.get("xl"), st.get("turn"),
               " ".join(v.cond), p.race or "", p.role or "", "never" if lp is None else "T%d (%d ago)" % (
                   lp, (st.get("turn") or 0) - lp), " (prayer broken)" if p.prayer_broken else "", CFG["mode"],
               CFG["risk"], CFG["effort"], clip(p.directive) or "-") +
           (" | time left %s" % time_left_text(p) if p.time_budget else "")]
    if p.pending:
        acts, i = p.pending
        out.append("model: %s | danger %.2f | confidence %.2f" % (", ".join("%s %.2f" % kv for kv in i["top"]),
                                                                 i["danger"], i.get("conf", 0)))
        out.append("options (rule order): " + " | ".join("%s: %s" % (a.key, a.desc) for a in acts[:8]))
    if reason.startswith("hook:") and p.hook_answers:
        out.append("hook answers: " + "; ".join("%s %s" % (k, json.dumps(x)) for k, x in p.hook_answers.items()))
    for label, group in (("hostiles", p.hostiles), ("never melee", p.obst)):
        if group:
            out.append(label + ": " + "; ".join("%s (%s) %d %s" % (
                h["name"], h["ch"], h["dist"], compass(v.hero or h["pos"], h["pos"])) for h in group[:6]))
    branch = p.branch
    lv = p.lv.get(st.get("dlvl") if branch == "main" else (branch, st.get("dlvl")))
    if lv:
        out.append("level: %d search turns, probes %s, %d bans, %d excluded targets%s%s" % (
            lv.search_turns, ",".join(sorted(lv.probed)) or "-", len(lv.bans), len(lv.excluded),
            ", shop" if lv.shop else "", ", Mines" if lv.mines else ""))
    if p.plan:
        out.append("plan queue: " + " | ".join(p.plan))
    food = p.food_letters()
    out.append("food: %s | prayer: %s | model since start: %d disagreements, %d overrides by orders | stairs known: %s" % (
        ", ".join(t for _, t in food[:3]) or ("none in pack" if p.inv_complete else "unknown (check with send i)"),
        p.prayer_band(st.get("turn") or 0),
        p.disagreements, p.overrides,
        ", ".join("%s%s" % (pos1(pos), "" if kind == "main" else " " + kind)
                  for pos, kind in (lv.downs.items() if lv else []))
        or "none"))
    if lv and lv.excluded:
        out.append("unreachable targets: " + ", ".join(pos1(t) for t, until in lv.excluded.items()
                                                       if until > p.decisions))
    recent = []
    for h in list(p.hist)[-30:]:
        if h["kind"] not in ("act", "msg", "level", "prompt", "plan", "manual"):
            continue
        text = h["text"][:80]
        if recent and recent[-1][0] == text:
            recent[-1][1] += 1
        else:
            recent.append([text, 1])
    out.append("recent: " + " / ".join(t + (" x%d" % n if n > 1 else "") for t, n in recent[-10:]))
    out.append(footer(p))
    # Repeat the gist last, so a reader that keeps only the tail still has it.
    return "\n".join(out + ["--- screen ---", v.text_screen(),
                             "REASON [%s]: %s | %s" % (classify(reason), reason, out[1])])


def time_left_text(p):
    left = p.seconds_left()
    if left is not None:
        return "%d s" % left
    if p.time_budget:
        return "unknown (set in another process; resume with --set time_left=SECONDS)"
    return "not set"


def clip(text, n=300):
    """Long orders are the outer loop's own text: echo a prefix, not all of it, in every report."""
    text = text or ""
    return text if len(text) <= n else "%s... [%d chars]" % (text[:n], len(text))


def footer(p):
    breaker = max(0, int(p.breaker_until - time.time()))
    return ("inner loop: %d keys, %d decisions, %d model calls (%d reused, avg %d ms, effort %s%s), %d escalations, "
            "%.0fs | next: %s" % (p.keys, p.decisions, p.calls, p.reused, 1000 * p.mtime / max(1, p.calls),
                                  CFG["effort"], ", model paused %ds after errors" % breaker if breaker else "",
                                  p.escs, time.time() - p.t0, RESUME_HINT))


def status(p):
    return {"settings": dict(CFG), "effort": dict(effort(), level=CFG["effort"]), "hooks": hooks_line(p),
            "role": p.role, "race": p.race, "alignment": p.align, "max_dlvl": p.max_dl, "keys": p.keys,
            "decisions": p.decisions, "model_calls": p.calls, "reused_answers": p.reused,
            "plan": list(p.plan), "orders": clip(p.directive, 2000), "prayer": {"last": p.last_prayer, "broken": p.prayer_broken},
            "kit": p.kit() if p.inv else {},
            "depth_cap": p.depth_cap(p.term.view().st.get("xl", 1), p.term.view().st.get("hpmax", 1),
                                     p.term.view().st.get("ac", 10)) if p.term else None,
            "milestones": list(p.milestones), "tiebreak_seed": p.tiebreak,
            "time_left": time_left_text(p),
            "model_errors": p.breaker_trips, "last_model_error": p.last_model_error,
            "recent_ms": [int(x * 1000) for x in p.latencies]}


KILLER = re.compile(r"killed by (?:an? |the )?([^,.!\n]+?)(?:,| while|\.|!|$)|"
                    r"\b(starved to death|died of starvation|drowned|turned to stone|choked on [^.!\n]+|"
                    r"burned to a crisp|died of [^.!\n]+)")
ATTACK = re.compile(r"(?:^|[.!]\s+)(?:The |the )([a-z][\w' -]*?) (?:hits|bites|stings|kicks|butts|touches|claws|"
                    r"stabs|thrusts|swings|zaps|shoots|throws|breathes|explodes)\b")


def postmortem(p, reason=None):
    """The death, or the last crisis, in one block: what a lesson needs."""
    v = p.term.view() if p.term else None
    st = (v.st if v and v.st.get("dlvl") is not None else p.last_st) if p.term else p.last_st
    screen = v.text_screen() if v else ""
    msgs = list(p.msg_log)
    killer = None
    for text in [screen] + [m for _, m in reversed(msgs)]:
        m = KILLER.search(text)
        if m and (m.group(1) or m.group(2)):
            killer = (m.group(1) or m.group(2)).strip()
            break
    if not killer and any(re.search(r"faint from lack of food|You die from starvation|starv", t) for _, t in msgs[-5:]):
        killer = "starvation"
    if not killer:
        killer = next((found[-1] for _, t in reversed(msgs) for found in [ATTACK.findall(t)] if found), "unknown")
    dead = bool(v and v.dead) or reason == "game_over"
    out = ["POSTMORTEM: %s at T%s on Dlvl %s, XL %s; %s %s; killer (best guess): %s" % (
        "died" if dead else "alive", st.get("turn", p.turn), st.get("dlvl"), st.get("xl"), p.race or "?",
        p.role or "?", killer)]
    out.append("HP trail (turn:hp/max): " + " ".join("%s:%s/%s" % x for x in list(p.hp_trail)[-20:]))
    cr = p.crisis or p.last_crisis
    if cr:
        out.append("crisis ladder: %s; tried: %s%s" % (cr["why"], ", ".join(cr["tried"]) or "nothing applied",
                                                      "" if p.crisis else " (ended T%s at HP %s)" % (
                                                          cr.get("ended"), cr.get("hp_end"))))
    esc = [h["text"] for h in p.hist if h["kind"] == "escalate"][-3:]
    out.append("last escalations: " + (" | ".join(esc) or "none"))
    out.append("prayer: %s" % p.prayer_band(st.get("turn") or p.turn or 0))
    out.append("settings: " + describe())
    out.append("last keys: " + " ".join(repr(k)[1:-1] for k in list(p.sent)[-30:]))
    out.append("last messages:")
    out += ["  T%s %s" % (t, m) for t, m in msgs[-20:]]
    if screen:
        out += ["--- screen ---", screen]
    return "\n".join(out) + "\n"
