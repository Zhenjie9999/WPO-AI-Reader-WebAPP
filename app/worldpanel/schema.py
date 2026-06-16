from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, TypeVar

from app.worldpanel.pivot_driver import PivotDriver
from app.worldpanel.pivot_models import DimensionTag, MemberNode, normalize


T = TypeVar("T")


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: datetime


class TTLCache(Generic[T]):
    def __init__(self, ttl_seconds: int):
        self.ttl = timedelta(seconds=ttl_seconds)
        self._entries: dict[tuple[object, ...], _CacheEntry[T]] = {}

    def get(self, key: tuple[object, ...]) -> T | None:
        entry = self._entries.get(key)
        if not entry:
            return None
        if datetime.now(timezone.utc) >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return entry.value

    def set(self, key: tuple[object, ...], value: T) -> None:
        self._entries[key] = _CacheEntry(value=value, expires_at=datetime.now(timezone.utc) + self.ttl)

    def invalidate_prefix(self, prefix: tuple[object, ...]) -> None:
        for key in tuple(self._entries):
            if key[: len(prefix)] == prefix:
                self._entries.pop(key, None)

    @property
    def size(self) -> int:
        return len(self._entries)


class SchemaService:
    def __init__(self, driver: PivotDriver, schema_ttl_seconds: int = 300, search_ttl_seconds: int = 120):
        self.driver = driver
        self.schema_cache: TTLCache[tuple[MemberNode, ...]] = TTLCache(schema_ttl_seconds)
        self.search_cache: TTLCache[tuple[MemberNode, ...]] = TTLCache(search_ttl_seconds)

    async def dimensions(self) -> tuple[DimensionTag, ...]:
        return tuple(await self.driver.list_dimension_tags())

    async def members(
        self,
        report: str,
        tag: DimensionTag,
        path: tuple[str, ...] = (),
    ) -> tuple[MemberNode, ...]:
        key = (normalize(report), normalize(tag.label), tuple(normalize(part) for part in path))
        cached = self.schema_cache.get(key)
        if cached is not None:
            return cached
        value = tuple(await self.driver.list_members(tag, path))
        self.schema_cache.set(key, value)
        return value

    async def search(self, report: str, tag: DimensionTag, text: str) -> tuple[MemberNode, ...]:
        key = (normalize(report), normalize(tag.label), normalize(text))
        cached = self.search_cache.get(key)
        if cached is not None:
            return cached
        value = tuple(await self.driver.search_member(tag, text))
        self.search_cache.set(key, value)
        return value

    def invalidate_dimension(self, report: str, dimension: str) -> None:
        prefix = (normalize(report), normalize(dimension))
        self.schema_cache.invalidate_prefix(prefix)
        self.search_cache.invalidate_prefix(prefix)
