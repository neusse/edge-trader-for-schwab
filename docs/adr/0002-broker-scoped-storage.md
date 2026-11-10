# ADR-0002: Separate databases for broker and account state

Status: Accepted

## Context

The Schwab installation already has a `data/companion.db` with integer order IDs.
Alpaca uses opaque string IDs. A shared database would let identical ticket or order IDs
from different accounts collide, and could display one account's cached state under another.

## Decision

Use one SQLite database per broker and account. `Store.for_broker(broker, account)`
derives the filename from the broker key and a SHA-256 digest of the account identifier.
The account identifier itself is not included in the filename. Each database records its
broker and account in `store_scope`; opening it with another identity fails.

The first Schwab account uses the existing `data/companion.db`. Its original schema and
contents remain intact and its first scoped open pins the account identity. If the user
selects another Schwab account, it must receive a separate store in a future migration;
the application must not silently reuse the legacy database.

New broker databases use text columns for order and activity IDs. They retain the same
local evidence tables, ticket idempotency rules, and unknown-outcome gates, now isolated
by the database selection. A runtime must discover the account and select its store
before publishing data or accepting order actions.

## Consequences

Broker switching must stop the old runtime, disarm entries, and create the new runtime
with the store for its authenticated account. It must not temporarily serve cached data
from the previous runtime. This is completed with the broker selector and polling work.
