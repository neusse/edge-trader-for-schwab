from __future__ import annotations

from collections import defaultdict, deque
from typing import TypeAlias

from .adapter import BrokerAdapter
from .errors import MissingFakeResponse
from .models import BrokerCommand, BrokerIdentity, BrokerQuery, BrokerQueryResult, MutationResult

ResponseKey: TypeAlias = type[BrokerQuery] | type[BrokerCommand]


class FakeBrokerAdapter(BrokerAdapter):
    """Deterministic in-memory adapter used at the production broker seam."""

    def __init__(self, identity: BrokerIdentity):
        super().__init__(identity)
        self.queries: list[BrokerQuery] = []
        self.commands: list[BrokerCommand] = []
        self._query_results: dict[type, deque[BrokerQueryResult]] = defaultdict(deque)
        self._command_results: dict[type, deque[MutationResult]] = defaultdict(deque)

    def given_query(self, query_type: type[BrokerQuery], *results: BrokerQueryResult) -> None:
        self._query_results[query_type].extend(results)

    def given_command(self, command_type: type[BrokerCommand], *results: MutationResult) -> None:
        self._command_results[command_type].extend(results)

    def _query(self, query: BrokerQuery) -> BrokerQueryResult:
        self.queries.append(query)
        results = self._query_results[type(query)]
        if not results:
            raise MissingFakeResponse(f"no fake query response for {type(query).__name__}")
        return results.popleft()

    def _execute(self, command: BrokerCommand) -> MutationResult:
        self.commands.append(command)
        results = self._command_results[type(command)]
        if not results:
            raise MissingFakeResponse(f"no fake command response for {type(command).__name__}")
        return results.popleft()
