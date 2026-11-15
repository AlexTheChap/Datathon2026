// node --test tests/js/: the rates section's wording (frontend/assets/portfolio-data.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import { rateFacts, rateRows, rateSentence } from "../../frontend/assets/portfolio-data.js";

const data = {
  rates: { "BoC policy rate (%)": 2.5, "CA 10y yield (%)": 3.72, "CA CPI y/y (%)": 3.1 },
  holdings: [
    { ticker: "XBB.TO", weight: 0.4, measured: true, duration: 6.5965, impact_pct_per_point: -6.6 },
    { ticker: "SPY", weight: 0.6, measured: false, duration: null, impact_pct_per_point: null },
  ],
  portfolio: { measured_share: 0.4, weighted_duration: 2.64, impact_pct_per_point: -2.64 },
};

test("a stock is 'Not measured', never zero", () => {
  const [bond, stock] = rateRows(data);
  assert.deepEqual([bond.weight, bond.duration, bond.impact], ["40%", "6.6", "−6.6%"]);
  assert.deepEqual([stock.duration, stock.impact], ["Not measured", "Not measured"]);
});

test("the sentence names the measured share, the effect of a 1-point rise, and that it is not a forecast", () => {
  const s = rateSentence(data);
  assert.match(s, /40% of your portfolio/);
  assert.match(s, /1-point rise in yields/);
  assert.match(s, /−2\.6%/);
  assert.match(s, /not a forecast/);
});

test("with nothing measured the sentence says so", () => {
  assert.match(rateSentence({ portfolio: { measured_share: 0 } }), /None of your holdings/);
});

test("the current rates come in a fixed order with plain names", () => {
  assert.deepEqual(rateFacts(data), [["Bank of Canada rate", "2.5%"], ["Canada 10-year yield", "3.72%"], ["Canadian inflation (year over year)", "3.1%"]]);
});
