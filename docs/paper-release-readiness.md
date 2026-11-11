# Alpaca Paper release readiness

This is the validation record for [issue #12](https://github.com/neusse/edge-trader-for-schwab/issues/12). Keep an item open until there is a dated result and a reproducible test or redacted Paper order ID. Demo screenshots and mocked responses do not count as broker end-to-end evidence.

## Automated gate

- [x] Run `\.venv\Scripts\python.exe -m pytest -q` from a clean checkout with no broker credentials loaded; record the commit and result. On 2026-11-10, detached clean worktree at `7e827f1`, with no `.env` and Alpaca/Schwab environment variables removed in the test process: 567 passed, 1 Starlette deprecation warning (Windows, Python 3.11 virtual environment). The worktree was removed after the run.
- [ ] Run CI on the release candidate on Windows and Linux, or record the platform gap.
- [x] Exercise the same canonical account, order, and mutation contract against Schwab fixtures, Alpaca fixtures, and the in-memory fake (`tests/test_shared_broker_contract.py`; 9 passing cases).
- [x] Verify that live Alpaca credentials and any endpoint other than `https://paper-api.alpaca.markets` are rejected before a network request (`tests/test_broker_configuration.py`: live-only credentials fail, live selection fails, fixed Paper client mode, and invalid endpoint fails before client construction).
- [ ] Verify that changing brokers disarms the previous ticket, closes its poller, activates the selected broker's store, and does not carry over position or order state.
- [ ] Verify restart recovery, stale reads, rate limits, unknown submit/cancel/replace results, and no automatic mutation retry.

## Controlled Paper scenarios

Use a small, disposable Paper position during regular market hours. Record date, app commit, account capability level without revealing the account ID, request/client order IDs with sensitive parts redacted, expected behavior, observed broker status, and cleanup. A rejected or unfilled order remains useful evidence when its final state is reconciled. Never use live Alpaca keys.

| Workflow | Evidence required | Status |
| --- | --- | --- |
| Broker selection | Default Paper, switch to Schwab and Demo, then back; each switch disarms | Pending |
| Equity entry | Long and short market/limit simple and bracket; fill, target and stop children | Pending |
| Equity management | Unfilled cancel/reprice, partial fill, held OCO, fixed stop/target adjust, trail, partial/full close, add shares | Pending |
| Equity Auto B/E | One +1R move, no loosening, restart and unknown replacement handling | Filled bracket and existing OCO paths implemented; Paper E2E pending (#8) |
| Single-leg options | Long entry, covered call or cash-secured put, fixed stop/stop-limit/target, partial/full close, cancel remainder | Pending |
| Vertical spreads | Debit and credit MLEG entry, target/reprice, cancel remainder, exact held pair close | Pending |
| Vertical stop | OPRA hit twice, one atomic close attempt, stale/403 suspension, unknown outcome, external position change | Blocked by OPRA entitlement on the current Paper account |
| Failure cases | Broker rejection, partial and unknown outcomes, rate limit, stale data, restart, external changes | Pending |

Alpaca Paper simulates fills, so these checks establish API and app behavior, not live execution quality. Stock quotes in the Paper workflow use IEX, which is not the full market. Ordinary option views use indicative quotes; only verified real-time OPRA quotes can trigger the app-watched vertical stop. On 2026-11-10 the configured Paper account's OPRA request returned HTTP 403, so automatic stop sending could not be validated there.

On 2026-11-10 the configured Paper endpoint authenticated successfully and reported an active account with no open orders. This was a read-only readiness check; it does not count as workflow end-to-end evidence. The US equity session was closed during this check.

## Documentation and release gate

- [x] `.env.example` contains Paper-only keys and the fixed Paper endpoint.
- [x] `requirements.txt` pins `alpaca-py` and the Schwab client.
- [x] README distinguishes broker-held equity brackets/OCO exits from app-watched vertical stops and labels unprotected orders.
- [x] README identifies local storage and broker/data outbound calls.
- [x] Demo broker selector, Paper overview, and Paper entry screenshots exist under `docs/`; they use invented data.
- [x] Capture updated invented-data screenshots for the Paper overview, equity entry, option discovery, vertical controls, Auto B/E, and app-watched spread stops. `scripts/capture_paper_demo.js` intercepts the state and option discovery responses; the resulting files are `docs/screenshot-alpaca-paper-demo.png`, `docs/screenshot-paper-entry-demo.png`, and `docs/screenshot-paper-options-demo.png`. Visually checked on 2026-11-10; no real account data was used.
- [ ] Complete every applicable controlled Paper scenario above and attach redacted evidence to #12.
- [ ] Re-run the automated gate on the final commit and record the results in #12.
- [ ] Confirm Schwab is selectable and its regression suite passes.
- [ ] Confirm no live Alpaca endpoint or mode is reachable.

## Rollback

1. Disarm orders in the app and stop it with `stop.bat`. Check Alpaca Paper and Schwab directly for any working orders before changing versions. App-watched vertical stops cease monitoring when the app stops; broker-held exits remain at the broker unless canceled there.
2. Copy `.env` and `data/` to a private backup. Keep the backup out of Git and support attachments.
3. For a git install, check out the last known good release tag or commit and run `setup.bat`, then `launch.bat`. For a ZIP install, unpack the previous release in a separate folder, copy `.env` and `data/` into it, run `setup.bat`, then `launch.bat`.
4. Select the intended broker, verify account identity, positions, working orders, and protection in the broker and app. Re-arm only after that review. If an order outcome is unknown, reconcile it with the broker before sending another action.

Do not announce a release as ready while any required item above is open. Record deliberate scope exclusions in #12 before changing this gate.
