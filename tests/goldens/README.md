# View goldens

Each `<fixture>.txt` here is the text view of `tests/fixtures/screens/<fixture>.json` as a fresh game shows it:
the screen is parsed, taken into empty memory, and rendered without sending any keys, so there are no farlook
names and no look underfoot. A reply whose code is not 0 starts with an `exit <code>` line.

`tests/goldens.rs` renders every fixture through `Play::show` over a terminal that shows only that screen and
compares the text with its golden, ignoring trailing whitespace.

To add a fixture or change the view, record the screen into `tests/fixtures/screens/`, run
`GOLDENS_WRITE=1 cargo test --test goldens` to rewrite the goldens, and review the diff. `DIFFERENCES.md` lists the
goldens an intentional view change rewrote, and why.
