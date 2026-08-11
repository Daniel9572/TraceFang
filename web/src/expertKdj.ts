import type { ExpertKdjDullingSnapshot } from "./expertTypes";

export interface KdjPoint {
  readonly k: number;
  readonly d: number;
  readonly j: number;
}

function zoneOf(point: KdjPoint): ExpertKdjDullingSnapshot["zone"] {
  if (point.k >= 80 && point.d >= 80) return "high";
  if (point.k <= 20 && point.d <= 20) return "low";
  return "middle";
}

function crossAt(points: readonly KdjPoint[]): ExpertKdjDullingSnapshot["cross"] {
  if (points.length < 2) return "none";
  const previous = points[points.length - 2];
  const current = points[points.length - 1];
  if (previous.k <= previous.d && current.k > current.d) return "bullish";
  if (previous.k >= previous.d && current.k < current.d) return "bearish";
  return "none";
}

function trailingZoneStreak(
  points: readonly KdjPoint[],
  zone: "high" | "low",
  endOffset = 0,
): number {
  let streak = 0;
  for (let index = points.length - 1 - endOffset; index >= 0; index -= 1) {
    if (zoneOf(points[index]) !== zone) break;
    streak += 1;
  }
  return streak;
}

/**
 * Separates stochastic extremes from reversal evidence.
 *
 * K and D must remain in the same extreme zone for three completed points
 * before the state is called dulling. A release is only reported when price
 * leaves that zone and K/D cross against the preceding extreme. J is retained
 * for display but deliberately does not control the lifecycle.
 */
export function deriveKdjDullingSnapshot(
  points: readonly KdjPoint[],
): ExpertKdjDullingSnapshot {
  if (points.length === 0) {
    return {
      zone: "middle",
      dulling: "normal",
      streak: 0,
      cross: "none",
      scoreEligible: false,
    };
  }

  const zone = zoneOf(points[points.length - 1]);
  const cross = crossAt(points);
  if (zone === "high" || zone === "low") {
    const streak = trailingZoneStreak(points, zone);
    return {
      zone,
      dulling: `${zone}-${streak >= 3 ? "dulling" : "entering"}`,
      streak,
      cross,
      scoreEligible: false,
    };
  }

  const releasedHigh = trailingZoneStreak(points, "high", 1) >= 3 && cross === "bearish";
  const releasedLow = trailingZoneStreak(points, "low", 1) >= 3 && cross === "bullish";
  return {
    zone,
    dulling: releasedHigh
      ? "high-releasing"
      : releasedLow
        ? "low-releasing"
        : "normal",
    streak: 0,
    cross,
    scoreEligible: releasedHigh || releasedLow || zone === "middle",
  };
}
