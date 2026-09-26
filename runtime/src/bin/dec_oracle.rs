//! Differential-testing oracle for `mah_vm::decimal`. Reads lines of the
//! form `OP A [B]` from stdin (`B` absent for unary ops) and prints, for
//! each, either the `format()` of the result or `ERR <message()>` -- see
//! `runtime/tests/decimal_diff.py`, which compares this against Python's
//! `decimal` module run under the exact same context.
//!
//! OPs:
//!   add sub mul div idiv rem pow   -- binary, `A OP B`
//!   neg exp ln                     -- unary, `A`
//!   fmt                            -- parses `A` and formats it (identity
//!                                     check on `parse`/`format`)
//!   cmp                            -- prints -1/0/1
//!   f64                            -- prints `format(from_f64(to_f64(A)))`
//!   int                            -- prints `is_integer() to_i64()` (`to_i64`
//!                                     as `-` when `None`)

use mah_vm::decimal::{DecError, DecResult, Decimal};
use std::io::{self, BufRead, Write};

fn fmt_result(r: DecResult) -> String {
    match r {
        Ok(v) => v.format(),
        Err(e) => format!("ERR {}", e.message()),
    }
}

fn parse_or_die(s: &str) -> Decimal {
    match Decimal::parse(s) {
        Some(d) => d,
        None => {
            eprintln!("dec_oracle: cannot parse {:?}", s);
            std::process::exit(2);
        }
    }
}

fn err_msg(e: DecError) -> &'static str {
    e.message()
}

fn main() {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());

    for line in stdin.lock().lines() {
        let line = line.expect("read stdin");
        let line = line.trim_end();
        if line.is_empty() {
            continue;
        }
        let mut parts = line.splitn(3, ' ');
        let op = parts.next().unwrap_or("");
        let a_str = parts.next().unwrap_or("");
        let b_str = parts.next();

        let result_line = match op {
            "add" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.add(&b))
            }
            "sub" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.sub(&b))
            }
            "mul" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.mul(&b))
            }
            "div" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.div(&b))
            }
            "idiv" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.idiv(&b))
            }
            "rem" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.rem(&b))
            }
            "pow" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                fmt_result(a.pow(&b))
            }
            "neg" => {
                let a = parse_or_die(a_str);
                fmt_result(a.neg())
            }
            "exp" => {
                let a = parse_or_die(a_str);
                fmt_result(a.exp())
            }
            "ln" => {
                let a = parse_or_die(a_str);
                fmt_result(a.ln())
            }
            "fmt" => {
                let a = parse_or_die(a_str);
                a.format()
            }
            "cmp" => {
                let a = parse_or_die(a_str);
                let b = parse_or_die(b_str.unwrap());
                match a.cmp(&b) {
                    std::cmp::Ordering::Less => "-1".to_string(),
                    std::cmp::Ordering::Equal => "0".to_string(),
                    std::cmp::Ordering::Greater => "1".to_string(),
                }
            }
            "f64" => {
                let a = parse_or_die(a_str);
                let f = a.to_f64();
                match Decimal::from_f64(f) {
                    Ok(v) => v.format(),
                    Err(e) => format!("ERR {}", err_msg(e)),
                }
            }
            "int" => {
                let a = parse_or_die(a_str);
                let is_int = a.is_integer();
                let to_i = match a.to_i64() {
                    Some(v) => v.to_string(),
                    None => "-".to_string(),
                };
                format!("{} {}", is_int, to_i)
            }
            other => {
                eprintln!("dec_oracle: unknown op {:?}", other);
                std::process::exit(2);
            }
        };
        writeln!(out, "{}", result_line).expect("write stdout");
    }
}
