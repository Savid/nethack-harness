//! A NetHack observation and execution adapter for a caller, human or program, driving one game over a terminal
//! socket. It decodes the screen, remembers what was observed, routes over observed geometry, runs bounded actions
//! and reports what happened.
#![forbid(unsafe_code)]
#![allow(unused_variables, dead_code, clippy::ptr_arg)]

pub mod cli;
pub mod daemon;
pub mod exec;
pub mod glyphs;
pub mod level;
pub mod link;
pub mod observe;
pub mod play;
pub mod screen;
pub mod types;
pub mod view;
pub mod vt;

#[cfg(test)]
pub mod testkit;
