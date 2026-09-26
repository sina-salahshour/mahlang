//! A minimal arbitrary-precision unsigned integer, used internally by
//! `decimal.rs` for the `Big` coefficient fallback. Standard library only.
//!
//! Representation: little-endian limbs in base `BASE = 10^9` (so a limb
//! always fits comfortably in a `u32`, and limb*limb fits in a `u64`). This
//! makes decimal-digit counting, multiplying/dividing by a power of ten,
//! and converting to/from decimal strings cheap -- all things `decimal.rs`
//! does constantly.
//!
//! `limbs` never has a trailing (most-significant) zero limb; the empty
//! vector represents zero. This keeps `PartialEq`/`Eq`/`Hash`/`Ord` correct
//! by construction.

use std::cmp::Ordering;
use std::fmt;

/// Limb base: 10^9.
pub const BASE: u64 = 1_000_000_000;
/// Decimal digits per limb.
pub const BASE_DIGITS: u32 = 9;

#[derive(Clone, Eq, PartialOrd, Ord, Hash)]
pub struct BigUint {
    /// Little-endian, base `BASE`. No trailing zero limb. Empty = 0.
    limbs: Vec<u32>,
}

impl fmt::Debug for BigUint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "BigUint({})", self.to_decimal_string())
    }
}

impl PartialEq for BigUint {
    fn eq(&self, other: &Self) -> bool {
        self.limbs == other.limbs
    }
}

fn trim(mut limbs: Vec<u32>) -> Vec<u32> {
    while limbs.last() == Some(&0) {
        limbs.pop();
    }
    limbs
}

fn num_digits_u32(mut x: u32) -> usize {
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

impl BigUint {
    pub fn zero() -> Self {
        BigUint { limbs: Vec::new() }
    }

    pub fn is_zero(&self) -> bool {
        self.limbs.is_empty()
    }

    pub fn from_u64(mut n: u64) -> Self {
        let mut limbs = Vec::new();
        while n > 0 {
            limbs.push((n % BASE) as u32);
            n /= BASE;
        }
        BigUint { limbs }
    }

    pub fn from_u128(mut n: u128) -> Self {
        let mut limbs = Vec::new();
        let base = BASE as u128;
        while n > 0 {
            limbs.push((n % base) as u32);
            n /= base;
        }
        BigUint { limbs }
    }

    pub fn to_u64(&self) -> Option<u64> {
        let mut r: u128 = 0;
        for &l in self.limbs.iter().rev() {
            r = r.checked_mul(BASE as u128)?.checked_add(l as u128)?;
            if r > u64::MAX as u128 {
                return None;
            }
        }
        Some(r as u64)
    }

    pub fn to_u128(&self) -> Option<u128> {
        let mut r: u128 = 0;
        for &l in self.limbs.iter().rev() {
            r = r.checked_mul(BASE as u128)?.checked_add(l as u128)?;
        }
        Some(r)
    }

    /// True if the represented value is odd. Since `BASE` is even, only the
    /// lowest limb determines parity.
    pub fn is_odd(&self) -> bool {
        matches!(self.limbs.first(), Some(l) if l % 2 == 1)
    }

    pub fn cmp(&self, other: &Self) -> Ordering {
        if self.limbs.len() != other.limbs.len() {
            return self.limbs.len().cmp(&other.limbs.len());
        }
        for i in (0..self.limbs.len()).rev() {
            if self.limbs[i] != other.limbs[i] {
                return self.limbs[i].cmp(&other.limbs[i]);
            }
        }
        Ordering::Equal
    }

    pub fn add(&self, other: &Self) -> Self {
        let n = self.limbs.len().max(other.limbs.len());
        let mut res = Vec::with_capacity(n + 1);
        let mut carry: u64 = 0;
        for i in 0..n {
            let a = *self.limbs.get(i).unwrap_or(&0) as u64;
            let b = *other.limbs.get(i).unwrap_or(&0) as u64;
            let s = a + b + carry;
            res.push((s % BASE) as u32);
            carry = s / BASE;
        }
        if carry > 0 {
            res.push(carry as u32);
        }
        BigUint { limbs: trim(res) }
    }

    /// `self - other`. Requires `self >= other` (debug-asserted).
    pub fn sub(&self, other: &Self) -> Self {
        let mut res = Vec::with_capacity(self.limbs.len());
        let mut borrow: i64 = 0;
        for i in 0..self.limbs.len() {
            let a = self.limbs[i] as i64;
            let b = *other.limbs.get(i).unwrap_or(&0) as i64;
            let mut d = a - b - borrow;
            if d < 0 {
                d += BASE as i64;
                borrow = 1;
            } else {
                borrow = 0;
            }
            res.push(d as u32);
        }
        debug_assert_eq!(borrow, 0, "BigUint::sub underflow");
        BigUint { limbs: trim(res) }
    }

    pub fn mul(&self, other: &Self) -> Self {
        if self.is_zero() || other.is_zero() {
            return BigUint::zero();
        }
        let mut res = vec![0u64; self.limbs.len() + other.limbs.len()];
        for (i, &a) in self.limbs.iter().enumerate() {
            if a == 0 {
                continue;
            }
            let mut carry: u64 = 0;
            for (j, &b) in other.limbs.iter().enumerate() {
                let idx = i + j;
                let cur = res[idx] + (a as u64) * (b as u64) + carry;
                res[idx] = cur % BASE;
                carry = cur / BASE;
            }
            let mut k = i + other.limbs.len();
            while carry > 0 {
                let cur = res[k] + carry;
                res[k] = cur % BASE;
                carry = cur / BASE;
                k += 1;
            }
        }
        BigUint {
            limbs: trim(res.into_iter().map(|x| x as u32).collect()),
        }
    }

    /// `self * m` for an arbitrary (possibly >= BASE) `u64` multiplier.
    pub fn mul_small(&self, m: u64) -> Self {
        if m == 0 || self.is_zero() {
            return BigUint::zero();
        }
        let mut res = Vec::with_capacity(self.limbs.len() + 3);
        let mut carry: u128 = 0;
        let m = m as u128;
        for &l in &self.limbs {
            let cur = (l as u128) * m + carry;
            res.push((cur % BASE as u128) as u32);
            carry = cur / BASE as u128;
        }
        while carry > 0 {
            res.push((carry % BASE as u128) as u32);
            carry /= BASE as u128;
        }
        BigUint { limbs: trim(res) }
    }

    /// `(self / d, self % d)` for an arbitrary (possibly >= BASE) `u64`
    /// divisor. Panics if `d == 0`.
    pub fn divmod_small(&self, d: u64) -> (Self, u64) {
        assert!(d > 0, "division by zero");
        let mut quotient = vec![0u32; self.limbs.len()];
        let mut rem: u128 = 0;
        let d128 = d as u128;
        for i in (0..self.limbs.len()).rev() {
            let cur = rem * BASE as u128 + self.limbs[i] as u128;
            quotient[i] = (cur / d128) as u32;
            rem = cur % d128;
        }
        (BigUint { limbs: trim(quotient) }, rem as u64)
    }

    /// Number of decimal digits (0 counts as having 1 digit).
    pub fn digit_count(&self) -> usize {
        match self.limbs.last() {
            None => 1,
            Some(&top) => BASE_DIGITS as usize * (self.limbs.len() - 1) + num_digits_u32(top),
        }
    }

    /// 10^k, computed directly (no squaring needed since BASE is itself a
    /// power of ten).
    pub fn pow10(k: u64) -> Self {
        if k == 0 {
            return BigUint::from_u64(1);
        }
        let whole = (k / BASE_DIGITS as u64) as usize;
        let rem = (k % BASE_DIGITS as u64) as u32;
        let mut limbs = vec![0u32; whole];
        limbs.push(10u32.pow(rem));
        BigUint { limbs }
    }

    /// `self * 10^k`.
    pub fn mul_pow10(&self, k: u64) -> Self {
        if self.is_zero() {
            return BigUint::zero();
        }
        let whole = (k / BASE_DIGITS as u64) as usize;
        let rem = (k % BASE_DIGITS as u64) as u32;
        let mut limbs = vec![0u32; whole];
        limbs.extend_from_slice(&self.limbs);
        let shifted = BigUint { limbs };
        if rem == 0 {
            shifted
        } else {
            shifted.mul_small(10u64.pow(rem))
        }
    }

    /// `(self / 10^k, self % 10^k)`.
    pub fn divmod_pow10(&self, k: u64) -> (Self, Self) {
        let whole = (k / BASE_DIGITS as u64) as usize;
        let rem = (k % BASE_DIGITS as u64) as u32;
        if whole >= self.limbs.len() {
            return (BigUint::zero(), self.clone());
        }
        let low = self.limbs[0..whole].to_vec();
        let high = BigUint {
            limbs: trim(self.limbs[whole..].to_vec()),
        };
        if rem == 0 {
            return (high, BigUint { limbs: trim(low) });
        }
        let (q, r) = high.divmod_small(10u64.pow(rem));
        let mut rem_limbs = low;
        rem_limbs.push(r as u32);
        (q, BigUint { limbs: trim(rem_limbs) })
    }

    /// Strip trailing decimal zeros: returns `(value with zeros removed,
    /// count of zeros removed)`. `(0, 0)` for zero.
    pub fn strip_trailing_zeros(&self) -> (Self, u64) {
        if self.is_zero() {
            return (BigUint::zero(), 0);
        }
        let mut idx = 0;
        while self.limbs[idx] == 0 {
            idx += 1;
        }
        let zero_limbs = idx as u64;
        let bottom = self.limbs[idx];
        let mut v = bottom;
        let mut extra = 0u64;
        while v % 10 == 0 {
            v /= 10;
            extra += 1;
        }
        let total = zero_limbs * BASE_DIGITS as u64 + extra;
        let (q, _r) = self.divmod_pow10(total);
        (q, total)
    }

    /// General division: `(self / other, self % other)`. Panics if `other`
    /// is zero.
    pub fn divmod(&self, other: &Self) -> (Self, Self) {
        assert!(!other.is_zero(), "division by zero");
        if self.cmp(other) == Ordering::Less {
            return (BigUint::zero(), self.clone());
        }
        if other.limbs.len() == 1 {
            let (q, r) = self.divmod_small(other.limbs[0] as u64);
            return (q, BigUint::from_u64(r));
        }
        // Knuth Algorithm D, base = BASE (10^9) "digits".
        let n = other.limbs.len();
        // Normalize so the divisor's top limb is >= BASE/2.
        let top = other.limbs[n - 1] as u64;
        let d = BASE / (top + 1);
        let u_norm = self.mul_small(d);
        let v = other.mul_small(d);
        debug_assert_eq!(v.limbs.len(), n);

        let m = u_norm.limbs.len().saturating_sub(n);
        // u needs m+n+1 limbs (pad high end with zero if needed).
        let mut u = u_norm.limbs.clone();
        while u.len() < m + n + 1 {
            u.push(0);
        }

        let mut q = vec![0u32; m + 1];
        let v_top = v.limbs[n - 1] as u64;
        let v_second = if n >= 2 { v.limbs[n - 2] as u64 } else { 0 };

        for j in (0..=m).rev() {
            let u_top2 = (u[j + n] as u64) * BASE + (u[j + n - 1] as u64);
            let mut qhat = u_top2 / v_top;
            let mut rhat = u_top2 % v_top;
            if qhat >= BASE {
                qhat = BASE - 1;
                rhat = u_top2 - qhat * v_top;
            }
            while rhat < BASE
                && qhat * v_second > rhat * BASE + if n >= 2 { u[j + n - 2] as u64 } else { 0 }
            {
                qhat -= 1;
                rhat += v_top;
            }
            // Multiply-and-subtract: u[j..j+n+1] -= qhat * v[0..n]
            let mut borrow: i64 = 0;
            let mut carry: u64 = 0;
            for i in 0..n {
                let p = qhat * (v.limbs[i] as u64) + carry;
                carry = p / BASE;
                let sub = (p % BASE) as i64;
                let mut cur = u[j + i] as i64 - sub - borrow;
                if cur < 0 {
                    cur += BASE as i64;
                    borrow = 1;
                } else {
                    borrow = 0;
                }
                u[j + i] = cur as u32;
            }
            let mut cur = u[j + n] as i64 - carry as i64 - borrow;
            if cur < 0 {
                cur += BASE as i64;
                borrow = 1;
            } else {
                borrow = 0;
            }
            u[j + n] = cur as u32;

            if borrow != 0 {
                // qhat was one too large: add v back once.
                qhat -= 1;
                let mut carry2: u64 = 0;
                for i in 0..n {
                    let s = u[j + i] as u64 + v.limbs[i] as u64 + carry2;
                    u[j + i] = (s % BASE) as u32;
                    carry2 = s / BASE;
                }
                let s = u[j + n] as u64 + carry2;
                u[j + n] = (s % BASE) as u32;
                // overflow out of top limb is expected/discarded here.
            }
            q[j] = qhat as u32;
        }

        let remainder_norm = BigUint {
            limbs: trim(u[0..n].to_vec()),
        };
        let (remainder, _r0) = remainder_norm.divmod_small(d);
        (BigUint { limbs: trim(q) }, remainder)
    }

    /// `self^exp` by repeated squaring.
    pub fn pow_u64(&self, mut exp: u64) -> Self {
        if exp == 0 {
            return BigUint::from_u64(1);
        }
        let mut base = self.clone();
        let mut result = BigUint::from_u64(1);
        while exp > 0 {
            if exp & 1 == 1 {
                result = result.mul(&base);
            }
            exp >>= 1;
            if exp > 0 {
                base = base.mul(&base);
            }
        }
        result
    }

    /// Parse a plain (unsigned) decimal digit string. `None` if empty or
    /// contains a non-digit.
    pub fn from_decimal_str(s: &str) -> Option<Self> {
        if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let bytes = s.as_bytes();
        let mut limbs = Vec::with_capacity(bytes.len() / BASE_DIGITS as usize + 1);
        let mut end = bytes.len();
        while end > 0 {
            let start = end.saturating_sub(BASE_DIGITS as usize);
            let chunk = std::str::from_utf8(&bytes[start..end]).unwrap();
            let limb: u32 = chunk.parse().unwrap();
            limbs.push(limb);
            end = start;
        }
        Some(BigUint { limbs: trim(limbs) })
    }

    pub fn to_decimal_string(&self) -> String {
        if self.limbs.is_empty() {
            return "0".to_string();
        }
        let mut s = String::with_capacity(self.limbs.len() * BASE_DIGITS as usize);
        for (i, &limb) in self.limbs.iter().enumerate().rev() {
            if i == self.limbs.len() - 1 {
                s.push_str(&limb.to_string());
            } else {
                s.push_str(&format!("{:0width$}", limb, width = BASE_DIGITS as usize));
            }
        }
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basic_roundtrip() {
        let a = BigUint::from_u64(123456789012345);
        assert_eq!(a.to_decimal_string(), "123456789012345");
        assert_eq!(a.to_u64(), Some(123456789012345));
    }

    #[test]
    fn add_sub() {
        let a = BigUint::from_decimal_str("999999999999999999999999999999").unwrap();
        let b = BigUint::from_u64(1);
        let c = a.add(&b);
        assert_eq!(c.to_decimal_string(), "1000000000000000000000000000000");
        assert_eq!(c.sub(&b).to_decimal_string(), a.to_decimal_string());
    }

    #[test]
    fn mul_basic() {
        let a = BigUint::from_decimal_str("123456789123456789").unwrap();
        let b = BigUint::from_decimal_str("987654321987654321").unwrap();
        let c = a.mul(&b);
        // Verified with python: 123456789123456789 * 987654321987654321
        assert_eq!(
            c.to_decimal_string(),
            "121932631356500531347203169112635269"
        );
    }

    #[test]
    fn divmod_small_cases() {
        let a = BigUint::from_u64(1000);
        let (q, r) = a.divmod(&BigUint::from_u64(7));
        assert_eq!(q.to_u64(), Some(142));
        assert_eq!(r.to_u64(), Some(6));
    }

    #[test]
    fn divmod_big() {
        let a = BigUint::from_decimal_str(&"7".repeat(50)).unwrap();
        let b = BigUint::from_decimal_str(&"3".repeat(25)).unwrap();
        let (q, r) = a.divmod(&b);
        // Check via reconstruction: q*b + r == a, and 0 <= r < b
        let recon = q.mul(&b).add(&r);
        assert_eq!(recon, a);
        assert_eq!(r.cmp(&b), Ordering::Less);
    }

    #[test]
    fn divmod_random_consistency() {
        // simple xorshift PRNG for determinism without extra crates
        let mut state: u64 = 0x243F6A8885A308D3;
        let mut next = || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _ in 0..200 {
            let a_digits = 1 + (next() % 60) as usize;
            let b_digits = 1 + (next() % 60) as usize;
            let a_str: String = (0..a_digits)
                .map(|i| {
                    let d = (next() % 10) as u8;
                    if i == 0 && d == 0 {
                        b'1'
                    } else {
                        b'0' + d
                    }
                })
                .map(|b| b as char)
                .collect();
            let b_str: String = (0..b_digits)
                .map(|i| {
                    let d = (next() % 10) as u8;
                    if i == 0 && d == 0 {
                        b'1'
                    } else {
                        b'0' + d
                    }
                })
                .map(|b| b as char)
                .collect();
            let a = BigUint::from_decimal_str(&a_str).unwrap();
            let b = BigUint::from_decimal_str(&b_str).unwrap();
            if b.is_zero() {
                continue;
            }
            let (q, r) = a.divmod(&b);
            assert!(r.cmp(&b) == Ordering::Less);
            let recon = q.mul(&b).add(&r);
            assert_eq!(recon, a, "a={} b={} q={:?} r={:?}", a_str, b_str, q, r);
        }
    }

    #[test]
    fn pow10_and_mulpow10() {
        assert_eq!(BigUint::pow10(0).to_decimal_string(), "1");
        assert_eq!(BigUint::pow10(9).to_decimal_string(), "1000000000");
        assert_eq!(BigUint::pow10(10).to_decimal_string(), "10000000000");
        let a = BigUint::from_u64(7);
        assert_eq!(a.mul_pow10(12).to_decimal_string(), "7000000000000");
    }

    #[test]
    fn strip_zeros() {
        let a = BigUint::from_decimal_str("1230000").unwrap();
        let (s, n) = a.strip_trailing_zeros();
        assert_eq!(s.to_decimal_string(), "123");
        assert_eq!(n, 4);
    }

    #[test]
    fn digit_count_basic() {
        assert_eq!(BigUint::zero().digit_count(), 1);
        assert_eq!(BigUint::from_u64(9).digit_count(), 1);
        assert_eq!(BigUint::from_u64(10).digit_count(), 2);
        assert_eq!(BigUint::pow10(28).digit_count(), 29);
    }
}
