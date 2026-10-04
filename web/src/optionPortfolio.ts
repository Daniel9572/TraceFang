export interface OptionLeg {
  id: string;
  kind: "call" | "put" | "stock";
  quantity: number;
  strike: number;
  premium: number;
  multiplier: number;
  expiry: string;
  contract?: string;
  source?: string;
  observedAt?: string | null;
  underlying?: string;
  currency?: string;
  sourceQuote?: {
    strike: string; premium: string; multiplier: string;
    policy: "approximate-option-scenario-v1";
    evidence?: Record<string, unknown>;
  };
}

export const OPTION_TEMPLATES = [
  { id: "long-call", name: "买入看涨", view: "看多", legs: [["call", 1, 0]] },
  { id: "long-put", name: "买入看跌", view: "看空", legs: [["put", 1, 0]] },
  {
    id: "covered-call",
    name: "备兑看涨",
    view: "温和看多",
    legs: [
      ["stock", 100, 0],
      ["call", -1, 1],
    ],
  },
  {
    id: "protective-put",
    name: "保护性看跌",
    view: "持仓保护",
    legs: [
      ["stock", 100, 0],
      ["put", 1, -1],
    ],
  },
  {
    id: "bull-call",
    name: "牛市看涨价差",
    view: "看多",
    legs: [
      ["call", 1, -1],
      ["call", -1, 1],
    ],
  },
  {
    id: "bear-put",
    name: "熊市看跌价差",
    view: "看空",
    legs: [
      ["put", 1, 1],
      ["put", -1, -1],
    ],
  },
  {
    id: "bull-put",
    name: "牛市看跌价差",
    view: "温和看多",
    legs: [
      ["put", 1, -2],
      ["put", -1, -1],
    ],
  },
  {
    id: "bear-call",
    name: "熊市看涨价差",
    view: "温和看空",
    legs: [
      ["call", -1, 1],
      ["call", 1, 2],
    ],
  },
  {
    id: "straddle",
    name: "买入跨式",
    view: "波动扩大",
    legs: [
      ["call", 1, 0],
      ["put", 1, 0],
    ],
  },
  {
    id: "strangle",
    name: "买入宽跨式",
    view: "波动扩大",
    legs: [
      ["call", 1, 1],
      ["put", 1, -1],
    ],
  },
  {
    id: "iron-condor",
    name: "铁鹰",
    view: "区间震荡",
    legs: [
      ["put", 1, -2],
      ["put", -1, -1],
      ["call", -1, 1],
      ["call", 1, 2],
    ],
  },
  {
    id: "butterfly",
    name: "看涨蝶式",
    view: "收敛",
    legs: [
      ["call", 1, -1],
      ["call", -2, 0],
      ["call", 1, 1],
    ],
  },
  {
    id: "collar",
    name: "领口",
    view: "持仓保护",
    legs: [
      ["stock", 100, 0],
      ["put", 1, -1],
      ["call", -1, 1],
    ],
  },
] as const;

export function templateLegs(
  id: string,
  spot: number,
  width: number,
  expiry: string,
): OptionLeg[] {
  const template =
    OPTION_TEMPLATES.find((item) => item.id === id) ?? OPTION_TEMPLATES[0];
  return template.legs.map(([kind, quantity, offset], i) => ({
    id: `leg-${Date.now()}-${i}`,
    kind,
    quantity,
    strike: kind === "stock" ? 0 : Math.max(0.01, spot + offset * width),
    premium: kind === "stock" ? spot : Number.NaN,
    multiplier: kind === "stock" ? 1 : 100,
    expiry,
  }));
}

export function validatePortfolio(
  legs: OptionLeg[],
  fees: number,
): string | null {
  if (!legs.length) return "请添加至少一条组合腿。";
  if (legs.length > 12) return "单个研究组合最多 12 条腿。";
  if (!Number.isFinite(fees) || fees < 0) return "总费用必须为非负数。";
  for (const leg of legs) {
    if (
      ![leg.strike, leg.premium, leg.quantity, leg.multiplier].every(
        Number.isFinite,
      )
    )
      return "请填写有效数字。";
    if (
      !Number.isFinite(leg.quantity * leg.multiplier) ||
      !Number.isFinite(
        leg.quantity * leg.multiplier * (leg.premium + leg.strike),
      )
    )
      return "组合数值超出可计算范围。";
    if (
      !Number.isInteger(leg.quantity) ||
      leg.quantity === 0 ||
      Math.abs(leg.quantity) > 100000
    )
      return "数量应为非零整数，正数买入、负数卖出。";
    if (
      leg.multiplier <= 0 ||
      leg.premium < 0 ||
      (leg.kind !== "stock" && leg.strike <= 0)
    )
      return "行权价、乘数须为正，权利金不可为负。";
    if (
      leg.kind !== "stock" &&
      (!/^\d{4}-\d{2}-\d{2}$/.test(leg.expiry) ||
        !Number.isFinite(Date.parse(leg.expiry)) ||
        new Date(leg.expiry).toISOString().slice(0, 10) !== leg.expiry)
    )
      return "请填写有效到期日。";
  }
  if (
    new Set(legs.filter((leg) => leg.kind !== "stock").map((leg) => leg.expiry))
      .size > 1
  )
    return "不同到期日需要期限结构估值。本面板仅计算同到期组合，请统一到期日。";
  if (
    new Set(legs.map((leg) => leg.underlying).filter(Boolean)).size > 1 ||
    new Set(legs.map((leg) => leg.currency).filter(Boolean)).size > 1
  )
    return "组合必须使用同一标的和币种，不能混合不同合约市场。";
  return null;
}

export function expirationPayoff(
  legs: OptionLeg[],
  price: number,
  fees = 0,
): number {
  return legs.reduce((sum, leg) => {
    const intrinsic =
      leg.kind === "stock"
        ? price
        : leg.kind === "call"
          ? Math.max(price - leg.strike, 0)
          : Math.max(leg.strike - price, 0);
    return sum + leg.quantity * leg.multiplier * (intrinsic - leg.premium);
  }, -fees);
}

export function portfolioRisk(legs: OptionLeg[], fees = 0) {
  const error = validatePortfolio(legs, fees);
  if (error) throw new Error(error);
  const knots = [
    ...new Set([
      0,
      ...legs.filter((leg) => leg.kind !== "stock").map((leg) => leg.strike),
    ]),
  ].sort((a, b) => a - b);
  const values = knots.map((value) => expirationPayoff(legs, value, fees));
  const tailSlope = legs
    .filter((leg) => leg.kind !== "put")
    .reduce((sum, leg) => sum + leg.quantity * leg.multiplier, 0);
  const breakevens: number[] = [];
  const flatRanges: [number, number | null][] = [];
  const epsilon = 1e-8;
  for (let i = 0; i < knots.length - 1; i++) {
    if (Math.abs(values[i]) < epsilon && Math.abs(values[i + 1]) < epsilon)
      flatRanges.push([knots[i], knots[i + 1]]);
    else if (values[i] * values[i + 1] < 0)
      breakevens.push(
        knots[i] -
          (values[i] * (knots[i + 1] - knots[i])) / (values[i + 1] - values[i]),
      );
    if (Math.abs(values[i]) < epsilon) breakevens.push(knots[i]);
  }
  const last = knots.at(-1)!;
  const value = values.at(-1)!;
  if (Math.abs(value) < epsilon) breakevens.push(last);
  if (Math.abs(tailSlope) > epsilon) {
    const root = last - value / tailSlope;
    if (root > last) breakevens.push(root);
  } else if (Math.abs(value) < epsilon) flatRanges.push([last, null]);
  return {
    maxProfit: tailSlope > epsilon ? Infinity : Math.max(0, ...values),
    maxLoss:
      tailSlope < -epsilon ? Infinity : Math.max(0, -Math.min(...values)),
    initialCashflow: -legs.reduce(
      (sum, leg) => sum + leg.quantity * leg.multiplier * leg.premium,
      fees,
    ),
    breakevens: [
      ...new Set(breakevens.map((value) => Math.round(value * 1e8) / 1e8)),
    ],
    flatRanges,
  };
}

function normalCdf(x: number): number {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const density = Math.exp((-x * x) / 2) / Math.sqrt(2 * Math.PI);
  const p =
    1 -
    density *
      t *
      (0.31938153 +
        t *
          (-0.356563782 +
            t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))));
  return x >= 0 ? p : 1 - p;
}

export function optionGreeks(
  kind: "call" | "put",
  spot: number,
  strike: number,
  years: number,
  volatility: number,
  rate: number,
  dividend = 0,
  futures = false,
) {
  if (
    ![spot, strike, years, volatility, rate, dividend].every(Number.isFinite) ||
    Math.min(spot, strike, years, volatility) <= 0
  )
    return null;
  const carry = futures ? rate : dividend;
  const root = Math.sqrt(years);
  const d1 =
    (Math.log(spot / strike) +
      (rate - carry + (volatility * volatility) / 2) * years) /
    (volatility * root);
  const d2 = d1 - volatility * root;
  const sign = kind === "call" ? 1 : -1;
  const discount = Math.exp(-rate * years),
    yieldDiscount = Math.exp(-carry * years);
  const density = Math.exp((-d1 * d1) / 2) / Math.sqrt(2 * Math.PI);
  const price =
    sign *
    (spot * yieldDiscount * normalCdf(sign * d1) -
      strike * discount * normalCdf(sign * d2));
  return {
    price,
    delta: sign * yieldDiscount * normalCdf(sign * d1),
    gamma: (yieldDiscount * density) / (spot * volatility * root),
    vega: (spot * yieldDiscount * density * root) / 100,
    theta:
      ((-spot * yieldDiscount * density * volatility) / (2 * root) -
        sign * rate * strike * discount * normalCdf(sign * d2) +
        sign * carry * spot * yieldDiscount * normalCdf(sign * d1)) /
      365,
  };
}
