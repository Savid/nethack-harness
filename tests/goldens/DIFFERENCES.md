# Intentional differences from the goldens

Each entry is a `### <fixture>` heading, then what the view prints differently and why.

The game draws an engraving on room floor as a bright blue `` ` `` and a boulder as a `` ` `` in any other colour.
The view reads the colour: an engraved square is passable floor, so routes cross it and it is no longer listed
under `items:`. What the game says when the hero reads an engraving underfoot is kept as an underfoot note.

### seed1001-0150

The bright blue `` ` `` squares at 44,7, 45,7 and 42,8 are engraved floor, not boulders: they leave `items:`, the open doors and items beyond them get routes, and `frontier:` gains the squares they reach. The default-colour `` ` `` at 38,7 stays a boulder.

### seed1001-0180

The bright blue `` ` `` squares are engraved floor, not boulders: they leave `items:`, and the stairs, doors and items beyond them get routes, so `seen:`, `items:` and `frontier:` reorder by route length. The default-colour `` ` `` at 38,7 stays a boulder.

### seed1001-0210

The bright blue `` ` `` squares are engraved floor, not boulders: they leave `items:`, and the stairs, doors and items beyond them get routes, so `seen:`, `items:` and `frontier:` reorder by route length. The default-colour `` ` `` at 38,7 stays a boulder.

### seed1001-0240

The bright blue `` ` `` squares are engraved floor, not boulders: they leave `items:`, and the stairs, doors and items beyond them get routes, so `seen:`, `items:` and `frontier:` reorder by route length. The default-colour `` ` `` at 38,7 stays a boulder.

### seed11-0150

The bright blue `` ` `` at 40,16 is engraved floor, not a boulder: it leaves `items:`, and the door at 40,15 above it gets a route.

### seed11-0180

The bright blue `` ` `` at 40,16 is engraved floor, not a boulder: it leaves `items:`, and the door at 40,15 above it gets a route.

### seed11-0210

The bright blue `` ` `` at 40,16 is engraved floor, not a boulder: it leaves `items:`, and the door at 40,15 above it gets a route. The engraving the hero reads underfoot (`Something is written here in the dust.  You read: …`) is kept as an underfoot note, so `here:` shows it instead of `unknown`.

### seed11-0240

The bright blue `` ` `` at 40,16 is engraved floor, not a boulder: it leaves `items:`, and the door at 40,15 above it gets a route.

### seed4101-0060

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:`.

### seed4101-0090

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:`.

### seed4101-0120

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:` and the `+N more` count drops by one.

### seed4101-0150

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:`.

### seed4101-0180

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:`.

### seed4101-0210

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:`.

### seed4101-0240

The bright blue `` ` `` at 57,4 is engraved floor, not a boulder, so it leaves `items:`.

### seed4242-quit

The screen asks an end-of-game question, so the reply's code is 3 (game over) rather than 0, and the test prints it as `error: …`. The text itself is unchanged.

### seed7-0030

The bright blue `` ` `` at 9,8 is engraved floor, not a boulder, so it leaves `items:`.

### seed7-0060

The bright blue `` ` `` at 9,8 is engraved floor, not a boulder, so it leaves `items:`.

### seed7-0090

The bright blue `` ` `` at 45,19 is engraved floor, not a boulder, so it leaves `items:`.

### seed7-0120

The bright blue `` ` `` at 45,19 is engraved floor, not a boulder, so it leaves `items:`.

### seed7-0150

The bright blue `` ` `` at 45,19 is engraved floor, not a boulder, so it leaves `items:`.

### seed7-0180

The bright blue `` ` `` at 45,19 is engraved floor, not a boulder, so it leaves `items:` and the `+N more` count drops by one.

### seed7-0210

The bright blue `` ` `` at 45,19 is engraved floor, not a boulder, so it leaves `items:` and the `+N more` count drops by one.

### seed7-0240

The bright blue `` ` `` at 45,19 is engraved floor, not a boulder, so it leaves `items:` and the `+N more` count drops by one.
