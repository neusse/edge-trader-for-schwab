# ADR-0001: One canonical seam for broker integrations

Status: Accepted

## Context

The application currently consumes Schwab response dictionaries directly in `Poller`, `Trader`,
`orders`, `view`, and the spread desk. Adding Alpaca by branching on the selected broker in those
callers would duplicate payload knowledge, safety rules, and reconciliation logic throughout the
codebase. It would also make tests choose between exercising application behaviour and exercising a
real broker integration.

Schwab and Alpaca are true external dependencies. Their account, order, fill, quote, option, and
calendar shapes differ, as do their supported order combinations. We need a real seam because there
will be two production adapters plus an in-memory adapter for tests.

## Decision

Create the `companion.brokers` module. Its interface has three public parts:

1. `BrokerIdentity`, including a complete `BrokerCapabilities` matrix.
2. `BrokerAdapter.query(query)`, which returns canonical read models.
3. `BrokerAdapter.execute(command)`, which returns a canonical mutation result.

The query and command arguments are closed unions of broker-neutral dataclasses. Adding a broker
operation extends those unions without adding vendor methods to callers. The base adapter resolves
the required capability and refuses unsupported work before the concrete adapter can perform an
external call.

Concrete adapters own all translation between canonical models and vendor payloads. Callers never
receive a Schwab response dictionary or an Alpaca SDK model. Broker order and activity identifiers
are opaque strings.

The in-memory `FakeBrokerAdapter` crosses the same public interface as production adapters. Adapter
contract tests and application tests configure canonical results and assert canonical queries and
commands rather than reaching into adapter implementation details.

## Canonical vocabulary

The seam defines canonical models for:

- broker identity, mode, and capabilities;
- account balances and positions;
- order groups, orders, and legs;
- fills, quotes, option contracts/chains, and market sessions;
- simple, bracket, OCO, trailing-stop, and multi-leg order intents;
- submit, cancel, and replace commands and their accepted/rejected/rate-limited/unknown outcomes.

## Capability matrix

| Capability | Schwab | Alpaca Paper | Demo |
| --- | --- | --- | --- |
| Account, orders, fills, calendar | yes | yes | synthetic reads |
| Equity and option market data | yes | yes | synthetic reads |
| Equity trading and order management | yes | yes | no |
| Single-leg option trading | yes | yes | no |
| Multi-leg options | yes | yes | no |
| Equity bracket and exit OCO | yes | yes | no |
| Option bracket and OCO | yes | no | no |
| Equity trailing stop | yes | yes, as a single order | no |
| Option trailing stop | preview-gated | no | no |
| Option stop/stop-limit | yes | yes | no |
| Order preview | yes | no | no |

The executable matrix lives in `companion/brokers/capabilities.py`; this table explains it but is not
the source of truth.

## Invariants

- Unsupported operations fail before any concrete adapter call and include a user-facing reason.
- No vendor payload names or SDK types cross the seam.
- No adapter automatically retries an ambiguous order mutation.
- Broker identifiers are opaque strings and must be persisted with broker/account ownership.
- Broker mode is explicit. Later configuration work will make Alpaca paper the default and reject
  live Alpaca execution.
- The interface is the test surface; internal transport helpers remain private to each adapter.

## Consequences

`Poller`, `Trader`, and the spread desk will be migrated incrementally in later issues. Until then,
the seam exists beside the current Schwab implementation and does not change runtime behaviour.
The next refactors must translate Schwab into canonical models first, keeping the existing tests
green, before connecting Alpaca.

The canonical interface is intentionally smaller than either broker's SDK. Broker-specific features
that do not serve an application workflow remain inside an adapter or are not exposed.

## Rejected alternatives

### Branch on broker in existing callers

Rejected because vendor knowledge and capability checks would spread across `Poller`, `Trader`,
`view`, and the spread desk. The deletion test shows that removing the seam would recreate this
complexity in every caller.

### Mirror each vendor SDK behind matching wrapper methods

Rejected as shallow pass-through modules. Callers would still need to understand two incompatible
interfaces and response shapes.

### Separate read and trading interfaces at the application seam

Rejected for now. The application needs one selected broker identity and one capability matrix, and
the two operations `query` and `execute` already separate reads from mutations. Concrete adapters may
use private internal seams for trading and market-data clients without exposing them to callers.
