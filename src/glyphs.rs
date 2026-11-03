//! Decoding what the tty draws: map symbols, colours, status-line conditions and the game's trap messages.

use crate::types::Colour;

/// A cell's base colour as the game chose it, before the terminal's bold or bright rendering.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Base {
    /// Black.
    Black,
    /// Red.
    Red,
    /// Green.
    Green,
    /// Brown, drawn bright as yellow.
    Brown,
    /// Blue.
    Blue,
    /// Magenta.
    Magenta,
    /// Cyan.
    Cyan,
    /// White: the default colour drawn bold, or SGR white.
    White,
    /// Gray: the default colour.
    Gray,
}

/// A cell's colour as the game chose it: a base colour and whether it is the bright variant.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Shade {
    /// Base colour.
    pub base: Base,
    /// Bright variant.
    pub bright: bool,
}

impl Base {
    /// The colour's name, such as `blue` or `gray`.
    pub fn name(self) -> &'static str {
        match self {
            Base::Black => "black",
            Base::Red => "red",
            Base::Green => "green",
            Base::Brown => "brown",
            Base::Blue => "blue",
            Base::Magenta => "magenta",
            Base::Cyan => "cyan",
            Base::White => "white",
            Base::Gray => "gray",
        }
    }
}

impl Shade {
    /// The bright blue the game draws engravings in.
    pub const BRIGHT_BLUE: Shade = Shade {
        base: Base::Blue,
        bright: true,
    };

    /// The shade of an off-map or blank cell.
    pub const GRAY: Shade = Shade {
        base: Base::Gray,
        bright: false,
    };
}

/// The shade the game drew, from what the emulator stored: bold plus SGR 3X, or a bright SGR 9X colour.
pub fn shade(fg: Colour, bold: bool) -> Shade {
    let (base, bright) = match fg {
        Colour::Default if bold => (Base::White, true),
        Colour::Default => (Base::Gray, false),
        Colour::White | Colour::BrightWhite => (Base::White, true),
        Colour::Black => (Base::Black, bold),
        Colour::Red => (Base::Red, bold),
        Colour::Green => (Base::Green, bold),
        Colour::Brown => (Base::Brown, bold),
        Colour::Blue => (Base::Blue, bold),
        Colour::Magenta => (Base::Magenta, bold),
        Colour::Cyan => (Base::Cyan, bold),
        Colour::BrightBlack => (Base::Black, true),
        Colour::BrightRed => (Base::Red, true),
        Colour::BrightGreen => (Base::Green, true),
        Colour::BrightBrown => (Base::Brown, true),
        Colour::BrightBlue => (Base::Blue, true),
        Colour::BrightMagenta => (Base::Magenta, true),
        Colour::BrightCyan => (Base::Cyan, true),
    };
    Shade { base, bright }
}

/// The direction name for a direction key, such as `west` for `h`.
pub fn direction_name(key: u8) -> Option<&'static str> {
    Some(match key {
        b'h' => "west",
        b'j' => "south",
        b'k' => "north",
        b'l' => "east",
        b'y' => "northwest",
        b'u' => "northeast",
        b'b' => "southwest",
        b'n' => "southeast",
        _ => return None,
    })
}

/// Whether the symbol is an object class symbol: `) [ % ? / = ! ( * " $`.
pub fn is_item(ch: char) -> bool {
    matches!(
        ch,
        ')' | '[' | '%' | '?' | '/' | '=' | '!' | '(' | '*' | '"' | '$'
    )
}

/// Whether the symbol is a monster class symbol, the hero's `@` included.
pub fn is_monster(ch: char) -> bool {
    // `I` marks a remembered unseen monster, not a monster class.
    (ch.is_ascii_alphabetic() && ch != 'I') || matches!(ch, '@' | '&' | '\'' | ';' | ':' | '~')
}

/// Whether the symbol is a warning level digit `1`–`5`.
pub fn is_warning(ch: char) -> bool {
    matches!(ch, '1'..='5')
}

/// Whether a `` ` `` is a floor engraving rather than a boulder: the game draws an engraving on room floor in bright
/// blue and a boulder in any other colour.
pub fn is_floor_engraving(ch: char, shade: Shade) -> bool {
    ch == '`' && shade == Shade::BRIGHT_BLUE
}

/// Whether a `#` is a corridor engraving: the game draws an engraving in a corridor as a bright blue `#`.
pub fn is_corridor_engraving(ch: char, shade: Shade) -> bool {
    ch == '#' && shade == Shade::BRIGHT_BLUE
}

/// The name of a map feature symbol: `<` up stairs, `>` down stairs, `^` trap, `_` altar, `{` fountain,
/// `}` water or lava, `\` throne.
pub fn feature_name(ch: char) -> Option<&'static str> {
    Some(match ch {
        '<' => "up stairs",
        '>' => "down stairs",
        '^' => "trap",
        '_' => "altar",
        '{' => "fountain",
        '}' => "water or lava",
        '\\' => "throne",
        _ => return None,
    })
}

/// The liquid a `}` is, from its colour: red is lava, blue is water.
pub fn liquid_name(base: Base) -> Option<&'static str> {
    match base {
        Base::Red => Some("lava"),
        Base::Blue => Some("water"),
        _ => None,
    }
}

/// A status-line condition.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Condition {
    /// Hungry.
    Hungry,
    /// Weak.
    Weak,
    /// Fainting.
    Fainting,
    /// Fainted.
    Fainted,
    /// Satiated.
    Satiated,
    /// Burdened.
    Burdened,
    /// Stressed.
    Stressed,
    /// Strained.
    Strained,
    /// Overtaxed.
    Overtaxed,
    /// Overloaded.
    Overloaded,
    /// Blind.
    Blind,
    /// Confused.
    Conf,
    /// Stunned.
    Stun,
    /// Hallucinating.
    Hallu,
    /// Food poisoning.
    FoodPois,
    /// Terminally ill.
    TermIll,
    /// Turning to stone.
    Stone,
    /// Turning to slime.
    Slime,
    /// Being strangled.
    Strngl,
    /// Levitating.
    Lev,
    /// Flying.
    Fly,
    /// Riding.
    Ride,
    /// Deaf.
    Deaf,
    /// Held by a monster.
    Held,
    /// Trapped.
    Trapped,
    /// Paralysed.
    Parlyz,
    /// Asleep.
    Zzz,
    /// In lava.
    InLava,
    /// Sliding.
    Sliding,
    /// Unconscious.
    Unconsc,
}

/// Every condition, in status-line order.
pub const CONDITIONS: [Condition; 30] = [
    Condition::Hungry,
    Condition::Weak,
    Condition::Fainting,
    Condition::Fainted,
    Condition::Satiated,
    Condition::Burdened,
    Condition::Stressed,
    Condition::Strained,
    Condition::Overtaxed,
    Condition::Overloaded,
    Condition::Blind,
    Condition::Conf,
    Condition::Stun,
    Condition::Hallu,
    Condition::FoodPois,
    Condition::TermIll,
    Condition::Stone,
    Condition::Slime,
    Condition::Strngl,
    Condition::Lev,
    Condition::Fly,
    Condition::Ride,
    Condition::Deaf,
    Condition::Held,
    Condition::Trapped,
    Condition::Parlyz,
    Condition::Zzz,
    Condition::InLava,
    Condition::Sliding,
    Condition::Unconsc,
];

impl Condition {
    /// The condition's full name, such as `Hungry` or `Conf`.
    pub fn name(self) -> &'static str {
        self.forms()[0]
    }

    /// The words the status line may show for it, full and abbreviated, such as `Hungry` and `Hngry`.
    pub fn forms(self) -> &'static [&'static str] {
        match self {
            Condition::Hungry => &["Hungry", "Hngry"],
            Condition::Weak => &["Weak", "Wk"],
            Condition::Fainting => &["Fainting", "Fnt"],
            Condition::Fainted => &["Fainted"],
            Condition::Satiated => &["Satiated", "Sat"],
            Condition::Burdened => &["Burdened", "Brd"],
            Condition::Stressed => &["Stressed", "Ssd"],
            Condition::Strained => &["Strained", "Snd"],
            Condition::Overtaxed => &["Overtaxed", "Otd"],
            Condition::Overloaded => &["Overloaded", "Old"],
            Condition::Blind => &["Blind", "Blnd"],
            Condition::Conf => &["Conf", "Cnf"],
            Condition::Stun => &["Stun", "Stn"],
            Condition::Hallu => &["Hallu", "Hal"],
            Condition::FoodPois => &["FoodPois", "Fpois"],
            Condition::TermIll => &["TermIll", "Ill"],
            Condition::Stone => &["Stone", "Ston"],
            Condition::Slime => &["Slime", "Slim"],
            Condition::Strngl => &["Strngl", "Stngl"],
            Condition::Lev => &["Lev"],
            Condition::Fly => &["Fly"],
            Condition::Ride => &["Ride"],
            Condition::Deaf => &["Deaf"],
            Condition::Held => &["Held", "Hld", "Grab"],
            Condition::Trapped => &["Trapped", "Trap"],
            Condition::Parlyz => &["Parlyz"],
            Condition::Zzz => &["Zzz"],
            Condition::InLava => &["InLava"],
            Condition::Sliding => &["Sliding"],
            Condition::Unconsc => &["Unconsc"],
        }
    }

    /// Whether it is a hunger state.
    pub fn is_hunger(self) -> bool {
        matches!(
            self,
            Condition::Hungry
                | Condition::Weak
                | Condition::Fainting
                | Condition::Fainted
                | Condition::Satiated
        )
    }
}

/// A set of conditions.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Conditions(pub u32);

impl Conditions {
    /// Whether `c` is in the set.
    pub fn contains(self, c: Condition) -> bool {
        self.0 & (1 << c as u8) != 0
    }

    /// Adds `c`.
    pub fn insert(&mut self, c: Condition) {
        self.0 |= 1 << c as u8;
    }

    /// The conditions in the set, in `CONDITIONS` order.
    pub fn iter(self) -> impl Iterator<Item = Condition> {
        CONDITIONS.into_iter().filter(move |&c| self.contains(c))
    }

    /// The hunger state shown, if any.
    pub fn hunger(self) -> Option<Condition> {
        self.iter().find(|c| c.is_hunger())
    }
}

/// The conditions named as whole words in `text`.
pub fn conditions(text: &str) -> Conditions {
    let mut found = Conditions::default();
    for word in text.split(|c: char| !is_word(c)).filter(|w| !w.is_empty()) {
        for c in CONDITIONS {
            if c.forms().contains(&word) {
                found.insert(c);
            }
        }
    }
    found
}

/// What the game calls the trap a `There is a … here` sentence names, by its last word or words.
const TRAP_NOUNS: [&str; 7] = [
    "trap",
    "trap door",
    "hole",
    "web",
    "pit",
    "squeaky board",
    "magic portal",
];

/// The game's announcements of traps that leave the hero where they triggered, matched without regard to case at
/// the start of a word, with the trap each one names.
const TRAP_EVENTS: [(&[&str], &str); 11] = [
    (&["dart shoots out at you"], "dart trap"),
    (&["arrow shoots out at you"], "arrow trap"),
    (&["bear trap closes on"], "bear trap"),
    (
        &[
            "you fall into a pit",
            "you fall into a spiked pit",
            "you fall in a pit",
            "you fall in a spiked pit",
            "you land into a pit",
            "you land into a spiked pit",
            "you land in a pit",
            "you land in a spiked pit",
        ],
        "pit",
    ),
    (&["sharp iron spikes"], "spiked pit"),
    (&["board beneath you squeaks"], "squeaky board"),
    (&["rock falls on your head"], "falling rock trap"),
    (&["cloud of gas"], "sleeping gas trap"),
    (&["gush of water hits"], "rust trap"),
    (
        &[
            "stumble into a web",
            "stumble into a spider web",
            "caught in a web",
            "caught in a spider web",
        ],
        "web",
    ),
    (&["kaablamm"], "land mine"),
];

/// The trap a message reports at the hero's square: the game's `There is a … trap here` description, or the
/// announcement of a trap that leaves the hero where it triggered (`A dart shoots out at you!` is `dart trap`).
pub fn trap_kind(message: &str) -> Option<&str> {
    if let Some(kind) = trap_here(message) {
        return Some(kind);
    }
    TRAP_EVENTS
        .iter()
        .find(|(phrases, _)| phrases.iter().any(|p| find_word_ci(message, p)))
        .map(|&(_, kind)| kind)
}

/// Whether a sentence is one of the game's engraving read-outs: `Something is written here in the dust.`, `You
/// read: "…".` and the like.
pub fn is_engraving_text(sentence: &str) -> bool {
    const PREFIXES: [&str; 7] = [
        "You read: ",
        "You feel the words: ",
        "Something is written here",
        "Something is engraved here",
        "Some text has been ",
        "There's some graffiti ",
        "You see a message scrawled ",
    ];
    let sentence = sentence.trim_start();
    PREFIXES.iter().any(|p| sentence.starts_with(p))
}

/// A regular-expression word character.
fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The trap named by the first `There is a(n) <words> <trap noun> here` in `message`, taking the fewest words.
fn trap_here(message: &str) -> Option<&str> {
    const LEAD: &str = "There is a";
    let mut from = 0;
    while let Some(at) = message[from..].find(LEAD) {
        let after = from + at + LEAD.len();
        from = after;
        let rest = &message[after..];
        let start = if rest.starts_with("n ") {
            after + 2
        } else if rest.starts_with(' ') {
            after + 1
        } else {
            continue;
        };
        if let Some(end) = trap_phrase(&message[start..]) {
            return Some(&message[start..start + end]);
        }
    }
    None
}

/// The length of the shortest run of `[\w-]+ ` words then a trap noun followed by ` here` at the start of `text`.
fn trap_phrase(text: &str) -> Option<usize> {
    let mut pos = 0;
    loop {
        let rest = &text[pos..];
        for noun in TRAP_NOUNS {
            if rest
                .strip_prefix(noun)
                .is_some_and(|tail| tail.starts_with(" here"))
            {
                return Some(pos + noun.len());
            }
        }
        let word = rest
            .find(|c: char| !(is_word(c) || c == '-'))
            .unwrap_or(rest.len());
        if word == 0 || !rest[word..].starts_with(' ') {
            return None;
        }
        pos += word + 1;
    }
}

/// Whether ASCII `needle` occurs in `hay`, ignoring ASCII case, starting a word.
fn find_word_ci(hay: &str, needle: &str) -> bool {
    let h = hay.as_bytes();
    let n = needle.as_bytes();
    if n.is_empty() || h.len() < n.len() {
        return false;
    }
    (0..=h.len() - n.len()).any(|i| {
        h[i..i + n.len()].eq_ignore_ascii_case(n)
            && hay.is_char_boundary(i)
            && !hay[..i].chars().next_back().is_some_and(is_word)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shades_follow_bold_and_bright_colours() {
        assert_eq!(shade(Colour::Default, false), Shade::GRAY);
        assert_eq!(
            shade(Colour::Default, true),
            Shade {
                base: Base::White,
                bright: true
            }
        );
        assert_eq!(
            shade(Colour::White, false),
            Shade {
                base: Base::White,
                bright: true
            }
        );
        assert_eq!(shade(Colour::Blue, true), Shade::BRIGHT_BLUE);
        assert_eq!(shade(Colour::BrightBlue, false), Shade::BRIGHT_BLUE);
        assert_eq!(
            shade(Colour::Blue, false),
            Shade {
                base: Base::Blue,
                bright: false
            }
        );
        assert_eq!(
            shade(Colour::BrightBrown, false),
            Shade {
                base: Base::Brown,
                bright: true
            }
        );
    }

    #[test]
    fn a_backtick_is_an_engraving_only_in_bright_blue() {
        assert!(is_floor_engraving('`', shade(Colour::Blue, true)));
        assert!(is_floor_engraving('`', shade(Colour::BrightBlue, false)));
        assert!(!is_floor_engraving('`', shade(Colour::Default, false)));
        assert!(!is_floor_engraving('`', shade(Colour::Blue, false)));
        assert!(!is_floor_engraving('`', shade(Colour::White, false)));
        assert!(!is_floor_engraving('.', Shade::BRIGHT_BLUE));
        assert!(is_corridor_engraving('#', Shade::BRIGHT_BLUE));
        assert!(!is_corridor_engraving('#', Shade::GRAY));
    }

    #[test]
    fn symbol_classes() {
        assert!(is_item('$') && is_item('"') && !is_item('`') && !is_item('0'));
        assert!(is_monster('d') && is_monster('@') && is_monster('~') && !is_monster('I'));
        assert!(is_warning('1') && is_warning('5') && !is_warning('6') && !is_warning('0'));
        assert_eq!(feature_name('\\'), Some("throne"));
        assert_eq!(feature_name('#'), None);
        assert_eq!(liquid_name(Base::Red), Some("lava"));
        assert_eq!(liquid_name(Base::Green), None);
        assert_eq!(direction_name(b'y'), Some("northwest"));
        assert_eq!(direction_name(b'x'), None);
    }

    #[test]
    fn conditions_match_whole_words_only() {
        let found = conditions("Dlvl:3 $:0 HP:12(16) T:400 Hngry Blind Conf");
        let names: Vec<&str> = found.iter().map(Condition::name).collect();
        assert_eq!(names, ["Hungry", "Blind", "Conf"]);
        assert_eq!(found.hunger(), Some(Condition::Hungry));
        assert_eq!(
            conditions("Confused Blindfold Trapdoor"),
            Conditions::default()
        );
        assert!(conditions("Lev Trap").contains(Condition::Trapped));
    }

    #[test]
    fn trap_sentences_name_the_trap() {
        assert_eq!(trap_kind("There is a dart trap here."), Some("dart trap"));
        assert_eq!(
            trap_kind("There is an arrow trap here."),
            Some("arrow trap")
        );
        assert_eq!(trap_kind("There is a trap door here."), Some("trap door"));
        assert_eq!(
            trap_kind("There is a magic portal here."),
            Some("magic portal")
        );
        assert_eq!(
            trap_kind("You see here a dagger.  There is a spiked pit here."),
            Some("spiked pit")
        );
        assert_eq!(trap_kind("There is a staircase down here."), None);
        assert_eq!(trap_kind("There is a fountain here."), None);
    }

    #[test]
    fn trap_announcements_name_the_trap() {
        assert_eq!(
            trap_kind("A little dart shoots out at you!  You are hit by a little dart."),
            Some("dart trap")
        );
        assert_eq!(trap_kind("You fall into a pit!"), Some("pit"));
        assert_eq!(trap_kind("You land in a spiked pit."), Some("pit"));
        assert_eq!(
            trap_kind("KAABLAMM!!!  You triggered a land mine!"),
            Some("land mine")
        );
        assert_eq!(trap_kind("kaablamm"), Some("land mine"));
        assert_eq!(trap_kind("You stumble into a spider web."), Some("web"));
        assert_eq!(
            trap_kind("The bear trap closes on your foot!"),
            Some("bear trap")
        );
        assert_eq!(
            trap_kind("A narrow dart shoots out at you"),
            Some("dart trap")
        );
        assert_eq!(trap_kind("Asharp iron spikes"), None);
        assert_eq!(trap_kind("You hit the jackal."), None);
    }

    #[test]
    fn engraving_read_outs() {
        assert!(is_engraving_text("Something is written here in the dust."));
        assert!(is_engraving_text("You read: \"Elbereth\"."));
        assert!(is_engraving_text(
            "Some text has been burned into the floor here."
        ));
        assert!(!is_engraving_text("You see here a dagger."));
        assert!(!is_engraving_text("There is a staircase down here."));
    }
}
