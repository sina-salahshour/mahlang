//! `mah-vm` -- the CLI for the native Rust Mah VM. Mirrors `mah runc`'s
//! behavior (see `mah/cli/main.py`): same stderr lines, same exit codes.

use std::io::Write;

use mah_vm::bundle;
use mah_vm::vm::{self, VmError};

/// Nested nearly as deep as `code_interpreter.py`'s Python call stack can
/// get (every Mah call frame is a few nested Rust calls: `step_task` ->
/// `exec_one` -> `enter_closure`/`invoke_sync`/`spawn_detached` ->
/// `step_task` again for a detached/`to_string` call). 1 GiB keeps
/// realistic recursive Mah programs from overflowing the native stack.
const STACK_SIZE: usize = 1 << 30;

fn usage() -> ! {
    eprintln!("usage: mah-vm --version | mah-vm run <path>");
    std::process::exit(2);
}

fn read_file_bytes(path: &str) -> Result<Vec<u8>, i32> {
    match std::fs::read(path) {
        Ok(data) => Ok(data),
        Err(_) => {
            eprintln!("Error: file not found '{path}'");
            Err(2)
        }
    }
}

/// `bundle::split` first, then `decode` (which itself skips a leading
/// shebang) -- see the spec's "Loading any file" note.
fn load_and_run(path: &str) -> i32 {
    let data = match read_file_bytes(path) {
        Ok(d) => d,
        Err(code) => return code,
    };
    let mahc: &[u8] = match bundle::split(&data) {
        Ok(Some((_info, slice))) => slice,
        Ok(None) => &data,
        Err(msg) => {
            eprintln!("error: invalid .mahc file: {msg}");
            return 2;
        }
    };
    match vm::run_bytes(mahc) {
        Ok(()) => 0,
        Err(VmError::Format(msg)) => {
            eprintln!("error: invalid .mahc file: {msg}");
            2
        }
        Err(VmError::Runtime(msg)) => {
            eprintln!("RuntimeError: {msg}");
            1
        }
    }
}

fn run_cli(args: Vec<String>) -> i32 {
    match args.as_slice() {
        [flag] if flag == "--version" => {
            println!("mah-vm {} ({}-{})", env!("CARGO_PKG_VERSION"), std::env::consts::ARCH, std::env::consts::OS);
            let _ = std::io::stdout().flush();
            0
        }
        [cmd, path] if cmd == "run" => load_and_run(path),
        _ => usage(),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = std::thread::Builder::new()
        .stack_size(STACK_SIZE)
        .spawn(move || run_cli(args))
        .expect("failed to spawn the VM thread")
        .join()
        .expect("the VM thread panicked");
    std::process::exit(code);
}
