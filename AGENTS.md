# Repository guidance

## What this is

A NetHack observation and execution adapter for a caller, human or program, driving one game over a terminal
socket. Keep it generic: nothing here may depend on or name the systems that call it, where they run, or how they
are deployed.

## Boundaries

- **Decode, remember, route, execute, report.** The harness decodes the screen, remembers what was observed,
  routes over observed geometry, runs bounded actions and reports what happened.
- **No game knowledge or policy.** It never advises, ranks, vetoes or chooses, and holds no survival rules or
  game reference tables. Decoding what a symbol or colour on screen is counts as UI decoding and belongs here.
- **No answers on the caller's behalf.** Prompts are reported with the keys that answered them; the harness
  answers only the end-of-game disclosure questions, to reach the final screens.
- **Bounded actions.** Every action names its target and bounds and stops when observations change. It never
  substitutes a different goal or action.
- Distinguish what is on screen, what is remembered, and what is unknown.

## Change rules

- **Hard cutover.** One current CLI, socket API and terminal protocol. No compatibility code, shims, aliases,
  fallbacks or notes about earlier shapes.
- **If it is useless, cut it.** Every surface needs a user.
- **Comments** say what code does or why a constraint exists. No history, plans or external references.
- **Performance goals:** low resident memory, no idle wakeups, no polling, minimal writes. The daemon blocks in
  `accept` with no timers, keeps all state in fixed-size structures reused across commands, and writes only its
  log, on errors. Hot paths use fixed, reused buffers; no cache grows without bound.

## Rust practice

- Edition 2024; the toolchain is pinned in `rust-toolchain.toml`.
- `#![forbid(unsafe_code)]`.
- No `unwrap` or `expect` outside tests.
- Small, flat modules.
- Dependencies: std, serde and serde_json only. No async runtime and no HTTP client crate.

## Structure

`src/`:

- `vt.rs`, `screen.rs`: the terminal emulator and the parsed screen (`View`: messages, prompts, status, phase).
- `glyphs.rs`: what symbols and colours on screen are.
- `level.rs`: remembered terrain per level, routes and frontier.
- `observe.rs`: memory across screens: messages, underfoot notes, inventory, end-of-game text, level identity.
- `exec.rs`: bounded execution of keys, walks, searches, farlook and look-here.
- `view.rs`, `play.rs`: the text view and facts, and the `look`, `send`, `go` and `rest` commands.
- `link.rs`: the HTTP/1.1 link to the terminal server.
- `daemon.rs`, `cli.rs`: the socket API and the command line.

## Verification

Run from the repository root:

```sh
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
```

Tests use local sockets, fakes and subprocesses; `tests/support/server.rs` is a fake terminal server that
implements `wait` and `waiting`. Put regression tests beside the affected module and test observable behaviour.
Screen fixtures record observations, not preferred actions. View changes show up in `tests/goldens`: rewrite them
with `GOLDENS_WRITE=1 cargo test --test goldens` and review the diff. Update README examples and interface
documentation with behaviour changes.
