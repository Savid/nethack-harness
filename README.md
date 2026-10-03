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
| `sturdy_hp`, `sturdy_ac`, `fragile_lead` | 25, 6, 1 | A hero with max HP below `sturdy_hp` or AC above `sturdy_ac` is fragile and descends no deeper than XL + `fragile_lead` (+1 at `risk=high`); `cap_lift` never lifts this before XL 3. When the gate holds on an explored level, the loop escalates once and searches for experience |
| `milestone`, `milestone_hp`, `milestone_from` | off, 0.67, 1 | `depth`, `xl` or `both`: pause once at each new deepest level and/or XL, when HP is at least `milestone_hp` and no hostile is in view (deferred otherwise), e.g. "milestone: new deepest Dlvl 6 (XL 4, HP 33/35, T1450; down stairs known: no)". `status` lists them |
| `fight_handoff`, `crisis_turns` | ladder, 12 | When losing fast, the loop first runs a crisis ladder (pray when safe, quaff, stairs underfoot, verified Elbereth, retreat, then fight) and escalates only if the ladder is exhausted or HP is still falling after `crisis_turns`; the report lists the steps tried. `escalate` hands over at once |
| `hp_drop`, `hp_drop_min` | 0.25, 5 | "Losing fast" needs both this fraction of max HP and this many points lost within 5 turns; the same fight re-escalates only after another step of loss |
| `mapping` | 1 | 1 reads magic mapping when a level runs out of options; 2 reads one on each new level |
| `mines` | auto | `allow` for gnome or dwarf heroes, otherwise `avoid`; or `escalate`. An unused Mines staircase looks like any other, so the harness reads the dungeon overview (`^O`) on each new level, leaves at once under `avoid`, and never takes that staircase again |
| `dig` | 0 | 1 = dig down with a pick-axe or mattock |
| `avoid` | | Regex of monster names never to melee |
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
```

The decision `facts` hold `dlvl`, `hp`, `hpmax`, `hp_percent`, `xl`, `turn`,
`conditions`, `new_level`, `hostiles`, `standing_on`, `role`, `race`,
`messages`, `decisions`, `keys`, `mode`, `risk`, `orders`, `screen` and `state`.

A plugin exception pauses the loop with a `hook error` instead of crashing it.

## Layout

| Module | Role |
| --- | --- |
| `term` | VT100/xterm screen emulator |
| `transport` | Terminal-socket client, settling, local pty server |
| `screen` | Messages, prompts, status, hero, colors |
| `knowledge` | Monster tables, prompt answers, roles, food |
| `level` | Per-level memory, paths, frontiers, search spots, bans |
| `policy` | The inner loop: legal actions, emergencies, escape ladder, plan queue |
| `decide` | Endpoint client, answer normalization, questions |
| `hooks` | Declarative and plugin hooks |
| `report` | Escalation reports, briefing, status |
| `settings` | Tunables, modes, risk, effort |
| `control` | Daemon, state directory, command line, help |

## Development

```sh
python3 -m unittest discover -s tests
```

The tests need no NetHack and no network.

`python3 tools/build_pyz.py --commit SHA --out dist/nethack-harness.pyz` builds
the release zipapp. Pushing a `v*` tag whose version matches `__version__`
runs the release workflow, which tests, builds twice to check the bytes match,
and publishes the zipapp and `SHA256SUMS` with the tag message as notes.

## License

MIT
