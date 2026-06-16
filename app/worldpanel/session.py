from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from playwright.async_api import Browser, Playwright, async_playwright

from app.config import Settings
from app.worldpanel.client import Credentials, WorldpanelClient
from app.worldpanel.pivot_driver import PivotDriver


CloseCallable = Callable[[], Awaitable[None]]


@dataclass
class DataExplorerSession:
    session_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    page: Any = None
    context: Any = None
    current_report: dict[str, str] | None = None
    last_verified_state: Any = None
    last_used_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    close_callback: CloseCallable | None = None

    async def serialized(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with self.lock:
            self.last_used_at = datetime.now(timezone.utc)
            return await operation()

    def expired(self, ttl: timedelta) -> bool:
        return datetime.now(timezone.utc) - self.last_used_at >= ttl

    async def close(self) -> None:
        if self.close_callback:
            await self.close_callback()
        self.page = None
        self.context = None
        self.close_callback = None


class DataExplorerSessionManager:
    def __init__(self, ttl_seconds: int = 1800):
        self.ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, DataExplorerSession] = {}

    def get_or_create(self, session_id: str) -> DataExplorerSession:
        session = self._sessions.get(session_id)
        if session is None:
            session = DataExplorerSession(session_id=session_id)
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> DataExplorerSession | None:
        return self._sessions.get(session_id)

    async def discard(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            await session.close()

    async def discard_expired(self) -> int:
        expired = [session_id for session_id, session in self._sessions.items() if session.expired(self.ttl)]
        for session_id in expired:
            await self.discard(session_id)
        return len(expired)

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.discard(session_id)

    @property
    def size(self) -> int:
        return len(self._sessions)


async def open_persistent_data_explorer(
    session: DataExplorerSession,
    *,
    settings: Settings,
    credentials: Credentials,
    report_set: str,
    report_parameter: str,
    report_name: str,
) -> PivotDriver:
    if session.page is not None:
        existing = session.context.get("driver") if isinstance(session.context, dict) else None
        if isinstance(existing, PivotDriver):
            return existing
        driver = PivotDriver(session.page, settings.timeout_ms)
        await driver.attach()
        return driver

    playwright: Playwright = await async_playwright().start()
    browser: Browser = await playwright.chromium.launch(headless=settings.headless)
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_default_timeout(settings.timeout_ms)
    client = WorldpanelClient(settings)
    try:
        await client._login(page, credentials)
        await client._select_report_set(page, report_set)
        if report_parameter:
            await client._open_report_parameter(page, report_parameter)
        else:
            await client._open_data_explorer(page)
        await client._read_key_measures_frame(page)
    except Exception:
        await browser.close()
        await playwright.stop()
        raise

    async def close() -> None:
        try:
            await asyncio.wait_for(browser.close(), timeout=10)
        finally:
            await playwright.stop()

    driver = PivotDriver(page, settings.timeout_ms)
    await driver.attach()
    session.page = page
    session.context = {"credentials": credentials, "driver": driver}
    session.current_report = {
        "report_set": report_set,
        "report_parameter": report_parameter,
        "report_name": report_name,
    }
    session.close_callback = close
    return driver
