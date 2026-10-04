//! Finite decimal facts: checked i128 fast path with arbitrary-width fallback.
//! Division is a derived calculation with 28 decimal places and half-even rounding.
use bigdecimal::{BigDecimal, RoundingMode};
use num_bigint::BigInt;
use num_traits::{FromPrimitive, ToPrimitive};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use std::{
    cmp::Ordering,
    fmt,
    hash::{Hash, Hasher},
    iter::Sum,
    ops::{Add, AddAssign, Div, DivAssign, Mul, MulAssign, Neg, Sub, SubAssign},
    str::FromStr,
    sync::Arc,
};
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct DecimalError(pub String);
pub const SOURCE_DECIMAL_LIMIT:usize=4096;
pub const INTERNAL_DECIMAL_LIMIT:usize=16384;
pub const CANONICAL_DECIMAL_TEXT_LIMIT:usize=2*INTERNAL_DECIMAL_LIMIT+3;
#[derive(Clone, Debug)]
enum Coefficient {
    Small(i128),
    Wide(Arc<BigInt>),
}
#[derive(Clone, Debug)]
pub struct Decimal {
    coefficient: Coefficient,
    scale: i64,
}
impl Decimal {
    pub const ZERO: Self = Self {
        coefficient: Coefficient::Small(0),
        scale: 0,
    };
    pub const ONE: Self = Self {
        coefficient: Coefficient::Small(1),
        scale: 0,
    };
    pub fn new(coefficient: i64, scale: u32) -> Self {
        Self::small(coefficient as i128, scale as i64)
    }
    fn small(mut coefficient: i128, mut scale: i64) -> Self {
        if coefficient == 0 {
            return Self::ZERO;
        }
        // Canonical coefficients have no trailing zero, including integers.
        // Otherwise encode/decode changes (10,0) into (1,-1), and the same
        // exact sum can acquire different bytes across aggregate node levels.
        while coefficient % 10 == 0 && scale>i64::MIN {
            coefficient /= 10;
            scale -= 1
        }
        Self {
            coefficient: Coefficient::Small(coefficient),
            scale,
        }
    }
    pub fn wide(value: BigDecimal) -> Self {
        let (coefficient, scale) = value.normalized().as_bigint_and_exponent();
        match coefficient.to_i128() {
            Some(v) => Self::small(v, scale),
            None => Self {
                coefficient: Coefficient::Wide(Arc::new(coefficient)),
                scale,
            },
        }
    }
    pub fn as_bigdecimal(&self) -> BigDecimal {
        BigDecimal::new(
            match &self.coefficient {
                Coefficient::Small(v) => BigInt::from(*v),
                Coefficient::Wide(v) => (**v).clone(),
            },
            self.scale,
        )
    }
    pub(crate) fn coefficient_and_scale(&self) -> (BigInt, i64) {
        match &self.coefficient {
            Coefficient::Small(value) => (BigInt::from(*value), self.scale),
            Coefficient::Wide(value) => ((**value).clone(), self.scale),
        }
    }
    pub(crate) fn from_coefficient(coefficient: BigInt, scale: i64) -> Self {
        Self::wide(BigDecimal::new(coefficient, scale))
    }
    pub fn from_str_exact(value: &str) -> Result<Self, DecimalError> {
        if value.len() > CANONICAL_DECIMAL_TEXT_LIMIT {
            return Err(DecimalError(
                "decimal lexeme exceeds bounded canonical limit".into(),
            ));
        }
        let d = BigDecimal::from_str(value).map_err(|e| DecimalError(e.to_string()))?;
        let (coefficient, scale) = d.normalized().as_bigint_and_exponent();
        if scale.unsigned_abs() > INTERNAL_DECIMAL_LIMIT as u64 || coefficient.to_string().trim_start_matches('-').len()>INTERNAL_DECIMAL_LIMIT {
            return Err(DecimalError(
                "decimal coefficient/exponent exceeds bounded canonical limit".into(),
            ));
        }
        Ok(Self::wide(d))
    }
    /// Provider lexical bounds are separate from canonical/derived representations.
    pub fn from_source_str(value:&str)->Result<Self,DecimalError> {
        if value.len()>SOURCE_DECIMAL_LIMIT {return Err(DecimalError("source decimal lexeme exceeds 4096 bytes".into()));}
        let result=Self::from_str_exact(value)?;
        if result.scale.unsigned_abs()>SOURCE_DECIMAL_LIMIT as u64 || result.coefficient_digits()>SOURCE_DECIMAL_LIMIT as u32 {
            return Err(DecimalError("source decimal coefficient/exponent exceeds 4096".into()));
        }Ok(result)
    }
    pub fn from_scientific(value: &str) -> Result<Self, DecimalError> {
        Self::from_str_exact(value)
    }
    pub fn from_f64_retain(value: f64) -> Option<Self> {
        BigDecimal::from_f64(value).map(Self::wide)
    }
    pub fn to_f64(&self) -> Option<f64> {
        self.as_bigdecimal().to_f64()
    }
    pub fn to_i64(&self) -> Option<i64> { self.as_bigdecimal().to_i64() }
    pub fn to_u64(&self) -> Option<u64> { self.as_bigdecimal().to_u64() }
    pub fn fract(&self) -> Self { self.clone()-Self::wide(self.as_bigdecimal().with_scale(0)) }
    pub fn normalize(&self) -> Self { self.clone() }
    pub fn abs(&self) -> Self {
        if self < &Self::ZERO {
            -self.clone()
        } else {
            self.clone()
        }
    }
    pub fn scale(&self) -> u32 {
        self.scale.max(0).try_into().expect("bounded scale")
    }
    pub fn coefficient_digits(&self) -> u32 {
        match &self.coefficient {
            Coefficient::Small(v) => v.unsigned_abs().to_string().len() as u32,
            Coefficient::Wide(v) => v.to_string().trim_start_matches('-').len() as u32,
        }
    }
    fn aligned(&self, scale: i64) -> Option<i128> {
        let Coefficient::Small(coefficient) = self.coefficient else {
            return None;
        };
        if coefficient == 0 {
            return Some(0);
        }
        coefficient.checked_mul(10i128.checked_pow((scale - self.scale).try_into().ok()?)?)
    }
    pub fn checked_add(self, other: Self) -> Option<Self> {
        let scale = self.scale.max(other.scale);
        if let Some(v) = self
            .aligned(scale)
            .and_then(|a| other.aligned(scale).and_then(|b| a.checked_add(b)))
        {
            Some(Self::small(v, scale))
        } else {
            Some(Self::wide(self.as_bigdecimal() + other.as_bigdecimal()))
        }
    }
    pub fn checked_sub(self, other: Self) -> Option<Self> {
        self.checked_add(-other)
    }
    pub fn checked_mul(self, other: Self) -> Option<Self> {
        let scale = self.scale.checked_add(other.scale)?;
        if let (Coefficient::Small(a), Coefficient::Small(b)) =
            (&self.coefficient, &other.coefficient)
        {
            if let Some(v) = a.checked_mul(*b) {
                return Some(Self::small(v, scale));
            }
        }
        Some(Self::wide(self.as_bigdecimal() * other.as_bigdecimal()))
    }
    pub fn checked_div(self, other: Self) -> Option<Self> {
        self.div_to_scale(&other, 28)
    }
    pub fn div_significant(&self, other: &Self, digits: u32) -> Option<Self> {
        if digits == 0 || other == &Self::ZERO {
            return None;
        }
        if self == &Self::ZERO {
            return Some(Self::ZERO);
        }
        let (a, sa) = self.abs().as_bigdecimal().as_bigint_and_exponent();
        let (b, sb) = other.abs().as_bigdecimal().as_bigint_and_exponent();
        let mut exponent = a.to_string().len() as i64 - b.to_string().len() as i64;
        let below = if exponent >= 0 {
            a < &b * BigInt::from(10u8).pow(exponent as u32)
        } else {
            &a * BigInt::from(10u8).pow((-exponent) as u32) < b
        };
        if below {
            exponent -= 1
        }
        let order = exponent.checked_add(sb)?.checked_sub(sa)?;
        self.div_to_scale(other, (digits as i64).checked_sub(1)?.checked_sub(order)?)
    }
    pub fn div_to_scale(&self, other: &Self, result_scale: i64) -> Option<Self> {
        if other == &Self::ZERO {
            return None;
        }
        let (mut a, a_scale) = self.as_bigdecimal().as_bigint_and_exponent();
        let (mut b, b_scale) = other.as_bigdecimal().as_bigint_and_exponent();
        let exponent = result_scale.checked_add(b_scale)?.checked_sub(a_scale)?;
        if exponent.unsigned_abs() > 16384 {
            return None;
        }
        let power = BigInt::from(10u8).pow(exponent.unsigned_abs() as u32);
        if exponent >= 0 {
            a *= power
        } else {
            b *= power
        }
        let negative = (a < BigInt::from(0)) != (b < BigInt::from(0));
        if a < BigInt::from(0) {
            a = -a
        }
        if b < BigInt::from(0) {
            b = -b
        }
        let mut quotient = &a / &b;
        let twice = (&a % &b) * 2;
        if twice > b || (twice == b && (&quotient % 2) != BigInt::from(0)) {
            quotient += 1
        }
        Some(Self::wide(BigDecimal::new(
            if negative { -quotient } else { quotient },
            result_scale,
        )))
    }
    pub fn round_dp(self, scale: u32) -> Self {
        Self::wide(
            self.as_bigdecimal()
                .with_scale_round(scale.into(), RoundingMode::HalfEven),
        )
    }
    pub fn round_significant(self, digits: u32) -> Self {
        let excess = self.coefficient_digits().saturating_sub(digits) as i64;
        Self::wide(
            self.as_bigdecimal()
                .with_scale_round(self.scale - excess, RoundingMode::HalfEven),
        )
    }
    pub fn round_dp_with_strategy(
        self,
        scale: u32,
        strategy: rust_decimal::RoundingStrategy,
    ) -> Self {
        use rust_decimal::RoundingStrategy::*;
        let mode = match strategy {
            MidpointNearestEven => RoundingMode::HalfEven,
            MidpointAwayFromZero => RoundingMode::HalfUp,
            MidpointTowardZero => RoundingMode::HalfDown,
            ToZero => RoundingMode::Down,
            AwayFromZero => RoundingMode::Up,
            ToPositiveInfinity => RoundingMode::Ceiling,
            ToNegativeInfinity => RoundingMode::Floor,
            // Deprecated strategy aliases retain their documented operation.
            #[allow(deprecated)]
            BankersRounding => RoundingMode::HalfEven,
            #[allow(deprecated)]
            RoundHalfUp => RoundingMode::HalfUp,
            #[allow(deprecated)]
            RoundHalfDown => RoundingMode::HalfDown,
            #[allow(deprecated)]
            RoundDown => RoundingMode::Down,
            #[allow(deprecated)]
            RoundUp => RoundingMode::Up,
        };
        Self::wide(self.as_bigdecimal().with_scale_round(scale.into(), mode))
    }
}
impl fmt::Display for Decimal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.as_bigdecimal().normalized().to_plain_string())
    }
}
impl FromStr for Decimal {
    type Err = DecimalError;
    fn from_str(v: &str) -> Result<Self, DecimalError> {
        Self::from_str_exact(v)
    }
}
impl PartialEq for Decimal {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == Ordering::Equal
    }
}
impl Eq for Decimal {}
impl PartialOrd for Decimal {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Decimal {
    fn cmp(&self, other: &Self) -> Ordering {
        let scale = self.scale.max(other.scale);
        match (self.aligned(scale), other.aligned(scale)) {
            (Some(a), Some(b)) => a.cmp(&b),
            _ => self.as_bigdecimal().cmp(&other.as_bigdecimal()),
        }
    }
}
impl Hash for Decimal {
    fn hash<H: Hasher>(&self, h: &mut H) {
        self.to_string().hash(h)
    }
}
impl Serialize for Decimal {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(&self.to_string())
    }
}
impl<'de> Deserialize<'de> for Decimal {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let v = serde_json::Value::deserialize(d)?;
        let text = match v {
            serde_json::Value::String(s) => s,
            serde_json::Value::Number(n) => n.to_string(),
            _ => return Err(serde::de::Error::custom("decimal text required")),
        };
        Self::from_str_exact(&text).map_err(serde::de::Error::custom)
    }
}
impl Neg for Decimal {
    type Output = Self;
    fn neg(self) -> Self {
        match self.coefficient {
            Coefficient::Small(v) => v
                .checked_neg()
                .map(|v| Self::small(v, self.scale))
                .unwrap_or_else(|| Self::wide(-BigDecimal::new(BigInt::from(v), self.scale))),
            Coefficient::Wide(v) => Self::wide(-BigDecimal::new((*v).clone(), self.scale)),
        }
    }
}
macro_rules! ops {
    ($trait:ident,$method:ident,$checked:ident) => {
        impl $trait for Decimal {
            type Output = Self;
            fn $method(self, other: Self) -> Self {
                self.$checked(other)
                    .expect("invalid derived decimal operation")
            }
        }
        impl<'a> $trait<&'a Decimal> for Decimal {
            type Output = Self;
            fn $method(self, other: &'a Decimal) -> Self {
                self.$checked(other.clone())
                    .expect("invalid derived decimal operation")
            }
        }
        impl<'a> $trait<Decimal> for &'a Decimal {
            type Output = Decimal;
            fn $method(self, other: Decimal) -> Decimal {
                self.clone()
                    .$checked(other)
                    .expect("invalid derived decimal operation")
            }
        }
        impl<'a, 'b> $trait<&'b Decimal> for &'a Decimal {
            type Output = Decimal;
            fn $method(self, other: &'b Decimal) -> Decimal {
                self.clone()
                    .$checked(other.clone())
                    .expect("invalid derived decimal operation")
            }
        }
    };
}
ops!(Add, add, checked_add);
ops!(Sub, sub, checked_sub);
ops!(Mul, mul, checked_mul);
ops!(Div, div, checked_div);
macro_rules! assign{($trait:ident,$method:ident,$op:tt)=>{impl $trait for Decimal{fn $method(&mut self,other:Self){*self=self.clone() $op other;}}};}
assign!(AddAssign,add_assign,+);
assign!(SubAssign,sub_assign,-);
assign!(MulAssign,mul_assign,*);
assign!(DivAssign,div_assign,/);
macro_rules! primitive{($($type:ty),*)=>{$(impl From<$type> for Decimal{fn from(v:$type)->Self{Self::small(v as i128,0)}})*};}
primitive!(u8, u16, u32, u64, usize, i8, i16, i32, i64, i128);
impl Sum for Decimal {
    fn sum<I: Iterator<Item = Self>>(iter: I) -> Self {
        iter.fold(Self::ZERO, |a, b| a + b)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wide_finite_values_and_operations_are_exact() {
        let max = Decimal::from_str_exact("79228162514264337593543950335").unwrap();
        let tiny = Decimal::from_str_exact("0.0000000000000000000000000001").unwrap();
        assert_eq!(
            (max + tiny).to_string(),
            "79228162514264337593543950335.0000000000000000000000000001"
        );
        let wide = Decimal::from_str_exact("1234567890123456789.123456789012345678").unwrap();
        assert_eq!(wide.to_string(), "1234567890123456789.123456789012345678");
        assert_eq!(
            serde_json::to_string(&wide).unwrap(),
            "\"1234567890123456789.123456789012345678\""
        );
        assert_eq!(
            (Decimal::from(1) / Decimal::from(6)).to_string(),
            "0.1666666666666666666666666667"
        );
    }
}
