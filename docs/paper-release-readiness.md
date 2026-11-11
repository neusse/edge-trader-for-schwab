# Alpaca Paper release readiness

This is the validation record for [issue #12](https://github.com/neusse/edge-trader-for-schwab/issues/12). Keep an item open until there is a dated result and a reproducible test or redacted Paper order ID. Demo screenshots and mocked responses do not count as broker end-to-end evidence.

## Automated gate

- [x] Run `\.venv\Scripts\python.exe -m pytest -q` from a clean checkout with no broker credentials loaded; record the commit and result. On 2026-11-11, detached clean worktree at PR #99 head `6d72adc`, with no `.env` and Alpaca/Schwab/APCA environment variables removed in the test process: 612 passed, 1 upstream Starlette deprecation warning (Windows). The worktree was removed after the run.
- [ ] Run CI on the final release candidate on Windows and Linux. Code PR #99 passed both platforms after controlled Paper short-exit testing. Repeat or record CI for this final documentation change.
- [x] Exercise the same canonical account, order, and mutation contract against Schwab fixtures, Alpaca fixtures, and the in-memory fake (`tests/test_shared_broker_contract.py`; 9 passing cases).
- [x] Verify that live Alpaca credentials and any endpoint other than `https://paper-api.alpaca.markets` are rejected before a network request (`tests/test_broker_configuration.py`: live-only credentials fail, live selection fails, fixed Paper client mode, and invalid endpoint fails before client construction).
- [x] Verify that changing brokers disarms the previous ticket, closes its poller, activates the selected broker's store, and does not carry over position or order state. `tests/test_broker_switch.py` exercises the HTTP selector from a Paper runtime with an armed order service and a marked old store; it observes both disarms, old poller stop, new poller start, and an unmarked disarmed store. `tests/test_store_scope.py` covers broker/account storage isolation.
- [x] Verify restart recovery, stale reads, rate limits, unknown submit/cancel/replace results, and no automatic mutation retry in the automated suite. `tests/test_broker_order_service.py` covers restart, exact client-ID reconciliation, unknown cancel, and exact replacement; `tests/test_alpaca_account.py` covers stale/rate-limited reads; `tests/test_paper_auto_be.py` covers interrupted equity watches. `tests/test_equities_only.py` verifies the option policy. Controlled broker failure scenarios remain open below.

## Controlled Paper equity scenarios

Use a small, disposable Paper position during regular market hours. Record date, app commit, account capability level without revealing the account ID, request/client order IDs with sensitive parts redacted, expected behavior, observed broker status, and cleanup. A rejected or unfilled order remains useful evidence when its final state is reconciled. Never use live Alpaca keys.

Start or restart the app from the same clean checkout before collecting evidence. Before and after each controlled action, run `\.venv\Scripts\python.exe scripts\capture_paper_evidence.py <scenario-label>` with Alpaca Paper selected. The tool checks the app's process-start commit and clean state against the current checkout before any broker read; a mismatch writes no record. It then compares the app view with independent Paper account, working-order, and fill reads. It appends timestamped evidence to ignored `data/paper-e2e-evidence.jsonl`, hashes account/order/client/fill IDs and symbols with a private salt in `data/.paper-e2e-salt`, and never arms or sends orders. Keep the salt with the evidence file so references remain comparable across snapshots. Use stable labels such as `baseline`, `bracket-filled`, and `cleanup`; inspect the record before sharing a redacted excerpt in #8 or #12. Record the reviewed order request and the exact cleanup separately, since a snapshot alone does not prove the user's confirmation step.

Before the first controlled order, run `\.venv\Scripts\python.exe scripts\capture_paper_evidence.py baseline --require-clean-market`. This read-only preflight writes a marked baseline only when the app and an independent Alpaca Paper clock both report a fresh open session, the app is ready and disarmed with no unresolved mutation, and the Paper account has no positions, working orders, or same-day fills. A refusal leaves the evidence file unchanged; inspect the cause before starting a scenario. Later snapshots omit the flag because an intentional Paper fill makes the account nonempty.

| Workflow | Evidence required | Status |
| --- | --- | --- |
| Broker selection | Default Paper, switch to Schwab and Demo, then back; each switch disarms | Passed 2026-11-10 on local running app; all three modes selected and Paper reselected, disarmed throughout |
| Equity entry | Long and short market/limit simple and bracket; fill, target and stop children | Controlled Paper: long and short market fills, unfilled limit/reprice/cancel, filled market and marketable-limit long brackets, and a filled short bracket. Both held exits were verified. |
| Equity management | Unfilled cancel/reprice, partial fill, held OCO, fixed stop/target adjust on OCO and filled bracket exits, trail, partial/full close, add shares | Controlled Paper: two-share long filled in two executions; partial close, add, full close, short cover, unfilled cancel/reprice, held bracket and OCO, both exit adjustments on each, and a standalone trailing stop. All test positions and working orders were cleaned up. |
| Equity Auto B/E | One +1R move, no loosening, restart and unknown replacement handling | Watches set on filled AAPL and F brackets and observed while armed. Neither reached +1R; no automatic stop move occurred. Automated tests cover trigger, no loosening, restart and unknown handling. Real Paper trigger remains open in #12. |
| Options and verticals | Trading is disabled for both brokers because this project has no options authorization | Excluded from release scope |
| Failure cases | Broker rejection, partial and unknown outcomes, rate limit, stale data, restart, external changes | Real Paper partial fills, unknown replace followed by exact reconciliation, and externally canceled exits observed. Automated tests cover rejection, rate limit, stale data, restart and external changes; those faults were not deliberately induced against Paper. |

Alpaca Paper simulates fills, so these checks establish API and app behavior, not live execution quality. Stock quotes in the Paper workflow use IEX, which is not the full market. Option and vertical order routes are disabled, and the background spread-stop sender cannot run. OPRA entitlement is therefore outside this release gate.

Read-only option quotes for Paper holdings use Schwab. The local Schwab authorization is expired, so a real read-only quote remains unverified in [issue #86](https://github.com/neusse/edge-trader-for-schwab/issues/86); missing quotes leave option holdings stale or unpriced and do not enable options trading.

On 2026-11-10 the configured Paper endpoint authenticated successfully and reported an active account with no open orders. This was a read-only readiness check; it does not count as workflow end-to-end evidence. The US equity session was closed during this check.

On the same date, a read-only 365-day Paper order query at `b3ed911` found 50 historical bracket groups that normalized to ENTRY, TARGET, and STOP, and 48 OCO groups that normalized to TARGET and STOP. No historical order carried an Edge Trader `et-` client ID, so these records cannot stand in for a controlled app order test. No order mutation was sent. At the merged app runtime, switching Paper → Schwab → Paper succeeded and left Paper disarmed. Paper option submit and Schwab option ticket requests were refused with HTTP 403.

On 2026-11-11, controlled Paper equity tests ran from the clean merged app at `34d16fd`. The independent open-session baseline had no positions, working orders, or fills. The redacted local evidence includes `f-simple-filled`, `f-oco-accepted`, `f-oco-target-adjusted`, `f-oco-stop-adjusted`, `f-oco-canceled`, `f-closed`, `f-trail-entry-filled`, `f-trail-accepted`, `f-trail-canceled`, `f-trail-closed`, `aapl-bracket-canceled`, and `final-test-cleanup`, alongside the earlier long/short and bracket labels. That sequence ended with **0 positions and 0 working orders**, 14 same-day fills, a disarmed app, and no unresolved mutation. Raw IDs and account details remain only in ignored local `data/` files. See the redacted [issue #8 record](https://github.com/neusse/edge-trader-for-schwab/issues/8) for action results. The Paper account is also used by the swing trader, so further controlled orders require an isolated account or a coordinated test window that preserves its activity.

A second coordinated one-ticker Paper sequence ran at clean merged app `42c1747`: a marketable one-share F limit bracket filled with both held exits; its stop was tightened, an Auto B/E watch was set but not triggered, and the position was cleaned up. A one-share F short bracket then filled and showed FULL held-exit protection. Its adjustment preview exposed that Alpaca normalizes a short exit as `BUY_TO_COVER` while the guard accepted only `BUY`; no replacement was sent. The protected short was canceled and covered. The final independent read after this sequence showed **0 positions, 0 working orders, and 18 same-day fills**; the app was disarmed with zero unresolved mutations. The short-exit guard regression is addressed in the subsequent code change. Only F was used during this coordinated sequence.

At clean merged app `2ecd8f6`, the same F short-bracket adjustment preview passed with the fix. A tight replacement stop was accepted after exact broker reconciliation and then filled before Auto B/E could be armed. A separate, isolated one-share AAPL long bracket filled and showed both held exits; an Auto B/E watch was set, but the original broker-held stop filled before +1R. The watch ended on the flat position and was cleared. The last independent Paper read showed **0 positions, 0 working orders, and 22 same-day fills**; the app was disarmed with zero unresolved mutations. No real Auto B/E stop replacement has been observed.

## Documentation and release gate

- [x] `.env.example` contains Paper-only keys and the fixed Paper endpoint.
- [x] `requirements.txt` pins `alpaca-py` and the Schwab client.
- [x] README Quick start and Schwab setup sections match `setup.bat`, `launch.bat`, `stop.bat`, and `login.bat` and link to this fork's CI and releases.
- [x] README describes the equities-only scope, broker-held equity brackets/OCO exits, and unprotected orders.
- [x] README identifies local storage and broker/data outbound calls.
- [x] README troubleshooting covers Paper connection, disabled reviews, stale IEX quotes, uncertain order outcomes, broker switching, and read-only option holdings.
- [x] Demo broker selector, Paper overview, and Paper entry screenshots exist under `docs/`; they use invented data.
- [x] Capture invented-data screenshots for the Paper overview and equity entry. `docs/screenshot-alpaca-paper-demo.png` and `docs/screenshot-paper-entry-demo.png` were visually checked on 2026-11-10; no real account data was used. The older option screenshot is historical and excluded from the release.
- [ ] Complete the remaining controlled Paper scenarios above and attach a final redacted result to #12. A real Auto B/E +1R trigger was not observed; further order tests need account isolation or coordination with the swing trader.
- [ ] Re-run the automated gate on the final commit and record the results in #12.
- [x] Confirm Schwab is selectable and its regression suite passes. The merged app switched Paper → Schwab → Paper on 2026-11-10; PR #81 passed Linux and Windows CI and the credential-free suite passed 593 tests.
- [x] Confirm no live Alpaca endpoint or mode is reachable. `tests/test_broker_configuration.py` verifies live-only credentials, live selection, and non-Paper endpoints fail before client construction; the merged runtime selected the fixed Paper endpoint.

## Rollback

1. Disarm orders in the app and stop it with `stop.bat`. Check Alpaca Paper and Schwab directly for any working orders before changing versions. Broker-held equity exits remain at the broker unless canceled there. Any legacy option orders must be reviewed directly with the broker.
2. Copy `.env` and `data/` to a private backup. Keep the backup out of Git and support attachments.
3. For a git install, check out the last known good release tag or commit and run `setup.bat`, then `launch.bat`. For a ZIP install, unpack the previous release in a separate folder, copy `.env` and `data/` into it, run `setup.bat`, then `launch.bat`.
4. Select the intended broker, verify account identity, positions, working orders, and protection in the broker and app. Re-arm only after that review. If an order outcome is unknown, reconcile it with the broker before sending another action.

Do not announce a release as ready while any required equity item above is open. Record deliberate scope exclusions in #12 before changing this gate.
