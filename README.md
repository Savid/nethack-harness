# nethack-harness

A fast NetHack autopilot built to sit under an outer-loop agent, such as an LLM
with a shell. The harness is the **inner loop**. It plays routine NetHack at
several keys per second and **escalates** to the outer loop when judgment is
needed. The outer loop reads a compact report, acts by hand or changes the
plan, and resumes it.

- **Rules own safety and mechanics:**
  - prompt answers;
  - monsters never to melee and level-dependent threats;
  - prayer timing (and never again after a failed prayer);
  - potions, Elbereth, hunger;
  - stall breakers and a per-level escape ladder;
  - free descents (stairs, trap doors, the game's own stairs memory).
- **A decision model judges contested steps.** It answers one batched call to
  a SystemOne-compatible endpoint, such as TypeSafe Jev or Cloudflare
  clef-flash: a `choice` over the legal actions plus a `danger` yes/no.
  - The model may only add caution; it never answers prompts or unlocks an
    unsafe melee.
  - How often it is asked is the `effort` setting.
- **The outer loop owns strategy:**
  - risk;
  - branch choice (Gnomish Mines or main dungeon);
  - digging and when to descend;
  - plans, hooks and plugins.

  All of these are settings and primitives it can change at runtime. Nothing
  role-specific is played automatically. Instead, a startup **briefing**
  reports the role, the kit and suggested settings.

The code needs only the Python 3 standard library (3.9 or later). It includes
its own terminal emulator.

## Install

**One file (a release).** Each release ships a single-file zipapp and its
checksum:

```sh
curl -fsSLO https://github.com/Savid/nethack-harness/releases/latest/download/nethack-harness.pyz
curl -fsSLO https://github.com/Savid/nethack-harness/releases/latest/download/SHA256SUMS
sha256sum -c SHA256SUMS
python3 nethack-harness.pyz --version      # prints the version and commit
python3 nethack-harness.pyz help
```

Releases are milestones, built reproducibly by CI from a `v*` tag
(`tools/build_pyz.py`). Rebuilding the same tag gives the same bytes.

**Latest master (the live default).** Fetch a commit's source tarball, record
the commit, and check that it runs:

```sh
SHA=$(git ls-remote https://github.com/Savid/nethack-harness refs/heads/master | cut -f1)
mkdir -p nh && curl -fsSL https://codeload.github.com/Savid/nethack-harness/tar.gz/$SHA | tar -xz --strip-components=1 -C nh
echo "$SHA" > nh/COMMIT          # shown by --version, status and help
python3 nh/nethack_harness.py --version && python3 nh/nethack_harness.py help
```

`nethack_harness.py` is a stable entry point beside the `nethack_harness/`
package; `python3 -m nethack_harness` works too.

`master` stays deployable:
- tests run in CI on Python 3.9 and 3.11;
- the command line stays backward compatible between commits;
- force-pushes to master are blocked.

## Quick start (local game)

```sh
python3 nethack_harness.py serve-local --socket /tmp/nh.sock \
    --nethack /usr/games/nethack --options 'color,!autopickup' -- -u Hero -@ &
python3 nethack_harness.py --dir /tmp/run start --socket /tmp/nh.sock \
    --decide http://localhost:8000/v1/systemone --timeout 120
```

- `--decide none` plays on rules alone (the same as `--set effort=off`).
- Hosted endpoints may need `--model NAME` and `--key-env VAR` (a variable
  holding a bearer key). `probe` accepts the same two options.

## The outer/inner protocol

`help` prints the full capability map: every command, setting, mode, effort
level, plan item, hook type and plugin function, with examples.
`help TOPIC` prints one section. Its topics are `protocol`, `commands`,
`settings`, `modes`, `effort`, `plan`, `hooks`, `plugins` and `playbook`.

`help` (or `help brief`) prints a short map (about 2 KB): exit codes, commands,
plan items and the settings used most. `help all` prints everything.

Reports are compact by default (about 150 tokens): the reason with its code,
HP, Dlvl, XL, turn, conditions and the prayer band, the monsters near the hero
with positions, the last messages, an 11×21 map crop around the hero, food and
known stairs. `--set report=full` restores the long report with the whole
screen, and `screen` shows the whole screen at any time. While the loop runs,
`wait` and `resume` print one short line.

By default the loop does not pause for notices it handles itself (`branch_point`,
`oscillating`): `pause_on` is `all,-branch_point,-oscillating`. Every safety pause
stays.

`start`, `wait` and `resume` block until an escalation, game over or the
`--timeout`:

| Exit | Meaning |
| --- | --- |
| 0 | Paused with a report (the first one is the BRIEFING). The keyboard is yours until you resume. |
| 2 | Timeout and still playing; call `wait` again. The line names any decision-model trouble (errors, rules-only time, slow calls). |
| 3 | Game over (or the terminal socket closed). |
| 1 | No inner loop in `--dir`, not running, a setup error, or stuck (no heartbeat for 15 s; `stop` ends a stuck loop). See `daemon.log`. |
| 64 | Usage error (unknown flag, bad `--set` value, `send` without keys); nothing happened. |

While paused:
- `send 'keys'` or `send --hex 1b` sends keys and prints the new screen;
  `screen` shows the screen.
- `repeat 'Fh' --times 6 [--stop-hp 0.5] [--stop-on REGEX] [--allow-hp-loss]`
  is a guarded batch: it sends the keys up to N times (1–50) and stops at the
  first HP loss, HP below the fraction, new monster in view, `--More--` or
  prompt, level change, or alarming or matching message, then prints why and
  the screen. Prefer it to unchecked key loops near danger.
- `status` prints JSON with the settings, effort, hooks, plan, counters, kit
  and the current depth cap.
- `mark NAME` names this moment in the key journal; `keys [--since NAME|T]
  [--raw]` prints every key sent since then, tagged loop, plan, plugin, hand
  or replay. `--raw` lines can be replayed with `--plan replay:FILE` (one send
  per step; it stops on a 15% HP loss).
- `--notes-out FILE` (start or resume) keeps FILE up to date with this game's
  level notes: stairs, up stairs, holes, the Mines staircase, turns spent and
  hazards, as 1-based `ROW,COL`. `--notes-in FILE` uses another copy's notes:
  the loop travels toward down stairs that copy saw, explores toward them first,
  and never mistakes its Mines staircase for the main one. Notes are only used
  when both games share the same first screen. `notes` prints one line per level.
- `--set tiebreak_seed=N` reseeds the loop's tie-breaking choices, so a copy of
  a game explores differently while every safety rule stays the same.
- `postmortem` prints the death (or the last crisis) in one block: best-guess
  killer, HP trail, crisis-ladder steps tried, last escalations, prayer band,
  settings, last keys and messages. It is also written to `postmortem.txt` at
  game over, and works after the loop has exited.

Every escalation has a code (`help escalations` lists them): the status JSON
has `code`, the report's last line reads `REASON [code]: …`, and plugins see
it. `--set pause_on=all,-milestone,-branch_point` silences codes (they are
logged and play goes on; safety codes such as `low_hp` always pause). A
plugin's `on_escalation(facts, esc)` may answer an escalation itself with
`{"continue": true}` or `{"plan": [items]}`.

Reports give prayer as a band: `safe`, `uncertain (last T…, N ago; the
timeout is random after a prayer)`, `fails (last T…, N ago)` when the last
prayer was under 200 turns ago, or `broken` after a failed prayer.

Resume options can be combined:
- `--directive TEXT`: orders shown to the model. While orders are set, the
  model decides contested steps among the safe options.
- `--mode descend|explore|careful`: sets only the keys modes own (`mode`,
  `risk`, `danger_max`, `p_min`, `esc_gap`); `mines`, `avoid` and the rest
  are kept.
- `--set k=v`: any setting.
- `--plan ITEM`: queue a step, such as `keys:Za.`, `goal:stairs`, `goal:dig`,
  `goal:rest:0.9`, `goal:search:30`, `goal:travel:R,C` or `goal:pray`.
  Crisis items do one fight action in one call: `goal:elbereth` (engraves,
  reads it back, re-engraves once), `goal:quaff[:L]`, `goal:retreat` (stairs
  within 8 safe steps, else a less exposed square) and `goal:fight:DIR[:N]`
  (stops on a 15% HP loss or a new adjacent hostile).
- `--questions FILE`, `--plugin FILE`, `--enable KEY`, `--disable KEY`:
  hot-load or switch hooks.

Every report ends with a reminder of these options and the counters: keys,
decisions, model calls (and how many reused an earlier answer) and escalations.

### Settings worth knowing

| Key | Default | Meaning |
| --- | --- | --- |
| `risk` | normal | `low`, `normal`, `high`: HP gates for descending, resting, Elbereth and escalating, plus the depth lead |
| `effort` | medium | Decision effort: `off` (rules only), `low`, `medium`, `high` |
| `lead` | by role | Max depth = XL + lead |
| `fragile_lead`, `pace_xl` | 1, 4 | Pace: the loop descends no deeper than XL + `fragile_lead` (+1 at `risk=high`) until XL `pace_xl`, then one level more unless the hero is fragile. The cap is the shallower of this pace and XL + `lead`, and the briefing and the "depth gate" escalation name which one binds and how to lift it. While it holds on an explored level, the loop wanders the level for experience |
| `sturdy_hp`, `sturdy_hp_per_xl`, `sturdy_ac` | 10, 4, 7 | Fragile means max HP below `sturdy_hp` + `sturdy_hp_per_xl` × XL (14 at XL 1) or AC above `sturdy_ac`; `cap_lift` never lifts the pace for a fragile hero before XL 3 |
| `time_left`, `endgame_secs` | unset, 180 | Seconds of play left from now, set by the outer loop at start and again on any resume (it is counted on this process's monotonic clock, so a restart or a copied state directory reports it as unknown until set again). In the last `endgame_secs` the loop pauses once ("endgame"), lifts depth caps, takes stairs at HP 50% or more and prefers any descent, the Mines included; a larger `time_left` re-arms it. Status and reports show the seconds left |
| `gate_patience` | 0 | Opt-in: after this many turns held by the depth cap on an explored level, allow one level more (0 = wait for experience) |
| `fight_question` | 1 | In a crisis, a close call between ladder steps (Elbereth against retreat, say) is one decision-model question; the model may only pick a legal ladder step |
| `milestone`, `milestone_hp`, `milestone_from` | off, 0.67, 1 | `depth`, `xl` or `both`: pause once at each new deepest level and/or XL, when HP is at least `milestone_hp` and no hostile is in view (deferred otherwise), e.g. "milestone: new deepest Dlvl 6 (XL 4, HP 33/35, T1450; down stairs known: no)". `status` lists them |
| `fight_handoff`, `crisis_turns` | ladder, 12 | When losing fast, the loop first runs a crisis ladder (pray when safe, quaff, stairs underfoot, verified Elbereth, retreat, then fight) and escalates only if the ladder is exhausted or HP is still falling after `crisis_turns`; the report lists the steps tried. `escalate` hands over at once |
| `hp_drop`, `hp_drop_min` | 0.25, 5 | "Losing fast" needs both this fraction of max HP and this many points lost within 5 turns; the same fight re-escalates only after another step of loss |
| `mapping` | 1 | 1 reads magic mapping when a level runs out of options; 2 reads one on each new level |
| `mines` | auto | `allow` for gnome or dwarf heroes, otherwise `avoid`; or `escalate`. An unused Mines staircase looks like any other, so the harness reads the dungeon overview (`^O`) on each new level, leaves at once under `avoid`, and never takes that staircase again |
| `dig` | 0 | 1 = dig down with a pick-axe or mattock |
| `swarm_count`, `swarm_xl`, `swarm_hold` | 3, 10, 150 | A swarm is at least `swarm_count` fast, poisonous attackers in view (killer bees, soldier ants; speed and poison come from the monster data) below XL `swarm_xl`. Below Dlvl 1 the loop leaves by the up stairs instead of fighting in the open, pauses once with "swarm: 4 killer bee, poisonous and fast, on Dlvl 6; ..." and holds off going back down for `swarm_hold` decisions |
| `avoid` | | Regex of monster names never to melee, even when they attack. Monsters the loop keeps away from by itself (nymphs, slow monsters far above the hero's level such as mimics) are not approached or waited for, but are fought back while they attack |
| `quiet` | 0.06 | Seconds of terminal silence that end a key send |

`help settings` lists every key.

### Effort

Effort sets when the decision model is asked, how much state it sees, how long
an answer is reused while the situation is unchanged, and whether ungated hook
questions run every step:

| Level | Ask | State | Reuse | Ungated hooks |
| --- | --- | --- | --- | --- |
| off | never | | | never |
| low | contested steps with an adjacent hostile or HP below half | compact | 20 decisions | with calls |
| medium | contested steps with a monster within 3, a recent hit or HP below half | full | 6 decisions | with calls |
| high | every contested step | full | none | always |

Engines of this kind have no reasoning-effort parameter: each call is one
forward pass, and its cost grows with input tokens and the number of
questions. So effort controls only how often and how much the harness asks.
Switch it at runtime, for example `--set effort=high` in a dangerous spot.

### Hunger

The loop feeds the hero itself:
- **Corpses.** It remembers what it kills and eats a fresh corpse (under 40
  turns old) when Hungry, or when the pack holds under 1500 nutrition. It
  never eats while Satiated or in a shop, nor with a hostile in view unless
  Weak. It never eats cockatrices, were-creatures, polymorphers, bats,
  mimics, the undead's corpses, acidic corpses, or cannibal and pet corpses,
  and eats poisonous ones only when resistant. A Monk eats no meat. Each
  floor prompt is answered by its own corpse.
- **The pack, at Hungry.** It eats the cheapest food first and keeps lembas
  wafers and C- and K-rations while other food lasts. Eggs, unpaid food and
  corpses of unknown age are never eaten. Tins and cures (wolfsbane,
  eucalyptus, a lizard) are eaten only when Weak with no safe prayer. Fruit
  is thrown at blockers only while 1000 nutrition of other food remains.
- **Prayer.** With no food, it prays at Weak once the prayer is safe. When
  Fainting, it prays 500 or more turns after the last prayer, or at any time
  once starvation is under 60 turns away, because starving is certain.
- **The `hunger` escalation** comes only when no food, no corpse and no safe
  prayer is available before Fainting. It comes once per hunger state, and
  at Hungry when possible.

Spells cost nutrition, so trivial adjacent monsters are meleed, not bolted.

### Monsters

- **Never-melee blockers** (floating eyes, molds, blobs, gas spores) are
  fired at, bolted or hit with thrown missiles, spare weapons, gems or fruit,
  never wielded weapons or launchers. A gas spore is killed from 2 squares
  away; the loop steps back first. The game's travel command stops at such a
  monster, so near one the loop steps around it along its own route. When the
  blocker is not in a straight line, the loop walks to the nearest square that
  lines up a clear shot.
- **Stuck boulders.** A boulder that would not move is broken with force bolt
  or a wand of striking or digging when the way is otherwise closed.
- **Mimics.** After "That boulder is a mimic!" every other boulder on the
  level is suspect: the loop does not push one, and steps around them by hand.
  It walks away from slow monsters it keeps away from and never waits for them.
- **Knights** never attack a monster that is fleeing (seen turning to flee) or
  helpless (asleep or unable to move) unless it is undead, because "You
  caitiff!" costs alignment and an out-of-favour Knight prays in vain. The
  crisis ladder may still fight.
- **Swarms** of fast, poisonous attackers make the loop leave by the up stairs
  (see `swarm_count`).
- **Walled in** by a blocker with a mild passive (acid blob, green, red or
  yellow mold, lichen) and nothing to throw, the loop hits it once probing has
  failed and its HP is clear of the worst passive damage. It never does this to
  floating eyes, gas spores, brown molds or cockatrices.
- **Blind**, the loop applies a unicorn horn (or a towel for a face covered in
  goo), and otherwise waits blindness out where nothing is attacking. Below half
  HP it does not swing at unseen-monster markers, and the crisis ladder skips
  them.

### Movement

The game's travel command picks its own path. When a trip makes no progress,
because travel swings between two squares or stops at a monster, the loop walks
its own route one step at a time. It waits a turn for a peaceful monster in the
way, and stops using a step the game refused without a turn passing. Once a
trip is under way, it keeps to it while nothing threatens and it is still
nearly the best option, so two near-equal goals cannot walk the hero back and
forth. A staircase hidden under objects is learned from the "There is a
staircase down here" message.

## Terminal socket protocol

```
POST /terminal  {"input": "<base64 keys>", "after": <cursor>}
200 {"output": "<base64 terminal bytes since cursor>", "cursor": <new cursor>, "truncated": <bool>}
```

- The server keeps a bounded tail of output. `truncated` means bytes were lost;
  the client redraws with Ctrl-R.
- `410` with a body containing `over` means the game takes no more input.
- `410` with a body containing `waiting` means input is held: nothing was sent,
  so retry.
- `503` means busy; retry.

The socket can be a Unix socket path, `unix:///path` or `http://host:port`.

## Decision endpoint

```
POST <url>  {"state": <json or text>, "questions": {key: {"type": "choice"|"noul"|"score", "instructions": "...",
                                                          "criteria": ...}}}
200 {"answers": {key: {"choice"|"noul"|"score": ..., "probabilities": {...}, "confidence": ...}}}
```

- Only `state` and `questions` are sent, plus `model` with `--model`.
- The state is about 800 tokens, or less at low effort.
- Answers are normalized, so engines that omit `probabilities` or `confidence`
  still work.
- Confidence is normalized by the number of options: `(n·p − 1)/(n − 1)`.
- HTTP 429, 503 and 529 are retried. If the endpoint fails, the loop keeps
  playing on the rules.

## Hooks

Hooks let a strategy add questions and escalation rules without the harness
knowing what they are for. Load them on `start` or `resume`. A firing hook
pauses with reason `hook:<key> (answer)`.

**Declarative questions** (`--questions FILE.json`; see `examples/shopkeeper.json`):

```json
{"questions": [{"key": "shopkeeper",
  "question": {"type": "noul", "instructions": "Is a shopkeeper or a shop entrance visible on the screen?"},
  "escalate_when": {"noul_gte": 0.8}, "when": {"every": 10}, "cooldown": 200}]}
```

- `escalate_when` takes any of `noul_gte`, `noul_lte`, `score_gte`,
  `score_lte`, `choice_in` and `min_confidence`. All given conditions must hold.
- `when` is `{"every": N}` decisions or `{"new_level": true}`.
- Ungated questions follow the effort level.

**Plugins** (`--plugin FILE.py`; see `examples/new_level.py`). Hook API version 1:

```python
API = 1
def extra_questions(facts): ...        # -> {key: question}
def on_answers(facts, answers): ...    # -> None | {"escalate": "reason"} | {"action": "keys to send instead"}
def on_resume(facts, orders): ...      # facts: orders, mode, decisions; orders: directive, mode, set, enable, disable
def on_escalation(facts, esc): ...     # esc: {"code", "text"} -> None | {"continue": True} | {"plan": [items]}
```

`examples/auto_answers.py` answers some escalations itself (a safe prayer for
lycanthropy, playing on at a trap door). Safety codes always reach the outer
loop. Plugins run with a 5 s time limit; one that overruns or fails twice is
disabled, and its output goes to a capped `hooks.log`.

The decision `facts` hold `dlvl`, `hp`, `hpmax`, `hp_percent`, `xl`, `turn`,
`conditions`, `new_level`, `hostiles`, `standing_on`, `role`, `race`,
`messages`, `decisions`, `keys`, `mode`, `risk`, `orders`, `screen` and `state`.

A plugin exception pauses the loop with a `hook error` instead of crashing it.
Alarming messages carry a remedy hint where one is known (lycanthropy, illness,
sliming, theft, an angry shopkeeper).

## Layout

| Module | Role |
| --- | --- |
| `term` | VT100/xterm screen emulator |
| `transport` | Terminal-socket client, settling, local pty server |
| `screen` | Messages, prompts, status, hero, colors |
| `knowledge` | Monster tables, prompt answers, roles, food worth picking up |
| `food` | Safe corpses and the corpses the hero made, pack food order, eating, hunger prayer and escalation |
| `level` | Per-level memory, paths, frontiers, search spots, bans |
| `policy` | The Pilot: state, clock, escalation routing; composed from the modules below |
| `perceive` | Screen, pack, character, branch, farlook, depth limits |
| `messages` | Messages and prompts, prayer timing |
| `candidates` | Legal actions with rule priorities (combat, doors, stairs, exploring, emergencies) |
| `crisis` | The crisis ladder, retreat, verified Elbereth |
| `execute` | Carrying out actions and plan items |
| `modelview` | What the decision model sees and is asked |
| `stepper` | One decision: bookkeeping, then arbitration and the action |
| `course` | Action outcomes, futility (oscillation) checks, keeping a trip under way |
| `pauses` | When to hand back: milestones, shop-door notes, the escalation checks of each step |
| `escalation` | Escalation codes, dedupe windows, pause_on |
| `base` | Act, Hard and shared sentinels |
| `decide` | Endpoint client, answer normalization, questions |
| `hooks` | Declarative and plugin hooks |
| `report` | Escalation reports, briefing, status |
| `settings` | Tunables, modes, risk, effort |
| `control` | Command line: start, wait, resume and the other commands |
| `daemon` | The background loop: start-up, command handling, pauses, the step loop |
| `store` | State directory: atomic files, command queue, key journal, level notes, saved pilot |
| `manual` | Guarded key batches (`repeat`) while paused |
| `helptext` | `help` text: the capability map and topics |

## Development

```sh
python3 -m unittest discover -s tests
```

The tests need no NetHack and no network. `tests/fixtures/screens` holds golden screens: a fresh pilot's
top actions on each. After an intended rule change, `python3 tools/record_fixtures.py regen
tests/fixtures/screens` rewrites them; the diff is the behaviour change. `tools/record_fixtures.py record`
captures new screens from a running game.

`python3 tools/build_pyz.py --commit SHA --out dist/nethack-harness.pyz` builds
the release zipapp. Pushing a `v*` tag whose version matches `__version__`
runs the release workflow, which tests, builds twice to check the bytes match,
and publishes the zipapp and `SHA256SUMS` with the tag message as notes.

## Benchmark

`tools/bench.py` judges behaviour changes on many games instead of one
anecdote. It plays seeded local games for a fixed wall time each, with no
decision model by default, under a scripted outer loop. The `resume` loop
resumes every escalation with no help. The `recommended` loop answers with the
plan items this README recommends.

For each game it records:
- max depth, and the turn and time it first reached each Dlvl;
- XL, turns, and death with its cause;
- escalations by code, decisions and keys;
- stuck periods (500 or more turns without a new deepest level) and turns
  spent behind the depth gate.

It prints medians and a per-role table. `compare` runs two commits side by
side:

```sh
python3 tools/bench.py run --seeds 1-12 --secs 240 --out head.jsonl
python3 tools/bench.py compare --base v0.4.1 --head master --seeds 1-12 --secs 240
python3 tools/bench.py report base.jsonl head.jsonl
```

`--serve` is the shell command that starts one game's terminal socket,
formatted with `{seed}`, `{socket}` and `{dir}`. The default uses
`serve-local` with `nethack` and `seed:{seed}`. Pick seeds that cover fragile
and sturdy roles. At most two games run at once.

## License

MIT
