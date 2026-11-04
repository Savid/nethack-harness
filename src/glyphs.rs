//! Decoding what the tty draws: map symbols, colours, status-line conditions and the game's description of a trap
//! underfoot.

use crate::types::Colour;

/// A cell's base colour as the game chose it, before the terminal's bold or bright rendering. Brown is drawn
/// bright as yellow; white is the default colour drawn bold, or SGR white; gray is the default colour.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Base {
    Black,
    Red,
    Green,
    Brown,
    Blue,
    Magenta,
    Cyan,
    White,
    Gray,
}

/// A cell's colour as the game chose it: a base colour and whether it is the bright variant.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct Shade {
    pub base: Base,
    pub bright: bool,
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

/// Whether the symbol is an object class symbol.
pub fn is_item(ch: char) -> bool {
    ")[%?/=!(*\"$".contains(ch)
}

/// Whether the symbol is a monster class symbol, the hero's `@` included.
pub fn is_monster(ch: char) -> bool {
    // `I` marks a remembered unseen monster, not a monster class.
    (ch.is_ascii_alphabetic() && ch != 'I') || matches!(ch, '@' | '&' | '\'' | ';' | ':' | '~')
}

/// Whether the symbol is a warning level: a digit `1`–`5`, or a `0` in bright white. A boulder is also drawn `0`,
/// in any other colour.
pub fn is_warning(ch: char, shade: Shade) -> bool {
    matches!(ch, '1'..='5') || ch == '0' && shade.base == Base::White
}

/// Whether a `` ` `` is a floor engraving rather than a boulder: the game draws an engraving on room floor in bright
/// blue and a boulder in any other colour.
pub fn is_floor_engraving(ch: char, shade: Shade) -> bool {
    ch == '`' && shade == Shade::BRIGHT_BLUE
}

/// Whether a `#` is a corridor engraving, which the game draws in bright blue.
pub fn is_corridor_engraving(ch: char, shade: Shade) -> bool {
    ch == '#' && shade == Shade::BRIGHT_BLUE
}

/// The name of a map feature symbol.
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

/// The liquid a `}` is, from its colour.
pub fn liquid_name(base: Base) -> Option<&'static str> {
    match base {
        Base::Red => Some("lava"),
        Base::Blue => Some("water"),
        _ => None,
    }
}

/// The status-line conditions in status-line order, each as the words the status line may show for it, full name
/// first. A condition's bit in `Conditions` is its index here.
#[rustfmt::skip]
const CONDITIONS: [&[&str]; 30] = [
    &["Hungry", "Hngry"], &["Weak", "Wk"], &["Fainting", "Fnt"], &["Fainted"], &["Satiated", "Sat"],
    &["Burdened", "Brd"], &["Stressed", "Ssd"], &["Strained", "Snd"], &["Overtaxed", "Otd"],
    &["Overloaded", "Old"], &["Blind", "Blnd"], &["Conf", "Cnf"], &["Stun", "Stn"], &["Hallu", "Hal"],
    &["FoodPois", "Fpois"], &["TermIll", "Ill"], &["Stone", "Ston"], &["Slime", "Slim"],
    &["Strngl", "Stngl"], &["Lev"], &["Fly"], &["Ride"], &["Deaf"], &["Held", "Hld", "Grab"],
    &["Trapped", "Trap"], &["Parlyz"], &["Zzz"], &["InLava"], &["Sliding"], &["Unconsc"],
];
/// The index of `Blind` in `CONDITIONS`.
pub const BLIND: usize = 10;

/// A set of status-line conditions, by their index in `CONDITIONS`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Conditions(pub u32);

impl Conditions {
    pub fn contains(self, index: usize) -> bool {
        self.0 & (1 << index) != 0
    }

    /// The hunger state shown, if any: the first five conditions are `Hungry` to `Satiated`.
    pub fn hunger(self) -> Option<&'static str> {
        (0..5).find(|&i| self.contains(i)).map(|i| CONDITIONS[i][0])
    }
}

/// The conditions named as whole words in `text`.
pub fn conditions(text: &str) -> Conditions {
    let mut found = Conditions::default();
    for word in text.split(|c: char| !is_word(c)).filter(|w| !w.is_empty()) {
        if let Some(i) = CONDITIONS.iter().position(|forms| forms.contains(&word)) {
            found.0 |= 1 << i;
        }
    }
    found
}

/// What the game calls the trap a `There is a … here` sentence names, by its last word or words.
#[rustfmt::skip]
const TRAP_NOUNS: [&str; 7] = ["trap", "trap door", "hole", "web", "pit", "squeaky board", "magic portal"];

/// The trap the game describes at the hero's square: the first `There is a(n) <words> <trap noun> here` in
/// `message`, as `:` reads it out. A trap going off is not one: the game marks a trap the hero sets off as seen and
/// draws it once the hero steps off, and the same announcements report traps that monsters set off elsewhere.
pub fn trap_kind(message: &str) -> Option<&str> {
    message.match_indices("There is a").find_map(|(at, lead)| {
        let rest = &message[at + lead.len()..];
        trap_phrase(rest.strip_prefix("n ").or_else(|| rest.strip_prefix(' '))?)
    })
}

/// Whether a sentence is one of the game's engraving read-outs: `Something is written here in the dust.`, `You
/// read: "…".` and the like.
pub fn is_engraving_text(sentence: &str) -> bool {
    #[rustfmt::skip]
    const PREFIXES: [&str; 7] = [
        "You read: ", "You feel the words: ", "Something is written here", "Something is engraved here",
        "Some text has been ", "There's some graffiti ", "You see a message scrawled ",
    ];
    let sentence = sentence.trim_start();
    PREFIXES.iter().any(|p| sentence.starts_with(p))
}

/// A regular-expression word character.
pub fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Where `word` first occurs in `text` as a whole word: no word character just before or just after it.
pub fn find_word(text: &str, word: &str) -> Option<usize> {
    text.match_indices(word).map(|(i, _)| i).find(|&i| {
        !text[..i].chars().next_back().is_some_and(is_word)
            && !text[i + word.len()..].chars().next().is_some_and(is_word)
    })
}

/// Whether ASCII `needle` occurs in `hay` without regard to ASCII case.
pub fn contains_ignore_case(hay: &str, needle: &str) -> bool {
    let (h, n) = (hay.as_bytes(), needle.as_bytes());
    !n.is_empty() && h.windows(n.len()).any(|w| w.eq_ignore_ascii_case(n))
}

/// The shortest run of `[\w-]+ ` words then a trap noun that starts `text` and is followed by ` here`.
fn trap_phrase(text: &str) -> Option<&str> {
    let mut pos = 0;
    loop {
        let rest = &text[pos..];
        for noun in TRAP_NOUNS {
            if rest.starts_with(noun) && rest[noun.len()..].starts_with(" here") {
                return Some(&text[..pos + noun.len()]);
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
    fn a_zero_is_a_warning_only_in_bright_white() {
        assert!(is_warning('0', shade(Colour::Default, true)));
        assert!(is_warning('0', shade(Colour::White, false)));
        assert!(is_warning('0', shade(Colour::BrightWhite, false)));
        assert!(!is_warning('0', shade(Colour::Default, false)));
        assert!(!is_warning('0', shade(Colour::Brown, false)));
    }

    #[test]
    fn symbol_classes() {
        assert!(is_item('$') && is_item('"') && !is_item('`') && !is_item('0'));
        assert!(is_monster('d') && is_monster('@') && is_monster('~') && !is_monster('I'));
        assert!(is_warning('1', Shade::GRAY) && is_warning('5', Shade::GRAY));
        assert!(!is_warning('6', Shade::GRAY));
        assert_eq!(feature_name('\\'), Some("throne"));
        assert_eq!(feature_name('#'), None);
        assert_eq!(liquid_name(Base::Red), Some("lava"));
        assert_eq!(liquid_name(Base::Green), None);
    }

    #[test]
    fn conditions_match_whole_words_only() {
        let found = conditions("Dlvl:3 $:0 HP:12(16) T:400 Hngry Blind Conf");
        assert_eq!(found, conditions("Hungry Blind Conf"));
        assert!(found.contains(BLIND));
        assert_eq!(found.hunger(), Some("Hungry"));
        assert_eq!(
            conditions("Confused Blindfold Trapdoor"),
            Conditions::default()
        );
        assert_eq!(conditions("Lev Trap"), conditions("Trapped Lev"));
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
    fn a_trap_going_off_names_no_trap() {
        for event in [
            "A gush of water hits the gold golem!",
            "Kaablamm!  You hear an explosion in the distance!",
            "KAABLAMM!!!  You triggered a land mine!",
            "A little dart shoots out at you!  You are hit by a little dart.",
            "You fall into a pit!",
            "The bear trap closes on your foot!",
            "You hit the jackal.",
        ] {
            assert_eq!(trap_kind(event), None, "{event}");
        }
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
