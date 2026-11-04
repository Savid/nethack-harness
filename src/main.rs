//! The `nethack-harness` binary.

fn main() -> std::process::ExitCode {
    let code = nethack_harness::cli::main();
    std::process::ExitCode::from(u8::try_from(code).unwrap_or(1))
}
