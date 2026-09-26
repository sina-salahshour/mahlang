//! Mah's `Number`: a base-10 floating-point decimal that reproduces the
//! results of Python's `decimal.Decimal` under the context the reference
//! VM runs in -- `prec=28, rounding=ROUND_HALF_EVEN, Emax=999999,
//! Emin=-999999` (so Etiny = Emin - prec + 1 = -1000026), with
//! InvalidOperation/DivisionByZero/Overflow trapped (-> `Err`).
//!
//! Only a value's *numeric value* is observable in Mah (printing goes
//! through `format`, which is value-only; equality and Map keys are
//! numeric), so unlike Python we don't track "ideal exponents": every
//! `Decimal` is kept in a canonical form (coefficient with no trailing
//! zeros; zero is always `+0` with exponent 0), which makes the derived
//! `PartialEq`/`Eq`/`Hash` numeric equality.

use crate::bigint::BigUint;
use std::cmp::Ordering;
use std::rc::Rc;

/// A trapped `decimal` condition. `message()` is exactly what Python's
/// `str(exc)` gives for the corresponding exception, since the Python VM
/// shows that text as the runtime error message.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecError {
    InvalidOperation,
    DivisionImpossible,
    DivisionByZero,
    Overflow,
}

impl DecError {
    pub fn message(self) -> &'static str {
        match self {
            DecError::InvalidOperation => "[<class 'decimal.InvalidOperation'>]",
            DecError::DivisionImpossible => "[<class 'decimal.DivisionImpossible'>]",
            DecError::DivisionByZero => "[<class 'decimal.DivisionByZero'>]",
            DecError::Overflow => "[<class 'decimal.Overflow'>]",
        }
    }
}

pub type DecResult = Result<Decimal, DecError>;

/// The context constants the reference VM runs under.
const PREC: usize = 28;
const EMAX: i64 = 999_999;
const EMIN: i64 = -999_999;
const ETINY: i64 = EMIN - PREC as i64 + 1; // -1_000_026

/// Small-integer fast path (`Small`) vs. arbitrary precision fallback
/// (`Big`). `Big` is only ever used when the value doesn't fit in a `u64`
/// -- see `Decimal::from_exact`, the single canonicalizing constructor.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Coeff {
    Small(u64),
    Big(Rc<BigUint>),
}

/// See the module docs. The representation is private; see
/// docs/RUNTIME.md for the design (small inline fast path,
/// `bigint::BigUint` fallback).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Decimal {
    neg: bool,
    exp: i64,
    coeff: Coeff,
}

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------

fn num_digits_u64(mut x: u64) -> usize {
    if x == 0 {
        return 1;
    }
    let mut n = 0;
    while x > 0 {
        x /= 10;
        n += 1;
    }
    n
}

/// Signed magnitude addition: `(-1)^a_neg * a_mag + (-1)^b_neg * b_mag`.
fn signed_add(a_neg: bool, a_mag: &BigUint, b_neg: bool, b_mag: &BigUint) -> (bool, BigUint) {
    if a_neg == b_neg {
        (a_neg, a_mag.add(b_mag))
    } else {
        match a_mag.cmp(b_mag) {
            Ordering::Equal => (false, BigUint::zero()),
            Ordering::Greater => (a_neg, a_mag.sub(b_mag)),
            Ordering::Less => (b_neg, b_mag.sub(a_mag)),
        }
    }
}

impl Decimal {
    pub fn zero() -> Decimal {
        Decimal {
            neg: false,
            exp: 0,
            coeff: Coeff::Small(0),
        }
    }

    /// The single canonicalizing constructor: strips trailing decimal
    /// zeros from `coeff` (folding them into `exp`), collapses zero to the
    /// canonical `+0`, and demotes to `Coeff::Small` whenever the
    /// (zero-stripped) coefficient fits a `u64`. Every `Decimal` in
    /// existence is built through this (directly, or via `round_result`).
    fn from_exact(neg: bool, exp: i64, coeff: BigUint) -> Decimal {
        if coeff.is_zero() {
            return Decimal::zero();
        }
        let (stripped, zeros) = coeff.strip_trailing_zeros();
        let exp = exp + zeros as i64;
        match stripped.to_u64() {
            Some(v) => Decimal {
                neg,
                exp,
                coeff: Coeff::Small(v),
            },
            None => Decimal {
                neg,
                exp,
                coeff: Coeff::Big(Rc::new(stripped)),
            },
        }
    }

    fn to_biguint(&self) -> BigUint {
        match &self.coeff {
            Coeff::Small(v) => BigUint::from_u64(*v),
            Coeff::Big(b) => (**b).clone(),
        }
    }

    fn coeff_digit_count(&self) -> usize {
        match &self.coeff {
            Coeff::Small(v) => num_digits_u64(*v),
            Coeff::Big(b) => b.digit_count(),
        }
    }

    /// Same value, sign flipped, with NO context rounding applied (unlike
    /// the public `neg()`). Used internally to implement `sub` in terms of
    /// `add`.
    fn negated_raw(&self) -> Decimal {
        if self.is_zero() {
            return Decimal::zero();
        }
        Decimal {
            neg: !self.neg,
            exp: self.exp,
            coeff: self.coeff.clone(),
        }
    }

    fn with_sign(&self, neg: bool) -> Decimal {
        if self.is_zero() {
            return Decimal::zero();
        }
        Decimal {
            neg,
            exp: self.exp,
            coeff: self.coeff.clone(),
        }
    }

    fn abs_value(&self) -> Decimal {
        self.with_sign(false)
    }

    /// Parity of an integral (`is_integer() == true`) value; `0` counts as
    /// even.
    fn integer_parity_odd(&self) -> bool {
        if self.is_zero() {
            return false;
        }
        if self.exp > 0 {
            return false;
        }
        match &self.coeff {
            Coeff::Small(v) => v % 2 == 1,
            Coeff::Big(b) => b.is_odd(),
        }
    }

    /// Exact magnitude of an integral, nonzero value as a `BigUint`.
    fn abs_integer_biguint(&self) -> BigUint {
        self.to_biguint().mul_pow10(self.exp.max(0) as u64)
    }

    pub fn from_i64(n: i64) -> Decimal {
        if n == 0 {
            return Decimal::zero();
        }
        let neg = n < 0;
        Decimal::from_exact(neg, 0, BigUint::from_u64(n.unsigned_abs()))
    }

    /// An exact integer from a zigzag-encoded LEB128 `varint` (the INT
    /// constant tag): `groups` are the 7-bit payloads, low group first.
    /// Arbitrary size, never rounded (Python's `Decimal(int)` is exact).
    pub fn from_zigzag_groups(groups: &[u8]) -> Decimal {
        let mut z = BigUint::zero();
        for &g in groups.iter().rev() {
            z = z.mul_small(128).add(&BigUint::from_u64((g & 0x7f) as u64));
        }
        if z.is_odd() {
            let (mag, _r) = z.add(&BigUint::from_u64(1)).divmod_small(2);
            Decimal::from_exact(true, 0, mag)
        } else {
            let (mag, _r) = z.divmod_small(2);
            Decimal::from_exact(false, 0, mag)
        }
    }

    /// Python `Decimal(text)` for a finite number: `[+-]digits[.digits][(e|E)[+-]digits]`
    /// (either side of the `.` may be empty but not both). Exact, never
    /// rounded. `None` for anything else (including NaN/Infinity).
    pub fn parse(text: &str) -> Option<Decimal> {
        let s = text.trim();
        if s.is_empty() {
            return None;
        }
        let bytes = s.as_bytes();
        let mut i = 0usize;
        let mut neg = false;
        if bytes[i] == b'+' {
            i += 1;
        } else if bytes[i] == b'-' {
            neg = true;
            i += 1;
        }
        let int_start = i;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            i += 1;
        }
        let int_part = &s[int_start..i];
        let mut frac_part = "";
        if i < bytes.len() && bytes[i] == b'.' {
            i += 1;
            let frac_start = i;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
            frac_part = &s[frac_start..i];
        }
        if int_part.is_empty() && frac_part.is_empty() {
            return None;
        }
        let mut exp: i64 = 0;
        if i < bytes.len() && (bytes[i] == b'e' || bytes[i] == b'E') {
            i += 1;
            let mut esign: i64 = 1;
            if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
                if bytes[i] == b'-' {
                    esign = -1;
                }
                i += 1;
            }
            let estart = i;
            while i < bytes.len() && bytes[i].is_ascii_digit() {
                i += 1;
            }
            if i == estart {
                return None;
            }
            let eval: i64 = s[estart..i].parse().unwrap_or(i64::MAX / 4);
            exp = esign * eval;
        }
        if i != bytes.len() {
            return None;
        }
        exp -= frac_part.len() as i64;
        let mut digits = String::with_capacity(int_part.len() + frac_part.len());
        digits.push_str(int_part);
        digits.push_str(frac_part);
        if digits.is_empty() {
            digits.push('0');
        }
        let coeff = BigUint::from_decimal_str(&digits)?;
        Some(Decimal::from_exact(neg, exp, coeff))
    }

    /// Python `Decimal(repr(f))`: the exact value of the shortest decimal
    /// string that round-trips `f`. `Err(InvalidOperation)` for NaN/inf.
    ///
    /// This does NOT use Rust's own `{:e}`/`Display` shortest-round-trip
    /// formatting: for a (rare) value that sits exactly on a decimal tie
    /// for its shortest representation (e.g. the f64 nearest to
    /// `171418102589152.1119761599831552` is exactly `...152.125`), Rust's
    /// formatter and Python's `repr` can break the tie in opposite
    /// directions (confirmed empirically: same f64 bit pattern, Python
    /// prints `...152.12`, Rust prints `...152.13`). Instead we decode the
    /// f64's *exact* binary value ourselves (mantissa * 2^e, computed
    /// exactly in `BigUint`) and search upward for the fewest significant
    /// digits `k` such that HALF_EVEN-rounding that exact value to `k`
    /// digits reparses (via the standard-library, correctly-rounded
    /// `str::parse`) back to the same f64 -- i.e. we replicate Python's
    /// own "shortest, ties-to-even" `dtoa` directly, sidestepping any
    /// difference in the two languages' float-formatting tie-breaks.
    pub fn from_f64(f: f64) -> DecResult {
        if f.is_nan() || f.is_infinite() {
            return Err(DecError::InvalidOperation);
        }
        if f == 0.0 {
            return Ok(Decimal::zero());
        }
        let neg = f.is_sign_negative();
        let bits = f.to_bits();
        let biased_exp = ((bits >> 52) & 0x7ff) as i64;
        let mantissa_bits = bits & 0x000f_ffff_ffff_ffff;
        let (mantissa, exp2): (u64, i64) = if biased_exp == 0 {
            (mantissa_bits, -1074) // subnormal
        } else {
            (mantissa_bits | (1u64 << 52), biased_exp - 1075)
        };
        // Exact value = mantissa * 2^exp2, converted to an exact decimal
        // (coeff * 10^exp): for exp2 >= 0 that's just `mantissa << exp2`;
        // for exp2 < 0, 2^exp2 = 5^|exp2] * 10^exp2 (terminates exactly).
        let mantissa_big = BigUint::from_u64(mantissa);
        let (exact_coeff, exact_exp): (BigUint, i64) = if exp2 >= 0 {
            (mantissa_big.mul(&BigUint::from_u64(2).pow_u64(exp2 as u64)), 0)
        } else {
            let n = (-exp2) as u64;
            (mantissa_big.mul(&BigUint::from_u64(5).pow_u64(n)), -(n as i64))
        };
        let total_digits = exact_coeff.digit_count();
        for k in 1..=total_digits {
            let (rc, re) = round_to_n(&exact_coeff, exact_exp, k, false);
            let s = format!("{}{}e{}", if neg { "-" } else { "" }, rc.to_decimal_string(), re);
            if let Ok(cand) = s.parse::<f64>() {
                if cand.to_bits() == f.to_bits() {
                    return Ok(Decimal::from_exact(neg, re, rc));
                }
            }
        }
        // Should be unreachable (the full exact value always round-trips).
        Ok(Decimal::from_exact(neg, exact_exp, exact_coeff))
    }

    /// Python `float(d)`: correctly rounded (inf when out of range).
    pub fn to_f64(&self) -> f64 {
        if self.is_zero() {
            return 0.0;
        }
        let digits = self.to_biguint().to_decimal_string();
        let s = format!("{}{}e{}", if self.neg { "-" } else { "" }, digits, self.exp);
        s.parse::<f64>()
            .unwrap_or(if self.neg { f64::NEG_INFINITY } else { f64::INFINITY })
    }

    pub fn is_zero(&self) -> bool {
        matches!(self.coeff, Coeff::Small(0))
    }

    pub fn is_negative(&self) -> bool {
        self.neg
    }

    /// `d == d.to_integral_value()`.
    pub fn is_integer(&self) -> bool {
        self.is_zero() || self.exp >= 0
    }

    /// Python `int(d)` (truncates toward zero) when it fits an `i64`.
    pub fn to_i64(&self) -> Option<i64> {
        if self.is_zero() {
            return Some(0);
        }
        let mag = if self.exp >= 0 {
            self.to_biguint().mul_pow10(self.exp as u64)
        } else {
            self.to_biguint().divmod_pow10((-self.exp) as u64).0
        };
        if mag.is_zero() {
            return Some(0);
        }
        let v = mag.to_u64()?;
        if self.neg {
            if v <= i64::MAX as u64 {
                Some(-(v as i64))
            } else if v == i64::MAX as u64 + 1 {
                Some(i64::MIN)
            } else {
                None
            }
        } else if v <= i64::MAX as u64 {
            Some(v as i64)
        } else {
            None
        }
    }

    pub fn add(&self, other: &Decimal) -> DecResult {
        if let (Coeff::Small(a), Coeff::Small(b)) = (&self.coeff, &other.coeff) {
            if self.exp == other.exp {
                if self.neg == other.neg {
                    if let Some(s) = a.checked_add(*b) {
                        return fast_finish(self.neg, self.exp, s);
                    }
                } else {
                    let (hi, lo, sign) = if a >= b {
                        (*a, *b, self.neg)
                    } else {
                        (*b, *a, other.neg)
                    };
                    return fast_finish(sign, self.exp, hi - lo);
                }
            }
        }
        let (neg, coeff, exp) = add_exact(self, other);
        round_result(neg, coeff, exp, false)
    }

    pub fn sub(&self, other: &Decimal) -> DecResult {
        self.add(&other.negated_raw())
    }

    pub fn mul(&self, other: &Decimal) -> DecResult {
        if let (Coeff::Small(a), Coeff::Small(b)) = (&self.coeff, &other.coeff) {
            if let Some(p) = a.checked_mul(*b) {
                let sign = self.neg ^ other.neg;
                return match self.exp.checked_add(other.exp) {
                    Some(e) => fast_finish(sign, e, p),
                    None => {
                        if self.exp > 0 {
                            Err(DecError::Overflow)
                        } else {
                            Ok(Decimal::zero())
                        }
                    }
                };
            }
        }
        let (neg, coeff, exp) = mul_exact(self, other);
        round_result(neg, coeff, exp, false)
    }

    /// `self / other`, correctly rounded. `Err(DivisionByZero)` if `other` is zero.
    pub fn div(&self, other: &Decimal) -> DecResult {
        div_round(self, other, PREC)
    }

    /// Python `Decimal.__floordiv__`: the quotient truncated toward zero
    /// (NOT floored). `Err(DivisionImpossible)` if it has more than 28 digits.
    pub fn idiv(&self, other: &Decimal) -> DecResult {
        if other.is_zero() {
            return Err(DecError::DivisionByZero);
        }
        if self.is_zero() {
            return Ok(Decimal::zero());
        }
        let neg = self.neg != other.neg;
        let (ac, bc, _e) = aligned(self, other);
        let (q, _r) = ac.divmod(&bc);
        if q.is_zero() {
            return Ok(Decimal::zero());
        }
        if q.digit_count() > PREC {
            return Err(DecError::DivisionImpossible);
        }
        Ok(Decimal::from_exact(neg, 0, q))
    }

    /// Python `Decimal.__mod__`: `self - other * trunc(self / other)`, sign
    /// of `self`. `Err(DivisionImpossible)` under the same condition as `idiv`.
    pub fn rem(&self, other: &Decimal) -> DecResult {
        if other.is_zero() {
            return Err(DecError::DivisionByZero);
        }
        if self.is_zero() {
            return Ok(Decimal::zero());
        }
        let (ac, bc, e) = aligned(self, other);
        let (q, _r) = ac.divmod(&bc);
        if q.digit_count() > PREC {
            return Err(DecError::DivisionImpossible);
        }
        // b * trunc(a/b) always has the same sign as `a` (self): trunc(a/b)
        // has sign (self.neg ^ other.neg), so b*trunc(a/b) has sign
        // other.neg ^ (self.neg ^ other.neg) == self.neg.
        let bq = bc.mul(&q);
        let (rem_neg, rem_mag) = signed_add(self.neg, &ac, !self.neg, &bq);
        round_result(rem_neg, rem_mag, e, false)
    }

    /// Python `Decimal.__pow__`, correctly rounded -- see docs/RUNTIME.md.
    pub fn pow(&self, other: &Decimal) -> DecResult {
        let a = self;
        let b = other;
        if matches!(a.coeff, Coeff::Small(1)) && a.exp == 0 && !a.neg {
            return Ok(Decimal::from_i64(1));
        }
        if b.is_integer() {
            pow_integer(a, b)
        } else {
            pow_fractional(a, b)
        }
    }

    /// `-self`, rounded to 28 digits like Python's `__neg__`.
    pub fn neg(&self) -> DecResult {
        if self.is_zero() {
            return Ok(Decimal::zero());
        }
        round_result(!self.neg, self.to_biguint(), self.exp, false)
    }

    /// Correctly rounded e**self.
    pub fn exp(&self) -> DecResult {
        if self.is_zero() {
            return Ok(Decimal::from_i64(1));
        }
        let x_f64 = self.to_f64();
        let log10_est = x_f64 / std::f64::consts::LN_10;
        if log10_est.is_finite() {
            if log10_est > 1_000_050.0 {
                return Err(DecError::Overflow);
            }
            if log10_est < -1_000_050.0 {
                return Ok(Decimal::zero());
            }
        } else if log10_est == f64::INFINITY {
            return Err(DecError::Overflow);
        } else if log10_est == f64::NEG_INFINITY {
            return Ok(Decimal::zero());
        }
        let neg = self.neg;
        let coeff = self.to_biguint();
        let exp = self.exp;
        ziv_round(|digits| {
            let (c, e) = exp_bf(neg, &coeff, exp, digits);
            (false, c, e)
        })
    }

    /// Correctly rounded natural log. `Err(InvalidOperation)` for `self <= 0`.
    pub fn ln(&self) -> DecResult {
        if self.is_negative() || self.is_zero() {
            return Err(DecError::InvalidOperation);
        }
        if matches!(self.coeff, Coeff::Small(1)) && self.exp == 0 {
            return Ok(Decimal::zero());
        }
        let coeff = self.to_biguint();
        let exp = self.exp;
        ziv_round(|digits| ln_bf(&coeff, exp, digits))
    }

    /// The Python VM's `_format_number`: an integer value prints every
    /// digit (`str(int(d))`), anything else prints in plain notation after
    /// rounding to 28 digits and stripping trailing zeros
    /// (`format(d.normalize(), "f")`).
    pub fn format(&self) -> String {
        if self.is_zero() {
            return "0".to_string();
        }
        if self.is_integer() {
            let digits = self.to_biguint().mul_pow10(self.exp as u64).to_decimal_string();
            return if self.neg {
                format!("-{}", digits)
            } else {
                digits
            };
        }
        // Mirror Python's `v.normalize()`: round to context precision, and
        // -- since `self` may be a raw, never-rounded `parse()` result far
        // outside the valid exponent range -- also apply the same Etiny
        // subnormal re-round `round_result` does when that first rounding
        // is subnormal (this is how e.g. an underflowing literal like
        // "-1757E-1000032" legitimately normalizes down to zero). `format`
        // has no way to signal Overflow (unlike every arithmetic op), so
        // the one thing this can't mirror is Python raising there for a
        // fractional value whose coefficient is itself huge enough to
        // overflow even after prec-rounding; that requires a literal with
        // well over a million digits and is treated as out of scope.
        let orig = self.to_biguint();
        let (mut c, mut e) = round_to_prec(&orig, self.exp, false);
        if !c.is_zero() {
            let adj = e + c.digit_count() as i64 - 1;
            if adj < EMIN {
                let (c2, e2) = round_to_exp(&orig, self.exp, ETINY, false);
                c = c2;
                e = e2;
            }
        }
        let (c, extra) = c.strip_trailing_zeros();
        let e = e + extra as i64;
        if c.is_zero() {
            return "0".to_string();
        }
        format_plain(self.neg, &c, e)
    }
}

impl PartialOrd for Decimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for Decimal {
    /// Numeric order, exact.
    fn cmp(&self, other: &Self) -> Ordering {
        if let (Coeff::Small(a), Coeff::Small(b)) = (&self.coeff, &other.coeff) {
            if self.exp == other.exp {
                if *a == 0 && *b == 0 {
                    return Ordering::Equal;
                }
                if *a == 0 {
                    return if other.neg { Ordering::Greater } else { Ordering::Less };
                }
                if *b == 0 {
                    return if self.neg { Ordering::Less } else { Ordering::Greater };
                }
                if self.neg != other.neg {
                    return if self.neg { Ordering::Less } else { Ordering::Greater };
                }
                let c = a.cmp(b);
                return if self.neg { c.reverse() } else { c };
            }
        }
        if self.is_zero() && other.is_zero() {
            return Ordering::Equal;
        }
        if self.is_zero() {
            return if other.neg { Ordering::Greater } else { Ordering::Less };
        }
        if other.is_zero() {
            return if self.neg { Ordering::Less } else { Ordering::Greater };
        }
        if self.neg != other.neg {
            return if self.neg { Ordering::Less } else { Ordering::Greater };
        }
        let (ac, bc, _e) = aligned(self, other);
        let c = ac.cmp(&bc);
        if self.neg {
            c.reverse()
        } else {
            c
        }
    }
}

// ---------------------------------------------------------------------------
// exact-arithmetic building blocks
// ---------------------------------------------------------------------------

fn aligned(a: &Decimal, b: &Decimal) -> (BigUint, BigUint, i64) {
    let e = a.exp.min(b.exp);
    let ac = a.to_biguint().mul_pow10((a.exp - e) as u64);
    let bc = b.to_biguint().mul_pow10((b.exp - e) as u64);
    (ac, bc, e)
}

fn add_exact(a: &Decimal, b: &Decimal) -> (bool, BigUint, i64) {
    let (ac, bc, e) = aligned(a, b);
    let (neg, mag) = signed_add(a.neg, &ac, b.neg, &bc);
    (neg, mag, e)
}

fn mul_exact(a: &Decimal, b: &Decimal) -> (bool, BigUint, i64) {
    let sign = a.neg ^ b.neg;
    let coeff = a.to_biguint().mul(&b.to_biguint());
    (sign, coeff, a.exp + b.exp)
}

fn fast_finish(neg: bool, exp: i64, mut val: u64) -> DecResult {
    if val == 0 {
        return Ok(Decimal::zero());
    }
    let mut e = exp;
    while val % 10 == 0 {
        val /= 10;
        e += 1;
    }
    let nd = num_digits_u64(val) as i64;
    let adj = e + nd - 1;
    if adj > EMAX {
        return Err(DecError::Overflow);
    }
    if adj < EMIN {
        return round_result(neg, BigUint::from_u64(val), e, false);
    }
    Ok(Decimal {
        neg,
        exp: e,
        coeff: Coeff::Small(val),
    })
}

// ---------------------------------------------------------------------------
// rounding: R(x) from the module spec
// ---------------------------------------------------------------------------

/// Drop `drop` decimal digits from `coeff`, HALF_EVEN. `sticky` means the
/// true (pre-truncation) value is known to be strictly greater than
/// `coeff` (e.g. it's the floor of an inexact division) -- this only
/// matters when the dropped part lands exactly on the halfway point,
/// where it forces rounding up instead of the even-digit tie break.
fn drop_digits(coeff: &BigUint, drop: u64, sticky: bool) -> BigUint {
    if drop == 0 {
        return coeff.clone();
    }
    let (q, r) = coeff.divmod_pow10(drop);
    let half = BigUint::pow10(drop - 1).mul_small(5);
    let round_up = match r.cmp(&half) {
        Ordering::Greater => true,
        Ordering::Less => false,
        Ordering::Equal => sticky || q.is_odd(),
    };
    if round_up {
        q.add(&BigUint::from_u64(1))
    } else {
        q
    }
}

/// Round `coeff` (with exponent `exp`) to `n` significant digits,
/// HALF_EVEN, with the carry-fix for a rounding-up that adds a digit
/// (`999.. -> 1000..`).
fn round_to_n(coeff: &BigUint, exp: i64, n: usize, sticky: bool) -> (BigUint, i64) {
    let nd = coeff.digit_count();
    if nd <= n {
        return (coeff.clone(), exp);
    }
    let drop = (nd - n) as u64;
    let mut q = drop_digits(coeff, drop, sticky);
    let mut e = exp + drop as i64;
    if q.digit_count() > n {
        let (q2, _r) = q.divmod_pow10(1);
        q = q2;
        e += 1;
    }
    (q, e)
}

fn round_to_prec(coeff: &BigUint, exp: i64, sticky: bool) -> (BigUint, i64) {
    round_to_n(coeff, exp, PREC, sticky)
}

fn round_to_exp_n(coeff: &BigUint, exp: i64, target_exp: i64, sticky: bool, prec: usize) -> (BigUint, i64) {
    if coeff.is_zero() {
        return (BigUint::zero(), target_exp);
    }
    if exp >= target_exp {
        return (coeff.mul_pow10((exp - target_exp) as u64), target_exp);
    }
    let drop = (target_exp - exp) as u64;
    let mut q = drop_digits(coeff, drop, sticky);
    let mut e = target_exp;
    if q.digit_count() > prec {
        let (q2, _r) = q.divmod_pow10(1);
        q = q2;
        e += 1;
    }
    (q, e)
}

fn round_to_exp(coeff: &BigUint, exp: i64, target_exp: i64, sticky: bool) -> (BigUint, i64) {
    round_to_exp_n(coeff, exp, target_exp, sticky, PREC)
}

/// `R(x)` generalized to an arbitrary working precision `prec` (with the
/// module's fixed `Emax`/`Emin` -- only the working `Etiny` shifts with
/// `prec`). Used both for the public context (`prec = PREC`, via
/// `round_result`) and for `pow`'s libmpdec-style integer-power simulation,
/// which rounds every intermediate multiply to a *widened* working
/// precision before a final round back to `PREC`.
fn round_result_prec(neg: bool, coeff: BigUint, exp: i64, sticky: bool, prec: usize) -> DecResult {
    if coeff.is_zero() {
        return Ok(Decimal::zero());
    }
    let (mut c, mut e) = round_to_n(&coeff, exp, prec, sticky);
    if c.is_zero() {
        return Ok(Decimal::zero());
    }
    let adj = e + c.digit_count() as i64 - 1;
    if adj > EMAX {
        return Err(DecError::Overflow);
    }
    if adj < EMIN {
        let working_etiny = EMIN - prec as i64 + 1;
        let (c2, e2) = round_to_exp_n(&coeff, exp, working_etiny, sticky, prec);
        c = c2;
        e = e2;
        if c.is_zero() {
            return Ok(Decimal::zero());
        }
        let adj2 = e + c.digit_count() as i64 - 1;
        if adj2 > EMAX {
            return Err(DecError::Overflow);
        }
    }
    Ok(Decimal::from_exact(neg, e, c))
}

/// `R(x)`: round the exact (or sticky-approximate) value
/// `(-1)^neg * coeff * 10^exp` per the module's context.
fn round_result(neg: bool, coeff: BigUint, exp: i64, sticky: bool) -> DecResult {
    round_result_prec(neg, coeff, exp, sticky, PREC)
}

/// `a * b`, correctly rounded to `prec` significant digits (multiplication
/// is always exact before rounding, so no sticky bit is needed).
fn mul_round(a: &Decimal, b: &Decimal, prec: usize) -> DecResult {
    let (neg, coeff, exp) = mul_exact(a, b);
    round_result_prec(neg, coeff, exp, false, prec)
}

/// `a / b`, correctly rounded to `prec` significant digits. Same algorithm
/// as the public `div`, parameterized by working precision.
fn div_round(a: &Decimal, b: &Decimal, prec: usize) -> DecResult {
    if b.is_zero() {
        return Err(DecError::DivisionByZero);
    }
    if a.is_zero() {
        return Ok(Decimal::zero());
    }
    let sign = a.neg ^ b.neg;
    let a_digits = a.coeff_digit_count() as i64;
    let b_digits = b.coeff_digit_count() as i64;
    let k = ((prec as i64 + 4) - a_digits + b_digits).max(0) as u64;
    let ac = a.to_biguint().mul_pow10(k);
    let bc = b.to_biguint();
    let (q, r) = ac.divmod(&bc);
    let sticky = !r.is_zero();
    let result_exp = a.exp - b.exp - k as i64;
    round_result_prec(sign, q, result_exp, sticky, prec)
}

fn format_plain(neg: bool, coeff: &BigUint, exp: i64) -> String {
    let sign = if neg { "-" } else { "" };
    if exp >= 0 {
        let digits = coeff.mul_pow10(exp as u64).to_decimal_string();
        return format!("{}{}", sign, digits);
    }
    let digits = coeff.to_decimal_string();
    let point_pos = (-exp) as usize;
    if digits.len() > point_pos {
        let split = digits.len() - point_pos;
        format!("{}{}.{}", sign, &digits[..split], &digits[split..])
    } else {
        let zeros = point_pos - digits.len();
        format!("{}0.{}{}", sign, "0".repeat(zeros), digits)
    }
}

// ---------------------------------------------------------------------------
// pow
// ---------------------------------------------------------------------------

/// Fast pre-check shared by both integer- and transcendental-power paths:
/// if `a_pos ** signed_e` is unambiguously overflowing or underflowing
/// (per a cheap `f64` log10 estimate), answer directly without running the
/// real (still generally fast, but let's not bother) computation.
/// `log10(a_pos)` for `a_pos > 0`, computed directly from the decimal
/// representation (`adjusted_exponent + log10(leading mantissa digits)`)
/// rather than via `a_pos.to_f64().log10()`. This matters whenever
/// `a_pos`'s magnitude is itself outside `f64`'s representable range (its
/// adjusted exponent can legitimately reach +-999999, while `f64` overflows
/// past ~+-308): `to_f64()` would collapse to `0.0`/`inf` first, losing the
/// (perfectly finite and useful) log10 value entirely.
fn decimal_log10_estimate(a_pos: &Decimal) -> f64 {
    let nd = a_pos.coeff_digit_count();
    let adj = a_pos.exp + nd as i64 - 1;
    let lead_n = nd.min(15);
    let coeff = a_pos.to_biguint();
    let (lead, _) = coeff.divmod_pow10((nd - lead_n) as u64);
    let lead_f64 = lead.to_u64().unwrap_or(1) as f64;
    let mantissa = lead_f64 / 10f64.powi(lead_n as i32 - 1); // in [1, 10)
    adj as f64 + mantissa.log10()
}

fn pow_bounds_check(a_pos: &Decimal, signed_e: f64) -> Option<DecResult> {
    let log10_est = signed_e * decimal_log10_estimate(a_pos);
    if log10_est.is_finite() {
        if log10_est > 1_000_050.0 {
            return Some(Err(DecError::Overflow));
        }
        if log10_est < -1_000_050.0 {
            return Some(Ok(Decimal::zero()));
        }
    } else if log10_est == f64::INFINITY {
        return Some(Err(DecError::Overflow));
    } else if log10_est == f64::NEG_INFINITY {
        return Some(Ok(Decimal::zero()));
    }
    None
}

/// Replicates libmpdec's `_mpd_qpow_int` (CPython's `_decimal`, the C
/// implementation actually used by `Decimal.__pow__` for an integral
/// exponent) as closely as our representation allows: left-to-right binary
/// exponentiation, with every intermediate multiply (and the initial
/// `1/a_pos` for a negative exponent) rounded HALF_EVEN to a *widened*
/// working precision `28 + digits(e) + 2` (`+3` for a negative exponent),
/// followed by one final round back down to the module's 28 digits.
///
/// This is deliberately NOT "compute the exact big-integer power then
/// round once" -- that's mathematically the *more* correct answer, but
/// CPython's own algorithm is this double-rounding one, and it can
/// (rarely) differ from the true correctly-rounded result in its last
/// digit. Bit-for-bit matching Python means replicating its algorithm,
/// not out-correcting it. `a_pos` must be `> 0`; the caller applies sign.
fn pow_integer_mpdec(a_pos: &Decimal, e: u64, e_neg: bool) -> DecResult {
    if e == 0 {
        return Ok(Decimal::from_i64(1));
    }
    let adj = num_digits_u64(e) as i64;
    let mut wprec = (PREC as i64 + adj + 2) as usize;
    let base: Decimal = if e_neg {
        wprec += 1;
        div_round(&Decimal::from_i64(1), a_pos, wprec)?
    } else {
        a_pos.clone()
    };
    let mut r = base.clone();
    let bits = 64 - e.leading_zeros(); // number of significant bits in e
    for i in (0..bits.saturating_sub(1)).rev() {
        r = mul_round(&r, &r, wprec)?;
        if (e >> i) & 1 == 1 {
            r = mul_round(&r, &base, wprec)?;
        }
    }
    // Final round to the module's context precision (the "double
    // rounding" -- r was only ever rounded to the wider `wprec` above).
    round_result_prec(r.neg, r.to_biguint(), r.exp, false, PREC)
}

fn pow_integer(a: &Decimal, b: &Decimal) -> DecResult {
    if a.is_zero() {
        if b.is_zero() {
            return Err(DecError::InvalidOperation);
        }
        return if b.is_negative() {
            Err(DecError::DivisionByZero)
        } else {
            Ok(Decimal::zero())
        };
    }
    if b.is_zero() {
        return Ok(Decimal::from_i64(1));
    }
    if matches!(a.coeff, Coeff::Small(1)) && a.exp == 0 {
        // a == -1 (a == 1 was already handled by the caller).
        let odd = b.integer_parity_odd();
        return Ok(Decimal::from_i64(if odd { -1 } else { 1 }));
    }
    let b_neg = b.is_negative();
    let b_mag = b.abs_integer_biguint();
    let sign = a.neg && b.integer_parity_odd();
    let a_pos = a.abs_value();

    if let Some(e) = b_mag.to_u64() {
        let signed_e = if b_neg { -(e as f64) } else { e as f64 };
        if let Some(result) = pow_bounds_check(&a_pos, signed_e) {
            return result;
        }
        let result = pow_integer_mpdec(&a_pos, e, b_neg)?;
        return Ok(result.with_sign(sign));
    }
    // |e| >= 2^64: fall back to the general transcendental machinery (this
    // can't be a "cheap" exact case either way at that magnitude).
    let result = pow_via_ln(&a_pos, b)?;
    Ok(result.with_sign(sign))
}

fn pow_fractional(a: &Decimal, b: &Decimal) -> DecResult {
    if a.is_negative() {
        return Err(DecError::InvalidOperation);
    }
    if a.is_zero() {
        return if b.is_negative() {
            Err(DecError::DivisionByZero)
        } else {
            Ok(Decimal::zero())
        };
    }
    pow_via_ln(a, b)
}

/// Correctly rounded `a_pos ** b` for `a_pos > 0`, via `exp(b * ln(a_pos))`.
fn pow_via_ln(a_pos: &Decimal, b: &Decimal) -> DecResult {
    let b_f64 = b.to_f64();
    let log10_est = b_f64 * decimal_log10_estimate(a_pos);
    if log10_est.is_finite() {
        if log10_est > 1_000_050.0 {
            return Err(DecError::Overflow);
        }
        if log10_est < -1_000_050.0 {
            return Ok(Decimal::zero());
        }
    } else if log10_est == f64::INFINITY {
        return Err(DecError::Overflow);
    } else if log10_est == f64::NEG_INFINITY {
        return Ok(Decimal::zero());
    }
    // `b * ln(a)` amplifies ln(a)'s *relative* error by roughly
    // `|b * ln a|`'s own magnitude; per the module spec we compensate with
    // `digits(ceil(|b * ln a|)) + 3` extra guard digits -- i.e. how many
    // decimal digits it takes to *write* that magnitude (a handful, even
    // for huge exponents), NOT the magnitude itself (which would make the
    // working precision scale with the exponent -- catastrophic for
    // something like `10 ** 1000000`).
    let m_est = log10_est.abs() * std::f64::consts::LN_10;
    let m_digit_count: i64 = if m_est < 1.0 {
        1
    } else {
        m_est.log10().floor() as i64 + 1
    };
    let extra_guard = (m_digit_count + 3).max(3) as usize;
    let a_coeff = a_pos.to_biguint();
    let a_exp = a_pos.exp;
    let b_neg = b.neg;
    let b_coeff = b.to_biguint();
    let b_exp = b.exp;
    ziv_round(|digits| {
        let wp = digits + extra_guard;
        let (ln_neg, ln_c, ln_e) = ln_bf(&a_coeff, a_exp, wp);
        let (mul_neg, mul_c, mul_e) = bf_mul(ln_neg, &ln_c, ln_e, b_neg, &b_coeff, b_exp, wp);
        let (ec, ee) = exp_bf(mul_neg, &mul_c, mul_e, digits);
        (false, ec, ee)
    })
}

// ---------------------------------------------------------------------------
// internal "big float": (sign, BigUint coeff, i64 exp), truncating
// arithmetic used to implement correctly-rounded exp/ln/pow (Ziv's
// strategy: compute at increasing working precision -- with generous
// internal guard digits on top of that -- until rounding the result to 28
// digits is unambiguous).
// ---------------------------------------------------------------------------

fn bf_trunc(neg: bool, c: BigUint, e: i64, digits: usize) -> (bool, BigUint, i64) {
    if c.is_zero() {
        return (false, BigUint::zero(), 0);
    }
    let nd = c.digit_count();
    if nd <= digits {
        return (neg, c, e);
    }
    let drop = (nd - digits) as u64;
    let (q, _r) = c.divmod_pow10(drop);
    (neg, q, e + drop as i64)
}

fn bf_add(
    neg1: bool,
    c1: &BigUint,
    e1: i64,
    neg2: bool,
    c2: &BigUint,
    e2: i64,
) -> (bool, BigUint, i64) {
    let e = e1.min(e2);
    let a = c1.mul_pow10((e1 - e) as u64);
    let b = c2.mul_pow10((e2 - e) as u64);
    let (neg, mag) = signed_add(neg1, &a, neg2, &b);
    (neg, mag, e)
}

fn bf_mul(
    neg1: bool,
    c1: &BigUint,
    e1: i64,
    neg2: bool,
    c2: &BigUint,
    e2: i64,
    digits: usize,
) -> (bool, BigUint, i64) {
    let neg = neg1 ^ neg2;
    let c = c1.mul(c2);
    bf_trunc(neg, c, e1 + e2, digits)
}

fn bf_div(
    neg1: bool,
    c1: &BigUint,
    e1: i64,
    neg2: bool,
    c2: &BigUint,
    e2: i64,
    digits: usize,
) -> (bool, BigUint, i64) {
    if c1.is_zero() {
        return (false, BigUint::zero(), 0);
    }
    let neg = neg1 ^ neg2;
    let d1 = c1.digit_count() as i64;
    let d2 = c2.digit_count() as i64;
    let extra = (digits as i64 + 10 - d1 + d2).max(0) as u64;
    let scaled = c1.mul_pow10(extra);
    let (q, _r) = scaled.divmod(c2);
    let e = e1 - e2 - extra as i64;
    bf_trunc(neg, q, e, digits)
}

fn bf_div_small(c: &BigUint, e: i64, n: u64, digits: usize) -> (BigUint, i64) {
    if c.is_zero() {
        return (BigUint::zero(), 0);
    }
    let extra = digits as u64 + 10;
    let scaled = c.mul_pow10(extra);
    let (q, _r) = scaled.divmod_small(n);
    let (_, qq, ee) = bf_trunc(false, q, e - extra as i64, digits);
    (qq, ee)
}

fn bf_div_by_pow2(neg: bool, c: &BigUint, e: i64, s: u32, digits: usize) -> (bool, BigUint, i64) {
    if c.is_zero() {
        return (false, BigUint::zero(), 0);
    }
    let divisor = BigUint::from_u64(1u64 << s);
    let extra = digits as u64 + s as u64 + 10;
    let scaled = c.mul_pow10(extra);
    let (q, _r) = scaled.divmod(&divisor);
    bf_trunc(neg, q, e - extra as i64, digits)
}

/// Truncate `coeff` to its top ~25 digits (enough for an `f64`) before
/// building the scientific-notation string, so this stays cheap even when
/// `coeff` has millions of digits.
fn bf_to_f64(neg: bool, coeff: &BigUint, exp: i64) -> f64 {
    if coeff.is_zero() {
        return 0.0;
    }
    let nd = coeff.digit_count();
    let (c2, e2) = if nd > 25 {
        let drop = (nd - 25) as u64;
        let (q, _r) = coeff.divmod_pow10(drop);
        (q, exp + drop as i64)
    } else {
        (coeff.clone(), exp)
    };
    let digits = c2.to_decimal_string();
    let s = format!("{}{}e{}", if neg { "-" } else { "" }, digits, e2);
    s.parse::<f64>()
        .unwrap_or(if neg { f64::NEG_INFINITY } else { f64::INFINITY })
}

fn f64_to_bf(f: f64) -> (bool, BigUint, i64) {
    if f == 0.0 {
        return (false, BigUint::zero(), 0);
    }
    let neg = f < 0.0;
    let s = format!("{:e}", f.abs());
    let (mantissa, exp_part) = s.split_once('e').unwrap();
    let exp: i64 = exp_part.parse().unwrap();
    let (int_part, frac_part) = match mantissa.split_once('.') {
        Some((i, fp)) => (i, fp),
        None => (mantissa, ""),
    };
    let mut digits = String::with_capacity(int_part.len() + frac_part.len());
    digits.push_str(int_part);
    digits.push_str(frac_part);
    let coeff = BigUint::from_decimal_str(&digits).unwrap();
    let e = exp - frac_part.len() as i64;
    (neg, coeff, e)
}

/// `ln(10)` to (up to) 2200 significant digits, generated once with
/// Python's `decimal` module (`Context(prec=2200).ln(Decimal(10))`) --
/// used as the fixed reduction constant for `exp_bf`/`ln_bf` so we don't
/// need a self-referential bootstrap for computing `ln(10)` itself.
const LN10_DIGITS: &str = "23025850929940456840179914546843642076011014886287729760333279009675726096773524802359972050895982983419677840422862486334095254650828067566662873690987816894829072083255546808437998948262331985283935053089653777326288461633662222876982198867465436674744042432743651550489343149393914796194044002221051017141748003688084012647080685567743216228355220114804663715659121373450747856947683463616792101806445070648000277502684916746550586856935673420670581136429224554405758925724208241314695689016758940256776311356919292033376587141660230105703089634572075440370847469940168269282808481184289314848524948644871927809676271275775397027668605952496716674183485704422507197965004714951050492214776567636938662976979522110718264549734772662425709429322582798502585509785265383207606726317164309505995087807523710333101197857547331541421808427543863591778117054309827482385045648019095610299291824318237525357709750539565187697510374970888692180205189339507238539205144634197265287286965110862571492198849978748873771345686209167058498078280597511938544450099781311469159346662410718466923101075984383191912922307925037472986509290098803919417026544168163357275557031515961135648465461908970428197633658369837163289821744073660091621778505417792763677311450417821376601110107310423978325218948988175979217986663943195239368559164471182467532456309125287783309636042629821530408745609277607266413547875766162629265682987049579549139549180492090694385807900327630179415031178668620924085379498612649334793548717374516758095370882810674524401058924449764796860751202757241818749893959716431055188481952883307466993178146349300003212003277656541304726218839705967944579434683432183953044148448037013057536742621536755798147704580314136377932362915601281853364984669422614652064599420729171193706024449293580377076189810973625332245483669885055282859661928050984471751985036666808749704969822732202448233430971691111368135884186965493237149969419796878030088504089796185987565798948364452120436982164152929878117429733325886079159125109671875109292484750239305726654462762009230687915181358034777012955936462984123664970233551745861955647724618577173693684046765770478743197805738532718109338834963388130699455697";
const LN10_LEN: usize = 2200;

/// `ln(10)` to (at most) `n` significant digits.
fn ln10_digits(n: usize) -> (BigUint, i64) {
    let n = n.clamp(1, LN10_LEN);
    let s = &LN10_DIGITS[0..n];
    let c = BigUint::from_decimal_str(s).unwrap();
    (c, -(n as i64 - 1))
}

/// e^x, truncated to `digits` significant digits, computed with generous
/// internal guard digits so the error at that precision is far below one
/// unit in the last place. `x = (-1)^x_neg * x_coeff * 10^x_exp`.
fn exp_bf(x_neg: bool, x_coeff: &BigUint, x_exp: i64, digits: usize) -> (BigUint, i64) {
    if x_coeff.is_zero() {
        return (BigUint::from_u64(1), 0);
    }
    let iwp = digits + 20;
    let (ln10_c, ln10_e) = ln10_digits(iwp + 10);
    let x_f64 = bf_to_f64(x_neg, x_coeff, x_exp);
    let k: i64 = if x_f64.is_finite() {
        (x_f64 / std::f64::consts::LN_10).round() as i64
    } else {
        0
    };
    let (kln_neg, kln_c, kln_e) = if k == 0 {
        (false, BigUint::zero(), 0)
    } else {
        (k < 0, BigUint::from_u64(k.unsigned_abs()).mul(&ln10_c), ln10_e)
    };
    let (r_neg, r_c, r_e) = bf_add(x_neg, x_coeff, x_exp, !kln_neg, &kln_c, kln_e);
    let (r_neg, r_c, r_e) = bf_trunc(r_neg, r_c, r_e, iwp + 5);

    // e^r via r' = r / 2^s, Taylor series, then squaring s times.
    let s: u32 = 20;
    let (rp_neg, rp_c, rp_e) = bf_div_by_pow2(r_neg, &r_c, r_e, s, iwp + 10);

    let (mut sum_neg, mut sum_c, mut sum_e) = (false, BigUint::from_u64(1), 0i64);
    let (mut term_neg, mut term_c, mut term_e) = (false, BigUint::from_u64(1), 0i64);
    let mut n: u64 = 0;
    loop {
        n += 1;
        let (tn, tc, te) = bf_mul(term_neg, &term_c, term_e, rp_neg, &rp_c, rp_e, iwp + 10);
        let (tc2, te2) = bf_div_small(&tc, te, n, iwp + 10);
        term_neg = tn;
        term_c = tc2;
        term_e = te2;
        let (sn, sc, se) = bf_add(sum_neg, &sum_c, sum_e, term_neg, &term_c, term_e);
        let (sn, sc, se) = bf_trunc(sn, sc, se, iwp + 10);
        sum_neg = sn;
        sum_c = sc;
        sum_e = se;
        if term_c.is_zero() {
            break;
        }
        let term_adj = term_e + term_c.digit_count() as i64 - 1;
        let sum_adj = sum_e + sum_c.digit_count() as i64 - 1;
        if term_adj < sum_adj - (iwp as i64 + 5) {
            break;
        }
        if n > 100_000 {
            break;
        }
    }

    let (mut res_neg, mut res_c, mut res_e) = (sum_neg, sum_c, sum_e);
    for _ in 0..s {
        let (rn, rc, re) = bf_mul(res_neg, &res_c, res_e, res_neg, &res_c, res_e, iwp + 10);
        res_neg = rn;
        res_c = rc;
        res_e = re;
    }
    debug_assert!(!res_neg);
    let final_exp = res_e + k;
    let (_, c, e) = bf_trunc(false, res_c, final_exp, digits);
    (c, e)
}

/// `ln(m)` for `m` in (0.1, 1], via Newton's method on `exp`:
/// `y <- y + 2*(m - e^y)/(m + e^y)`, doubling correct digits each step.
fn ln_mantissa_bf(m_coeff: &BigUint, m_exp: i64, digits: usize) -> (bool, BigUint, i64) {
    let m_f64 = bf_to_f64(false, m_coeff, m_exp);
    let y0 = m_f64.ln();
    let (mut y_neg, mut y_c, mut y_e) = f64_to_bf(y0);
    let mut cur_digits: usize = 17.min(digits);
    loop {
        if cur_digits >= digits {
            break;
        }
        cur_digits = (cur_digits * 2).min(digits + 5);
        let wp = cur_digits + 10;
        let (ey_c, ey_e) = exp_bf(y_neg, &y_c, y_e, wp);
        let e_al = m_exp.min(ey_e);
        let m_al = m_coeff.mul_pow10((m_exp - e_al) as u64);
        let ey_al = ey_c.mul_pow10((ey_e - e_al) as u64);
        let (num_neg, num_c) = signed_add(false, &m_al, true, &ey_al);
        let (den_neg, den_c) = signed_add(false, &m_al, false, &ey_al);
        let num_c2 = num_c.mul_small(2);
        let (frac_neg, frac_c, frac_e) = bf_div(num_neg, &num_c2, e_al, den_neg, &den_c, e_al, wp);
        let (new_neg, new_c, new_e) = bf_add(y_neg, &y_c, y_e, frac_neg, &frac_c, frac_e);
        let (new_neg, new_c, new_e) = bf_trunc(new_neg, new_c, new_e, wp);
        y_neg = new_neg;
        y_c = new_c;
        y_e = new_e;
    }
    bf_trunc(y_neg, y_c, y_e, digits)
}

/// `ln(coeff * 10^exp)` for `coeff > 0`, to `digits` significant digits.
fn ln_bf(coeff: &BigUint, exp: i64, digits: usize) -> (bool, BigUint, i64) {
    let nd = coeff.digit_count() as i64;
    let e10 = nd + exp;
    let mantissa_exp = -nd;
    let iwp = digits + 20;
    let (lm_neg, lm_c, lm_e) = ln_mantissa_bf(coeff, mantissa_exp, iwp + 10);
    let (ln10_c, ln10_e) = ln10_digits(iwp + 10);
    let (e10_neg, e10_c, e10_e) = if e10 == 0 {
        (false, BigUint::zero(), 0)
    } else {
        (e10 < 0, BigUint::from_u64(e10.unsigned_abs() as u64).mul(&ln10_c), ln10_e)
    };
    let (total_neg, total_c, total_e) = bf_add(lm_neg, &lm_c, lm_e, e10_neg, &e10_c, e10_e);
    bf_trunc(total_neg, total_c, total_e, digits)
}

/// Ziv's strategy: call `compute(digits)` at increasing `digits` (starting
/// at `PREC + 12`, doubling the guard on failure, capped around `PREC +
/// 2000`) until rounding `result - 10ulp` and `result + 10ulp` to `PREC`
/// digits HALF_EVEN agree, then return that.
fn ziv_round<F>(mut compute: F) -> DecResult
where
    F: FnMut(usize) -> (bool, BigUint, i64),
{
    let mut g: usize = 12;
    loop {
        let digits = PREC + g;
        let (neg, c, e) = compute(digits);
        if c.is_zero() {
            return Ok(Decimal::zero());
        }
        let err = 10u64;
        let lo = if c.digit_count() > 3 {
            c.sub(&BigUint::from_u64(err))
        } else {
            BigUint::zero()
        };
        let hi = c.add(&BigUint::from_u64(err));
        let (lo_r, lo_e) = round_to_prec(&lo, e, false);
        let (hi_r, hi_e) = round_to_prec(&hi, e, false);
        if lo_r == hi_r && lo_e == hi_e {
            return round_result(neg, hi_r, hi_e, false);
        }
        if g > 2100 {
            return round_result(neg, c, e, false);
        }
        g *= 2;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(s: &str) -> Decimal {
        Decimal::parse(s).unwrap_or_else(|| panic!("failed to parse {:?}", s))
    }

    fn fmt(r: DecResult) -> String {
        match r {
            Ok(v) => v.format(),
            Err(e) => format!("ERR {}", e.message()),
        }
    }

    #[test]
    fn worked_examples() {
        assert_eq!(fmt(d("2").pow(&d("100"))), "1267650600228229401496703205000");
        assert_eq!(fmt(d("0.1").add(&d("0.2"))), "0.3");
        assert_eq!(fmt(d("1").div(&d("3"))), "0.3333333333333333333333333333");
        assert_eq!(fmt(d("2").div(&d("3"))), "0.6666666666666666666666666667");
        assert_eq!(fmt(d("-7").idiv(&d("2"))), "-3");
        assert_eq!(fmt(d("-7").rem(&d("3"))), "-1");
        assert_eq!(fmt(d("7.5").rem(&d("-2"))), "1.5");
        assert_eq!(fmt(d("2").pow(&d("0.5"))), "1.414213562373095048801688724");
        assert_eq!(fmt(d("2").pow(&d("-1"))), "0.5");
        assert_eq!(fmt(d("3").pow(&d("-2"))), "0.1111111111111111111111111111");
        assert_eq!(fmt(d("10").pow(&d("30"))), "1000000000000000000000000000000");
        assert_eq!(
            fmt(Ok(d("1234567890123456789012345678901234567890"))),
            "1234567890123456789012345678901234567890"
        );
        assert_eq!(
            fmt(Ok(d("0.1234567890123456789012345678901"))),
            "0.1234567890123456789012345679"
        );
        assert_eq!(fmt(Ok(d("1.50"))), "1.5");
        assert_eq!(fmt(d("4").pow(&d("0.5"))), "2");
        assert_eq!(fmt(d("2").pow(&d("2.25"))), "4.756828460010884266869999882");
        assert_eq!(fmt(d("-8").pow(&d("0.5"))), "ERR [<class 'decimal.InvalidOperation'>]");
        assert_eq!(fmt(d("0").pow(&d("0"))), "ERR [<class 'decimal.InvalidOperation'>]");
        assert_eq!(fmt(d("10").pow(&d("1000000"))), "ERR [<class 'decimal.Overflow'>]");
        assert_eq!(
            fmt(d("1000000000000000000000000000000").idiv(&d("0.001"))),
            "ERR [<class 'decimal.DivisionImpossible'>]"
        );
        assert_eq!(
            fmt(d("1000000000000000000000000000000").rem(&d("0.001"))),
            "ERR [<class 'decimal.DivisionImpossible'>]"
        );
        assert_eq!(fmt(d("2").pow(&d("0.1"))), "1.071773462536293164213006325");
        assert_eq!(
            fmt(d("1E-999999").div(&d("10"))),
            format!("0.{}1", "0".repeat(999999))
        );
    }

    #[test]
    fn basic_roundtrip_and_cmp() {
        assert_eq!(Decimal::from_i64(5).format(), "5");
        assert_eq!(Decimal::from_i64(-5).format(), "-5");
        assert_eq!(Decimal::zero().format(), "0");
        assert!(d("1") < d("2"));
        assert!(d("-1") < d("1"));
        assert!(d("-2") < d("-1"));
        assert_eq!(d("1.0"), d("1"));
        assert_eq!(d("1.50").format(), "1.5");
        assert_eq!(d("0"), Decimal::from_i64(0));
        assert!(d("0").is_zero());
        assert!(!d("0").is_negative());
    }

    #[test]
    fn to_i64_and_is_integer() {
        assert_eq!(d("5").to_i64(), Some(5));
        assert_eq!(d("-5").to_i64(), Some(-5));
        assert_eq!(d("5.5").to_i64(), Some(5));
        assert_eq!(d("-5.5").to_i64(), Some(-5));
        assert!(d("5").is_integer());
        assert!(!d("5.5").is_integer());
        assert!(d("50").is_integer());
    }

    #[test]
    fn from_zigzag_groups_basic() {
        // zigzag(0) = 0, zigzag(1) = -1, zigzag(2) = 1
        assert_eq!(Decimal::from_zigzag_groups(&[0]).format(), "0");
        assert_eq!(Decimal::from_zigzag_groups(&[1]).format(), "-1");
        assert_eq!(Decimal::from_zigzag_groups(&[2]).format(), "1");
        // 130 = 1*128^0 + 1*128^1 -> groups [2, 1] (low first, 7-bit payloads)
        // value z = 2 + 1*128 = 130 -> even -> 65
        assert_eq!(Decimal::from_zigzag_groups(&[2, 1]).format(), "65");
    }

    #[test]
    fn from_f64_to_f64() {
        assert_eq!(Decimal::from_f64(0.1).unwrap().format(), "0.1");
        assert_eq!(Decimal::from_f64(100.0).unwrap().format(), "100");
        assert!(Decimal::from_f64(f64::NAN).is_err());
        assert!(Decimal::from_f64(f64::INFINITY).is_err());
        assert_eq!(Decimal::from_f64(-0.0).unwrap().format(), "0");
        assert_eq!(d("0.5").to_f64(), 0.5);
        assert_eq!(d("100").to_f64(), 100.0);
    }

    #[test]
    fn format_examples() {
        assert_eq!(d("0.5").format(), "0.5");
        assert_eq!(d("-0.25").format(), "-0.25");
        assert_eq!(d("0.0001").format(), "0.0001");
        assert_eq!(d("1E-7").format(), "0.0000001");
        assert_eq!(d("123.45").format(), "123.45");
        assert_eq!(d("0.99999999999999999999999999999").format(), "1");
    }

    #[test]
    fn div_by_zero_and_zero_cases() {
        assert!(d("1").div(&d("0")).is_err());
        assert!(d("1").idiv(&d("0")).is_err());
        assert!(d("1").rem(&d("0")).is_err());
        assert_eq!(fmt(d("0").div(&d("5"))), "0");
        assert_eq!(fmt(d("0").idiv(&d("5"))), "0");
        assert_eq!(fmt(d("0").rem(&d("5"))), "0");
    }

    #[test]
    fn overflow_and_underflow() {
        assert!(matches!(d("1E999999").mul(&d("10")), Err(DecError::Overflow)));
        // Deep underflow: an arithmetic result (not a raw unrounded parse)
        // whose adjusted exponent is far below Etiny rounds to exact zero.
        assert_eq!(fmt(d("1E-999999").div(&d("1E999999"))), "0");
        // `format()` mirrors Python's `_format_number` exactly, including
        // the fractional branch's `v.normalize()` call: normalize() is a
        // *context* operation, so even a raw, never-rounded, out-of-range
        // `parse()` result is subject to the same Emin/Etiny handling as
        // any arithmetic result when it's *formatted* (verified against
        // Python: `format_number(Decimal('1E-1000030'))` is exactly "0").
        //
        // Subnormal-but-not-quite-zero (adjusted exponent -1000020, still
        // above Etiny=-1000026): prints every digit.
        assert_eq!(
            fmt(Ok(d("1E-1000020"))),
            format!("0.{}1", "0".repeat(1000019))
        );
        // Subnormal enough (adjusted exponent -1000030) to round all the
        // way down to exact zero.
        assert_eq!(fmt(Ok(d("1E-1000030"))), "0");
    }

    /// `pow` for an integral exponent must replicate libmpdec's
    /// `_mpd_qpow_int` double-rounding algorithm (left-to-right binary
    /// exponentiation, every intermediate multiply rounded to a widened
    /// working precision, then one final round to 28 digits) -- NOT the
    /// mathematically-exact-then-round-once answer. These two cases are
    /// confirmed (via `int` power, a 200-digit `decimal` recomputation,
    /// and Python's `ctx.power`) to be places where CPython's own
    /// algorithm differs from true correct rounding in its last digit; we
    /// must match CPython, not out-correct it.
    #[test]
    fn pow_integer_matches_libmpdec_double_rounding() {
        assert_eq!(
            fmt(d("514488").pow(&d("21"))),
            "868720845732965369139091077000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        );
        assert_eq!(
            fmt(d("985219").pow(&d("-67"))),
            "0.000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000002712088337380886095480252852"
        );
    }
}
