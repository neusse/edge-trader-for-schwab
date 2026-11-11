# ADR-0003: Equities-only application trading scope

Status: Accepted

## Context

The broker seam in ADR-0001 records operations the brokers technically expose. This project does
not have authorization to trade options in either Alpaca or Schwab. Previously implemented option
and vertical routes must not remain actionable merely because a broker reports an options level.

## Decision

The application enables equity trading only for both brokers. The UI hides option and spread
controls, the public API rejects option requests, and broker order services reject canonical option
intents before sending. The Alpaca background spread-stop sender is disabled. Existing option
positions and orders may remain visible as read-only account information; users manage them directly
with their broker.

When Alpaca Paper is selected, visible option holdings and working option orders may use a separate
Schwab read-only market quote client. Those quotes never alter the Alpaca account, order, or fill source.
Schwab quote or login failures leave option prices unavailable and do not disarm Paper equity trading.

The adapter capability matrix in ADR-0001 remains a description of technical broker support. The
application's broker metadata reports the narrower enabled trading scope. The Alpaca Paper release
gate requires equity scenarios only.

## Consequences

The single-leg and vertical implementation remains in source and unit tests but is outside the
product's enabled workflows. Any future change to this policy requires a separate authorization
decision and a new release validation gate.
