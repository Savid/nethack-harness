# nethack-harness

A NetHack observation and execution adapter. A caller or a decision engine
chooses what to do. The harness reads the terminal, maintains observed game
knowledge, presents executable choices and carries out the selected action.
Python 3.9+, standard library only.

A caller that makes its own decisions, such as an LLM agent, plays a session
without a decision endpoint through [direct play](#direct-play): a compact text
view, raw keys, and bounded walking and resting. A session with an endpoint runs
the decision cycle below.

## Decision cycle

1. Read a structured observation: hero stats, conditions, messages, map, entities,
   remembered terrain, inspected inventory and spells, and this objective's attempts.
2. Present choices with explicit targets and bounded execution.
3. Ask the configured decision endpoint, in one request, for a tool and the concrete
   arguments of each tool that has several.
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

## Goal planning and model calls

The intended use is to reduce expensive planning-model calls by letting a
decision engine carry out concrete goals. An outer LLM chooses a goal and its
completion/review conditions. The engine chooses tools and arguments; the
harness executes them and records the results. The caller checks those
conditions and continues the same goal through bounded action windows without
calling the outer LLM for each check.

For example, reaching a known destination may take several decision requests
under one unchanged goal. A bounded travel action can send several movement
commands without further model requests. Observed completion, a requested
review or an execution failure returns control to the caller, which decides
the next goal or recovery plan.

The harness does not call an LLM planner or select the next goal. Compare total
planner and decision-engine calls, token usage, latency and cost at comparable
gameplay progress when evaluating savings. Goal counts and action accuracy alone
do not establish a cost reduction.

## Run

Start a local game behind a terminal socket:

```sh
python3 nethack_harness.py serve-local --socket /tmp/game.sock \
  --nethack /usr/games/nethack -- -u Hero -@
```

For direct play, start a session without `--decide`; it prints the first view:

```sh
python3 nethack_harness.py --dir /tmp/game-session start --socket /tmp/game.sock \
  --max-action-steps 32
```

To run the decision cycle, start the adapter with an endpoint instead:

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

`--decision-timeout` bounds one decision request including its retries,
`--max-request-tokens` bounds its estimated size (see
[Decision endpoint](#decision-endpoint)), `--max-action-steps` bounds a selected
action (default 8; maximum 64), and
`--quiet` controls terminal settling. These are execution limits, not gameplay
preferences. The status `calls` counts every HTTP request to the endpoint,
retries included.
Set `--review-after-calls N` on `start` or `resume` to return control after N
decision requests, including tool and argument selections; a retried request
counts once. The default, 0, disables
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
`pause` and protocol records (pagination, redraws and the information looks of
the direct-play view) do not consume attempts. A bounded travel or
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
are rejected. Caller commands (`send`, `go` and `rest`) are not limited.

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
a caller running one action per resume keeps the attempts its requests show and
the repetition and movement-cycle evidence across those resumes; budgets still
renew. For example, a caller that gives the engine one action at a time for a
window of play resumes the window's first action without it and the rest with it:

```sh
python3 nethack_harness.py --dir /tmp/game-session resume --objective 'Explore this level.' \
  --max-action-attempts 1
python3 nethack_harness.py --dir /tmp/game-session resume --objective 'Explore this level.' \
  --max-action-attempts 1 --continue-scope
```

The terminal should use an 80×24 TTY with standard keyboard bindings, letter
movement, standard menu selection markers, colour, the turn counter and pet
highlighting enabled. `serve-local` supplies `color,time,hilite_pet,!autopickup`;
`--options` can set the game options explicitly. The game executable and its
playground must be supplied separately.

## Direct play

A session started without `--decide` never decides by itself. The caller plays
it with four commands; `help` prints this interface for the caller, and an
unknown command prints only these four. `wait` and `observe` refuse such a
session, and `resume` reports that it has no decision endpoint.

| Command | Effect |
| --- | --- |
| `look` | Print the view. Uses no game time. |
| `send 'KEYS'` | Type up to 256 bytes, then print the messages and the view. |
| `go TARGET` | Walk the shortest known route, then print why it stopped and the view. |
| `rest [N]` | Search up to N times (default 20, at most 200), then the same. |

`send` escapes are `\e` Escape, `\r` Enter, `\n`, `\t`, `\\` and `\xHH`, which
sends exactly that byte (`\xf0` is M-p); other characters must be ASCII. Keys
starting with `-` need no `--`. The keys are written one at a time, so a prompt
opened by one of them is known before the next arrives; a prompt or menu still
takes any keys that follow it. `--More--` pages are dismissed and their text,
including text windows such as "Things that are here", is reported.

`go` takes `x,y`, `<`, `>`, `item`, `frontier` or a kind listed under `seen:`
(`door`, `open door`, `altar`, `fountain`, `dart trap`, ...) and walks to the
nearest by known route, skipping one the hero already stands at; for a closed
door it walks to a square beside it. A named square is the destination even at a
corridor end. `frontier` takes the nearest group of
[frontier squares](#actions-and-observations) and walks to its far side; at the
end of a known corridor it follows the corridor on, as the exploration action
does. `go` runs the bounded travel or exploration action with
the session's `--max-action-steps` bound and their stopping rules; messages that
are only about a tame monster the game has named do not stop it, and a step that
the pet blocks is tried once more. Routes pass through the highlighted pet,
swapping places. When no route exists `go` sends nothing and says why: the
monster blocking the only route, a target that is a door or was never seen, or,
for `frontier`, the doors, blocking monsters, boulders and corridor ends that
remain, each corridor end with the number of searches made there. `rest` is
the bounded search; it refuses at a prompt, and when no game time passes the
game refused to search and the exit code is 1. `go`, `rest` and `send` refuse to
start while another of them runs on the same session, and `go` and `rest` stop
between steps on `pause`, `stop` or SIGTERM.

Exit codes are 0; 3 once the game is over or its process has exited, for every
command (`send` can still answer the game's closing questions); 1 for a refused
or failed command; 2 when `start` returns before the session is ready (run
`look`); and 64 for invalid arguments. `start` names a terminal socket it cannot
reach.

A view of the recorded start of a seeded game:

```
Dlvl:1 $:0 HP:14(14) Pw:5(5) AC:4 Xp:1 T:1
Hero the Candidate St:17 Dx:13 Co:13 In:10 Wi:14 Ch:8 Neutral
msg: This is a seeded game (seed "1001", generator version 1).
   30        40
   01234567890
 3 -----------
 4 |../.......
 5 +.d.......|
 6 |.@...d...|
 7 --+--------
you: 32,6  here: < up stairs; There is a staircase up out of the dungeon here.
monsters: d tame little dog 32,5 adjacent k; d jackal 36,6 4 away
seen: + door 32,7 (adjacent j); + door 30,5 (1)
items: / 33,4 (2)
frontier: 40,4 (8)
```

The view holds the game's two status lines; `msg:` lines with the complete
messages written during the last command (a long message joined from the rows
it wrapped over; text the game left on screen from before is not repeated, and
a bare `look` shows only messages no command has shown); an open `prompt:`, the
targeting `cursor:` and `keys:`, the answers the screen offers; the map without
blank rows, cropped to known columns, with column numbers above and row numbers
to the left. Every position is `x,y` read from those numbers: x is the terminal
column, y the terminal row. `you:` gives the hero's square and what is
underfoot: remembered terrain and what the game said about the square ("There
is a staircase up here", "You see here ..."). Where the hero arrives on a square
whose terrain was never seen or where an object lay, the view asks the game with
`:`, which takes no game time. `monsters:` are named by the game's own farlook
(`;`), including "tame" and "peaceful" and answers that run to `--More--`. A
name is kept only while that monster stays on its square with the same glyph
and colour at every view, and shows `(looked T:n)` once that turn has passed; a
monster that moved or arrived is looked at again, at most eight looks per view.
Naming and `:` happen only while the caller holds control, which is always in a
session without a decision endpoint and while a session with one is paused, and
only while the game verifiably waits for a command: no prompt, the cursor on the
hero's `@` and the status lines shown. A `look` while the engine plays sends
nothing. A farlook that names a statue keeps that
square an object. `adjacent K` is the movement key toward a monster or feature
beside the hero. `seen:` lists remembered stairs, traps (by kind when a message
named them), altars, fountains, doors and squares that may be either a door or an
object (below); `items:` remembered objects and statues, each with its known route
length in steps; `frontier:` the targets of `go frontier`. Lists show the nearest
six. Menus, `--More--` and the end of the game print the screen rows instead of the
map.

Traps that messages report at the hero's square, such as "There is a dart trap
here" or "A little dart shoots out at you!", are remembered there like a `^`, so
routes avoid them where another known route exists. The view reads the default
ASCII symbols; a map with DECgraphics, IBMgraphics or other non-ASCII symbols is
refused with that explanation. The view contains no gameplay advice: the game's
own prompts, such as leaving the dungeon from level 1, are shown in full and
answered by the caller.

Every command, including each farlook and `:`, is a decision record with source
`manual` or `protocol`, so `export` contains the complete input history. A
protocol record is no action attempt or recent action and leaves the no-effect
and movement evidence as it was, and the answers to the view's looks stay out
of the remembered messages, so a paused engine session resumes with the evidence
it had.

## Actions and observations

Normal-play choices include individual moves and attacks, opening or kicking a
door, using items, casting, engraving, praying, inspecting game information,
bounded searching/waiting and travel toward explicit known map targets.
Travel destinations include squares beside closed doors, reachable objects, and
the edge of known terrain: frontier squares, of which each group that touches is
offered once, by its square nearest by route. A square is unknown when it has
never shown a glyph and the hero has never stood beside it; standing on a square
shows all of its neighbours, so one still blank after that is rock, and a square
seen under a monster or object is not unknown. A frontier square is one the hero
has not stood on with an unknown square beside it in a cardinal direction. The
game digs corridors one orthogonal step at a time, so the blank square diagonally
past a corridor bend that a route cut across is not a way on. Visits are those
this harness session observed: on a level explored before the session started,
remembered corridors count as frontier until the hero walks them. Routes may cross
squares seen only under objects. Like the game's own travel, routes never cross
remembered water or lava (`}`), and cross a
remembered trap (`^`) only where no other known route exists; a red `}` is
named lava and a blue one water. A destination remains offered for the final
step of its route.
Corridors expose junctions as travel destinations and endpoints as exploration,
which follows the same route to the endpoint and then the corridor beyond it.
Interior corridor tiles remain part of routes without each becoming another
destination choice.
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
result. The choice says so in its description. Entering known water or lava on
purpose therefore needs caller input.
Remembered green or cyan `#` tiles are structural obstacles rather than corridor
connections and remain visible in `level.structural_obstacles`. Direct movement
commands remain available for the engine to choose.

`explore:x,y` selects a known corridor frontier. It follows the route to
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
arrives. Regenerating HP or Pw is not a change; losing either is, and a message
is one unless every sentence is about a tame monster that a farlook named. Search and wait
also stop when HP that was below its maximum reaches it (`hp_full`), and report
no observed effect only after three unchanged screens in a row, because a fast
hero can act twice within one turn. A monster arrives when one more of a glyph
and colour is in view, or adjacent to the hero, than when the action began. Monsters already in view moving about, a monster the
terminal highlights as the pet, and objects or floor they cover or uncover do not
stop them. Search and wait also stop
on any terrain change; travel stops on newly seen features, doors and structural
obstacles but not on ordinary floor, wall or corridor discoveries.
Atomic game commands can themselves consume multiple turns; actual elapsed turns
are recorded separately from command count.

Information queries read all menu pages and close the menu. Pagination is
mechanical, cancellable and recorded. Directions supplied as part of an action
are sent only after the expected direction prompt appears; `--More--` pages and
text windows without entries, such as first-use tips, are dismissed before it.
An unexpected prompt
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
at a time.

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

Every position is **x,y**, the numbers the direct-play view prints: x is the
terminal column (0 to 79) and y the terminal row (the map is rows 1 to 21). JSON
observations hold `[x, y]`; action IDs, descriptions and decision requests write
`x,y`; a goal condition on `hero.position` can be written from the view. Action IDs encode
direction or target, for example `attack:l`, `travel:23,7`, `explore:23,7`,
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
connections. Messages about the hero's square ("There is a dart trap here", "You
see here ...", trap announcements) are kept for that square and name its trap or
stairs. A closed door and a spellbook are both drawn `+`. A `+` with a wall glyph
continuing its line, or remembered as a door, is a door; one on remembered ground,
or with floor beside it both across and along, is an object. The screen cannot tell
the rest, such as a `+` at a corridor's end whose walls have not been seen: it is a
`door or object`, an entity of that kind and a travel destination by that name, and
routes neither enter nor leave it diagonally, as with a door. A square a farlook
named as a statue stays an object. New terrain observations replace contradicted
landmarks.
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

Retaining information is separate from including it in every request. The
observation (`observe`, and `after` in the decision log) keeps the last twenty
messages, the remembered terrain grid, visit counts, the inferred-floor list and
the inputs of the recent attempts. A request states each fact once, mostly as one short line
(see [Decision endpoint](#decision-endpoint)): the current screen map, which shows
remembered terrain; the current level's inspections, searches and structural
obstacles; a landmark and connection index for the known levels; the last eight
messages; inventory, spell, attribute and overview snapshots that were queried;
and this scope's latest attempts. Visit counts travel with each destination.
Other levels' full terrain stays in the observer until revisited. A request over
its token budget omits more. The full decision trace is written to SQLite.
Starting a fresh harness session resets the observer; memory is not carried
between games or restored from old traces.

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

Requests describe each travel and exploration destination before tool selection,
in `state.navigation.destinations` under its action ID: its kind, known route
length, whether it is a frontier square as defined above, how often the hero has
stood on it, and where a travel bound stops short of it. These are the offered
routes through remembered terrain, not a ranking or guarantee of passage.
`observation.adjacent` describes the square in each direction under its direction
key: position, glyph, colour, the highlighted pet and remembered terrain, naming a
remembered feature such as `lava`, `water` or `trap`. The target is the square
addressed by the command, not a guaranteed resulting position. The engine chooses
the destination; the executor follows that route and returns when its bound or an
observed boundary is reached. `navigation_obstacles` (in requests,
`blocked_destinations`) preserves destinations excluded because occupied squares
block their known routes, including the observed blocking positions. They remain
evidence even while they are absent from executable choices. Interruptions report
`changed_fields` when available, such as entities, terrain, status or messages.
Unknown terrain under the hero uses a floor placeholder for routing; `underfoot`
marks it as inferred until a map glyph or underfoot message is observed.

If the engine selects an action again after it had no observed effect, on the
same terminal screen and under the same objective, the session pauses with
`repeated_no_effect` before sending that duplicate input. Its recorded outcome
includes `review.previous_decision`, the selected action and the evidence.
Two repetitions of the same cycle of movement action endpoints on unchanged remembered terrain,
under the same objective, pause with `navigation_cycle`. The outcome preserves
the original execution reason and includes the cycle positions and decision
records for review. New terrain, level or objective changes, nonmovement actions,
and a resume that starts a new scope reset this movement window. A single return
through a corridor does not trigger it.
Intermediate steps inside a bounded action are not used in this cycle signature.

Unexpected follow-up prompts also pause with `unexpected_prompt`. A caller can
read `wait`'s status/observation or `look`, replan, then `resume --objective`.
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
turns remain null; a pending input has an uncertain result. Requests carry, as
`observation.history`, the scope's attempt count and its latest 8 attempts in one
line each: the action (with its description for a prompt answer, so quantity
digits the game does not echo stay readable), how it ended, its steps and turns,
where the hero went, and its source when not the engine; and the attempts and
calls left under the caller budgets. Inputs, screen fingerprints and record
numbers stay in `observe` and the decision log, so a request does not grow with
the keys an attempt sent. Full records remain available through `export`.
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

`state` holds `decision` (stage and selected tool), `objective`, `caller_context`
when the caller set one, `omitted` when the token budget left something out,
`navigation` when travel or exploration is offered, and the `observation`. Each
fact appears once. Destinations are described in `navigation.destinations` and
adjacent squares in `observation.adjacent`, under the action IDs and direction keys
the questions offer, so those options need no description of their own; facts
that are lists of one kind of thing (entities, messages, attempts, landmarks,
inspections) are one short line each, with positions written `x,y`.

During normal play one request asks the tool question `action` and, for every
offered tool with several concrete actions, a question named after that tool
offering those actions and `pause`:

```json
{
  "state": {
    "decision": {"stage": "tool", "tool": null},
    "objective": "Explore the dungeon and survive.",
    "navigation": {"destinations": {"travel:34,3": "edge of known terrain, 11 steps, frontier",
                                    "explore:43,14": "corridor endpoint, 2 steps, frontier"}},
    "observation": {"phase": "play", "hero": {"hp": 12, "position": "43,12"},
                    "adjacent": {"h": "west 42,12 'd' yellow"},
                    "history": {"attempts": 1, "recent": ["explore:43,10: branch_discovered, 3 steps, 3 turns, 43,9 to 43,12"]}}
  },
  "questions": {
    "action": {
      "type": "choice",
      "instructions": "Choose the tool to use next. ... Objective: Explore the dungeon and survive.",
      "criteria": {"search": "Search for traps and secret doors for a selected bounded number of turns",
                   "travel": "Follow a route to a selected known destination without attacking; bounded movement",
                   "pause": "Return control to the caller"}
    },
    "search": {
      "type": "choice",
      "instructions": "If action is search, choose its concrete arguments.",
      "criteria": {"search:1": "up to 1 turn", "search:8": "up to 8 turns", "pause": "Return control to the caller"}
    },
    "travel": {
      "type": "choice",
      "instructions": "If action is travel, choose its concrete arguments.",
      "criteria": {"travel:34,3": "", "pause": "Return control to the caller"}
    }
  }
}
```

An optional top-level `model` selects the engine model. The engine answers every
question together:

```json
{"answers": {"action": {"choice": "search"}, "search": {"choice": "search:8"}}}
```

The harness executes the chosen tool's action named by that tool's answer; an
answer of `pause` there returns control as the `pause` tool does. Answers to the
questions of tools not chosen are recorded and otherwise ignored. A tool with
one action executes without an argument answer. Game prompts offer their input
choices directly as one question with stage `input`. The `command` tool offers
the other named commands, such as `pay`, `loot` or `enhance`, which keeps
infrequent commands out of the tool question without another hierarchy or
arbitrary generated code; its description lists them, and its own question is
asked once it is chosen, so the dozens of rarely used commands do not fill every
request.

Argument questions use the room that `--max-request-tokens` leaves after the
tool question, in catalogue order. When the chosen tool's question was not asked,
the record says `tool_selected` with zero execution steps, and a second request
with decision `{"stage": "arguments", "tool": "search"}` offers only that tool's
actions and `pause`, with the selected tool and objective in its instructions. A
changed screen, objective, caller context or caller interruption invalidates that
pending selection.

Each `choice` must name an offered criterion; a response without an offered
answer to `action`, or to the chosen tool's question when it was asked, is
malformed. Each request has its own record and contributes to the call count;
its `choice` is the executed action, or the selected tool for `tool_selected`.
Repeated execution within a bounded action and information pagination need no
additional request.

Additional response metadata, such as probabilities, is recorded but never used
to override the choice. Transient failures, which are HTTP 429, 502, 503 and 529
and refused, reset or timed-out connections, are retried after a backoff that
starts at 0.5 seconds and doubles up to 8, or after the Retry-After the endpoint
sends when that is longer, while the retry still fits in `--decision-timeout`.
Each failed attempt is kept in the record's `outcome.retries` with its error,
HTTP status, response, latency, Retry-After and wait. Other HTTP failures,
malformed responses and a transient failure with no time left pause play.

`--max-request-tokens N` on `start` or `resume` (default 14000) bounds the
estimated size of each request sent to the engine, counting one token for every
two bytes of its JSON. Requests measured 2.2 to 2.6 bytes per token on the Jev and
Clef engines, so the estimate overstates their counts and an engine with a
16,384-token limit keeps room for its own framing. A request over the budget leaves out, in
this order and only as far as needed, the landmarks of other levels, the messages
before the latest two, and the destinations beyond the 24 nearest by route with
their travel and exploration choices. `state.omitted` lists what was left out, for
example `["landmarks of other levels"]`. A request still over the budget is not
sent: the decision pauses with `decision_error`.

## Caller commands

```sh
harness() { python3 nethack_harness.py --dir /tmp/game-session "$@"; }
harness wait --timeout 30
harness pause
harness observe
harness look
harness send '\e'
harness go '>'
harness rest 10
harness resume --objective 'Find the downstairs.' --timeout 30
harness status
harness export --out /tmp/decisions.jsonl
harness note --kind lesson --text 'Locked doors here open with kicks.'
harness purpose --kind trial --about 'Try the east corridor first.'
harness stop
```

Caller input (`send`, `go`, `rest`) requires a paused session and is attributed
separately in records. It is an explicit caller intervention; arbitrary key
sequences are not engine-generated actions. A session without an endpoint is
always paused, and refuses `resume`.

## Caller intent records

The session record stream also holds what the caller intended, in the same
sequence as decision records and returned by `export` (including `--after`):

- Objective and goal transitions: `start` and a `resume --objective` that changes
  the objective record `set`. `goal --file FILE` records caller transitions from a
  JSON object or list with `transition` (`set`, `added`, `activated`, `updated`,
  `suspended`, `completed`, `removed` or `replaced`), `objective`, and optional
  `goal_id`, `parent_id` and `reason`.
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
at 1 MiB; the error states when that response was truncated. A failure that is
not retried pauses execution without sending game input; the record's `latency`
covers all attempts of its request.

## Tests and measurements

```sh
python3 -m unittest discover -s tests -v
uv tool run ruff==0.16.10 check --select F,E9 --line-length 120 nethack_harness tests tools
python3 tools/record_fixtures.py /tmp/game.sock /tmp/observation.json
```

Fixtures contain terminal observations. Tests cover the direct-play view, choice
authority, prompt handling, bounded interruption, observation freshness, decision
records and process lifecycle using local fakes and endpoint stubs.

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
not run an outer LLM planner, and does not calculate token
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
