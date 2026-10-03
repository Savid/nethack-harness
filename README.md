# nethack-harness

A fast NetHack autopilot built to sit under an outer-loop agent, such as an LLM
with a shell. The harness is the **inner loop**: it plays routine NetHack at
several keys per second and **escalates** to the outer loop only when judgment
is needed. The outer loop reads a compact situation report, acts (by hand or by
giving new orders) and resumes it.

- **Code does geometry.** It parses the 80x24 tty screen, remembers each
  level, and finds paths and frontiers. It builds a small set of legal macro
  actions: explore (travel or corridor runs), go to the stairs, descend,
  fight, step away, rest, search dead ends, open or kick doors, push boulders,
  eat, pray when it is safe, and engrave Elbereth.
- **A decision model does judgment.** On contested steps it makes one batched
  call to a SystemOne-compatible endpoint (typed `choice`/`noul`/`score`
  questions with probabilities), such as TypeSafe Jev or Cloudflare
  clef-flash. The call holds a `choice` over the legal actions and a `danger`
  yes/no. Calm, clear-cut steps use the rules and skip the call.
- **The outer loop does strategy.** The harness pauses when HP is low and
  prayer is not safe, when danger is high and the model disagrees with the
  rules, on unknown prompts, alarming messages, stalls, a branch decision, or
  any hook you add.

The socket and endpoint path uses only the Python 3 standard library
(Python 3.9 or later). It includes its own terminal emulator.

## Quick start (local game)

```sh
# 1. Run nethack behind a local terminal socket (any nethack binary with a tty port)
python3 nethack_harness.py serve-local --socket /tmp/nh.sock \
    --nethack /usr/games/nethack --options 'color,!autopickup' -- -u Hero -@ &

# 2. Start the inner loop; it blocks until it needs you
python3 nethack_harness.py --dir /tmp/run start --socket /tmp/nh.sock \
    --decide http://localhost:8000/v1/systemone --timeout 120
```

Use `--decide none` to play on rules alone. For hosted endpoints, pass
`--model NAME` and `--key-env VAR`, where `VAR` holds a bearer key.

## The outer/inner protocol

Every blocking command returns at an escalation, at game over, or at its
`--timeout`:

| Exit | Meaning |
| --- | --- |
| 0 | Paused with an ESCALATION report. The keyboard is yours until you resume. |
| 2 | Timeout and still playing fine. Call `wait` again. |
| 3 | Game over (or the terminal socket closed). |
| 1 | Not running, or a setup error (see `daemon.log` in the state directory). |

```sh
H="python3 nethack_harness.py --dir STATE_DIR"
$H start --socket SOCK --decide URL [--mode M] [--directive TEXT] [--set k=v]... [--timeout S]
$H wait [--timeout S]
$H resume [--directive TEXT] [--mode descend|explore|careful] [--set k=v]... [--timeout S]
$H screen              # what the inner loop sees
$H send 'keys'         # manual keys while paused (also: send --hex 1b); prints the new screen
$H pause | stop | status | log [N]
$H probe --socket SOCK --decide URL   # one decision on the current screen
```

A typical outer-loop turn is a single command:
`resume [orders] → block until the next escalation → read the report → decide`.
While paused you may also drive the game with any other client of the same
socket. The inner loop re-reads the screen when it resumes.

An escalation report contains:
- the reason;
- Dlvl, HP, AC, XL and turn, plus conditions and the last prayer;
- the model's top options with probabilities, and its danger estimate;
- the legal options in rule order;
- hostile and never-melee monsters;
- recent actions and messages;
- the full screen.

Orders (`--directive`) are shown to the decision model every step. While orders
are set, the model's pick wins contested steps when it is at least 40%
confident. `--directive ''` clears them.

### Modes and tunables

`--mode careful` rests to 85% HP and engraves Elbereth below 50% HP. It also
stays within XL+2 of depth and escalates earlier. `descend` is the default and
dives; `explore` explores levels before descending. `--set k=v` changes any
tunable:

| Key | Default | Meaning |
| --- | --- | --- |
| `danger_max` | 0.8 | Escalate when danger is above this and the model disagrees with the rules (or HP < 50%) |
| `p_min` | 0.25 | Escalate in risky spots when the model's top probability is below this |
| `hp_escalate` | 0.34 | Escalate below this HP fraction under attack when prayer is not safe |
| `elbereth_hp` | 0.34 | Engrave Elbereth below this HP fraction when threatened |
| `rest_hp` / `descend_hp` | 0.75 | Rest below this fraction; take stairs eagerly above it |
| `xl_lead` | 99 | Do not descend deeper than XL + this |
| `stall` | 60 | Escalate after this many decisions without new squares or depth |
| `mines` | escalate | `allow`, `avoid` (climb back out), or `escalate` on entering the Gnomish Mines |
| `avoid` | | Regex of monster names never to melee |
| `calm` | 8 | Decisions after a resume before model-based escalations can fire again |
| `last_prayer` | -1 | Set to the turn number after you pray by hand |

## Terminal socket protocol

The harness and `serve-local` both speak HTTP over a Unix socket (or TCP with
`http://host:port`):

```
POST /terminal  {"input": "<base64 keys>", "after": <cursor>}
200 {"output": "<base64 terminal bytes since cursor>", "cursor": <new cursor>, "truncated": <bool>}
```

- The server keeps a bounded tail of output; `truncated` means bytes were lost
  and the client redraws with Ctrl-R.
- `410` with a body containing `over` means the game takes no more input.
- `410` with a body containing `waiting` means input is temporarily held;
  nothing was sent, so retry.
- `503` means busy; retry.

The client emulates an 80x24 VT100/xterm screen itself.

## Decision endpoint

```
POST <url>  {"state": <json or text>, "questions": {key: {"type": "choice"|"noul"|"score",
                                                         "instructions": "...", "criteria": ...}}}
200 {"answers": {key: {"choice"|"noul"|"score": ..., "probabilities": {...}, "confidence": ...}}}
```

The harness sends only `state` and `questions` (plus `model` when `--model` is
given). The state is about 800 tokens: structured facts, a 9x17 map crop
around the hero, recent messages and actions, orders, and the raw screen. That
fits small context windows. Answers are normalized, so engines that omit
`probabilities` or `confidence` still work. HTTP 429, 503 and 529 are retried.
If the endpoint fails, the loop keeps playing on the rules.

## Hooks: plugging in outside strategies

Hooks let a strategy add questions and escalation rules without the harness
knowing what they are for. Load them on `start` or `resume`. You can switch
them off and on at runtime with `--disable KEY` and `--enable KEY`. Hook
questions are merged into the step's batched decision call. When a hook is due
on a calm step, the harness makes a call for the hook questions alone, which
adds latency, so gate frequent hooks. When a hook fires, the loop pauses with
reason `hook:<key> (answer)`, and the report lists the hook answers.

**Declarative questions** (`--questions FILE.json`; see `examples/shopkeeper.json`):

```json
{"questions": [{
  "key": "shopkeeper",
  "question": {"type": "noul", "instructions": "Is a shopkeeper or a shop entrance visible on the screen?"},
  "escalate_when": {"noul_gte": 0.8},
  "when": {"every": 10},
  "cooldown": 200
}]}
```

- `escalate_when` takes one or more of `noul_gte`, `noul_lte`, `score_gte`,
  `score_lte`, `choice_in` (a list) and `min_confidence`. All of them must hold.
- `when` gates how often the question is asked: `{"every": N}` decisions, or
  `{"new_level": true}` for the first decision on a newly reached Dlvl. With no
  gate, it is asked every decision.
- `cooldown` is the number of decisions after firing before it can fire again.

**Plugins** (`--plugin FILE.py`; see `examples/new_level.py`). Hook API version 1:

```python
API = 1
def extra_questions(facts): ...        # -> {key: question} to add to this decision's call
def on_answers(facts, answers): ...    # -> None | {"escalate": "reason"} | {"action": "keys to send instead"}
def on_resume(facts, orders): ...      # orders: the directive, mode, set, enable and disable of the resume
```

`facts` holds `dlvl`, `hp`, `hpmax`, `hp_percent`, `xl`, `turn`, `conditions`,
`new_level`, `hostiles`, `standing_on`, `messages`, `decisions`, `keys`,
`mode`, `orders`, `screen`, and `state` (the decision state).

`answers` holds the plugin's own answers under their bare keys. It also holds
the built-in `act` and `danger` answers when the step made that call.

An exception inside a plugin pauses the loop with reason `hook error` instead of
crashing it.

## Design notes

Measured on local games with clef-flash and Jev:
- Inner-loop throughput was 350–600 keys per minute. Most of that time is the
  terminal settling after each key, not the model.
- A decision call took about 80–150 ms with clef-flash and about 240 ms with Jev.
- On contested steps, rules agreed with expert labels more often than the
  model's argmax did.
- The model is useful as a danger signal and as a check on the rules, and as a
  judge of semantic questions such as "which item is food" or "is this
  monster dangerous". It is poor at spatial questions such as "which direction
  are the stairs", so the code answers those.
- Shuffling the option order costs nothing and guards against position bias.

Ideas borrowed from prior work: legal macro-action menus with computed facts,
rule-first arbitration, per-action no-progress masking, and escalation
cooldowns after a resume.

## Development

```sh
python3 -m unittest discover -s tests
```

The tests need no NetHack and no network. They use fake terminal sockets and
fake decision endpoints.

## License

MIT
