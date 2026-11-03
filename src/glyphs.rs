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

impl Shade {
    /// The shade of an off-map or blank cell.
    pub const GRAY: Shade = Shade {
        base: Base::Gray,
        bright: false,
    };
}

/// The shade the game drew, from what the emulator stored: bold plus SGR 3X, or a bright SGR 9X colour.
pub fn shade(fg: Colour, bold: bool) -> Shade {
    todo!()
}

/// The direction name for a direction key, such as `west` for `h`.
pub fn direction_name(key: u8) -> Option<&'static str> {
    todo!()
}

/// Whether the symbol is an object class symbol: `) [ % ? / = ! ( * " $`.
pub fn is_item(ch: char) -> bool {
    todo!()
}

/// Whether the symbol is a monster class symbol, the hero's `@` included.
pub fn is_monster(ch: char) -> bool {
    todo!()
}

/// Whether the symbol is a warning level digit `1`–`5`.
pub fn is_warning(ch: char) -> bool {
    todo!()
}

/// Whether a `` ` `` is a floor engraving rather than a boulder: the game draws an engraving on room floor in bright
/// blue and a boulder in any other colour.
pub fn is_floor_engraving(ch: char, shade: Shade) -> bool {
    todo!()
}

/// The name of a map feature symbol: `<` up stairs, `>` down stairs, `^` trap, `_` altar, `{` fountain,
/// `}` water or lava, `\` throne.
pub fn feature_name(ch: char) -> Option<&'static str> {
    todo!()
}

/// The liquid a `}` is, from its colour: red is lava, blue is water.
pub fn liquid_name(base: Base) -> Option<&'static str> {
    todo!()
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
        todo!()
    }

    /// The words the status line may show for it, full and abbreviated, such as `Hungry` and `Hngry`.
    pub fn forms(self) -> &'static [&'static str] {
        todo!()
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
    todo!()
}

/// The trap a message reports at the hero's square: the game's `There is a … trap here` description, or the
/// announcement of a trap that leaves the hero where it triggered (`A dart shoots out at you!` is `dart trap`).
pub fn trap_kind(message: &str) -> Option<&str> {
    todo!()
}
