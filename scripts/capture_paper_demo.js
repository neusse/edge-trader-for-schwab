// Run with the local --demo server on port 8791:
// npx --yes @playwright/cli run-code --filename scripts/capture_paper_demo.js
// Intercepts all Paper responses used here; every value is invented demo data.
async (page) => {
  const response = await page.request.get("http://127.0.0.1:8791/api/state");
  const state = await response.json();
  const now = new Date();
  const nextClose = new Date(now.getTime() + 6 * 60 * 60 * 1000);
  const expiry = "2026-12-18";
  const longContract = "SPY261218C00600000";
  const shortContract = "SPY261218C00605000";
  state.broker = {key: "alpaca-paper", name: "Alpaca Paper", mode: "paper",
                  capabilities: ["equity-bracket", "multi-leg-options", "option-trading"],
                  workflows: ["equity-bracket", "equity-simple", "equity-oco", "equity-close",
                              "equity-partial", "equity-oco-adjust", "equity-auto-be", "equity-add",
                              "equity-trailing", "option-long-entry", "option-close", "option-cancel",
                              "option-fixed-exit", "option-covered-short", "vertical-entry",
                              "vertical-close", "vertical-target", "vertical-cancel",
                              "vertical-target-reprice", "vertical-stop"],
                  help: "Paper equity, option, and vertical workflows are available in this invented demo."};
  state.demo = false;
  state.pending_broker = null;
  state.broker_ready = true;
  state.broker_health = {state: "ready", message: "Alpaca Paper connected"};
  state.alpaca = {
    account_status: "ACTIVE", equity: "100000", cash: "48000", buying_power: "96000",
    options_trading_level: 3, options_approved_level: 3,
    updated_at: now.toISOString(),
    orders: [{status: "filled", type: "limit", symbols: ["AAPL"], quantities: ["2"]}],
  };
  state.positions = [{
    symbol: "AAPL", display: "AAPL", side: "LONG", qty: 2, avg_price: 100,
    last: 105, quote_stale: false, quote_source: "Alpaca IEX", open_pnl: 10,
    asset_type: "EQUITY", protection: "FULL", stop_qty: 2,
  }, {
    symbol: longContract, display: longContract, side: "LONG", qty: 1, avg_price: 9.20,
    last: 10.10, quote_stale: false, quote_source: "Alpaca indicative", open_pnl: 90,
    asset_type: "OPTION", protection: "NONE", stop_qty: 0,
  }, {
    symbol: shortContract, display: shortContract, side: "SHORT", qty: 1, avg_price: 7.40,
    last: 8.00, quote_stale: false, quote_source: "Alpaca indicative", open_pnl: -60,
    asset_type: "OPTION", protection: "NONE", stop_qty: 0,
  }];
  state.orders = [{symbol: "AAPL", display: "AAPL", role: "STOP", status: "WORKING",
                   order_type: "STOP", qty: 2, group_type: "BRACKET", order_id: "demo-stop-1"}];
  state.paper_auto_be = [{symbol: "AAPL", side: "LONG", quantity: "2", trigger: "110.00",
                          new_stop: "100.00", status: "watching", reason: "Waiting for +1R"}];
  state.paper_spread_stops = [{long_contract: longContract, short_contract: shortContract,
                               kind: "debit", stop_value: "1.00", send: false,
                               status: "suspended", reason: "OPRA quote unavailable", stop_id: "demo-watch-1"}];
  state.fills = [{time: now.toISOString(), symbol: "AAPL", instruction: "buy",
                  quantity: "2", price: "100"}];
  state.market_hours = {is_open: true, next_open: now.toISOString(),
                        next_close: nextClose.toISOString()};
  state.summary.open_pnl = {intraday: 10, mixed: 0, carried: 0, complete: true};
  state.summary.realized.securities = {net: 45, complete: true};
  await page.route("**/api/state", route => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(state),
  }));
  await page.route("**/api/paper/status", route => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ok: true, armed: false, unresolved: []}),
  }));
  await page.route("**/api/paper/options/expirations", route => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ok: true, expirations: [expiry]}),
  }));
  await page.route("**/api/paper/options/chain", route => route.fulfill({
    status: 200, contentType: "application/json",
    body: JSON.stringify({ok: true, underlying: "SPY", expiration: expiry, option_type: "call",
      contracts: [{symbol: longContract, strike: "600", bid: "10.00", ask: "10.10", mark: "10.05",
                   delta: "0.56", gamma: "0.02", theta: "-0.15", vega: "0.20", rho: "0.04",
                   implied_volatility: "0.19", open_interest: "1200", quote_at: now.toISOString(), tradable: true},
                  {symbol: shortContract, strike: "605", bid: "8.00", ask: "8.10", mark: "8.05",
                   delta: "0.48", gamma: "0.02", theta: "-0.14", vega: "0.19", rho: "0.04",
                   implied_volatility: "0.20", open_interest: "960", quote_at: now.toISOString(), tradable: true}]}),
  }));
  await page.reload();
  await page.locator("#alpaca-overview").screenshot({
    path: "docs/screenshot-alpaca-paper-demo.png", scale: "css",
  });
  await page.locator("#paper-entry").screenshot({
    path: "docs/screenshot-paper-entry-demo.png", scale: "css",
  });
  await page.locator('#paper-options-form [name="underlying"]').fill("SPY");
  await page.locator("#paper-options-expirations").click();
  await page.locator('#paper-options-form [name="expiration"]').selectOption(expiry);
  await page.locator('#paper-options-form [name="option_type"]').selectOption("call");
  await page.locator("#paper-options-load").click();
  await page.locator("#paper-options-chain table").waitFor();
  await page.locator("#paper-options").screenshot({
    path: "docs/screenshot-paper-options-demo.png", scale: "css",
  });
  if (!(await page.locator("#paper-entry").isVisible())) throw new Error("Paper entry form is hidden");
  if (!(await page.locator("#paper-vertical-close-form").isVisible())) throw new Error("Vertical controls are hidden");
}
