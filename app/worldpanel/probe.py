from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Frame, Locator, Page, Response

from app.config import Settings
from app.worldpanel.client import Credentials, WorldpanelClient, WorldpanelError


_SAFE_HEADERS = {
    "accept",
    "content-type",
    "referer",
    "x-microsoftajax",
    "x-requested-with",
}
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_AUTH = re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+")
_LONG_TOKEN = re.compile(r"(?<![\w-])[A-Za-z0-9+/=_-]{24,}(?![\w-])")
_BUSINESS_NUMBER = re.compile(r"(?<![\w-])[-+]?\d[\d,]*\.\d+%?(?![\w-])")
_INPUT_VALUE = re.compile(
    r"""(<input\b[^>]*(?:type\s*=\s*["']?password|name\s*=\s*["']?"""
    r"""(?:__VIEWSTATE|__EVENTVALIDATION|__EVENTARGUMENT|__EVENTTARGET|[^"'>\s]*(?:token|password|email|session)))[^>]*\bvalue\s*=\s*)(["'])(.*?)(\2)""",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_STYLE_CONTENT = re.compile(
    r"(<(?:script|style)\b[^>]*>).*?(</(?:script|style)>)",
    re.IGNORECASE | re.DOTALL,
)
_DOM_VALUE_ATTRIBUTE = re.compile(
    r"""(\s(?:value|title|alt|href|src|action|onclick|onload|"""
    r"""dimname|dimid|infosetname|origcaptionformat)\s*=\s*)(["'])(.*?)(\2)""",
    re.IGNORECASE | re.DOTALL,
)
_DOM_TEXT = re.compile(r">([^<]*\S[^<]*)<", re.DOTALL)


def redact_text(value: str | None) -> str:
    if not value:
        return ""
    redacted = _INPUT_VALUE.sub(r"\1\2[REDACTED]\4", value)
    redacted = _EMAIL.sub("[REDACTED_EMAIL]", redacted)
    redacted = _AUTH.sub(r"\1[REDACTED]", redacted)
    redacted = _LONG_TOKEN.sub("[REDACTED_TOKEN]", redacted)
    redacted = _BUSINESS_NUMBER.sub("[REDACTED_NUMBER]", redacted)
    return redacted


def redact_url(value: str) -> str:
    parts = urlsplit(value)
    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    query = urlencode([(key, "[REDACTED]") for key, _ in parse_qsl(parts.query, keep_blank_values=True)])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def redact_dom_html(value: str) -> str:
    redacted = redact_text(value)
    redacted = _SCRIPT_STYLE_CONTENT.sub(r"\1[REDACTED_CONTENT]\2", redacted)
    redacted = _DOM_VALUE_ATTRIBUTE.sub(r"\1\2[REDACTED]\4", redacted)
    return _DOM_TEXT.sub(">[REDACTED_TEXT]<", redacted)


def redact_payload(value: str | None) -> str:
    if not value:
        return ""
    try:
        payload = json.loads(value)

        def shape(item: Any) -> Any:
            if isinstance(item, dict):
                return {key: shape(child) for key, child in item.items()}
            if isinstance(item, list):
                return [shape(child) for child in item]
            return "[REDACTED]"

        return json.dumps(shape(payload), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass
    if "=" in value:
        pairs = parse_qsl(value, keep_blank_values=True)
        if pairs:
            return urlencode([(key, "[REDACTED]") for key, _ in pairs])
    return "[REDACTED_BODY]"


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered not in _SAFE_HEADERS:
            continue
        sanitized[lowered] = redact_url(value) if lowered == "referer" else redact_text(value)
    return sanitized


def is_network_candidate(resource_type: str, url: str, method: str) -> bool:
    if resource_type in {"xhr", "fetch"}:
        return True
    lowered = url.lower()
    return method.upper() == "POST" and any(
        token in lowered for token in ("telerik", "webresource.axd", "reportingcs", "callback", "ajax")
    )


@dataclass
class NetworkCapture:
    active: bool = False
    records: list[dict[str, Any]] = field(default_factory=list)
    _pending: set[asyncio.Task[Any]] = field(default_factory=set)
    _handler: Any = None

    def attach(self, page: Page) -> None:
        def schedule(response: Response) -> None:
            if not self.active:
                return
            task = asyncio.create_task(self._capture_response(response))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

        self._handler = schedule
        page.on("response", schedule)

    def detach(self, page: Page) -> None:
        self.active = False
        if self._handler is not None:
            page.remove_listener("response", self._handler)
            self._handler = None

    async def drain(self) -> int:
        pending = tuple(task for task in self._pending if not task.done())
        for task in pending:
            task.cancel()
        await asyncio.sleep(0)
        return len(pending)

    async def _capture_response(self, response: Response) -> None:
        request = response.request
        if not is_network_candidate(request.resource_type, request.url, request.method):
            return
        response_headers = await response.all_headers()
        record: dict[str, Any] = {
            "method": request.method,
            "resource_type": request.resource_type,
            "url": redact_url(request.url),
            "request_headers": sanitize_headers(await request.all_headers()),
            "post_data": redact_payload(request.post_data),
            "status": response.status,
            "response_headers": sanitize_headers(response_headers),
        }
        content_type = response_headers.get("content-type", "")
        if any(token in content_type.lower() for token in ("json", "text", "xml", "javascript")):
            try:
                body = await response.text()
                record["response_body"] = redact_payload(body[:100_000])
                record["response_body_truncated"] = len(body) > 100_000
            except Exception as exc:
                record["response_body_error"] = type(exc).__name__
        self.records.append(record)


async def _find_frame_and_control(page: Page, selectors: list[str]) -> tuple[Frame, Locator]:
    for frame in page.frames:
        for selector in selectors:
            try:
                index = await asyncio.wait_for(
                    frame.evaluate(
                        """
                        selector => [...document.querySelectorAll(selector)].findIndex(element => {
                          const style = getComputedStyle(element);
                          const rect = element.getBoundingClientRect();
                          return style.visibility !== 'hidden' && style.display !== 'none'
                            && rect.width > 0 && rect.height > 0;
                        })
                        """,
                        selector,
                    ),
                    timeout=5,
                )
                if index >= 0:
                    return frame, frame.locator(selector).nth(index)
            except Exception:
                continue
    raise WorldpanelError(f"Could not find visible control matching: {selectors}")


async def _find_control_in_frame(frame: Frame, selectors: list[str]) -> Locator | None:
    for selector in selectors:
        index = await frame.evaluate(
            """
            selector => [...document.querySelectorAll(selector)].findIndex(element => {
              const style = getComputedStyle(element);
              const rect = element.getBoundingClientRect();
              return style.visibility !== 'hidden' && style.display !== 'none'
                && rect.width > 0 && rect.height > 0;
            })
            """,
            selector,
        )
        if index >= 0:
            return frame.locator(selector).nth(index)
    return None


async def _write_frame_inventory(
    page: Page,
    output_dir: Path,
    *,
    prefix: str = "frame",
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for index, frame in enumerate(page.frames):
        record: dict[str, Any] = {
            "index": index,
            "name": redact_text(frame.name),
            "url": redact_url(frame.url),
        }
        try:
            html = await asyncio.wait_for(
                frame.locator("html").evaluate("element => element.outerHTML"),
                timeout=10,
            )
            redacted = redact_dom_html(html)
            filename = f"{prefix}-{index:02}.redacted.html"
            (output_dir / filename).write_text(redacted, encoding="utf-8")
            record["html_file"] = filename
            record["html_bytes"] = len(redacted)
            record["pivot_markers"] = len(re.findall(r"pivot", redacted, re.IGNORECASE))
            record["plus_markers"] = len(re.findall(r"rtPlus|rtExpand", redacted, re.IGNORECASE))
        except Exception as exc:
            record["error"] = type(exc).__name__
        inventory.append(record)
    (output_dir / f"{prefix}-inventory.redacted.json").write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return inventory


async def _dom_fragment(control: Locator) -> str:
    html = await control.evaluate(
        """
        element => {
          const root = element.closest(
            '.RadTreeView, [class*="RadTreeView"], [class*="Pivot"], [id*="Pivot"], [class*="tree"], [id*="tree"]'
          );
          return (root || document.documentElement).outerHTML;
        }
        """
    )
    return redact_dom_html(html)


async def _control_fingerprint(control: Locator) -> dict[str, str]:
    return await control.evaluate(
        """
        element => ({
          tag: element.tagName,
          id: element.id || '',
          class: element.className || '',
          title: element.getAttribute('title') || '',
          role: element.getAttribute('role') || '',
          text: (element.innerText || element.value || element.getAttribute('aria-label') || '').trim()
        })
        """
    )


async def _wait_for_frame_url(page: Page, pattern: str, timeout_ms: int) -> Frame:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for frame in reversed(page.frames):
            if pattern.lower() in frame.url.lower():
                return frame
        await asyncio.sleep(0.25)
    raise WorldpanelError(f"Timed out waiting for frame URL containing: {pattern}")


async def run_phase0_probe(
    settings: Settings,
    output_dir: Path,
    *,
    report_parameter: str | None = None,
    report_set: str | None = None,
) -> Path:
    if not settings.has_credentials:
        raise WorldpanelError("Missing WORLDPANEL_EMAIL or WORLDPANEL_PASSWORD in .env")

    output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = output_dir / "progress.jsonl"

    def progress(stage: str, **details: Any) -> None:
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            **{key: redact_text(str(value)) for key, value in details.items()},
        }
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    client = WorldpanelClient(settings)
    credentials = Credentials(settings.email or "", settings.password or "")
    capture = NetworkCapture()

    async for page in client._new_page():
        page.set_default_timeout(min(settings.timeout_ms, 20_000))
        capture.attach(page)
        progress("login:start")
        await client._login(page, credentials)
        progress("login:done")
        progress("report-set:start")
        await client._select_report_set(page, report_set or settings.report_set)
        progress("report-set:done")
        if report_parameter:
            progress("report:open-parameter:start")
            await client._open_report_parameter(page, report_parameter)
        else:
            progress("report:open-data-explorer:start")
            await client._open_data_explorer(page)
        progress("report:open:done", frames=len(page.frames))
        progress("report:key-measures:wait:start")
        _, iframe_url = await client._read_key_measures_frame(page)
        progress("report:key-measures:wait:done", iframe_url=redact_url(iframe_url), frames=len(page.frames))
        inventory = await _write_frame_inventory(page, output_dir)
        progress(
            "frames:inventory:written",
            frames=len(inventory),
            pivot_markers=sum(int(item.get("pivot_markers", 0)) for item in inventory),
        )

        pivot_selectors = [
            "input[id*='Pivot']",
            "button[id*='Pivot']",
            "a[id*='Pivot']",
            "input[value*='pivot' i]",
            "[title*='pivot screen' i]",
        ]
        progress("pivot:find:start")
        _, pivot = await _find_frame_and_control(page, pivot_selectors)
        pivot_info = await _control_fingerprint(pivot)
        progress("pivot:found", **pivot_info)
        progress("pivot:click:start")
        # OpenPivotingDialog performs legacy synchronous work. Schedule the real
        # DOM click in-page so Playwright does not wait indefinitely for it.
        await pivot.evaluate("element => setTimeout(() => element.click(), 0)")
        pivot_frame = await _wait_for_frame_url(page, "DB_Pivoting.aspx", settings.timeout_ms)
        await pivot_frame.locator(".RadListBox").first.wait_for(state="visible", timeout=settings.timeout_ms)
        progress("pivot:click:done")
        pivot_inventory = await _write_frame_inventory(page, output_dir, prefix="pivot-frame")
        progress(
            "pivot:frames:inventory:written",
            frames=len(pivot_inventory),
            plus_markers=sum(int(item.get("plus_markers", 0)) for item in pivot_inventory),
        )

        member_trigger_selectors = [
            ".rlbItem[title='Product'] img.rlbImage",
            ".rlbItem[title='Outlet'] img.rlbImage",
            ".rlbItem img.rlbImage",
            ".rlbItem img",
            "img[selectorsource='pivot']",
        ]
        progress("member-selector:find:start")
        _, member_trigger = await _find_frame_and_control(page, member_trigger_selectors)
        member_trigger_info = await _control_fingerprint(member_trigger)
        progress("member-selector:found", **member_trigger_info)
        await member_trigger.evaluate("element => setTimeout(() => element.click(), 0)")
        progress("member-selector:page:wait:start")
        member_frame = await _wait_for_frame_url(page, "SimpleSelector.aspx", settings.timeout_ms)
        await member_frame.locator(".RadTreeView").first.wait_for(state="visible", timeout=settings.timeout_ms)
        member_inventory = await _write_frame_inventory(page, output_dir, prefix="member-frame")
        progress(
            "member-selector:click:done",
            frames=len(member_inventory),
            plus_markers=sum(int(item.get("plus_markers", 0)) for item in member_inventory),
        )

        plus_selectors = [
            ".rtPlus",
            "[class*='rtPlus']",
            "[class*='rtExpand']",
            "input[value^='+']",
            "button[aria-expanded='false']",
            "[role='treeitem'] [aria-expanded='false']",
        ]
        progress("plus:find:start")
        frame = member_frame
        plus = await _find_control_in_frame(frame, plus_selectors)
        if plus is None:
            collapse = await _find_control_in_frame(frame, [".rtMinus", "[class*='rtMinus']"])
            if collapse is None:
                raise WorldpanelError("Member tree contains neither an expandable nor expanded node")
            progress("plus:prepare:collapse:start")
            await collapse.evaluate("element => setTimeout(() => element.click(), 0)")
            await member_frame.locator(".rtPlus").first.wait_for(state="visible", timeout=settings.timeout_ms)
            plus = await _find_control_in_frame(frame, plus_selectors)
            progress("plus:prepare:collapse:done")
        if plus is None:
            raise WorldpanelError("Collapsing a member tree node did not expose a plus control")
        plus_info = await _control_fingerprint(plus)
        progress("plus:found", **plus_info)
        capture.active = True
        before_html = await _dom_fragment(plus)
        (output_dir / "pivot-before-plus.redacted.html").write_text(before_html, encoding="utf-8")
        progress("plus:before-dom:written", bytes=len(before_html))

        before_node_count = await frame.locator(
            ".rtLI, [role='treeitem'], input[type='checkbox'], .rtIn"
        ).count()
        progress("plus:click:start", nodes_before=before_node_count)
        await plus.evaluate("element => setTimeout(() => element.click(), 0)")
        progress("plus:click:done")
        try:
            await frame.locator(".rtMinus").first.wait_for(state="visible", timeout=settings.timeout_ms)
            expanded = True
        except Exception:
            expanded = False
        if not expanded:
            progress("plus:mutation-timeout")
        else:
            progress("plus:mutation:confirmed")
        cancelled_network_tasks = await capture.drain()
        if cancelled_network_tasks:
            progress("network:drain-timeout", pending=cancelled_network_tasks)

        after_html = await _dom_fragment(frame.locator(".RadTreeView").first)
        (output_dir / "pivot-after-plus.redacted.html").write_text(after_html, encoding="utf-8")
        (output_dir / "network.redacted.json").write_text(
            json.dumps(capture.records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "report_set": "[REDACTED]",
            "report_parameter_supplied": bool(report_parameter),
            "pivot_control": {key: redact_text(str(value)) for key, value in pivot_info.items()},
            "plus_control": {key: redact_text(str(value)) for key, value in plus_info.items()},
            "nodes_before": before_node_count,
            "nodes_after": await frame.locator(
                ".rtLI, [role='treeitem'], input[type='checkbox'], .rtIn"
            ).count(),
            "network_records": len(capture.records),
            "telerik_observed": any(
                "telerik" in json.dumps(record).lower() or "rtplus" in after_html.lower()
                for record in capture.records
            )
            or any(token in after_html.lower() for token in ("radtreeview", "rtplus", "rtminus")),
            "files": [
                "pivot-before-plus.redacted.html",
                "pivot-after-plus.redacted.html",
                "network.redacted.json",
            ],
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        progress("probe:done", network_records=len(capture.records))
        capture.detach(page)
        try:
            await asyncio.wait_for(page.close(run_before_unload=False), timeout=5)
        except TimeoutError:
            progress("page:close-timeout")
        return output_dir

    raise WorldpanelError("Browser automation ended before Phase 0 probe completed")
