from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from app.worldpanel.pivot_models import ExecutionReceipt, QueryPlan, plan_cache_key
from app.worldpanel.pivot_result import PivotResultTable


@dataclass(frozen=True)
class VerifiedResult:
    receipt: ExecutionReceipt
    answer: str | None = None
    data: dict[str, Any] | None = None
    tables: dict[str, PivotResultTable] | None = None


@dataclass
class _Entry:
    result: VerifiedResult
    expires_at: datetime


class VerifiedResultCache:
    """Caches verified pivot results.

    `scope` must isolate everything the plan itself does not encode: the
    account that produced the result and the exact report parameter. Results
    are never shared across scopes.
    """

    def __init__(self, ttl_seconds: int = 900):
        self.ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[tuple[object, ...], _Entry] = {}

    def get(self, plan: QueryPlan, scope: str) -> VerifiedResult | None:
        key = self._key(plan, scope)
        entry = self._entries.get(key)
        if not entry:
            return None
        if datetime.now(timezone.utc) >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return replace(entry.result, receipt=replace(entry.result.receipt, cache_hit=True))

    def set(self, plan: QueryPlan, scope: str, result: VerifiedResult) -> None:
        if not result.receipt.verified or not result.receipt.table_refreshed:
            raise ValueError("Only verified, refreshed results may be cached")
        self._entries[self._key(plan, scope)] = _Entry(
            result=result,
            expires_at=datetime.now(timezone.utc) + self.ttl,
        )

    @staticmethod
    def _key(plan: QueryPlan, scope: str) -> tuple[object, ...]:
        return (scope, *plan_cache_key(plan))

    @property
    def size(self) -> int:
        return len(self._entries)
