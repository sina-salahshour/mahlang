//! `MahRuntimeError` (docs/MAHC_FORMAT.md #6.8) -- a `.mahc` program's own
//! runtime error, distinct from `crate::decode::FormatError` (a malformed
//! FILE). `located` mirrors `mah/runtime_values.py`'s `MahRuntimeError`:
//! set once the step loop has appended an `at position ...` suffix (or
//! decided no location is available), so a nested step loop's error
//! (`invoke_sync`, a native `to_string`, `detach`) gets exactly one
//! location suffix, not one per step loop it unwinds through.
//!
//! M25 (docs/ERRORS.md, docs/MAHC_FORMAT.md #4.4/#4.5): `kind` says which
//! `RuntimeError` enum variant this becomes once thrown as a Mah value;
//! `thrown` is this type's `MahThrow` equivalent -- a Mah *value* in
//! flight across a Rust function-call boundary (an explicit `throw`, a
//! Failed `.await`, or a failed sub-task's error propagating out of
//! `invoke_sync`) rather than a host-level failure description. When
//! `thrown` is `Some`, `step_task`'s own unwinding uses that value
//! directly instead of building a fresh one from `kind`/`message`.

use std::fmt;

use super::value::{demangle_text, Value};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    DivisionByZero,
    TypeMismatch,
    NoSuchField,
    NoSuchMethod,
    ArgumentError,
    IndexOutOfRange,
    MatchFailed,
    InputError,
    Internal,
}

impl ErrorKind {
    /// The `RuntimeError` variant name, exactly as `decode::RUNTIME_ERROR_VARIANTS`
    /// spells it (docs/MAHC_FORMAT.md #4.1).
    pub fn variant_name(self) -> &'static str {
        match self {
            ErrorKind::DivisionByZero => "DivisionByZero",
            ErrorKind::TypeMismatch => "TypeMismatch",
            ErrorKind::NoSuchField => "NoSuchField",
            ErrorKind::NoSuchMethod => "NoSuchMethod",
            ErrorKind::ArgumentError => "ArgumentError",
            ErrorKind::IndexOutOfRange => "IndexOutOfRange",
            ErrorKind::MatchFailed => "MatchFailed",
            ErrorKind::InputError => "InputError",
            ErrorKind::Internal => "Internal",
        }
    }
}

#[derive(Clone)]
pub struct RuntimeError {
    pub message: String,
    pub located: bool,
    pub kind: ErrorKind,
    /// M25: see this module's docstring.
    pub thrown: Option<Value>,
}

impl RuntimeError {
    pub fn new(msg: impl Into<String>) -> Self {
        RuntimeError { message: demangle_text(msg.into()), located: false, kind: ErrorKind::Internal, thrown: None }
    }

    /// Same as `new`, with an explicit M25 classification (docs/MAHC_FORMAT.md
    /// #4.5) -- every raise site that has one should use this instead of
    /// leaving the default `Internal`.
    pub fn with_kind(msg: impl Into<String>, kind: ErrorKind) -> Self {
        RuntimeError { message: demangle_text(msg.into()), located: false, kind, thrown: None }
    }

    /// M25: a Mah *value* being thrown across a Rust boundary -- `exec.rs`'s
    /// `throw` handler, `.await` of a Failed Promise, and `invoke_sync`
    /// when the sub-task it drove failed. Always caught again by the
    /// nearest enclosing `step_task` loop; never reported directly (see
    /// `RuntimeError`'s own docstring).
    pub fn thrown_value(value: Value) -> Self {
        RuntimeError { message: String::new(), located: false, kind: ErrorKind::Internal, thrown: Some(value) }
    }
}

impl fmt::Display for RuntimeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.message)
    }
}

impl fmt::Debug for RuntimeError {
    // Manual impl: `Value` (in `thrown`) has no `Debug` (matching the
    // Python VM's own Mah values, which have no useful `repr()` either) --
    // this deliberately omits it rather than requiring one everywhere.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RuntimeError")
            .field("message", &self.message)
            .field("located", &self.located)
            .field("kind", &self.kind)
            .field("thrown", &self.thrown.is_some())
            .finish()
    }
}

impl std::error::Error for RuntimeError {}

pub type RResult<T> = Result<T, RuntimeError>;
