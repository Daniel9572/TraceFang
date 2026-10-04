//! Wide finite decimals; only division/root/transcendental statistics are rounded.
use anyhow::{Result, ensure};
use bigdecimal::{BigDecimal, RoundingMode};
use num_bigint::BigInt;
use num_traits::{One, Signed, Zero};
use serde::{Deserialize, Deserializer, Serializer};
use std::str::FromStr;

pub type D = BigDecimal;
pub const SCALE: i64 = 28;
pub const POLICY: &str = "arbitrary-width exact add/multiply; division/root 28 decimal places half-even; POC bin indices exact rational, bin centers/input weights input-scale+16 half-even; ln/tanh 96-place guarded series, absolute approximation error below 1e-60 before final 28-place half-even (not certified rounding at a half-ulp boundary); exp statistics |x|<=1200 relative approximation error below 1e-60 before quantization";
const WORK_SCALE: i64 = 96;
pub fn n(value: usize) -> D { D::from(u64::try_from(value).expect("bounded count")) }
pub fn d(value: &str) -> D { D::from_str(value).expect("decimal literal") }
pub fn text(value: &D) -> String { value.normalized().to_plain_string() }
pub fn parse(value: &str) -> Result<D> {
    ensure!(!value.is_empty(), "decimal text must not be empty");
    ensure!(!value.contains(['e', 'E']), "use plain decimal notation");
    Ok(crate::exact::Decimal::from_str_exact(value)?.as_bigdecimal())
}
pub mod decimal {
    use super::*;
    pub fn serialize<S: Serializer>(value: &D, serializer: S) -> Result<S::Ok, S::Error> { serializer.serialize_str(&text(value)) }
    pub fn deserialize<'de, T: Deserializer<'de>>(deserializer: T) -> Result<D, T::Error> {
        let value = String::deserialize(deserializer)?;
        parse(&value).map_err(serde::de::Error::custom)
    }
}
pub mod optional_decimal {
    use super::*;
    pub fn serialize<S: Serializer>(value: &Option<D>, serializer: S) -> Result<S::Ok, S::Error> {
        match value { Some(value) => serializer.serialize_some(&text(value)), None => serializer.serialize_none() }
    }
    pub fn deserialize<'de, T: Deserializer<'de>>(deserializer: T) -> Result<Option<D>, T::Error> {
        Option::<String>::deserialize(deserializer)?.map(|value| parse(&value).map_err(serde::de::Error::custom)).transpose()
    }
}
fn ten(exponent: i64) -> BigInt { BigInt::from(10_u8).pow(u32::try_from(exponent).expect("bounded exponent")) }
pub fn round(value: &D, scale: u32) -> D { value.with_scale_round(scale.into(), RoundingMode::HalfEven) }
pub fn div(a: &D, b: &D) -> D { div_at(a, b, SCALE) }
pub fn div_at(a: &D, b: &D, places: i64) -> D {
    assert!(!b.is_zero(), "internal division requires a nonzero denominator");
    let (mut numerator, a_scale) = a.as_bigint_and_exponent();
    let (mut denominator, b_scale) = b.as_bigint_and_exponent();
    let exponent = places + b_scale - a_scale;
    if exponent >= 0 { numerator *= ten(exponent); } else { denominator *= ten(-exponent); }
    let negative = numerator.is_negative() != denominator.is_negative();
    numerator = numerator.abs(); denominator = denominator.abs();
    let mut quotient = &numerator / &denominator;
    let remainder = &numerator % &denominator;
    let twice = remainder * 2_u8;
    if twice > denominator || (twice == denominator && (&quotient % 2) != BigInt::zero()) { quotient += 1; }
    D::new(if negative { -quotient } else { quotient }, places)
}
/// Exact floor of a rational; used for bins, never divide by a rounded bin width.
pub fn floor_ratio(a:&D,b:&D)->BigInt{assert!(!b.is_zero());let(mut numerator,ascl)=a.as_bigint_and_exponent();let(mut denominator,bscl)=b.as_bigint_and_exponent();let exponent=bscl-ascl;if exponent>=0{numerator*=ten(exponent);}else{denominator*=ten(-exponent);}if denominator.is_negative(){numerator=-numerator;denominator=-denominator;}let mut q=&numerator/&denominator;if numerator.is_negative()&&!(&numerator%&denominator).is_zero(){q-=1;}q}
pub fn population_deviation(sum:&D,sum_squares:&D,count:usize)->Option<D>{if count==0{return None;}let count=n(count);let numerator=&count*sum_squares-sum*sum;sqrt(&div_at(&numerator.max(D::zero()),&(&count*&count),96))}
fn integer_sqrt(value: &BigInt) -> BigInt {
    if value.is_zero() { return BigInt::zero(); }
    let mut x = BigInt::one() << usize::try_from(value.bits().div_ceil(2)).unwrap();
    loop { let next = (&x + value / &x) >> 1; if next >= x { return x; } x = next; }
}
pub fn sqrt(value: &D) -> Option<D> {
    if value.is_negative() { return None; }
    let (mut numerator, scale) = value.as_bigint_and_exponent();
    let mut denominator = BigInt::one();
    let exponent = SCALE * 2 - scale;
    if exponent >= 0 { numerator *= ten(exponent); } else { denominator = ten(-exponent); }
    let mut root = integer_sqrt(&(&numerator / &denominator));
    let boundary = &root * 2_u8 + 1_u8;
    let comparison = (&numerator * 4_u8).cmp(&(&denominator * &boundary * boundary));
    if comparison.is_gt() || (comparison.is_eq() && (&root % 2) != BigInt::zero()) { root += 1; }
    Some(D::new(root, SCALE))
}
// Guarded approximations: the reduced ln series has |z| <= 1/5, and
// the exp series uses |x| <= 1/2. 1e-90 tail stopping plus 96-place
// arithmetic leaves a conservative absolute error budget <1e-60 for
// supported inputs (canonical decimals accepted by the shared exact kernel). This is not an interval
// proof of correctly rounded output when the result lies at a half-ulp.
fn work(value: D) -> D { value.with_scale_round(WORK_SCALE, RoundingMode::HalfEven) }
fn ln_work(value: &D) -> Option<D> {
    if value <= &D::zero() { return None; }
    let mut x = value.clone(); let mut power = 0_i64;
    while x > d("1.5") { x = div_at(&x, &d("2"), WORK_SCALE); power += 1; }
    while x < d("0.75") { x *= 2; power -= 1; }
    let z = div_at(&(&x - 1), &(&x + 1), WORK_SCALE); let z2 = work(&z * &z);
    let mut term = z.clone(); let mut sum = z;
    for index in 1..4096_i64 {
        term = work(&term * &z2);
        let part = div_at(&term, &D::from(index * 2 + 1), WORK_SCALE);
        sum += &part;
        if part.abs() < d("0.000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001") { break; }
    }
    let ln2 = d("0.693147180559945309417232121458176568075500134360255254120680009493393621969694715605863326996418687542001481");
    Some(work(sum * 2_i64 + ln2 * power))
}
pub fn ln(value: &D) -> Option<D> { ln_work(value).map(|v|v.with_scale_round(SCALE,RoundingMode::HalfEven)) }
pub fn ln_ratio(a:&D,b:&D)->Option<D>{if a<=&D::zero()||b<=&D::zero(){return None;}if a==b{return Some(D::zero());}let ratio=div_at(a,b,WORK_SCALE);if ratio>D::zero(){ln(&ratio)}else{Some((ln_work(a)?-ln_work(b)?).with_scale_round(SCALE,RoundingMode::HalfEven))}}
fn exp_work(value: &D) -> D {
    let negative = value.is_negative(); let mut x = value.abs(); let mut squarings = 0;
    while x > d("0.5") { x = div_at(&x, &d("2"), WORK_SCALE); squarings += 1; }
    let mut term = D::one(); let mut exponential = D::one();
    for index in 1..4096_u32 {
        term = div_at(&(&term * &x), &D::from(index), WORK_SCALE); exponential += &term;
        if term.abs() < d("0.000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000001") { break; }
    }
    for _ in 0..squarings { exponential = work(&exponential * &exponential); }
    if negative { div_at(&D::one(), &exponential, WORK_SCALE) } else { exponential }
}
/// Statistics domain only: |x| <= 1200. Relative approximation error <1e-60
/// before final quantization; absolute error grows with exp(x), unlike ln/tanh.
pub fn exp(value: &D) -> Option<D> { if value.abs()>d("1200"){None}else{Some(exp_work(value).with_scale_round(SCALE,RoundingMode::HalfEven))} }
pub fn tanh(value: &D) -> D {
    if value >= &d("80") { return D::one(); } if value <= &d("-80") { return -D::one(); }
    // Use |x| so subtraction never loses a tiny negative argument.
    let exponential = exp_work(&(value.abs() * 2));
    let result = div_at(&(&exponential - 1), &(&exponential + 1), WORK_SCALE).with_scale_round(SCALE, RoundingMode::HalfEven);
    if value.is_negative() { -result } else { result }
}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn wide_operations_and_rounding() {
        let maximum = d("79228162514264337593543950335");
        let tiny = d("0.0000000000000000000000000001");
        assert_eq!(text(&(&maximum + &tiny)), "79228162514264337593543950335.0000000000000000000000000001");
        assert_eq!(text(&div(&d("1"), &d("6"))), "0.1666666666666666666666666667");
        assert_eq!(round(&d("2.345"), 2), d("2.34"));
        assert_eq!(round(&d("2.355"), 2), d("2.36"));
        assert_eq!(sqrt(&d("4")), Some(d("2")));
        assert_eq!(sqrt(&d("2")), Some(d("1.4142135623730950488016887242")));
        assert_eq!(ln(&d("1")), Some(D::zero()));
        assert_eq!(tanh(&D::zero()), D::zero());
    }
}
