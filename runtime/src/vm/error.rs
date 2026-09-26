//! `MahRuntimeError` (docs/MAHC_FORMAT.md #6.8) -- a `.mahc` program's own
//! runtime error, distinct from `crate::decode::FormatError` (a malformed
//! FILE). `located` mirrors `mah/runtime_values.py`'s `MahRuntimeError`:
//! set once the step loop has appended an `at position ...` suffix (or
//! decided no location is available), so a nested step loop's error
//! (`invoke_sync`, a native `to_string`, `detach`) gets exactly one
//! location suffix, not one per step loop it unwinds through.

use std::fmt;

#[derive(Debug, Clone)]
pub struct RuntimeError {
    pub message: String,
    pub located: bool,
}

impl RuntimeError {
    pub fn new(msg: impl Into<String>) -> Self {
        RuntimeError { message: msg.into(), located: false }
    }
}

impl fmt::Display for RuntimeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for RuntimeError {}

pub type RResult<T> = Result<T, RuntimeError>;
