# Repository guidance

## Purpose and boundaries

This harness makes NetHack observable and controllable. Gameplay intelligence
belongs outside it. Keep these responsibilities separate:

- **Harness:** decode the UI, retain observed facts, expose mechanical primitives
  and navigation geometry, execute the selected bounded action, and report results.
- **Decision layer:** choose the next tool and arguments from the observations
  and the caller's objective. It owns gameplay choices, including dangerous ones.
- **LLM layer:** supply goals, game knowledge and strategy; review failures and
  replan. Model routing and orchestration belong outside the game adapter.
  The optional `goal_supervisor/` package is a separate caller, connected through
  the public CLI. Keep its goal lifecycle and conditions out of `nethack_harness/`.

- The intended model split lets a decision engine execute concrete goals between
  outer-LLM planning calls. Automatic condition checks and bounded execution must
  not require an outer-LLM call per action. The supervisor currently returns
  completion/review evidence; the host caller owns the LLM planning loop and
  selection of the next goal.
- Keep the project generic and independent of external orchestration systems.
  Deployment details, coordination, training objectives and reward functions
  belong outside the game adapter.
- Distinguish observations, remembered facts and uncertainty. Do not embed game
  reference tables, tactical rankings, survival rules, hidden goals or policy.
  Mechanical checks protect command integrity and freshness, not survival.
- Actions name their targets and bounds. Execution may paginate information and
  stop when observations change; it must not choose a different goal or action.
- Use System One style JSON state and constrained choices. Select a tool, then
  its concrete arguments when needed. Keep endpoint and model configurable;
  count and record every request. Prompt answers are direct choices.
- Treat every decision request as stateless: include its objective, observed
  progress and complete choices for that stage. Keep caller handoff available
  when the evidence or choices are insufficient, including argument selection.
  Argument facts must come from the same actions and observation as their choice
  descriptions. Preserve targets, bounds, modifier meanings and observation
  provenance; a command target is not a guaranteed resulting hero position.
- Enforce caller-specified action bounds mechanically. Scope observed progress
  to each explicit resume; consumed budgets and completed actions do not establish
  that an objective succeeded. Goal completion and conditional replanning belong
  to the external caller.
- Endpoint failures pause play. Record the observation, offered choices, engine
  response, selected action, attempted input and observed result.
- Surface navigation geometry before tool selection. When execution stalls or
  a prompt violates its contract, return control with evidence for the LLM layer;
  do not invent a recovery action or change the objective.
- Use Python 3.9+ and standard-library runtime dependencies.
- This is a hard cutover with fresh databases and one current API and storage
  schema. Remove code that exists only to recognize, translate, repair, backfill
  or preserve a prior schema, API or wire shape, along with its tests. Do not add
  compatibility aliases or migrations, or tests asserting that old features are
  absent. Keep schema, index and foreign-key creation that defines the new
  database from scratch.
- Comments should explain a non-obvious constraint or mechanism in the code;
  omit change history, references to plans and labels that explain nothing.

## Structure

The modules below live in `nethack_harness/`:

- `term.py`, `screen.py`, `transport.py`: terminal emulation, parsing and I/O.
- `perceive.py`, `level.py`, `knowledge.py`: observations, memory, geometry,
  terminal symbols and direction bindings.
- `tools.py`, `actions.py`, `decide.py`, `execute.py`, `session.py`: command registry, action catalogue, endpoint
  contract, bounded execution and the decision cycle.
- `progress.py`: objective-scoped attempt evidence and bounded movement cycle detection.
- `control.py`, `daemon.py`, `store.py`, `settings.py`: CLI, process lifecycle,
  fresh SQLite schema and execution limits.

The separate `goal_supervisor/` package contains `goals.py` and `conditions.py`
for caller-defined goal state and predicates, `harness.py` and `runner.py` for
bounded CLI execution, and `storage.py` and `__main__.py` for persistence and
caller commands. It does not call a planning model or automatically choose goals.

## Verification

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 nethack_harness.py --version
python3 -m nethack_harness help
uv tool run ruff==0.16.10 check --select F,E9 --line-length 120 nethack_harness goal_supervisor tests tools
```

CI tests Python 3.9 and 3.11. Tests use local sockets, fakes and subprocesses.
Put regression tests beside the affected subsystem and verify meaningful
observable behavior. Screen fixtures record observations, not preferred actions.
Each added test must catch a concrete failure or protect a meaningful boundary.
Avoid assertions that copy implementation details or freeze prompt wording;
measure decision quality separately with actual endpoint trials.
Use `tools/bench.py` with an explicit endpoint for gameplay measurements; report
endpoint failures separately from completed game outcomes.
When evaluating model-call savings, count outer-LLM and decision-engine requests
and token usage separately, including reviews, retries and prompt answers.
Compare total cost and latency at comparable gameplay progress; authored goal
counts and frozen-request accuracy are not substitutes for this measurement.
`tools/bench.py` currently measures only the decision endpoint, not an outer planner.

For packaging changes, build and run the zipapp with `tools/build_pyz.py`.
Update README examples and interface documentation with behavior changes.
