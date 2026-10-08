//! The Mah VM -- a Rust port of `mah/code_interpreter.py` (plus
//! `mah/bytecode/decode.py` and `mah/natives.py`) that must behave
//! identically: same stdout, same error messages, same exit codes. See
//! `docs/MAHC_FORMAT.md` for the normative spec this implements.
//!
//! Two public entry points, mirroring the Python module: [`run_bytes`]
//! (decode + link + run) and [`run_program`] (an already-decoded
//! `crate::decode::Program`).

mod bytes;
mod error;
mod exec;
mod fs;
mod link;
mod methods;
mod natives;
mod process;
mod reflect;
mod socket;
mod thread;
mod value;

pub use error::RuntimeError;
pub use exec::TestOutcome;
pub use link::is_known_native;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use crate::decode::{self, FormatError};

/// The native stack of every thread that runs a VM -- the main VM's thread
/// (`main.rs`) and every std:thread worker (M44): nested nearly as deep as
/// `code_interpreter.py`'s Python call stack can get (every Mah call frame is
/// a few nested Rust calls: `step_task` -> `exec_one` ->
/// `enter_closure`/`invoke_sync`/`spawn_detached` -> `step_task` again for a
/// detached/`to_string` call). 1 GiB keeps realistic recursive Mah programs
/// from overflowing the native stack; it is reserved virtual memory, not
/// committed.
pub const VM_STACK_SIZE: usize = 1 << 30;

/// M44 (docs/contracts/M44_threads.md #6.3): set by the first way out of the
/// process, so only one thread ever runs `std::process::exit`.
static EXITING: AtomicBool = AtomicBool::new(false);

/// Claim the process exit: `true` for the first caller only.
pub fn claim_exit() -> bool {
    !EXITING.swap(true, Ordering::SeqCst)
}

/// End the process with `code` -- the first caller wins; every later one
/// (from any thread) parks forever, so `exit` never runs twice at once.
pub fn exit_process(code: i32) -> ! {
    if !claim_exit() {
        loop {
            std::thread::park();
        }
    }
    std::process::exit(code)
}

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
/// and run it. M36: `args` are the program's arguments (`process.args()`).
pub fn run_bytes(data: &[u8], args: &[String]) -> Result<(), VmError> {
    let program = Arc::new(decode::decode(data)?);
    run_arc(program, args)
}

/// M28 (docs/MAHC_FORMAT.md #6.10): run test number `index` of a `mah
/// test` build -- its top-level declarations, then that test -- and
/// describe how it ended. Only a bad file or index is an `Err`.
pub fn run_test_bytes(data: &[u8], index: usize) -> Result<exec::TestOutcome, VmError> {
    let program = decode::decode(data)?;
    let Some(entry) = program.tests.get(index) else {
        return Err(VmError::Format(format!("no test number {index} (the file has {})", program.tests.len())));
    };
    let slot = entry.slot;
    let program = Arc::new(program);
    let linked = link::link(&program)?;
    link::check_modes(&program)?;
    Ok(exec::execute_test(program.clone(), &linked, slot)?)
}

/// Link and run an already-decoded `Program`.
pub fn run_program(program: &decode::Program, args: &[String]) -> Result<(), VmError> {
    run_arc(Arc::new(program.clone()), args)
}

/// M44: the program is shared (`Arc`) with every worker thread of the run,
/// each of which links it once.
fn run_arc(program: Arc<decode::Program>, args: &[String]) -> Result<(), VmError> {
    let linked = link::link(&program)?; // raises a format error for an unsupported native, before anything runs
    link::check_modes(&program)?;
    exec::execute(program.clone(), &linked, args)?;
    Ok(())
}
