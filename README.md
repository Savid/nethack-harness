# nethack-harness

A NetHack observation and execution adapter for a decision engine. The engine
chooses what to do. The harness reads the terminal, maintains observed game
knowledge, presents executable choices and carries out the selected action.
Python 3.9+, standard library only.

## Decision cycle

1. Read a structured observation: hero stats, conditions, messages, map, entities,
   remembered terrain, inspected inventory and spells, and recent actions.
2. Present choices with explicit targets and bounded execution.
3. Ask the configured decision endpoint for a tool and, when needed, its concrete arguments.
4. Verify the observation is still current, execute the bounded choice, and return
   its result to the decision cycle. New information or a prompt can end the
   action; the daemon continues choosing until a caller handoff or terminal outcome.
5. Record the request, response, action, input attempts and resulting observation.

The engine chooses combat, retreat destinations, resource use, exploration,
prayer, descent and prompt answers. Actions have no tactical scores. The harness
does not veto dangerous decisions or substitute a gameplay policy when the
endpoint fails. Failure pauses the session before another gameplay action.

A caller supplies the objective and can pause, inspect, change the objective,
manually act, or resume. Training and rewards live outside this repository.

An optional [caller-side goal supervisor](goal_supervisor/README.md) maintains
editable goals and subgoals, checks caller-defined completion/review conditions,
and runs one bounded action per execution window. The LLM can add, update,
switch, suspend, replace or remove goals while retaining their history.
This is a separate package using the adapter's public CLI; the adapter does not
import it or select goals. It is available from the source checkout and is not
part of the harness zipapp.

## Goal planning and model calls

The intended use is to reduce expensive planning-model calls by letting a
decision engine carry out concrete goals. An outer LLM chooses a goal and its
completion/review conditions. The engine chooses tools and arguments; the
harness executes them and records the results. The optional supervisor checks
those conditions and continues the same goal through bounded action windows
without calling the outer LLM for each check.

For example, reaching a known destination may take several decision requests
under one unchanged goal. A bounded travel action can send several movement
commands without further model requests. Observed completion, a requested
review or an execution failure returns control to the caller, which decides
the next goal or recovery plan.

The supervisor does not call an LLM planner or select the next goal automatically.
Parent/child goals provide context and shared conditions; they do not define an
ordered execution sequence. A host caller must implement the planning/review
loop. Compare total planner and decision-engine calls, token usage, latency and
cost at comparable gameplay progress when evaluating savings. Goal counts and
action accuracy alone do not establish a cost reduction.

## Run

Start a local game behind a terminal socket:

```sh
python3 nethack_harness.py serve-local --socket /tmp/game.sock \
  --nethack /usr/games/nethack -- -u Hero -@
```

Then start the adapter in another shell:

```sh
python3 nethack_harness.py --dir /tmp/game-session start \
  --socket /tmp/game.sock --decide http://localhost:8000/v1/systemone \
  --objective 'Explore the dungeon and survive.'
```

For a hosted endpoint, add `--model NAME --key-env ENVIRONMENT_VARIABLE`.
The bearer key stays in the environment and is not recorded in session data.
Use a new session directory for each `start`. `start --paused` dismisses display
pages, publishes the first observation and pauses before any decision request, so a
caller can attach and set the first objective with `resume`.

The daemon runs independently of the shell command. `start`, `wait` and `resume`
wait for a pause, game over, or `--timeout` (30 seconds by default).

| Exit | Meaning |
| --- | --- |
| 0 | Paused, or a command succeeded |
| 2 | Still running; call `wait` again |
| 3 | Game over |
| 1 | Execution/setup failure |
| 64 | Invalid CLI arguments |

`--decision-timeout` controls one engine request, `--max-action-steps` bounds a
selected action (default 8; maximum 64), and `--quiet` controls terminal settling.
These are execution limits, not gameplay preferences.
Set `--review-after-calls N` on `start` or `resume` to return control after N
endpoint calls, including tool and argument selections. The default, 0, disables
this budget. It resets on explicit resume and pauses with `review_budget`;
reaching it requests review without declaring the objective failed. `--timeout`
only limits how long the CLI waits and does not stop the running session.

Set `--max-action-attempts N` on `start` or `resume` to pause with `action_budget`
after N selected executable actions. For example:

```sh
python3 nethack_harness.py --dir /tmp/game-session resume \
  --objective 'Attempt one ordinary move east, then pause.' --max-action-attempts 1
```

An attempt is consumed immediately before its first gameplay input, even if the
move is blocked or the write result is uncertain. Tool/argument selection,
`pause`, pagination and redraws do not consume attempts. A bounded travel or
search action counts once and retains its own step limit; built-in command
follow-ups belong to that same attempt. A separately selected prompt answer is
another action, so the limit can return control at an unresolved prompt.
The budget covers engine and manual actions. Once exhausted, another executable
action requires an explicit resume, which renews the budget. Resume without the
flag preserves the limit; 0 disables it, and is the default. Errors and terminal
outcomes retain their more specific reasons. Exhaustion does not declare the
objective achieved or failed.

`--tools LIST` on `start` or `resume` limits the tools the engine may choose during
normal play to a comma-separated list such as `travel,explore,move,open,kick,search`;
`pause` stays available and game prompts still offer all of their answers. Resume
without the flag preserves the list; `--tools all` removes the limit. Unknown names
are rejected. `actions`, `act` and `send` are not limited.

`--context-file FILE` on `start` or `resume` supplies an arbitrary JSON object
as `state.caller_context` on every decision request, including prompt and argument
selection. `-` reads stdin. The adapter passes this caller-owned context through
without interpreting goals or conditions. Resume preserves it when omitted;
an empty object clears it. `resume --max-action-steps N` changes the per-action
step bound for subsequent choices. `export --after ID` exports only newer
decision records, allowing a caller to reconcile a specific execution window.
`resume --records-after ID` adds those records to the status and observation it
prints once paused, so one command can run and report a bounded window.
`resume --continue-scope` keeps the current objective scope described below, so
a caller running one action per resume keeps the repetition and movement-cycle
evidence across its windows; budgets still renew.

The terminal should use an 80×24 TTY with standard keyboard bindings, letter
movement, standard menu selection markers, colour, the turn counter and pet
highlighting enabled. `serve-local` supplies `color,time,hilite_pet,!autopickup`;
`--options` can set the game options explicitly. The game executable and its
playground must be supplied separately.

## Actions and observations

Normal-play choices include individual moves and attacks, opening or kicking a
door, using items, casting, engraving, praying, inspecting game information,
bounded searching/waiting and travel toward explicit known map targets.
Travel destinations include squares beside closed doors, reachable objects, and
the edge of known terrain: a known square next to one that has never shown a
glyph. A square seen under a monster or object is not unknown, and a square the
hero has stood on is not an edge, because standing there shows all of its
neighbours. Routes may cross squares seen only under objects. Like the game's
own travel, routes never cross remembered water or lava (`}`), and cross a
remembered trap (`^`) only where no other known route exists; a red `}` is
named lava and a blue one water. A destination remains offered for the final
step of its route.
Corridors expose endpoints and junctions as destinations. Interior corridor
tiles remain part of routes without each becoming another destination choice.
Corridor squares connect cardinally, and diagonally where no cardinal path joins them.
Move commands can bump into or attack occupants according to game mechanics.
Travel uses movement without attacks along a shortest known route. Both can
fail or reveal new information; the result is returned to the engine. A move
into water or lava stays a direct movement choice.
Route steps and `move:no_pickup` use the game's `m` prefix, which moves without
autopickup or attacking; into a visible monster it only bumps, using a turn.
A route step into the highlighted pet is sent without the prefix, so the hero
swaps places with it as in the game's own travel. The prefix also skips the game's own checks before
guarded terrain: it refuses a step into known water or lava, and asks before a
step onto a known trap or into a visible gas cloud (a coloured `#`). Toward a
remembered `}`, `^` or coloured `#`, these moves are sent without the prefix,
and the refusal or confirmation prompt returns to the engine like any other
result. The choice says so in its description and in
`argument_facts.modifier.withheld`. Entering known water or lava on purpose
therefore needs caller input.
Remembered green or cyan `#` tiles are structural obstacles rather than corridor
connections and remain visible in `level.structural_obstacles`. Direct movement
commands remain available for the engine to choose.

`explore:row,column` selects a known corridor frontier. It follows the route to
that square, then follows newly revealed corridor tiles within the same command
budget. Ordinary corridor and wall discoveries do not interrupt this action.
It returns on a newly discovered room (terrain where no glyph had shown) or
feature, an arriving monster, changed
hero state, messages, prompts, a blocked move, or caller interruption. At a branch
it returns without choosing a branch. Continuation follows corridor connections,
cardinal ones first, and does not revisit terrain known before the action. If there is
no newly revealed continuation, it reports an exploration boundary. Results
include the stopping position and newly observed terrain. Room-wide exploration
is not currently an action.

Attacks are one command. Travel, search and wait can execute several commands up
to the selected bound. They stop early on changed hero stats, conditions, level,
messages, prompts, movement failure or caller interruption, and when a monster
arrives: one more of a glyph and colour is in view, or adjacent to the hero, than
when the action began. Monsters already in view moving about, a monster the
terminal highlights as the pet, and objects or floor they cover or uncover do not
stop them. Search and wait also stop
on any terrain change; travel stops on newly seen features, doors and structural
obstacles but not on ordinary floor, wall or corridor discoveries.
Atomic game commands can themselves consume multiple turns; actual elapsed turns
are recorded separately from command count.

Information queries read all menu pages and close the menu. Pagination is
mechanical, cancellable and recorded. Directions supplied as part of an action
are sent only after the expected direction prompt appears. An unexpected prompt
pauses for caller review without sending the remaining argument keys.
Confirmations, inventory selection and text input are engine choices. Menus
offer quantities, bulk selection, paging and search; explicit visible selectors
take precedence over menu shortcuts. Spell choices use observed spell labels.
Targeting remains a cursor operation when the game replaces its instructions
with a description of the selected square. Text input offers characters, Enter,
Backspace and Escape. Unknown screens allow the engine to return control to the caller.

### Tool coverage

`tools.py` defines command descriptions, launch keys, modifiers, direction
arguments, repetition and information capture in one registry. Common tools are
offered directly. The `command` tool offers other named commands as a constrained
second choice; it sends the complete command without spelling it one character
at a time. `actions` lists the concrete choices for the current observation.

| Capability | Tools and named commands |
| --- | --- |
| Movement and combat | move, attack, open, close, kick, ascend, descend, travel, explore, wait, search |
| Objects and containers | pickup, drop, droptype, eat, quaff, read, apply, zap, throw, fire, dip, loot, tip, force, rub, invoke |
| Equipment | wield, wear, takeoff, takeoffall, puton, remove, quiver, swap, twoweapon, adjust |
| Abilities | cast, enhance, jump, teleport, monster, turn |
| Interaction | chat, pay, offer, pray, ride, sit, untrap, wipe, engrave |
| Knowledge | inventory, inventtype, spells, showspells, attributes, overview, inspect, look_here, glance, whatis, lookaround, known, knownclass, showtrap, terrain |
| Information and settings | name, annotate, conduct, chronicle, genocided, vanquished, equipment summaries, showgold, prevmsg, help, command menus, version information, options, optionsfull, autopickup, redraw |
| Session | pause, save, quit |

Modifiers are explicit choices with command-specific meanings: movement without
pickup or deliberate attacks, inventory-only consumption, saddle interaction,
payment menus, and overriding the game's safe-wait prevention. The game still
checks the prerequisites for every command. Saving or quitting may be refused
by the game or its launcher.

Native run, rush, repeat and automatic travel are represented by bounded
movement, search, wait, travel and exploration, which return at observable
boundaries. Wizard/discovery modes, host shell and job control, persistent
inventory window management, configuration-file writes and fork-specific
diagnostics are outside the gameplay interface. Synonyms and menu conveniences
do not require separate tools when the same capability is already available.

Positions in observations and action IDs are **1-based screen row,column**.
Action IDs encode direction or target, for example `attack:l`, `travel:8,24`, `explore:8,24`,
`search:8` and `input:79` (the character `y`). The offered catalogue is the authority
for a particular observation. A command being offered does not promise it will
succeed; some game prerequisites are hidden.

Inventory, spells, attributes, dungeon overview and inspections are remembered
with the turn they were observed. Query snapshots include their age in turns
and whether all menu pages were observed; complete does not mean still current.
An interrupted query can contain only part of the inventory. The engine can
query again after consuming, acquiring or rearranging items.

Memory lasts for the active game session. Known levels retain terrain, landmarks
(stairs, traps, altars, fountains and other map features), and observed stair
connections. Recognized shop welcome/untended messages retain the position where
they appeared, their exact text and turn. This is evidence of a shop entry, not
its boundary, current stock or safety; silent entries may only appear in a queried
dungeon overview. Vibrating-square discovery messages are also retained at their
observed location. New terrain observations replace contradicted landmarks.
While engulfed, `map_context` identifies the raw map as an engulfment overlay.
The overlay does not update terrain, visits, searches, inspections or map
entities. Remembered level facts remain available; adjacent and underfoot
geometry, map inspection targets and navigation routes are unavailable until
the dungeon map returns.
Inspections and single-message `look_here` results remain in the current level's
location memory. Direct inspection targets include terrain landmarks and the hero;
use `look_here` for underfoot details. A monster inspection
is attached to a visible entity only on the inspected turn with matching display
attributes: the terminal provides no stable monster identity. Prayer requests
remain recorded throughout the session, without assuming they succeeded.

Retaining information is separate from including it in every request. Every
decision receives the current screen map, which shows remembered terrain, the
current level's inspections, searches and structural obstacles, and a compact
landmark and connection index for all known levels. The remembered terrain grid,
visit counts and inferred-floor list stay in `observe` and the decision log;
requests carry visit counts on each navigation destination instead. Other levels'
full terrain stays in the observer until revisited. Requests carry the last eight
messages and omit inventory, spell, attribute and overview snapshots that were
never queried. The full decision trace is written to SQLite. Starting a fresh harness session resets
the observer; memory is not carried between games or restored from old traces.

The last eight executed actions include their
targets, starting and ending positions, elapsed turns and stopping reasons.
Identical consecutive history entries are represented once with a repetition
count. Every attempt remains separate in the full decision log.
Prompt input history includes the literal keys and their descriptions, including
quantity digits that the game does not echo. This records attempted input, not
an inferred accepted quantity.
A repetition summary counts consecutive identical actions within that history
and reports whether they consumed turns or changed position. It is an observed
fact, not a rule that changes or blocks the engine's choices. The last twenty
messages, terrain memory, visits and search counts are also supplied on every
decision. The complete decision log is stored separately.

Level identity is inferred from status labels
and traversed staircase connections, including stairs taken with caller-sent `<`
or `>` keys; the terminal does not provide a level UUID.
`observation.level` is always present: at prompts and menus it holds the current
level's `id` and `label`, and it is null before any map has been seen.
Quest floors and elemental planes have distinct recognized status labels.
Same-depth branch transitions and portal connections can remain ambiguous;
queried dungeon overview rows preserve branch headings and their order for the
caller to inspect, without inventing a branch identity from depth alone.
The adapter exposes observations and mechanical capabilities. Game reference
knowledge, species statistics, tactical advice and strategy belong to the caller
or decision engine. Unknown glyphs remain unidentified until inspected in-game.

Tool-selection requests expose navigation destinations with their kind,
coordinates, known path lengths, next squares, execution bounds, how often the
hero has stood on them, and whether they are a frontier: not yet stood on and
next to a square that never showed a glyph. These are the offered routes through remembered terrain, not a ranking
or guarantee of passage. The observation also identifies remembered terrain
underfoot. The engine chooses the destination; the executor follows that route
and returns when its bound or an observed boundary is reached.
Argument requests include `argument_facts` for every offered choice, with its
target, bound, modifier when it has one, and the destination facts above for
travel and exploration. Directional actions include their direction, origin,
coordinate delta and the target square's glyph, colour and remembered terrain,
naming a remembered feature such as `lava`, `water` or `trap`, and whether the
terminal highlights a monster there as the pet. The target
is the square addressed by the command, not a guaranteed resulting position.
These facts supplement the complete observation and unchanged choice descriptions.
`navigation_obstacles` preserves destinations excluded because occupied squares
block their known routes, including the observed blocking positions. They remain
evidence even while they are absent from executable choices. Interruptions report
`changed_fields` when available, such as entities, terrain, status or messages.
Unknown terrain under the hero uses a floor placeholder for routing; it is
explicitly marked as inferred until a map glyph or underfoot message is observed.

If the engine selects an action again after it had no observed effect, on the
same terminal screen and under the same objective, the session pauses with
`repeated_no_effect` before sending that duplicate input. Its recorded outcome
includes `review.previous_decision`, the selected action and the evidence.
Two repetitions of the same cycle of movement action endpoints on unchanged remembered terrain,
under the same objective, pause with `navigation_cycle`. The outcome preserves
the original execution reason and includes the cycle positions and decision
records for review. `observation.navigation_progress` keeps up to 32 recent
movement actions; requests carry the latest 8 and every recent position. New terrain, level or objective changes,
nonmovement actions, and a resume that starts a new scope reset this movement
window. A single
return through a corridor does not trigger it.
Intermediate steps inside a bounded action are not used in this cycle signature.

Unexpected follow-up prompts also pause with `unexpected_prompt`. A caller can
read `wait`'s status/observation and `actions`, replan, then `resume --objective`.
A resume that starts a new scope permits another attempt. Time-consuming searches, waits and
combat are not classified as failures merely because the hero stays in place.
These checks do not establish whether a plan is useful or detect every detour.
The decision endpoint is stateless: each request supplies the objective,
observations, recent progress and the complete choices for its current stage.
`observation.objective_progress` identifies the current execution scope with an
opaque ID, its first observed position/turn/level/fingerprint, attempts in the
scope, attempts used and remaining under the caller budget since the latest
resume, and up to 32 recent attempts with omitted-count metadata.
Each attempt records the selected action, source, attempted inputs and delivery
status, before/after observations, and execution result. Unknown coordinates or
turns remain null; a pending input has an uncertain result. Requests carry the
latest 8 attempts, naming each action by its ID, without screen fingerprints or
record numbers; `observe` keeps the rest. Full records remain available through `export`.
This scope starts fresh on every resume, even with identical objective text,
unless the resume passes `--continue-scope`, and always on an objective change.
It is supplied during tool, argument and prompt
selection. It records execution evidence; it does not infer objective completion.
`pause` is available at tool, argument and prompt selection as "Return control to
the caller". The instructions ask the engine to decide from the supplied state and
to prefer a choice that has not already failed to make progress. They do not
elaborate on pausing: in endpoint trials, pause guidance in the instructions or a
longer pause description made engines pause between several workable choices.
Confidence and all response metadata remain in the trace for the caller
to assess; the adapter does not apply a universal confidence threshold or call
a second model itself.

The normal command set includes the primitives used in an ascension: object
application, reading, offering, movement and stair travel. Ritual order, resource
preparation and route selection belong to the engine. Initial death messages
are allowed to paginate because life-saving may follow. Irreversible disclosure
or farewell ends play; `game_result.outcome` reports `ascended` only when explicit
ascension text was observed, otherwise `unknown`. A generic end screen is not
treated as a win. One-item inventory submenus displayed as `--More--` are still
automatically dismissed; the engine can select the item at the original prompt.

## Decision endpoint

The endpoint accepts System One style JSON state and constrained choice questions.
The harness supplies facts and executable options; the model returns an offered
choice rather than free-form text, reasoning or generated code. Endpoint URL and
model name are configurable.

`state` holds `decision` (stage and selected tool), `objective`, `caller_context`,
then `navigation` (tool stage) or `argument_facts` (argument stage), then the
`observation`. The stage-specific members are compact; the observation is the
largest member. Requests in early levels are typically 3 to 7 thousand tokens,
and the shortened attempt histories keep them near that size during long goals.
An endpoint that serializes keys in sorted order and truncates long input keeps
these members ahead of the observation's later keys.

During normal play the first request selects a tool:

```json
{
  "state": {
    "decision": {"stage": "tool", "tool": null},
    "objective": "Explore the dungeon and survive.",
    "caller_context": {},
    "navigation": {"destinations": []},
    "observation": {"phase": "play", "hero": {"hp": 12}}
  },
  "questions": {
    "action": {
      "type": "choice",
      "instructions": "Choose the tool to use next. Objective: Explore the dungeon and survive.",
      "criteria": {"search": "Search here for a selected bounded number of turns.", "pause": "Return control to the caller"}
    }
  }
}
```

An optional top-level `model` selects the engine model. A successful response is:

```json
{"answers": {"action": {"choice": "search"}}}
```

When the tool has several concrete actions, a second request offers only those
actions, such as `search:1` and `search:8`. Its decision state is
`{"stage": "arguments", "tool": "search"}`. The selected tool and objective appear
in the instructions. Tools with one action execute immediately. Game prompts
offer their input choices directly with stage `input`.

The `command` tool uses the same argument stage to select a named command such
as `pay`, `loot` or `enhance`. This keeps infrequent commands out of the first
choice without adding another hierarchy or allowing arbitrary generated code.

`choice` must name an offered criterion. Each endpoint call has its own record
and contributes to the call count. Tool selection records `tool_selected` with
zero execution steps; argument selection records the executed action and result.
A changed screen, objective or caller interruption invalidates a pending tool
selection. Repeated execution within a bounded action and information pagination
need no additional request.

Additional response metadata, such as probabilities, is recorded but never used
to override the choice. Malformed responses, HTTP failures and timeouts pause
play. The harness does not impose a model-specific option or token limit; the
endpoint enforces its own request limits.

## Caller commands

```sh
harness() { python3 nethack_harness.py --dir /tmp/game-session "$@"; }
harness wait --timeout 30
harness pause
harness observe
harness actions
harness screen
harness act search:1
harness send '\e'
harness resume --objective 'Find the downstairs.' --timeout 30
harness status
harness export --out /tmp/decisions.jsonl
harness note --kind lesson --text 'Locked doors here open with kicks.'
harness purpose --kind trial --about 'Try the east corridor first.'
harness stop
```

Manual input requires a paused session and is attributed separately in records.
`act` chooses an action from the current catalogue. `send` takes up to 256 bytes
with `\e`, `\r`, `\n`, `\t`, `\\` and `\xHH` escapes. It is an explicit caller
intervention; arbitrary key sequences are not engine-generated actions.

## Caller intent records

The session record stream also holds what the caller intended, in the same
sequence as decision records and returned by `export` (including `--after`):

- Objective and goal transitions: `start` and a `resume --objective` that changes
  the objective record `set`. `goal --file FILE` records caller transitions from a
  JSON object or list with `transition` (`set`, `added`, `activated`, `updated`,
  `suspended`, `completed`, `removed` or `replaced`), `objective`, and optional
  `goal_id`, `parent_id` and `reason`. The goal supervisor records its transitions
  this way.
- `note --kind KIND --text TEXT` (`--text -` reads stdin): a caller note stored
  verbatim, such as a plan, lesson, hypothesis or observation.
- `purpose --kind KIND [--about TEXT] [--parent ID]`: why this session exists, for
  example `main`, `checkpoint`, `trial` or `scout`. A copied session directory keeps
  the earlier records and appends its own; the latest purpose is the current one.

These commands write to the session database directly, without the daemon. Kinds
are caller labels. Each record carries `schema` 1 and a UTC `recorded_at`. The
adapter does not interpret intent records or add them to decision requests; pass
anything the engine should see through `--context-file`.

## Storage and training data

Each session has a fresh SQLite database, `session.sqlite3`, plus `daemon.log`,
a process lock and a schema-initialization lock. SQLite transactions serialize caller commands and record decisions.
Decision payloads (request, response, outcome and resulting observation) are
stored zlib-compressed, since each holds complete observations; `export` returns JSON.
Completed commands are removed. The session retains its decision history for export.

An exported JSONL row has a `type`: `intent` rows carry `id`, `created`,
`source` (`caller`) and the `intent` record above. A `decision` row includes:

- `id`, `created`, `source` (`engine`, `manual` or `protocol`).
- The exact `request`, including the observation and offered choices.
- `response`, selected `choice`, and endpoint `latency`.
- `inputs`: keys, source and completion status for each input attempt.
- `outcome`: selected action metadata, execution boundary, steps, elapsed turns,
  intermediate messages/status frames and termination.
- `after`: the resulting structured observation.

Intent is written before input. A `pending` input means completion was not
confirmed; a row without an outcome means the decision did not finish recording.
Such records are not automatically replayed. A training consumer must distinguish
these from completed transitions. Terminal disconnection is recorded separately
from observed game over. No reward function is embedded in the adapter.
HTTP endpoint failures retain their response as parsed JSON or raw text, capped
at 1 MiB; the error states when that response was truncated. They pause execution
without retrying the decision request or sending game input.

## Tests and measurements

```sh
python3 -m unittest discover -s tests -v
uv tool run ruff==0.16.10 check --select F,E9 --line-length 120 nethack_harness goal_supervisor tests tools
python3 tools/record_fixtures.py /tmp/game.sock /tmp/observation.json
```

Fixtures contain terminal observations. Tests cover choice authority, prompt
handling, bounded interruption, observation freshness, decision records and
process lifecycle using local fakes and endpoint stubs.

`tools/bench.py` runs seeded games against an explicit decision endpoint:

```sh
python3 tools/bench.py run \
  --serve 'python3 nethack_harness.py serve-local --socket {socket} --nethack nethack --options seed:{seed},color,time,hilite_pet,!autopickup -- -u Hero -@' \
  --decide http://localhost:8000/v1/systemone --seeds 1-3 --seconds 120 \
  --objective 'Reach the greatest depth you can while surviving.' --out results.jsonl
python3 tools/bench.py report results.jsonl
```

The server command is parsed into arguments and run without a shell. Seed support
and isolated game storage depend on the supplied game executable/server command.
Benchmarks stop on a pause, game over, endpoint failure or the time budget. They
report depth, turns, actions, calls and latency; endpoint failures and truncated
sessions are reported separately. There is no scripted gameplay fallback.
This tool measures the decision endpoint under one supplied objective. It does
not run the goal supervisor or an outer LLM planner, and does not calculate token
usage or monetary cost. A comparison against direct LLM control needs an external
evaluation that records those calls and uses comparable game conditions.

## Distribution

`python3 -m nethack_harness` and `python3 nethack_harness.py` run the CLI.
Build a reproducible single-file zipapp:

```sh
python3 tools/build_pyz.py --commit "$(git rev-parse HEAD)" --out dist/nethack-harness.pyz
python3 dist/nethack-harness.pyz --version
```

CI tests Python 3.9 and 3.11. Tagged releases publish the zipapp and its checksum.
