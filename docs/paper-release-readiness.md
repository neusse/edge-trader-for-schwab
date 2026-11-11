# Alpaca Paper release readiness

This is the validation record for [issue #12](https://github.com/neusse/edge-trader-for-schwab/issues/12). Keep an item open until there is a dated result and a reproducible test or redacted Paper order ID. Demo screenshots and mocked responses do not count as broker end-to-end evidence.

## Automated gate

- [x] Run `\.venv\Scripts\python.exe -m pytest -q` from a clean checkout with no broker credentials loaded; record the commit and result. On 2026-11-10, detached clean worktree at merged commit `b3ed911`, with no `.env` and Alpaca/Schwab environment variables removed in the test process: 593 passed, 1 Starlette deprecation warning (Windows). The worktree was removed after the run.
- [ ] Run CI on the final release candidate on Windows and Linux. Interim equities-only PR #81 passed both platforms at `4dbb1e1`; repeat after the controlled Paper equity scenarios and final changes.
- [x] Exercise the same canonical account, order, and mutation contract against Schwab fixtures, Alpaca fixtures, and the in-memory fake (`tests/test_shared_broker_contract.py`; 9 passing cases).
- [x] Verify that live Alpaca credentials and any endpoint other than `https://paper-api.alpaca.markets` are rejected before a network request (`tests/test_broker_configuration.py`: live-only credentials fail, live selection fails, fixed Paper client mode, and invalid endpoint fails before client construction).
- [x] Verify that changing brokers disarms the previous ticket, closes its poller, activates the selected broker's store, and does not carry over position or order state. `tests/test_broker_switch.py` exercises the HTTP selector from a Paper runtime with an armed order service and a marked old store; it observes both disarms, old poller stop, new poller start, and an unmarked disarmed store. `tests/test_store_scope.py` covers broker/account storage isolation.
- [x] Verify restart recovery, stale reads, rate limits, unknown submit/cancel/replace results, and no automatic mutation retry in the automated suite. `tests/test_broker_order_service.py` covers restart, exact client-ID reconciliation, unknown cancel, and exact replacement; `tests/test_alpaca_account.py` covers stale/rate-limited reads; `tests/test_paper_auto_be.py` covers interrupted equity watches. `tests/test_equities_only.py` verifies the option policy. Controlled broker failure scenarios remain open below.

## Controlled Paper equity scenarios

Use a small, disposable Paper position during regular market hours. Record date, app commit, account capability level without revealing the account ID, request/client order IDs with sensitive parts redacted, expected behavior, observed broker status, and cleanup. A rejected or unfilled order remains useful evidence when its final state is reconciled. Never use live Alpaca keys.

| Workflow | Evidence required | Status |
| --- | --- | --- |
| Broker selection | Default Paper, switch to Schwab and Demo, then back; each switch disarms | Passed 2026-11-10 on local running app; all three modes selected and Paper reselected, disarmed throughout |
| Equity entry | Long and short market/limit simple and bracket; fill, target and stop children | Pending |
| Equity management | Unfilled cancel/reprice, partial fill, held OCO, fixed stop/target adjust, trail, partial/full close, add shares | Pending |
| Equity Auto B/E | One +1R move, no loosening, restart and unknown replacement handling | Filled bracket and existing OCO paths implemented; Paper E2E pending (#8) |
| Options and verticals | Trading is disabled for both brokers because this project has no options authorization | Excluded from release scope |
| Failure cases | Broker rejection, partial and unknown outcomes, rate limit, stale data, restart, external changes | Pending |

Alpaca Paper simulates fills, so these checks establish API and app behavior, not live execution quality. Stock quotes in the Paper workflow use IEX, which is not the full market. Option and vertical order routes are disabled, and the background spread-stop sender cannot run. OPRA entitlement is therefore outside this release gate.

On 2026-11-10 the configured Paper endpoint authenticated successfully and reported an active account with no open orders. This was a read-only readiness check; it does not count as workflow end-to-end evidence. The US equity session was closed during this check.

On the same date, a read-only 365-day Paper order query at `b3ed911` found 50 historical bracket groups that normalized to ENTRY, TARGET, and STOP, and 48 OCO groups that normalized to TARGET and STOP. No historical order carried an Edge Trader `et-` client ID, so these records cannot stand in for a controlled app order test. No order mutation was sent. At the merged app runtime, switching Paper → Schwab → Paper succeeded and left Paper disarmed. Paper option submit and Schwab option ticket requests were refused with HTTP 403.

## Documentation and release gate

- [x] `.env.example` contains Paper-only keys and the fixed Paper endpoint.
- [x] `requirements.txt` pins `alpaca-py` and the Schwab client.
- [x] README describes the equities-only scope, broker-held equity brackets/OCO exits, and unprotected orders.
- [x] README identifies local storage and broker/data outbound calls.
- [x] README troubleshooting covers Paper connection, disabled reviews, stale IEX quotes, uncertain order outcomes, broker switching, and read-only option holdings.
- [x] Demo broker selector, Paper overview, and Paper entry screenshots exist under `docs/`; they use invented data.
- [x] Capture invented-data screenshots for the Paper overview and equity entry. `docs/screenshot-alpaca-paper-demo.png` and `docs/screenshot-paper-entry-demo.png` were visually checked on 2026-11-10; no real account data was used. The older option screenshot is historical and excluded from the release.
- [ ] Complete every applicable controlled Paper scenario above and attach redacted evidence to #12.
- [ ] Re-run the automated gate on the final commit and record the results in #12.
- [x] Confirm Schwab is selectable and its regression suite passes. The merged app switched Paper → Schwab → Paper on 2026-11-10; PR #81 passed Linux and Windows CI and the credential-free suite passed 593 tests.
- [x] Confirm no live Alpaca endpoint or mode is reachable. `tests/test_broker_configuration.py` verifies live-only credentials, live selection, and non-Paper endpoints fail before client construction; the merged runtime selected the fixed Paper endpoint.

## Rollback

1. Disarm orders in the app and stop it with `stop.bat`. Check Alpaca Paper and Schwab directly for any working orders before changing versions. Broker-held equity exits remain at the broker unless canceled there. Any legacy option orders must be reviewed directly with the broker.
2. Copy `.env` and `data/` to a private backup. Keep the backup out of Git and support attachments.
3. For a git install, check out the last known good release tag or commit and run `setup.bat`, then `launch.bat`. For a ZIP install, unpack the previous release in a separate folder, copy `.env` and `data/` into it, run `setup.bat`, then `launch.bat`.
4. Select the intended broker, verify account identity, positions, working orders, and protection in the broker and app. Re-arm only after that review. If an order outcome is unknown, reconcile it with the broker before sending another action.

Do not announce a release as ready while any required equity item above is open. Record deliberate scope exclusions in #12 before changing this gate.
