/** Display exact decimal text without routing cash or identity through binary floats. */
export function formatQuantDecimal(value: string | null | undefined, places = 2, suffix = ""): string {
  if (value == null || !/^[+-]?\d+(?:\.\d+)?$/.test(value)) return "—";
  const negative = value.startsWith("-");
  const [whole, fraction = ""] = value.replace(/^[+-]/, "").split(".");
  const precision = Math.max(0, Math.min(28, Math.trunc(places)));
  const retained = fraction.slice(0, precision).padEnd(precision, "0");
  let coefficient = BigInt(whole + retained);
  const removed = fraction.slice(precision);
  if (removed) {
    const leading = removed[0];
    if (leading > "5" || (leading === "5" && (/[1-9]/.test(removed.slice(1)) || coefficient % 2n === 1n))) coefficient += 1n;
  }
  const digits = coefficient.toString().padStart(precision + 1, "0");
  const integer = precision ? digits.slice(0, -precision) : digits;
  const fractional = precision ? `.${digits.slice(-precision)}` : "";
  return `${negative && coefficient !== 0n ? "−" : ""}${integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${fractional}${suffix}`;
}
export function multiplyQuantBy100(value: string | null | undefined): string | null {
  if (value == null || !/^[+-]?\d+(?:\.\d+)?$/.test(value)) return null;
  const negative = value.startsWith("-"); const [integer, fraction = ""] = value.replace(/^[+-]/, "").split(".");
  const padded = fraction.padEnd(2, "0"); const joined = (integer + padded.slice(0, 2)).replace(/^0+(?=\d)/, "");
  return `${negative ? "-" : ""}${joined}${padded.length > 2 ? `.${padded.slice(2)}` : ""}`;
}
export function exactU64(value: string | number | null | undefined): bigint | null {
  if (typeof value === "number") return Number.isSafeInteger(value) && value >= 0 ? BigInt(value) : null;
  if (typeof value !== "string" || value.length>20 || !/^\d+$/.test(value)) return null;
  const parsed = BigInt(value); return parsed <= 18446744073709551615n ? parsed : null;
}
export function compareSourceRevision(a: string | number, b: string | number): number {
  const first = exactU64(a), second = exactU64(b);
  return first === null ? -1 : second === null ? 1 : first < second ? -1 : first > second ? 1 : 0;
}
