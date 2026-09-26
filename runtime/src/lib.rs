//! The Mah VM in Rust: a port of `mah/code_interpreter.py` (plus
//! `mah/bytecode/decode.py` and `mah/natives.py`) that must behave
//! identically -- same stdout, same error messages, same exit codes.
//! docs/MAHC_FORMAT.md is the normative spec; the Python VM is the
//! reference implementation.

pub mod bigint;
pub mod bundle;
pub mod decimal;
pub mod decode;
pub mod vm;
