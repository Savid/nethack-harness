//! `schema`: every command's request as JSON Schema, with the bounds the request check enforces, so a caller can
//! offer the commands as typed tools and send what it gets with `request`.

use serde_json::{Value, json};

use crate::daemon::{AVOID_MAX, DIRECTIONS, GUARD_MAX, GUARDS_MAX, HP_MAX, NEAR_MAX, TARGET_MAX};
use crate::play::{KEYS_MAX, LEGS_MAX, REST_MAX, SWINGS_MAX};
use crate::types::{COLS, MAP_ROWS};

/// The schema document.
pub fn schema() -> Value {
    let near = json!({
        "type": "integer", "minimum": 1, "maximum": NEAR_MAX,
        "description": "Only a monster arriving within this many squares of the hero stops it; without it, a monster coming into view anywhere does.",
    });
    let directions: Vec<String> = DIRECTIONS.chars().map(String::from).collect();
    let map_rows = |what: &str| {
        json!({
            "type": "array", "minItems": MAP_ROWS, "maxItems": MAP_ROWS,
            "items": {"type": "string", "minLength": COLS, "maxLength": COLS, "pattern": "^[ -~]*$"},
            "description": what,
        })
    };
    let commands = [
        command(
            "look",
            "Print the view. Reads the inventory unless the one held is from this turn, names monsters by farlook and reads underfoot when needed; uses no game time.",
            json!({}),
            &[],
        ),
        command(
            "send",
            "Type keys, then print the view with the prompts they passed. A prompt opened by one key is seen before the next is sent, and a --More-- in the way is paged through first, so every key reaches what waits behind it; pages left open after the last key are dismissed.",
            json!({
                "keys": {
                    "type": "string", "minLength": 1,
                    "description": format!("1 to {KEYS_MAX} keys. Escapes: \\e Escape, \\r Enter, \\n, \\t, \\\\ and \\xHH for any byte; other characters must be ASCII."),
                },
                "guard": {
                    "type": "array", "minItems": 1, "maxItems": GUARDS_MAX,
                    "items": {"type": "string", "minLength": 1, "maxLength": GUARD_MAX, "pattern": "^[ -~]*$"},
                    "description": "Texts of prompts not to answer: keys stop before a prompt whose text contains one, the rest are not sent and the prompt stays open.",
                },
                "at": {
                    "type": "array", "minItems": 1, "maxItems": GUARDS_MAX,
                    "items": {"type": "string", "minLength": 1, "maxLength": GUARD_MAX, "pattern": "^[ -~]*$"},
                    "description": "Where these keys belong: a text of the prompt they answer, or what the game waits for (play, more, menu, item, direction, choice, text, position, spell). No key is sent anywhere else, so an answer for a prompt that never opened does not run as commands.",
                },
            }),
            &["keys"],
        ),
        command(
            "go",
            "Walk the shortest known route to a target, at most 64 steps, then print the view. Stops short when a monster arrives, a message appears, the status, conditions or terrain change, HP becomes full, a step is refused or a prompt opens.",
            json!({
                "target": {
                    "type": "string", "minLength": 1, "maxLength": TARGET_MAX,
                    "description": "x,y; <; >; a map glyph; a kind listed under seen:; or frontier.",
                },
                "ground": {
                    "type": "string", "enum": ["dry", "wet", "lava", "inland"], "default": "dry",
                    "description": "wet lets the route step into remembered water; lava into remembered water and lava; inland keeps it off every square beside remembered water.",
                },
                "near": near,
                "avoid": {
                    "type": "array", "maxItems": AVOID_MAX, "items": {"type": "string"},
                    "description": "Squares as x,y the route must not enter, such as a monster's reach, a row of trap doors or a crowded stair. The target itself is never avoided. With no route around them the walk is refused and says so, rather than taking one through them.",
                },
            }),
            &["target"],
        ),
        command(
            "rest",
            "Search in place up to count times, then print the view. Stops as go does, and when three searches in a row change nothing.",
            json!({
                "count": {"type": "integer", "minimum": 1, "maximum": REST_MAX},
                "near": near,
            }),
            &["count"],
        ),
        command(
            "explore",
            "Walk up to count legs of go frontier. A leg that ends the way a walk ends on its own (done, new terrain, a corridor's end or branch, the step limit) is followed by the next; any other stop ends it, as do down stairs coming into view that the level had not shown and no frontier left.",
            json!({
                "count": {"type": "integer", "minimum": 1, "maximum": LEGS_MAX, "description": "Legs to walk at most."},
                "near": near,
            }),
            &["count"],
        ),
        command(
            "fight",
            "Attack toward direction with F up to count times while the monster shown there at the start is still shown there. Stops once HP is at or below hp, a prompt opens, a condition changes, or the hero is engulfed or released; while engulfed it attacks the engulfer until released. Makes no attack with HP already at or below hp or with no monster shown there.",
            json!({
                "direction": {"type": "string", "enum": directions, "description": "h west, j south, k north, l east, y north-west, u north-east, b south-west, n south-east."},
                "count": {"type": "integer", "minimum": 1, "maximum": SWINGS_MAX, "description": "Attacks to make at most."},
                "hp": {"type": "integer", "minimum": 0, "maximum": HP_MAX, "description": "Stop once HP is at or below this."},
            }),
            &["direction", "count", "hp"],
        ),
        command(
            "learn",
            "Tell the harness about squares of the current level it has not observed, so routes may cross them. Sends nothing to the game.",
            json!({
                "rows": map_rows("The map's rows, row 1 first: each square's map symbol, a space where nothing is told."),
                "colours": map_rows("Under each told square, the game's colour number as one hex digit, 0 to f."),
            }),
            &["rows", "colours"],
        ),
        command("stop", "Stop the session's daemon.", json!({}), &[]),
    ];
    json!({
        "request": "A request is a JSON object: cmd, the command's name, and its input. nethack-harness request JSON sends one.",
        "codes": {
            "0": "done",
            "1": "refused or failed; the text says why",
            "2": "a bounded action stopped short; the report says why",
            "3": "the game is over; facts.end holds the end messages",
            "4": "the terminal is closed or unreachable",
            "5": "the game has not started taking input yet",
            "64": "the request was malformed",
        },
        "commands": commands,
    })
}

fn command(name: &str, description: &str, properties: Value, required: &[&str]) -> Value {
    json!({
        "name": name,
        "description": description,
        "input": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": false,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::Cmd;

    /// Every command the daemon takes has a schema whose properties are exactly the fields its request check takes.
    #[test]
    fn every_command_is_described_with_the_fields_it_takes() {
        let schema = schema();
        let commands = schema["commands"].as_array().unwrap();
        let all = [
            Cmd::Look,
            Cmd::Send,
            Cmd::Go,
            Cmd::Rest,
            Cmd::Explore,
            Cmd::Fight,
            Cmd::Learn,
            Cmd::Stop,
        ];
        for cmd in all {
            // A new command fails to compile here until it is listed above.
            match cmd {
                Cmd::Look
                | Cmd::Send
                | Cmd::Go
                | Cmd::Rest
                | Cmd::Explore
                | Cmd::Fight
                | Cmd::Learn
                | Cmd::Stop => {}
            }
            let described = commands
                .iter()
                .find(|c| c["name"] == cmd.name())
                .unwrap_or_else(|| panic!("{} has no schema", cmd.name()));
            let input = &described["input"];
            let mut properties: Vec<&str> = input["properties"]
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect();
            properties.sort_unstable();
            let mut takes = cmd.takes().to_vec();
            takes.sort_unstable();
            assert_eq!(properties, takes, "{}", cmd.name());
            for required in input["required"].as_array().unwrap() {
                assert!(
                    takes.contains(&required.as_str().unwrap()),
                    "{}",
                    cmd.name()
                );
            }
            assert_eq!(input["additionalProperties"], false);
        }
        assert_eq!(commands.len(), all.len());
        let fight = commands.iter().find(|c| c["name"] == "fight").unwrap();
        assert_eq!(fight["input"]["properties"]["count"]["maximum"], SWINGS_MAX);
        assert_eq!(fight["input"]["properties"]["hp"]["maximum"], HP_MAX);
        let explore = commands.iter().find(|c| c["name"] == "explore").unwrap();
        assert_eq!(explore["input"]["properties"]["count"]["maximum"], LEGS_MAX);
    }
}
