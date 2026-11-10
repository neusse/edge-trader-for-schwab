// Run with the local --demo server on port 8791:
// npx --yes @playwright/cli run-code --filename scripts/capture_paper_demo.js
// Intercepts only the browser's state response; every value is invented demo data.
async (page) => {
  const response = await page.request.get("http://127.0.0.1:8791/api/state");
  const state = await response.json();
  const now = new Date();
  const nextOpen = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
                                     now.getUTCDate() + 1, 14, 30));
  state.broker = {key: "alpaca-paper", name: "Alpaca Paper", mode: "paper", capabilities: []};
  state.demo = false;
  state.pending_broker = null;
  state.broker_ready = true;
  state.broker_health = {state: "ready", message: "Alpaca Paper connected"};
  state.alpaca = {
    account_status: "ACTIVE", equity: "100000", cash: "48000", buying_power: "96000",
    updated_at: now.toISOString(),
    orders: [{status: "filled", type: "limit", symbols: ["AAPL"], quantities: ["2"]}],
  };
  state.positions = [{
    symbol: "AAPL", display: "AAPL", side: "LONG", qty: 2, avg_price: 100,
    last: 105, quote_stale: false, quote_source: "Alpaca IEX", open_pnl: 10,
    protection: "FULL", stop_qty: 2,
  }];
  state.orders = [{display: "AAPL", role: "STOP", status: "WORKING",
                   order_type: "STOP", qty: 2}];
  state.fills = [{time: now.toISOString(), symbol: "AAPL", instruction: "buy",
                  quantity: "2", price: "100"}];
  state.market_hours = {is_open: false, next_open: nextOpen.toISOString(),
                        next_close: nextOpen.toISOString()};
  state.summary.open_pnl = {intraday: 10, mixed: 0, carried: 0, complete: true};
  state.summary.realized.securities = {net: 45, complete: true};
  await page.route("**/api/state", route => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify(state),
  }));
  await page.reload();
  await page.locator("#alpaca-overview").screenshot({
    path: "docs/screenshot-alpaca-paper-demo.png", scale: "css",
  });
}
