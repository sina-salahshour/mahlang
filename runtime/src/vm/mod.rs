//! The Mah VM -- a Rust port of `mah/code_interpreter.py` (plus
//! `mah/bytecode/decode.py` and `mah/natives.py`) that must behave
//! identically: same stdout, same error messages, same exit codes. See
//! `docs/MAHC_FORMAT.md` for the normative spec this implements.
//!
//! Two public entry points, mirroring the Python module: [`run_bytes`]
//! (decode + link + run) and [`run_program`] (an already-decoded
//! `crate::decode::Program`).

mod error;
mod exec;
mod link;
mod methods;
mod natives;
mod value;

pub use error::RuntimeError;

use crate::decode::{self, FormatError};

/// Either category of failure the CLI must tell apart: a malformed/
/// unsupported `.mahc` file (exit 2) or a Mah program's own runtime error
/// (exit 1) -- see `docs/MAHC_FORMAT.md` #6.8 and the CLI spec.
#[derive(Debug)]
pub enum VmError {
    Format(String),
    Runtime(String),
}

impl From<FormatError> for VmError {
    fn from(e: FormatError) -> Self {
        VmError::Format(e.0)
    }
}

impl From<RuntimeError> for VmError {
    fn from(e: RuntimeError) -> Self {
        VmError::Runtime(e.message)
    }
}

/// Decode `data` as a `.mahc` file (including a possible leading shebang)
/// and run it.
pub fn run_bytes(data: &[u8]) -> Result<(), VmError> {
    let program = decode::decode(data)?;
    run_program(&program)
}

/// Link and run an already-decoded `Program`.
pub fn run_program(program: &decode::Program) -> Result<(), VmError> {
    let linked = link::link(program)?; // raises a format error for an unsupported native, before anything runs
    exec::execute(&linked)?;
    Ok(())
}
