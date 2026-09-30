import { useEffect, useMemo, useRef, useState } from "react";
import { marketApi } from "./api";
import {
  OPTION_TEMPLATES,
  expirationPayoff,
  optionGreeks,
  portfolioRisk,
  templateLegs,
  validatePortfolio,
  type OptionLeg,
} from "./optionPortfolio";
import {
  downloadText,
  researchApi,
  type OptionMonths,
  type OptionUnderlying,
} from "./researchApi";

const number = (value: number) =>
  Number.isNaN(value)
    ? "—"
    : Number.isFinite(value)
      ? value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })
      : "无限";
type Contract = {
  symbol: string;
  underlying: string;
  expiry: string;
  kind: "call" | "put";
  strike: number;
  bid: number | null;
  ask: number | null;
  last: number | null;
  observed: string | null;
  multiplier: number;
  currency: string;
  referenceSpot?: number | null;
  futuresModel?: boolean;
  source?: string;
};
const defaultExpiry = () =>
  new Date(Date.now() + 30 * 86400000).toISOString().slice(0, 10);

function readScenario() {
  const defaults = {
    spot: 100,
    width: 5,
    vol: 25,
    rate: 3,
    dividend: 0,
    fees: 0,
    futures: false,
  };
  try {
    const saved = JSON.parse(
      localStorage.getItem("tracefang.optionScenario.v1") ?? "null",
    );
    if (saved) {
      for (const key of [
        "spot",
        "width",
        "vol",
        "rate",
        "dividend",
        "fees",
      ] as const) {
        if (typeof saved[key] === "number" && Number.isFinite(saved[key]))
          defaults[key] = saved[key];
      }
      defaults.futures = saved.futures === true;
    }
  } catch {
    /* Use explicit scenario defaults. */
  }
  return defaults;
}

function readPortfolio(): OptionLeg[] {
  try {
    const saved = JSON.parse(
      localStorage.getItem("tracefang.optionPortfolio.v1") ?? "null",
    );
    if (
      Array.isArray(saved) &&
      saved.length <= 12 &&
      saved.every((leg) => leg && ["call", "put", "stock"].includes(leg.kind))
    ) {
      return saved.map((leg) => ({
        ...leg,
        premium: leg.premium === null ? Number.NaN : leg.premium,
      }));
    }
  } catch {
    /* Private browsing may disable storage. */
  }
  return templateLegs("bull-call", 100, 5, defaultExpiry());
}

export function OptionsLab() {
  const [savedScenario] = useState(readScenario);
  const [template, setTemplate] = useState("bull-call");
  const [spot, setSpot] = useState(savedScenario.spot);
  const [width, setWidth] = useState(savedScenario.width);
  const [expiry, setExpiry] = useState(defaultExpiry);
  const [vol, setVol] = useState(savedScenario.vol);
  const [rate, setRate] = useState(savedScenario.rate);
  const [dividend, setDividend] = useState(savedScenario.dividend);
  const [futures, setFutures] = useState(savedScenario.futures);
  const [fees, setFees] = useState(savedScenario.fees);
  const [legs, setLegs] = useState<OptionLeg[]>(readPortfolio);
  const [chainSource, setChainSource] = useState("akshare");
  const [symbol, setSymbol] = useState("SPY");
  const [akSymbol, setAkSymbol] = useState("510050.SH");
  const [underlyings, setUnderlyings] = useState<OptionUnderlying[]>([]);
  const [months, setMonths] = useState<OptionMonths["months"]>([]);
  const [month, setMonth] = useState("");
  const [monthBusy, setMonthBusy] = useState(false);
  const [monthRefresh, setMonthRefresh] = useState(0);
  const [chainStale, setChainStale] = useState(false);
  const chainController = useRef<AbortController | null>(null);
  const [chain, setChain] = useState<Contract[]>([]);
  const [chainFilter, setChainFilter] = useState("");
  const [chainExpiry, setChainExpiry] = useState("");
  const [chainMessage, setChainMessage] = useState(
    "连接真实期权链，或在右侧输入研究参数。",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [scenario, setScenario] = useState(savedScenario.spot);
  useEffect(() => () => chainController.current?.abort(), []);
  useEffect(() => {
    if (chainSource !== "akshare") return;
    const abort = new AbortController();
    setMonthBusy(true);
    setError(null);
    setMonths([]);
    setMonth("");
    setChain([]);
    setChainMessage("正在读取并核对有效合约月份与实际到期日…");
    void Promise.all([
      researchApi.optionUnderlyings(abort.signal),
      researchApi.optionMonths(akSymbol, abort.signal),
    ])
      .then(([choices, result]) => {
        if (abort.signal.aborted) return;
        setUnderlyings(choices);
        setMonths(result.months);
        setMonth(result.months[0]?.month ?? "");
        setChainMessage(
          result.months.length
            ? "已核对合约月份与实际到期日，选择月份后加载期权链。"
            : "当前目录没有该标的的有效月份合约。",
        );
      })
      .catch((failure) => {
        if (!abort.signal.aborted)
          setError(
            failure instanceof Error ? failure.message : "合约月份读取失败",
          );
      })
      .finally(() => {
        if (!abort.signal.aborted) setMonthBusy(false);
      });
    return () => abort.abort();
  }, [chainSource, akSymbol, monthRefresh]);
  useEffect(() => {
    try {
      localStorage.setItem(
        "tracefang.optionPortfolio.v1",
        JSON.stringify(legs),
      );
    } catch {
      /* Keep working in memory. */
    }
  }, [legs]);
  useEffect(() => {
    try {
      localStorage.setItem(
        "tracefang.optionScenario.v1",
        JSON.stringify({ spot, width, vol, rate, dividend, futures, fees }),
      );
    } catch {
      /* Keep working in memory. */
    }
  }, [spot, width, vol, rate, dividend, futures, fees]);
  const validation =
    !Number.isFinite(spot) || spot <= 0
      ? "请填写有效的正数标的价格。"
      : validatePortfolio(legs, fees);
  const risk = useMemo(
    () => (validation ? null : portfolioRisk(legs, fees)),
    [legs, fees, validation],
  );
  const portfolioExpiry =
    legs.find((leg) => leg.kind !== "stock")?.expiry ?? expiry;
  const years = Math.max(
    0,
    (Date.parse(portfolioExpiry + "T16:00:00Z") - Date.now()) /
      (365 * 86400000),
  );
  const greeks = useMemo(() => {
    if (validation || spot <= 0 || vol <= 0 || years <= 0) return null;
    const total = { delta: 0, gamma: 0, theta: 0, vega: 0 };
    for (const leg of legs) {
      if (leg.kind === "stock") {
        total.delta += leg.quantity * leg.multiplier;
        continue;
      }
      const greek = optionGreeks(
        leg.kind,
        spot,
        leg.strike,
        years,
        vol / 100,
        rate / 100,
        dividend / 100,
        futures,
      );
      if (!greek) return null;
      for (const field of ["delta", "gamma", "theta", "vega"] as const)
        total[field] += greek[field] * leg.quantity * leg.multiplier;
    }
    return total;
  }, [validation, legs, spot, vol, rate, dividend, futures, years]);
  const strikes = legs
    .filter((leg) => leg.kind !== "stock")
    .map((leg) => leg.strike)
    .filter(Number.isFinite);
  const low = Math.max(
    0,
    Math.min(spot * 0.7, ...strikes.map((strike) => strike * 0.85)),
  );
  const high = Math.max(
    spot * 1.3,
    ...strikes.map((strike) => strike * 1.15),
    low + 1,
  );
  const points = risk
    ? Array.from({ length: 121 }, (_, index) => {
        const x = low + ((high - low) * index) / 120;
        return { x, y: expirationPayoff(legs, x, fees) };
      })
    : [];
  const minY = Math.min(0, ...points.map((point) => point.y));
  const maxY = Math.max(0, ...points.map((point) => point.y));
  const spread = Math.max(1, maxY - minY);
  const px = (x: number) => 64 + ((x - low) / (high - low)) * 776;
  const py = (y: number) => 25 + ((maxY - y) / spread) * 220;
  const update = (id: string, values: Partial<OptionLeg>) =>
    setLegs((current) =>
      current.map((leg) => (leg.id === id ? { ...leg, ...values } : leg)),
    );
  const loadChain = async () => {
    chainController.current?.abort();
    const abort = new AbortController();
    chainController.current = abort;
    setBusy(true);
    setError(null);
    setChain([]);
    setChainStale(false);
    try {
      if (chainSource === "shfe") {
        const result = await marketApi.expertGoldOptions();
        if (abort.signal.aborted) return;
        setChain(
          result.contracts.map((item) => ({
            symbol: item.contract_id,
            underlying: item.underlying_contract_id,
            expiry: item.expiry.slice(0, 10),
            kind: item.option_type,
            strike: item.strike,
            bid: item.bid,
            ask: item.ask,
            last: item.last,
            observed: item.observed_at,
            multiplier: item.contract_multiplier,
            currency: "CNY",
            futuresModel: true,
            source: "上期所",
            referenceSpot: result.expiries.find(
              (entry) =>
                entry.underlying_contract_id === item.underlying_contract_id,
            )?.underlying_price,
          })),
        );
        setChainMessage(
          `${result.detail} · ${result.observed_at ?? "尚无报价"}`,
        );
      } else {
        const result = await researchApi.chain(
          chainSource === "akshare" ? akSymbol : symbol.trim().toUpperCase(),
          chainSource === "akshare" ? undefined : chainExpiry || undefined,
          abort.signal,
          chainSource === "akshare" ? "akshare" : "alpaca",
          chainSource === "akshare" ? month : undefined,
        );
        if (abort.signal.aborted) return;
        if (
          chainSource === "akshare" &&
          result.contracts.some(
            (item) =>
              !item.multiplier ||
              item.multiplier <= 0 ||
              item.currency !== "CNY",
          )
        ) {
          throw new Error("期权链缺少有效合约乘数或币种，无法导入策略。");
        }
        setChainStale(result.cache_state === "stale");
        setChain(
          result.contracts.map((item) => ({
            ...item,
            observed: item.observed_at,
            multiplier: item.multiplier ?? 100,
            currency: item.currency ?? "USD",
            referenceSpot: result.reference_spot,
            futuresModel: result.pricing_model === "black76",
            source: result.feed,
          })),
        );
        setChainMessage(
          `${result.note}${result.truncated ? " 当前为部分目录，请缩小查询。" : ""} ${(result.warnings ?? []).join(" ")} · 读取 ${new Date(result.fetched_at).toLocaleString()}`,
        );
      }
    } catch (failure) {
      if (!abort.signal.aborted)
        setError(failure instanceof Error ? failure.message : "加载期权链失败");
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  };
  const addContract = (contract: Contract, side: 1 | -1) => {
    if (chainStale) {
      setError("当前为过期缓存，请重新读取成功后再导入报价。");
      return;
    }
    const premium = (side === 1 ? contract.ask : contract.bid) ?? contract.last;
    if (premium === null) {
      setError("该合约没有可用价格，请选择有报价的合约。");
      return;
    }
    const newLeg: OptionLeg = {
      id: crypto.randomUUID(),
      kind: contract.kind,
      quantity: side,
      strike: contract.strike,
      premium,
      expiry: contract.expiry,
      multiplier: contract.multiplier,
      contract: contract.symbol,
      underlying: contract.underlying,
      source: `${contract.source ?? chainSource} · ${(side === 1 ? contract.ask : contract.bid) !== null ? (side === 1 ? "卖价" : "买价") : "最后成交"}`,
      currency: contract.currency,
      observedAt: contract.observed,
    };
    setLegs((current) =>
      current.every((leg) => !leg.contract)
        ? [newLeg]
        : [...current.slice(0, 11), newLeg],
    );
    if (legs.every((leg) => !leg.contract)) {
      const price = contract.referenceSpot ?? 0;
      setSpot(price);
      setScenario(price);
      setChainMessage(
        (current) =>
          `${current} ${price > 0 ? "已带入对应标的延迟价格作为情景初值。" : "本期权链未提供标的价格，请填写情景价格后再计算。"}`,
      );
    }
    setFutures(contract.futuresModel ?? chainSource === "shfe");
    setExpiry(contract.expiry);
  };
  const useModel = () =>
    setLegs((current) =>
      current.map((leg) => {
        if (leg.kind === "stock")
          return { ...leg, premium: spot, source: "手工情景" };
        const term =
          (Date.parse(leg.expiry + "T16:00:00Z") - Date.now()) /
          (365 * 86400000);
        const result = optionGreeks(
          leg.kind,
          spot,
          leg.strike,
          term,
          vol / 100,
          rate / 100,
          dividend / 100,
          futures,
        );
        return result
          ? {
              ...leg,
              premium: Math.round(result.price * 10000) / 10000,
              source: "理论情景（非市场报价）",
              observedAt: null,
            }
          : leg;
      }),
    );
  return (
    <main className="options-lab">
      <header className="research-page-heading">
        <div>
          <small>OPTIONS / STRATEGY BUILDER</small>
          <h1>期权策略实验室</h1>
          <p>用真实合约或自定义价格搭建组合，在同一张图上审视收益与风险。</p>
        </div>
        <button
          onClick={() =>
            downloadText(
              "tracefang-options.json",
              JSON.stringify(
                { legs, spot, vol, rate, dividend, futures, fees, risk },
                (_key, value) =>
                  value === Infinity
                    ? "unlimited"
                    : value === -Infinity
                      ? "negative-unlimited"
                      : value,
                2,
              ),
              "application/json",
            )
          }
        >
          导出组合
        </button>
      </header>
      <div className="options-layout">
        <aside className="option-library research-card">
          <h2>常用组合</h2>
          <p className="muted">模板只定义结构；权利金需填写或从行情链载入。</p>
          <div className="option-template-list">
            {OPTION_TEMPLATES.map((item) => (
              <button
                key={item.id}
                className={template === item.id ? "is-active" : ""}
                onClick={() => {
                  setTemplate(item.id);
                  setLegs(templateLegs(item.id, spot, width, expiry));
                }}
              >
                <strong>{item.name}</strong>
                <small>{item.view}</small>
              </button>
            ))}
          </div>
          <h2>真实期权链</h2>
          <label>
            来源
            <select
              disabled={busy}
              value={chainSource}
              onChange={(event) => {
                setChainSource(event.target.value);
                setChain([]);
                setError(null);
              }}
            >
              <option value="alpaca">Alpaca · 美股期权</option>
              <option value="shfe">上期所 · 黄金期权</option>
              <option value="akshare">
                AKShare · 国内 ETF / 股指 / 商品期权
              </option>
            </select>
          </label>
          {chainSource === "akshare" ? (
            <>
              <label>
                期权标的
                <select
                  aria-label="国内期权标的"
                  value={akSymbol}
                  disabled={busy}
                  onChange={(event) => setAkSymbol(event.target.value)}
                >
                  {underlyings.length ? (
                    underlyings.map((item) => (
                      <option key={item.symbol} value={item.symbol}>
                        {item.name} · {item.symbol}
                      </option>
                    ))
                  ) : (
                    <option value="510050.SH">上证50 ETF · 510050.SH</option>
                  )}
                </select>
              </label>
              <label>
                合约月份
                <select
                  aria-label="期权合约月份"
                  value={month}
                  disabled={busy || monthBusy}
                  onChange={(event) => {
                    setMonth(event.target.value);
                    setChain([]);
                  }}
                >
                  {months.length ? (
                    months.map((item) => (
                      <option key={item.month} value={item.month}>
                        {item.label}
                      </option>
                    ))
                  ) : (
                    <option value="">
                      {monthBusy ? "读取有效月份…" : "暂无有效月份"}
                    </option>
                  )}
                </select>
              </label>
              <button
                disabled={busy || monthBusy}
                onClick={() => setMonthRefresh((value) => value + 1)}
              >
                重读合约月份
              </button>
            </>
          ) : null}
          {chainSource === "alpaca" ? (
            <>
              <label>
                标的代码
                <input
                  disabled={busy}
                  value={symbol}
                  onChange={(event) => {
                    setSymbol(event.target.value);
                    setChain([]);
                  }}
                  maxLength={10}
                  placeholder="SPY / AAPL"
                />
              </label>
              <label>
                到期日（可选）
                <input
                  type="date"
                  value={chainExpiry}
                  disabled={busy}
                  onChange={(event) => {
                    setChainExpiry(event.target.value);
                    setChain([]);
                  }}
                />
              </label>
            </>
          ) : null}
          <button
            disabled={
              busy || (chainSource === "akshare" && (monthBusy || !month))
            }
            onClick={() => void loadChain()}
          >
            {busy ? "读取期权链…" : "加载期权链"}
          </button>
          <p className="muted">{chainMessage}</p>
          {error ? (
            <p role="alert" className="research-error">
              {error}
            </p>
          ) : null}
          {chain.length ? (
            <>
              <label>
                筛选到期日 / 合约
                <input
                  value={chainFilter}
                  onChange={(event) => setChainFilter(event.target.value)}
                  placeholder="例如 2026-10"
                />
              </label>
              <small>
                已读取 {chain.length} 个合约，显示筛选后的前 150
                条。买入取卖价、卖出取买价；无盘口时取最后成交。首次添加会替换模板。
              </small>
              <div className="chain-list">
                {chain
                  .filter((item) =>
                    `${item.symbol} ${item.expiry}`.includes(chainFilter),
                  )
                  .slice(0, 150)
                  .map((item) => (
                    <article key={item.symbol}>
                      <strong>
                        {item.underlying} · {item.kind === "call" ? "购" : "沽"}{" "}
                        {item.strike}
                      </strong>
                      <small>
                        {item.expiry} · {item.bid ?? "—"} / {item.ask ?? "—"}
                      </small>
                      <small>
                        乘数 {item.multiplier} · {item.currency} ·{" "}
                        {item.observed
                          ? new Date(item.observed).toLocaleString()
                          : "报价时间未提供"}
                      </small>
                      <div>
                        <button
                          disabled={legs.length >= 12 || chainStale}
                          onClick={() => addContract(item, 1)}
                        >
                          买入
                        </button>
                        <button
                          disabled={legs.length >= 12 || chainStale}
                          onClick={() => addContract(item, -1)}
                        >
                          卖出
                        </button>
                      </div>
                    </article>
                  ))}
              </div>
            </>
          ) : null}
        </aside>
        <section className="option-builder">
          <div className="research-card option-assumptions">
            <h2>
              情景参数 <span>手动输入或来源快照，非实时报价</span>
            </h2>
            <div className="research-form-grid">
              <label>
                标的价格
                <input
                  type="number"
                  min="0.01"
                  step="any"
                  value={spot}
                  onChange={(event) => setSpot(Number(event.target.value))}
                />
              </label>
              <label>
                模板价差间隔
                <input
                  type="number"
                  min="0.01"
                  step="any"
                  value={width}
                  onChange={(event) => setWidth(Number(event.target.value))}
                />
              </label>
              <label>
                模板到期日
                <input
                  type="date"
                  value={expiry}
                  onChange={(event) => setExpiry(event.target.value)}
                />
              </label>
              <label>
                总费用
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={fees}
                  onChange={(event) => setFees(Number(event.target.value))}
                />
              </label>
              <label>
                波动率 %
                <input
                  type="number"
                  min="0.1"
                  value={vol}
                  onChange={(event) => setVol(Number(event.target.value))}
                />
              </label>
              <label>
                利率 %
                <input
                  type="number"
                  step="0.1"
                  value={rate}
                  onChange={(event) => setRate(Number(event.target.value))}
                />
              </label>
              <label>
                股息率 %
                <input
                  type="number"
                  step="0.1"
                  value={dividend}
                  onChange={(event) => setDividend(Number(event.target.value))}
                />
              </label>
              <label>
                估值模型
                <select
                  value={futures ? "future" : "spot"}
                  onChange={(event) =>
                    setFutures(event.target.value === "future")
                  }
                >
                  <option value="spot">Black–Scholes（欧式）</option>
                  <option value="future">Black–76（期货）</option>
                </select>
              </label>
            </div>
            <button onClick={useModel} disabled={spot <= 0 || vol <= 0}>
              用模型估值填入权利金（理论情景）
            </button>
          </div>
          <div className="research-card">
            <h2>
              组合腿 <span>正数买入，负数卖出 · 乘数按合约核实</span>
            </h2>
            <div className="research-table-scroll">
              <table className="research-table">
                <thead>
                  <tr>
                    <th>类别</th>
                    <th>数量</th>
                    <th>行权价</th>
                    <th>权利金 / 成本</th>
                    <th>乘数</th>
                    <th>到期日</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {legs.map((leg) => (
                    <tr
                      key={leg.id}
                      title={`${leg.contract ?? "自定义"} · ${leg.source ?? "手动输入"} · ${leg.observedAt ?? "无市场时间"}`}
                    >
                      <td>
                        <select
                          aria-label="组合腿类型"
                          value={leg.kind}
                          onChange={(event) =>
                            update(leg.id, {
                              kind: event.target.value as OptionLeg["kind"],
                              contract: undefined,
                              source: "手动修改",
                              observedAt: null,
                            })
                          }
                        >
                          <option value="call">看涨</option>
                          <option value="put">看跌</option>
                          <option value="stock">标的</option>
                        </select>
                        <small>
                          {leg.contract ?? "自定义"} ·{" "}
                          {leg.source ?? "手动输入"}
                        </small>
                      </td>
                      <td>
                        <input
                          aria-label="组合腿数量"
                          type="number"
                          value={leg.quantity}
                          onChange={(event) =>
                            update(leg.id, {
                              quantity: Number(event.target.value),
                            })
                          }
                        />
                      </td>
                      <td>
                        <input
                          aria-label="行权价"
                          type="number"
                          step="any"
                          disabled={leg.kind === "stock"}
                          value={leg.strike}
                          onChange={(event) =>
                            update(leg.id, {
                              strike: Number(event.target.value),
                              contract: undefined,
                              source: "手动修改",
                            })
                          }
                        />
                      </td>
                      <td>
                        <input
                          aria-label="权利金"
                          type="number"
                          step="any"
                          min="0"
                          value={
                            Number.isFinite(leg.premium) ? leg.premium : ""
                          }
                          placeholder="填写价格"
                          onChange={(event) =>
                            update(leg.id, {
                              premium:
                                event.target.value === ""
                                  ? NaN
                                  : Number(event.target.value),
                              source: "手动输入",
                            })
                          }
                        />
                      </td>
                      <td>
                        <input
                          aria-label="合约乘数"
                          type="number"
                          min="1"
                          value={leg.multiplier}
                          onChange={(event) =>
                            update(leg.id, {
                              multiplier: Number(event.target.value),
                            })
                          }
                        />
                      </td>
                      <td>
                        <input
                          aria-label="到期日"
                          type="date"
                          value={leg.expiry}
                          disabled={leg.kind === "stock"}
                          onChange={(event) =>
                            update(leg.id, {
                              expiry: event.target.value,
                              contract: undefined,
                              source: "手动修改",
                            })
                          }
                        />
                      </td>
                      <td>
                        <button
                          aria-label="删除组合腿"
                          onClick={() =>
                            setLegs((current) =>
                              current.filter((item) => item.id !== leg.id),
                            )
                          }
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <button
              disabled={legs.length >= 12}
              onClick={() =>
                setLegs((current) => [
                  ...current,
                  {
                    ...templateLegs("long-call", spot, width, expiry)[0],
                    id: crypto.randomUUID(),
                  },
                ])
              }
            >
              ＋ 添加组合腿
            </button>
          </div>
          <div className="research-card payoff-card">
            <h2>
              到期损益 <span>含初始权利金 / 标的成本与总费用</span>
            </h2>
            {validation ? (
              <p className="research-notice" role="status">
                {validation}
              </p>
            ) : (
              <>
                <div className="option-risk-grid">
                  <div>
                    <small>最大收益</small>
                    <strong>{number(risk!.maxProfit)}</strong>
                  </div>
                  <div>
                    <small>最大亏损</small>
                    <strong
                      className={
                        Number.isFinite(risk!.maxLoss) ? "" : "research-error"
                      }
                    >
                      {number(risk!.maxLoss)}
                    </strong>
                  </div>
                  <div>
                    <small>初始净现金流</small>
                    <strong>{number(risk!.initialCashflow)}</strong>
                  </div>
                  <div>
                    <small>盈亏平衡点</small>
                    <strong>
                      {risk!.breakevens.map(number).join(" / ") || "无"}
                    </strong>
                  </div>
                </div>
                <svg
                  viewBox="0 0 900 290"
                  role="img"
                  aria-label="组合到期盈亏曲线"
                  className="payoff-chart"
                >
                  {[0, 0.25, 0.5, 0.75, 1].map((ratio) => (
                    <g key={ratio}>
                      <line
                        x1={64}
                        x2={840}
                        y1={25 + ratio * 220}
                        y2={25 + ratio * 220}
                        stroke="#e3e9ef"
                      />
                      <text x={54} y={29 + ratio * 220} textAnchor="end">
                        {number(maxY - ratio * spread)}
                      </text>
                      <text x={64 + ratio * 776} y={275} textAnchor="middle">
                        {number(low + ratio * (high - low))}
                      </text>
                    </g>
                  ))}
                  <line
                    x1={64}
                    x2={840}
                    y1={py(0)}
                    y2={py(0)}
                    stroke="#8293a2"
                    strokeDasharray="4 4"
                  />
                  <path
                    d={points
                      .map(
                        (point, i) =>
                          `${i ? "L" : "M"}${px(point.x)},${py(point.y)}`,
                      )
                      .join(" ")}
                    fill="none"
                    stroke="#245c8c"
                    strokeWidth={3}
                  />
                  {scenario >= low && scenario <= high ? (
                    <circle
                      cx={px(scenario)}
                      cy={py(expirationPayoff(legs, scenario, fees))}
                      r={5}
                      fill="#18776e"
                    />
                  ) : null}
                </svg>
                <label className="scenario-slider">
                  到期标的价 {number(scenario)} · 组合损益{" "}
                  {number(expirationPayoff(legs, scenario, fees))}
                  <input
                    aria-label="到期价格情景"
                    type="range"
                    min={low}
                    max={high}
                    step={(high - low) / 200}
                    value={Math.min(high, Math.max(low, scenario))}
                    onChange={(event) =>
                      setScenario(Number(event.target.value))
                    }
                  />
                </label>
                {risk!.flatRanges.length ? (
                  <p className="muted">
                    存在零损益区间：
                    {risk!.flatRanges
                      .map(
                        ([a, b]) =>
                          `${number(a)} 至 ${b === null ? "无限" : number(b)}`,
                      )
                      .join("；")}
                  </p>
                ) : null}
              </>
            )}
            <div className="option-risk-grid">
              {["delta", "gamma", "theta", "vega"].map((key) => (
                <div key={key}>
                  <small>
                    {key.toUpperCase()}
                    {key === "theta"
                      ? " / 日"
                      : key === "vega"
                        ? " / 1%波动率"
                        : ""}
                  </small>
                  <strong>
                    {greeks ? number(greeks[key as keyof typeof greeks]) : "—"}
                  </strong>
                </div>
              ))}
            </div>
            <p className="muted">
              Greeks
              是上述波动率、利率和近似剩余期限下的欧式理论值，所有腿共用波动率。美式提前行权、保证金、滑点、交割和负标的价格未建模。静态到期损益不代表当前平仓价值。货币单位以组合合约为准。
            </p>
          </div>
        </section>
      </div>
    </main>
  );
}
