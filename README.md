# nethack-harness

A NetHack observation and execution adapter for a caller, human or program, driving one game over a terminal
socket. It decodes the screen, remembers what was observed, routes over observed geometry, runs bounded actions
and reports what happened. It never decides what to do: it holds no game reference tables, survival rules or
advice, and it never answers a prompt the caller did not answer.

It is one static binary, `nethack-harness`, with no runtime dependencies. A client invocation sends one request
to a per-session daemon and prints its reply; the daemon owns the game's memory and the terminal connection.

```
caller ──exec──► nethack-harness <cmd>       client: one request, one reply, exit
                    │ <dir>/sock: one JSON request line, one JSON reply line
                    ▼
                 nethack-harness serve        daemon: one thread, blocked in accept, all state in RAM
                    │ one HTTP/1.1 keep-alive connection
                    ▼
                 terminal server: POST /terminal on a Unix socket
```

## Build

```sh
cargo build --release                                            # target/release/nethack-harness
cargo build --release --target x86_64-unknown-linux-musl         # the static release binary
```

The toolchain is pinned in `rust-toolchain.toml`. Tagged releases (`v*`) publish the musl binary and its
`.sha256`.

## The game it reads

The terminal is an 80×24 TTY running NetHack with its standard keyboard bindings and letter movement, colour, the
turn counter (`time`) and pet highlighting (`hilite_pet`), and the default ASCII symbol set. A map drawn with
DECgraphics, IBMgraphics or another non-ASCII symset is refused with that explanation. `!autopickup` is
recommended, so walking does not pick things up.

## Command line

```
nethack-harness [--dir D] [--json] [--socket PATH] [--max-steps N] COMMAND
```

| Option | Meaning |
| --- | --- |
| `--dir D` | Session directory holding `sock`, `sock.lock`, `lock` and `log` (default `.nethack-harness`). It is created readable by its owner only, and the socket is mode 0600. |
| `--json` | Print the reply object instead of its text. |
| `--socket PATH` | Terminal socket a newly started daemon connects to (default `/run/nethack/terminal.sock`). |
| `--max-steps N` | Most steps one `go` takes, 1 to 64 (default 64); fixed when the daemon starts. |

| Command | Effect |
| --- | --- |
| `look` | Print the view. Names monsters by farlook and reads underfoot with `:` when needed; uses no game time. |
| `send KEYS` | Type 1 to 256 bytes, then print the view with the prompts they passed. |
| `go [--wet] [--near N] TARGET` | Walk the shortest known route to `TARGET`, at most `--max-steps` steps, then print the view. `--wet` lets the route step into remembered water. |
| `rest [--near N] COUNT` | Search in place up to COUNT times (1 to 200), then print the view. |
| `stop` | Stop the session's daemon. With none running it prints `no daemon is running` and exits 0. |
| `serve` | Run the daemon in the foreground (the client starts it on demand). |

Options come before the command, so `send` keys may start with `-` (`send -` sends a minus). `send` escapes are
`\e` Escape, `\r` Enter, `\n`, `\t`, `\\` and `\xHH`, which sends exactly that byte (`\xf0` is M-p); other
characters must be ASCII. Keys are written one at a time, so a prompt opened by one is seen before the next
arrives; text typed into a free-text prompt goes as one batch up to the key that ends it. `--More--` pages left
open after the last key are dismissed and their text, including text windows such as "Things that are here", is
reported.

`go` takes `x,y`, `<`, `>`, a map glyph, a kind listed under `seen:` (`door`, `open door`, `altar`,
`fountain`, `dart trap`, …) or `frontier`, and walks to the nearest match by known route. For a closed door it
walks to a square beside it. `frontier` walks to the nearest group of frontier squares, and at the end of a known
corridor follows the corridor on. Routes never cross remembered lava, or water except with `--wet` (below), cross
a remembered trap or a step the game refused only where no other route exists, and pass through the highlighted
pet. When no route exists `go` sends nothing and says why. A walk or rest stops short when a monster arrives (a
monster with no monster of the same glyph and colour within one square of it at the previous step; `I` and warning
digits never count), a message appears, the status, conditions or terrain change, HP becomes full, the hero is engulfed, a step is
refused or a prompt opens. A rest also stops when three searches in a row leave the screen unchanged (`nothing
changed`), and checks its last search too; searches that pass no game time and change nothing but the message
were refused by the game (`no time passed`, exit code 1). A `frontier` walk that follows a corridor stops where
the corridor branches or ends.

`go --wet TARGET` is for a caller whose hero can cross water (flying, levitating, swimming): its route may also
step into remembered water (a blue `}`), never lava or a `}` of another colour, and of routes equally short it takes
the one with fewest steps into water. Each step into water is sent after the `m` prefix, which the game takes as a
deliberate move into known water without its swim check (`paranoid_confirmation:swim`). The harness does not judge
whether the hero can cross; what the game does is reported. A step into water that leaves the hero where it was,
such as `You avoid stepping into the water.`, stops the walk short (exit code 2) with the game's message under
`msg:` and `wet step X,Y->X,Y refused`; it is not tried again or routed around. The report counts the steps sent
into water. Without `--wet`, nothing changes.

`--near N` on `go` or `rest` (1 to 80; options may come before or after the operand) narrows the monster stop to a
radius the caller picks: a monster stops the walk or rest only when it is within N squares of the hero (the larger
of the column and row distances), measured from where the hero is at each step. A monster counts when it comes
into view inside the radius, or moves into it from outside; one already inside that moves a square does not. The
stop reads `a monster came within N squares of the hero (d 41,8, 2 away)`. Every other stop is unchanged, and
without `--near` a monster coming into view anywhere stops the action, as above.

After `send`, `go` and `rest`, as with `look`, the daemon names new or moved monsters by farlook (`;`, at most
eight per command) and reads underfoot with `:` where the hero stands on a square never seen, an object square, or
a known engraving last read on an earlier turn. Both take no game time and run only while the map waits for a
command.

When the game ends during a command, the daemon pages through the end-of-game screens, answering `n` to the
disclosure questions and dismissing `--More--`, past the tombstone and the farewell to the score list the game
prints as it exits. It reports what they say under `end:` and in `facts.end`. Afterwards every command answers with
exit code 3 and sends nothing; the terminal closing once the game has exited is not an error.

The client connects to `<dir>/sock`. When nothing answers it takes `flock(<dir>/lock)`, checks again, starts
`serve` in its own process group with standard error to `<dir>/log`, and waits up to 10 s for it to listen. The
daemon holds `flock(<dir>/sock.lock)` while it serves, so a refused connect to a live daemon (its queue of waiting
clients is full) is retried for up to 10 s rather than starting a second one.

A command stops sending to the game after 60 s and stops short (exit code 2), so a slow or stuck game cannot hold
the caller; the client waits at most 90 s for its reply.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Done. |
| 1 | Refused or failed, with the message: no route, a prompt is open, a search that passed no time. |
| 2 | Stopped short; the report line says why. |
| 3 | The game is over; `end:` and `facts.end` hold the end text. |
| 4 | The terminal is closed or cannot be reached. This is not a death. The daemon exits after telling a client. |
| 5 | Held: the game takes no input yet; the keys from the refused one on were not sent. |
| 64 | Usage: bad arguments or a malformed request. |

## Socket API

A program may talk to the daemon directly. Connect to `<dir>/sock`, write one JSON request line (at most 4096
bytes, all of it within 2 s), read one JSON reply line. One request per connection; requests are served one at a
time, in order.

```json
{"cmd": "look"}
{"cmd": "send", "keys": "Fh"}
{"cmd": "go", "target": "frontier"}
{"cmd": "go", "target": ">", "wet": true}
{"cmd": "rest", "count": 20}
{"cmd": "rest", "count": 20, "near": 3}
{"cmd": "stop"}
```

`keys` uses the escape syntax above. Each command takes exactly its own field: `send` requires `keys`, `go`
requires `target` (at most 64 bytes) and takes an optional `wet` (`true` is `go --wet`), `rest` requires `count`
(1 to 200), `go` and `rest` take an optional `near` (1 to 80, as `--near`), and any other field is refused with
code 64. The reply is

```json
{"code": 0, "text": "…the text view…", "facts": {…}}
```

A connection that closes without sending anything gets no reply. Only the client starts a daemon on demand; a
program using the socket directly starts one with `nethack-harness --dir D --socket PATH serve`, which prints
`ready` once it listens.

## The text view

A `look` at the start of a recorded game:

```
Dlvl:1 $:0 HP:14(14) Pw:5(5) AC:4 Xp:1 T:1
Hero the Candidate St:17 Dx:13 Co:13 In:10 Wi:14 Ch:8 Neutral
   30        40
   01234567890
 3 -----------
 4 |../.......
 5 +.d.......|
 6 |.@...d...|
 7 --+--------
you: 32,6  here: < up stairs; There is a staircase up out of the dungeon here.
monsters: d tame little dog 32,5 adjacent k; d jackal 36,6 4 away
seen: + door 32,7 (adjacent j), 30,5 (1)
items: / 33,4 (2)
frontier: 40,4 (8)
```

Other commands add a report line at the top and, when they apply, lines such as:

```
rest 20: 3 searches, 3 turns; stopped: a monster came into view or next to the hero (d 41,8)
go --wet <: 4 steps, 1 wet, 4 turns, now at 16,3; done
inventory: (T:4) a - a +0 scalpel (weapon in hand); b - an uncursed stethoscope
answered: "Do you want your possessions identified? [ynq] (n)" ← n
end: killed by a jackal
```

The parts, in order:

- **Report line**, after `send`, `go` and `rest`: the command with its options (`go --wet --near 3 >`), steps or
  searches, for `go --wet` the steps into water (`N wet`), turns passed, where the hero is, and `done` or
  `stopped:` with each reason.
- **Status**: the game's two status lines, spaces squashed.
- **`msg:`**: each complete message written during the command, joined across wrapped rows. Text already on a
  screen printed in full is not repeated. A command reports its last 32 lines, after `(N earlier lines left out)`.
- **Prompts**: `prompt:` with the question, `cursor:` while targeting, and `keys:` with the answers the screen
  offers. Menus, `--More--` windows and the end of the game print `screen (MODE):` and the screen rows instead of
  the map.
- **Map**: without blank rows, cropped to known columns, with column numbers above and row numbers on the left.
  Every position is `x,y`: x the terminal column, y the terminal row (the map is rows 1 to 21).
- **`you:`**: the hero's square and what is underfoot: remembered terrain, `engraving` for an engraved square,
  and what the game said about the square.
- **`monsters:`**: glyph, farlook name, `pet` when highlighted, square, and distance or `adjacent K` (the move key
  toward it). A name lasts while the monster stays on its square looking the same; `(looked T:n)` marks a name
  from an earlier turn.
- **`seen:`**: remembered stairs, traps, altars, fountains, water, lava, doors, and squares that may be a door or
  an object, grouped by kind (glyph and name). Kinds with fewest squares come first, so a kind seen on one square
  is never hidden by one seen on hundreds; kinds with as many squares go by their nearest square. Each kind lists
  its nearest three squares by route with the route length, `(no route)` or `(blocked)`, then `+N more` of that
  kind: `seen: > down stairs 60,12 (no route); + door 32,7 (adjacent j), 30,5 (1); } water 33,6 (adjacent l),
  34,6 (no route), 35,6 (no route), +712 more`. **`items:`**: remembered objects. **`frontier:`**: known ground
  beside unexplored squares. These two list the nearest six by route with the route length, `(no route)` or
  `(blocked)`, then `+N more`.
- **`inventory:`**: the last inventory menu the game showed, whoever opened it, with the turn it was shown.
- **`answered:`**: each prompt the sent keys passed and the key that answered it, marked
  `(typed into a text prompt)` when the keys went into a free-text prompt; the last 32, after
  `(N earlier prompts left out)`.
- **`end:`**: the death line, then every end-of-game message line. The death line is the game's own words for the
  death, whole: the tombstone's text, continued from the game's entry in the score list, which prints the reason in
  full where the tombstone leaves out the circumstances and cuts off what its lines cannot hold (the score list's
  `Killed by a gray unicorn, while praying.` completes the carved `killed by a gray unicorn`). Without a tombstone
  it is the final summary line.

An engraving on room floor (a bright blue `` ` ``) is passable floor; any other `` ` `` is a boulder. A bright
white `0` is warning level 0, a monster sensed but not seen; any other `0` is a boulder. Traps are remembered
from the map's `^` and from the game's description of the hero's square (`There is a dart trap here.`), which also
names the trap. A trap going off (`A dart shoots out at you!`, `Kaablamm!`) is only a message: the game draws a
trap the hero set off once the hero steps off it, and the same words report traps that monsters set off. A
remembered trap whose square the screen shows with other terrain is forgotten, with its name and the notes
underfoot that describe it.

## Facts

`facts` is the machine form of the view. Optional fields are left out when unknown.

| Field | Type | Meaning |
| --- | --- | --- |
| `dlvl`, `turn`, `hp`, `hpmax`, `xl`, `ac` | int? | From the status lines. |
| `hunger` | string? | The hunger word on the status line. |
| `pos` | `[x, y]`? | The hero's square. |
| `here` | string? | What is underfoot, as `here:` prints it. |
| `prompt` | string? | The open prompt's text. |
| `msgs` | string[] | Messages written since the last reply. |
| `monsters` | object[] | `{name?, glyph, x, y, dist, looked_turn?, pet?}`, nearest first. |
| `items`, `seen` | object[] | `{glyph, label, x, y, steps?, blocked?}`, every one, nearest by route first. |
| `frontier` | object[] | `{x, y, steps}`. |
| `stairs_up`, `stairs_down` | `[x, y]`? | Remembered stairs on this level. |
| `inventory` | object? | `{turn?, lines[]}`, lines such as `a - a +0 scalpel (weapon in hand)`. |
| `report` | object? | `{verb, done, stopped[], steps, wet?, turns?, error?}` for `send`, `go` and `rest`; `wet` counts the steps `go --wet` sent into water. |
| `answered` | object[] | `{prompt, key, text_entry?}`; `key` in escape syntax. |
| `end` | object? | `{death?, lines[]}` once the game is over; `death` is the death line, verbatim. |

## Terminal protocol

The daemon talks to a terminal server over one persistent HTTP/1.1 keep-alive connection on a Unix socket. It
needs exactly this protocol:

```
POST /terminal
{"input": "<base64 bytes>", "after": <cursor>, "wait": <ms 0-3000>}

200 {"output": "<base64 bytes>", "cursor": <n>, "truncated": <bool>, "waiting": <bool>}
```

- `output` is the terminal output after `after`, and `cursor` the position after it. `truncated` says output
  between `after` and the oldest kept byte was dropped. A cursor that goes backwards means the server restarted;
  the daemon reads from 0 again.
- `waiting` is true when the game is blocked reading a key, has consumed all input sent so far, and all its output
  is in `output`.
- With `wait` > 0 the server holds the reply until `waiting` is true or the deadline passes, whether or not input
  was written; with `wait` 0 it answers at once. A frozen, stopping or ended game answers at once with
  `waiting: false`.
- `503` means busy; the daemon retries with pauses growing from 10 ms to 1 s, for at most 5 s.
- `410` with a body containing `waiting` (such as `waiting to start`) means the game takes no input yet: exit code 5.
- Any other `410` means the terminal is closed: exit code 4.

The daemon sends each key batch with `wait: 3000` and treats `waiting: true` as settled. While `waiting` stays
false it reads again with `wait`, at most 4 times (about 12 s more); a game still busy after that stops the command
short (exit code 2) and the keys after the batch are not sent. Screen shape (the cursor on `@` with the status
drawn, `--More--`, a menu footer, the cursor on row 0) only classifies what the game is waiting for, never when it
is done.

Before its first command, and again whenever the output restarts, the daemon reads with `wait: 3000`, so a game
still starting up has drawn its first screen. If no full clear of the screen has been seen, it asks the game to
redraw (`^R`). It then dismisses the `--More--` pages the screen opens with, such as the welcome, and reports
their text with the next view. No caller key is spent on any of this.

A reply must arrive in full within its `wait` plus 5 s. A connection that fails partway through a reply fails the
command (exit code 1) and is replaced for the next one. Input is never sent twice: a kept connection is checked
before input goes out on it, and one that closes after taking input without answering is reported as an unknown
outcome (exit code 1).

## Resources

The daemon has no timers and no polling: idle, it is blocked in `accept` and wakes for nothing. Its memory (the
terminal grid, level maps, notes, inventory, names) lives in fixed-size structures reused across commands. It
writes only its log, on errors. The client is one short-lived process that connects, writes, reads and exits.

## Development

```sh
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
GOLDENS_WRITE=1 cargo test --test goldens   # rewrite tests/goldens from the current view; review the diff
cargo test --release --test bench -- --ignored --nocapture --test-threads 1   # resource measurements and bounds
```

The tests include per-module unit tests, golden views of recorded screens in `tests/fixtures/screens`, and end
to end runs of the real binary as client and daemon against a fake terminal server that implements `wait` and
`waiting`.
