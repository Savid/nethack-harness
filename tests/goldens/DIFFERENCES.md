# Intentional differences in the goldens

Each section names a view change, why it was made, and the goldens it rewrote.

## `seen:` groups squares by kind, rarest kind first

`seen:` listed the six nearest squares of every kind together, then `+N more`, so on a level with many squares of
one kind (doors, or hundreds of water squares) a kind seen on a single square, such as the down stairs, could be
left out. It now groups squares by kind (glyph and name), lists the kinds with fewest squares first (kinds with as
many squares by their nearest square), and gives each kind its nearest three squares by route, then `+N more` of
that kind. Every kind appears. The order is by count alone, not by what a kind is. `facts.seen` is unchanged: every
square, nearest by route first.

Only the `seen:` line changes in these goldens:

- `lava-room`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed1001-0000`: the same squares, regrouped.
- `seed1001-0030`: the same squares, regrouped.
- `seed1001-0060`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed1001-0090`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed1001-0120`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed1001-0150`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed1001-0180`: now shows `| open door`, which `+N more` hid.
- `seed1001-0210`: now shows `| open door`, which `+N more` hid.
- `seed1001-0240`: now shows `| open door`, which `+N more` hid.
- `seed1001-start`: the same squares, regrouped.
- `seed11-0060`: the same squares, regrouped.
- `seed11-0090`: the same squares, regrouped.
- `seed11-0120`: the same squares, regrouped.
- `seed11-0150`: the same squares, regrouped.
- `seed11-0180`: the same squares, regrouped.
- `seed11-0210`: the same squares, regrouped.
- `seed11-0240`: the same squares, regrouped.
- `seed4101-0120`: now shows `> down stairs`, which `+N more` hid.
- `seed4101-0150`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed4101-0180`: now shows `- open door`, which `+N more` hid.
- `seed4101-0210`: now shows `- open door`, which `+N more` hid.
- `seed4101-0240`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed5012-0030`: the same squares, regrouped.
- `seed5012-0060`: now shows `< up stairs`, which `+N more` hid.
- `seed5012-0090`: now shows `_ altar`, `< up stairs`, which `+N more` hid.
- `seed5012-0120`: now shows `_ altar`, `< up stairs`, which `+N more` hid.
- `seed5012-0150`: now shows `_ altar`, `< up stairs`, which `+N more` hid.
- `seed5012-0180`: the same squares, regrouped.
- `seed5012-0210`: now shows `{ fountain`, which `+N more` hid.
- `seed5012-0240`: now shows `| open door`, which `+N more` hid.
- `seed7-0030`: the same squares, regrouped.
- `seed7-0060`: the same squares, regrouped.
- `seed7-0090`: the same squares, regrouped.
- `seed7-0120`: the same kinds, regrouped; `+N more` now counts per kind.
- `seed7-0150`: now shows `| open door`, `_ altar`, `< up stairs`, which `+N more` hid.
- `seed7-0180`: now shows `_ altar`, `< up stairs`, which `+N more` hid.
- `seed7-0210`: now shows `+ door`, `< up stairs`, which `+N more` hid.
- `seed7-0240`: now shows `+ door`, `< up stairs`, which `+N more` hid.
