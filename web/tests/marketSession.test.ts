import assert from "node:assert/strict";
import test from "node:test";

import { marketSessionAt, SPOT_METALS_MARKET_SCHEDULE } from "../src/marketSession.ts";

test("spot metals are closed over the weekend", () => {
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-08T05:00:00+08:00")).phase,
    "closed",
  );
});

test("spot metals follow the New York session across daylight saving time", () => {
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-07T16:58:00-04:00")).phase,
    "open",
  );
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-07T17:00:00-04:00")).phase,
    "closed",
  );
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-09T18:04:00-04:00")).phase,
    "closed",
  );
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-09T18:05:00-04:00")).phase,
    "open",
  );
});

test("spot metals report the normal daily maintenance break as closed", () => {
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-10T17:30:00-04:00")).phase,
    "closed",
  );
  assert.equal(
    marketSessionAt(SPOT_METALS_MARKET_SCHEDULE, new Date("2026-08-10T18:05:00-04:00")).phase,
    "open",
  );
});

test("missing or invalid schedules never claim that a market is open", () => {
  assert.equal(marketSessionAt(null).phase, "unknown");
  assert.equal(
    marketSessionAt({ ...SPOT_METALS_MARKET_SCHEDULE, time_zone: "Not/A_Time_Zone" }).phase,
    "unknown",
  );
});
