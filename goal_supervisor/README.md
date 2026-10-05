# Caller-side goal supervisor

This optional Python 3.9+ package manages goals for an LLM or another caller.
It uses the harness CLI and standard library only. The game adapter neither
imports it nor interprets its conditions. There are no combat rules, goal
rankings, model routing, automatic fallback goals, or gameplay reference tables.

One leaf goal is active at a time. The caller can add, update, activate, suspend,
replace or remove goals. Activating another goal suspends the current one.
Goal IDs and accumulated attempts persist across harness resumes; each execution
window has a separate ID. Parent objectives supply context, and their validity
and review conditions apply to the active child. Completing a child does not
complete or reactivate its parent.

## Planning loop and model calls

An outer LLM or another host caller supplies the goals and handles reviews.
The supervisor executes the active goal and checks observation conditions with
ordinary Python code. It makes no planning-model calls. Only the harness's
configured decision endpoint chooses tools, arguments and prompt answers during
an execution window; bounded execution and condition checks need no extra model call.

`run` continues the same goal automatically while its conditions and budgets
allow. Completion or review returns JSON evidence to the host caller, which can
choose, revise or activate the next goal. A `window_limit` return leaves the goal
active and can be continued with another `run` without replanning. There is no
built-in LLM planner, automatic next-child traversal or behavior-tree scheduler.

This is the mechanism for replacing routine outer-LLM action decisions with
decision-engine requests. Actual savings depend on how long goals can run before
review and how much planning/recovery the host performs. Record both model layers'
requests and usage when comparing against direct LLM control.

## Start and edit goals

Use an existing, paused harness session. Run these commands from the source
checkout; `--harness` can optionally name a harness script or zipapp elsewhere.
The supervisor state directory must differ from the harness session directory.

```sh
python3 -m goal_supervisor --dir /tmp/goals init --session /tmp/game-session
python3 -m goal_supervisor --dir /tmp/goals add --file goal.json
python3 -m goal_supervisor --dir /tmp/goals activate reach-location
python3 -m goal_supervisor --dir /tmp/goals run --max-windows 8
python3 -m goal_supervisor --dir /tmp/goals status
```

Example `goal.json` (the caller supplies the actual level ID and destination):

```json
{
  "id": "reach-location",
  "objective": "Reach [8,24] on level-1. Continue until the destination is reached, then pause.",
  "success": [
    {"path": ["level", "id"], "op": "eq", "value": "level-1"},
    {"path": ["hero", "position"], "op": "eq", "value": [8, 24]}
  ],
  "valid_while": [
    {"path": ["level", "id"], "op": "eq", "value": "level-1"}
  ],
  "review_on": ["no_observed_effect", "movement_interrupted", "route_changed"],
  "limits": {
    "action_attempts": 6,
    "decision_calls_per_action": 8,
    "steps_per_action": 1
  },
  "metadata": {"reason": "Caller-selected milestone"}
}
```

All JSON file arguments also accept `--file -` for stdin. Outputs are JSON.

| Command | Effect |
| --- | --- |
| `add --file FILE` | Add a pending goal; omitted ID is generated |
| `activate ID` | Check eligibility, switch to this leaf and assess completion |
| `suspend ID` | Suspend this goal and its active descendant, if any |
| `update ID --file FILE` | Patch its definition; preserve ID and accumulated attempts; suspend affected active work |
| `replace ID --file FILE` | Remove the old subtree and add a pending goal with a new ID |
| `remove ID` | Remove this subtree from execution; retain its records and history |
| `complete ID --evidence TEXT` | Explicit caller confirmation, attributed separately from checked conditions |
| `check` | Freshly assess the active goal and list conditional eligibility |
| `step` | Assess and execute at most one bounded action |
| `run --max-windows N` | Repeat up to N windows, stopping on completion or review |
| `recover` | Pause and reconcile an unresolved execution; never retry it |
| `status`, `events` | Inspect current state or retained history without game input |

`parent_id` links a goal to an existing unfinished parent. Only leaves without
unfinished children can activate. Adding a child to an active parent suspends
the parent. Removing or replacing a parent removes its subtree. Updating an
active goal or its ancestor requires explicit reactivation. Cycles are rejected.
Updates merge individual `limits` entries; other supplied fields replace their
previous values. Finished goals can be replaced, not edited in place.

## Conditions and completion

Conditions compare values at paths in the structured observation. Supported
operators are `eq`, `ne`, `lt`, `lte`, `gt`, `gte` and `contains`. Paths contain
object keys or array indexes; no expression language or code execution is used.

- `success`: all conditions must hold. An empty list requires caller-confirmed
  completion; it never means automatic success.
- `activate_when`: all must hold before explicit activation. Eligibility alone
  never switches the active goal.
- `valid_while`: all must remain true during execution, including ancestor conditions.
- `review_when`: any true condition requests review, including ancestor conditions.
- `review_on`: named harness boundaries or underlying execution reasons that
  request review. The default is empty; the caller chooses these extra triggers.

Missing or null values and incompatible numeric comparisons yield unknown.
Unknown activation evidence prevents activation; unknown completion, validity
or review evidence returns control. A fresh observation may include remembered
inventory or inspections; include their age/source metadata in conditions when
freshness matters. The supervisor does not infer whether a remembered fact is
sufficient for a particular goal.

Validity and review conditions take precedence over success. Verified success
takes precedence over an exhausted goal budget. A bounded action finishing does
not establish goal completion. Budget exhaustion suspends a goal for review;
it does not declare success or failure. Increase its budget explicitly if more
attempts are warranted. Native harness handoffs, such as endpoint errors,
unexpected prompts or a chosen pause, are not automatically resumed.

A goal such as "kill all goblins in this room" can use a natural-language
objective and caller-supplied room/target context in `metadata`. The decision
engine chooses actions, and may hand back control when it believes it is done
or needs clarification. With no reliable observation predicate for room
clearance, the LLM reviews the evidence and uses `complete --evidence ...`.
That event is labelled `caller_confirmation`; it is not represented as a
mechanically verified success. There is no built-in definition of goblins,
rooms, threats, or suitable combat tactics in this package.

## Execution and interruption

Each window sends the goal, parent intent/conditions, revision, cumulative
progress and execution ID through `caller_context` on every stateless decision
request. It includes a current completion assessment and remaining goal budget.
Recent attempted actions retain their inputs and observed results; historical
window-stop envelopes stay in the audit history rather than the decision context.
Goals without success predicates explicitly require caller confirmation.
It resumes the harness with one action attempt, a default one-step
action bound and a default eight-call selection budget. A goal's default total
budget is eight attempts. These limits are configurable by the caller.
Multiple game turns may still elapse inside one atomic command.

Every window explicitly resumes the harness, resetting its per-resume attempt
and navigation-cycle evidence. The goal's cumulative attempts and recent action
results persist across windows. The default one-step bound can require many
decision requests for a longer route; increase `steps_per_action` when several
steps between supervisor checks are appropriate. Persistent cross-window loops
may still require caller review or reach the goal's total attempt budget.

Conditions are checked before and after each action window, not between keys
inside a command. Raising `steps_per_action` permits several movement or search
steps before the next supervisor check. Prompt answers are separately selected
actions. An unresolved prompt or unknown completion evidence may therefore need
LLM review; the supervisor does not invent a response.

State persists atomically. Goal edits and execution serialize at window
boundaries; `run` reloads the current goal before every window. Edits from another
process wait for the current bounded window, then take effect. When its own
window limit is reached, `run` returns with the goal still active and the harness
paused. The caller can continue, change or remove it.

An execution reservation is saved before requesting game input. If the supervisor
crashes or loses a response, later execution and edits require `recover`. Recovery
pauses the harness, reconciles the recorded inputs by execution ID, counts uncertain
input attempts, and suspends the goal for caller review. It never resends the action.
Use one supervisor as the owner of a harness session while it is running goals;
manual inputs are reported as interference when reconciling a window.

The Python entry point `GoalBoard` can also be embedded in another caller. Its
state is JSON-serializable; callers embedding it own persistence, synchronization,
observation acquisition and execution. `metadata` carries caller extensions
without teaching the adapter a goal schema.
