// Run with the local --demo server on port 8791:
// npx --yes @playwright/cli run-code --filename scripts/capture_paper_demo.js
// Intercepts all Paper responses used here; every value is invented demo data.
async (page) => {
  const response = await page.request.get("http://127.0.0.1:8791/api/state");
  const state = await response.json();
  const now = new Date();
  const nextClose = new Date(now.getTime() + 6 * 60 * 60 * 1000);
  state.broker = {key: "alpaca-paper", name: "Alpaca Paper", mode: "paper",
                  capabilities: ["equity-bracket", "equity-trading"],
                  workflows: ["equity-bracket", "equity-simple", "equity-oco", "equity-close",
                              "equity-partial", "equity-oco-adjust", "equity-auto-be", "equity-add",
                              "equity-trailing"],
                  help: "Paper equity orders and management are available. Option trading is disabled."};
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
  }];
  state.orders = [{symbol: "AAPL", display: "AAPL", role: "STOP", status: "WORKING",
                   order_type: "STOP", qty: 2, group_type: "BRACKET", order_id: "demo-stop-1"}];
  state.paper_auto_be = [{symbol: "AAPL", side: "LONG", quantity: "2", trigger: "110.00",
                          new_stop: "100.00", status: "watching", reason: "Waiting for +1R"}];
  state.paper_spread_stops = [];
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
  await page.reload();
  await page.locator("#alpaca-overview").screenshot({
    path: "docs/screenshot-alpaca-paper-demo.png", scale: "css",
  });
  await page.locator("#paper-entry").screenshot({
    path: "docs/screenshot-paper-entry-demo.png", scale: "css",
  });
  if (!(await page.locator("#paper-entry").isVisible())) throw new Error("Paper entry form is hidden");
  if (await page.locator("#paper-options").isVisible()) throw new Error("Option controls are visible");
  if (await page.locator('#t-asset button[data-v="OPTION"]').isVisible()) throw new Error("Option ticket is visible");
}
