# View goldens

Each `<fixture>.txt` here is the text view of `tests/fixtures/screens/<fixture>.json` as a fresh game shows it:
the screen is parsed, taken into empty memory, and rendered without sending any keys, so there are no farlook
names and no look underfoot. A screen the view refuses is written as `error: <message>`.

`tools/goldens.py` wrote them, run from the repository root with `python3 -I tools/goldens.py` against the
reference implementation in this checkout.

`tests/goldens.rs` renders every fixture through `Play::show` over a terminal that shows only that screen, and
compares the text with its golden, ignoring trailing whitespace. A fixture whose view differs on purpose is listed
in `DIFFERENCES.md` under a `### <fixture>` heading, followed by what differs and why; the test skips it.

To add a fixture, record it into `tests/fixtures/screens/`, regenerate the goldens and review the new file.
