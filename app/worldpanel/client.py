from __future__ import annotations

from dataclasses import dataclass
import asyncio
import re
from typing import AsyncIterator

from playwright.async_api import Browser, Page, async_playwright

from app.config import Settings
from app.worldpanel.data_explorer import (
    DataExplorerContext,
    DataExplorerControls,
    DataExplorerDimension,
    QuerySpec,
    discover_controls_from_html,
)


class WorldpanelError(RuntimeError):
    """Raised when Worldpanel automation cannot complete the requested step."""


@dataclass(frozen=True)
class ExtractedReport:
    report_set: str
    report_name: str
    iframe_url: str
    text: str
    metric: str = "Spend (RMB 000)"


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str


@dataclass(frozen=True)
class ReportSetList:
    current: str
    options: list[str]


@dataclass(frozen=True)
class ReadyToUseReport:
    title: str
    report_id: str
    parameter: str


@dataclass(frozen=True)
class ReadyToUseCatalog:
    report_set: str
    current_category: str
    categories: list[str]
    reports: list[ReadyToUseReport]


@dataclass(frozen=True)
class KpiOption:
    label: str
    value: str


@dataclass(frozen=True)
class MultiKpiExtract:
    report_set: str
    report_name: str
    iframe_url: str
    reports: list[ExtractedReport]


class WorldpanelClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def list_report_sets(self, credentials: Credentials) -> ReportSetList:
        async for page in self._new_page():
            await self._login(page, credentials)
            current = await page.locator("#ctl00_cphMain_drpCommunity_Input").input_value()
            options = await self._report_set_options(page)
            return ReportSetList(current=current, options=options)

        raise WorldpanelError("Browser automation ended before report sets could be listed")

    async def list_ready_to_use(
        self,
        credentials: Credentials,
        report_set: str,
        category: str | None = None,
    ) -> ReadyToUseCatalog:
        async for page in self._new_page():
            await self._login(page, credentials)
            await self._select_report_set(page, report_set)
            if category:
                await self._select_ready_to_use_category(page, category)
            await self._wait_for_ready_to_use_reports(page)
            categories = await self._ready_to_use_categories(page)
            current_category = await page.locator(
                "#ctl00_cphMain_ReadyToUseReports1_drpCategories_Input"
            ).input_value()
            reports = await self._ready_to_use_reports(page)
            return ReadyToUseCatalog(
                report_set=report_set,
                current_category=current_category,
                categories=categories,
                reports=reports,
            )

        raise WorldpanelError("Browser automation ended before Ready-to-Use reports could be listed")

    async def extract_key_measures_text(
        self,
        credentials: Credentials | None = None,
        report_set: str | None = None,
        report_parameter: str | None = None,
        report_name: str | None = None,
    ) -> ExtractedReport:
        credentials = credentials or self._settings_credentials()
        report_set = report_set or self.settings.report_set

        async for page in self._new_page():
            await self._login(page, credentials)
            await self._select_report_set(page, report_set)
            if report_parameter:
                await self._open_report_parameter(page, report_parameter)
            else:
                await self._open_data_explorer(page)
            text, iframe_url = await self._read_key_measures_frame(page)
            return ExtractedReport(
                report_set=report_set,
                report_name=report_name or "Data Explorer / Key Measures Data Table",
                iframe_url=iframe_url,
                text=text,
                metric=_metric_from_text(text),
            )

        raise WorldpanelError("Browser automation ended before a report could be extracted")

    async def extract_all_kpis(
        self,
        credentials: Credentials,
        report_set: str,
        report_parameter: str,
        report_name: str,
        requested_kpis: list[str] | None = None,
    ) -> MultiKpiExtract:
        async for page in self._new_page():
            await self._login(page, credentials)
            await self._select_report_set(page, report_set)
            await self._open_report_parameter(page, report_parameter)
            iframe_url = await page.locator("#NavigationReportPanel").get_attribute("src") or ""
            kpis = await self._kpi_options(page)
            selected = _select_kpis(kpis, requested_kpis)
            reports: list[ExtractedReport] = []
            for index, kpi in enumerate(selected):
                if index > 0:
                    await self._select_kpi(page, kpi)
                text, iframe_url = await self._read_key_measures_frame(page)
                reports.append(
                    ExtractedReport(
                        report_set=report_set,
                        report_name=report_name,
                        iframe_url=iframe_url,
                        text=text,
                        metric=kpi.label,
                    )
                )
            return MultiKpiExtract(
                report_set=report_set,
                report_name=report_name,
                iframe_url=iframe_url,
                reports=reports,
            )

        raise WorldpanelError("Browser automation ended before all KPIs could be extracted")

    async def discover_data_explorer_context(
        self,
        credentials: Credentials,
        report_set: str,
        report_parameter: str,
        report_name: str,
    ) -> DataExplorerContext:
        async for page in self._new_page():
            await self._login(page, credentials)
            await self._select_report_set(page, report_set)
            await self._open_report_parameter(page, report_parameter)
            await self._read_key_measures_frame(page)
            controls = await self._discover_data_explorer_controls(page)
            controls = await self._with_pivot_segments(page, controls)
            return _context_from_controls(
                report_set=report_set,
                report_name=report_name,
                report_parameter=report_parameter,
                controls=controls,
            )

        raise WorldpanelError("Browser automation ended before Data Explorer controls could be discovered")

    async def extract_query_spec(
        self,
        credentials: Credentials,
        report_set: str,
        report_parameter: str,
        report_name: str,
        spec: QuerySpec,
    ) -> MultiKpiExtract:
        async for page in self._new_page():
            await self._login(page, credentials)
            await self._select_report_set(page, report_set)
            await self._open_report_parameter(page, report_parameter)
            await self._read_key_measures_frame(page)
            controls = await self._discover_data_explorer_controls(page)
            await self._apply_query_dimensions(page, controls, spec)

            reports: list[ExtractedReport] = []
            for metric in spec.metrics:
                controls = await self._discover_data_explorer_controls(page)
                dimension = _dimension_by_key(controls, "kpi")
                if dimension:
                    await self._select_dimension(page, dimension, str(metric))
                text, iframe_url = await self._read_key_measures_frame(page)
                reports.append(
                    ExtractedReport(
                        report_set=report_set,
                        report_name=report_name,
                        iframe_url=iframe_url,
                        text=text,
                        metric=_metric_label_for_request(str(metric), controls),
                    )
                )

            iframe_url = await page.locator("#NavigationReportPanel").get_attribute("src") or ""
            return MultiKpiExtract(
                report_set=report_set,
                report_name=report_name,
                iframe_url=iframe_url,
                reports=reports,
            )

        raise WorldpanelError("Browser automation ended before the query could be extracted")

    def _settings_credentials(self) -> Credentials:
        if not self.settings.has_credentials:
            raise WorldpanelError("Missing Worldpanel email or password")
        return Credentials(email=self.settings.email or "", password=self.settings.password or "")

    async def _new_page(self) -> AsyncIterator[Page]:
        browser: Browser | None = None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self.settings.headless)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            page.set_default_timeout(self.settings.timeout_ms)
            try:
                yield page
            finally:
                try:
                    await asyncio.wait_for(browser.close(), timeout=10)
                except TimeoutError:
                    # Legacy report dialogs can leave a response pending forever.
                    # Do not let cleanup prevent a completed automation run from returning.
                    pass

    async def _login(self, page: Page, credentials: Credentials) -> None:
        await page.goto(self.settings.login_url, wait_until="domcontentloaded")
        await page.locator("#ctl00_cphMain_txtEmailAddress").fill(credentials.email)
        await page.locator("#ctl00_cphMain_txtPassword").fill(credentials.password)
        await page.locator("#ctl00_cphMain_btnLogin").click()
        await page.wait_for_url("**/Commissioning/Pages/Home.aspx", timeout=self.settings.timeout_ms)

        body = await page.locator("body").inner_text()
        if "Please select a Report Set" not in body:
            raise WorldpanelError("Login succeeded but the Report Set selector was not found")

    async def _select_report_set(self, page: Page, report_set: str) -> None:
        current_value = await page.locator("#ctl00_cphMain_drpCommunity_Input").input_value()
        if report_set != current_value:
            await page.locator("#ctl00_cphMain_drpCommunity_Arrow").click()
            await page.locator("#ctl00_cphMain_drpCommunity_DropDown li.rcbItem", has_text=report_set).click()
        await page.locator("#ctl00_cphMain_btnEnter").click()
        await page.wait_for_url("**/Commissioning/Pages/ReportSelector.aspx", timeout=self.settings.timeout_ms)

    async def _open_data_explorer(self, page: Page) -> None:
        data_explorer = page.locator(
            "a[onclick*='openReadyToUseReport']",
            has_text="Data Explorer",
        )
        await data_explorer.wait_for(state="visible", timeout=self.settings.timeout_ms)
        onclick = await data_explorer.get_attribute("onclick") or ""
        parameter_container = _ready_to_use_parameter(onclick)
        await self._open_report_parameter(page, parameter_container)

    async def _open_report_parameter(self, page: Page, parameter_container: str) -> None:
        await page.goto(
            f"https://eu.worldpanelonline.com/ReportingCS/Content/Navigator.aspx?{parameter_container}",
            wait_until="domcontentloaded",
        )
        await page.frame_locator("#NavigationReportPanel").locator("body").wait_for(
            state="visible",
            timeout=self.settings.timeout_ms,
        )

    async def _read_key_measures_frame(self, page: Page) -> tuple[str, str]:
        frame = page.frame_locator("#NavigationReportPanel")
        body = frame.locator("body")
        # Readiness signal: the rendered data grid OR the "Key Measures Data
        # Table" title, whichever appears first. Different Report Sets / accounts
        # (and non-English UIs) may not show that exact English title even though
        # the data table is present, so waiting only on the title text made the
        # tool fail for those reports. The data grid is the universal signal.
        grid = frame.locator("table.infoset, table[id$='_DB_0001_01']").first
        title = frame.get_by_text("Key Measures Data Table", exact=False).first
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_ms / 1000
        ready = False
        while asyncio.get_running_loop().time() < deadline:
            try:
                if await grid.count() and await grid.is_visible():
                    ready = True
                    break
                if await title.count() and await title.is_visible():
                    ready = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not ready:
            raise WorldpanelError(
                "打开了报表，但未在限定时间内渲染出可读取的数据表格。"
                "请确认所选报表是 Data Explorer 的 Key Measures 数据表（而非图表或其它报表类型），"
                "或稍后重试（服务器休眠/繁忙时首次加载较慢）。"
            )
        text = await body.inner_text(timeout=self.settings.timeout_ms)
        iframe_url = await page.locator("#NavigationReportPanel").get_attribute("src") or ""
        return text, iframe_url

    async def _kpi_options(self, page: Page) -> list[KpiOption]:
        frame = page.frame_locator("#NavigationReportPanel")
        select = frame.locator("select").first
        await select.wait_for(state="visible", timeout=self.settings.timeout_ms)
        records = await select.evaluate(
            """
            element => [...element.options].map(option => ({
              label: option.textContent.trim().replace(/\\s+/g, ' '),
              value: option.value
            }))
            """
        )
        return [KpiOption(label=_clean_text(record["label"]), value=record["value"]) for record in records]

    async def _select_kpi(self, page: Page, kpi: KpiOption) -> None:
        frame = page.frame_locator("#NavigationReportPanel")
        select = frame.locator("select").first
        before = await frame.locator("#ReportIDTemp").get_attribute("value")
        await select.select_option(value=kpi.value)
        await frame.locator("body").wait_for(state="visible", timeout=self.settings.timeout_ms)
        await frame.locator("body").evaluate(
            """
            async (body, args) => {
              const deadline = Date.now() + args.timeout;
              while (Date.now() < deadline) {
                const reportId = document.querySelector('#ReportIDTemp')?.value || '';
                if ((reportId !== args.before && body.innerText.toLowerCase().includes(args.keyword))
                    || body.innerText.includes(args.label)) return;
                await new Promise(resolve => setTimeout(resolve, 100));
              }
              throw new Error('KPI selection did not refresh the report');
            }
            """,
            {"before": before or "", "keyword": kpi.label.split()[0].lower(), "label": kpi.label, "timeout": self.settings.timeout_ms},
        )

    async def _discover_data_explorer_controls(self, page: Page) -> DataExplorerControls:
        frame = page.frame_locator("#NavigationReportPanel")
        html = await frame.locator("body").evaluate("() => document.documentElement.outerHTML")
        return discover_controls_from_html(html)

    async def _with_pivot_segments(self, page: Page, controls: DataExplorerControls) -> DataExplorerControls:
        if controls.segments:
            return controls
        frame = page.frame_locator("#NavigationReportPanel")
        pivot = frame.locator("input[id*='Pivot'], button[id*='Pivot'], a[id*='Pivot']").first
        try:
            if not await pivot.count():
                return controls
            await pivot.evaluate("element => setTimeout(() => element.click(), 0)")
            await _wait_for_page_frame(page, "DB_Pivoting.aspx", self.settings.timeout_ms)
            pivot_controls = await self._discover_data_explorer_controls(page)
        except Exception:
            return controls
        if not pivot_controls.segments:
            return controls
        return DataExplorerControls(
            dimensions=controls.dimensions,
            pivot_button_id=controls.pivot_button_id or pivot_controls.pivot_button_id,
            segments=pivot_controls.segments,
            pivot_slots=pivot_controls.pivot_slots,
        )

    async def _apply_query_dimensions(self, page: Page, controls: DataExplorerControls, spec: QuerySpec) -> None:
        selections: dict[str, str] = dict(spec.dimensions)
        products = list(spec.products)
        if products:
            selections.setdefault("product", products[0])

        for key, value in selections.items():
            dimension = _dimension_by_key(controls, key)
            if dimension:
                await self._select_dimension(page, dimension, value)
                controls = await self._discover_data_explorer_controls(page)

        for segment in spec.segments:
            await self._select_segment(page, segment)

    async def _select_dimension(self, page: Page, dimension: DataExplorerDimension, requested: str) -> None:
        option = _match_option(requested, dimension)
        if not option:
            raise WorldpanelError(f"Could not find option '{requested}' in {dimension.label}")
        frame = page.frame_locator("#NavigationReportPanel")
        select = frame.locator("select").nth(_dimension_index(dimension))
        before = await _report_id_value(frame)
        await select.select_option(value=option.value)
        await frame.locator("#ReportIDTemp").evaluate(
            """
            async (element, args) => {
              const deadline = Date.now() + args.timeout;
              while (Date.now() < deadline) {
                if ((element.value || '') !== args.before) return;
                await new Promise(resolve => setTimeout(resolve, 100));
              }
              throw new Error('Dimension selection did not refresh the report');
            }
            """,
            {"before": before or "", "timeout": self.settings.timeout_ms},
        )

    async def _select_segment(self, page: Page, segment: str) -> None:
        frame = page.frame_locator("#NavigationReportPanel")
        pivot = frame.locator("input[id*='Pivot'], button[id*='Pivot'], a[id*='Pivot']").first
        if await pivot.count():
            await pivot.evaluate("element => setTimeout(() => element.click(), 0)")
            await _wait_for_page_frame(page, "DB_Pivoting.aspx", self.settings.timeout_ms)
        target = frame.locator(f"text={segment}").first
        await target.click()

    async def _report_set_options(self, page: Page) -> list[str]:
        options = await page.locator("#ctl00_cphMain_drpCommunity_DropDown li.rcbItem").all_text_contents()
        cleaned = [_clean_text(option) for option in options]
        return [option for option in cleaned if option]

    async def _wait_for_ready_to_use_reports(self, page: Page) -> None:
        report_link = page.locator("a[onclick*='openReadyToUseReport']").first
        await report_link.wait_for(state="visible", timeout=self.settings.timeout_ms)

    async def _ready_to_use_categories(self, page: Page) -> list[str]:
        categories = await page.locator(
            "#ctl00_cphMain_ReadyToUseReports1_drpCategories_DropDown li.rcbItem"
        ).all_text_contents()
        cleaned = [_clean_text(category) for category in categories]
        return [category for category in cleaned if category and not category.startswith("-")]

    async def _ready_to_use_reports(self, page: Page) -> list[ReadyToUseReport]:
        records = await page.locator("a[onclick*='openReadyToUseReport']").evaluate_all(
            """
            elements => elements
              .map((element) => ({
                title: (element.innerText || element.querySelector('img')?.alt || '').trim(),
                onclick: element.getAttribute('onclick') || ''
              }))
              .filter((record) => record.title && record.onclick.includes('openReadyToUseReport'))
            """
        )

        reports: list[ReadyToUseReport] = []
        seen: set[str] = set()
        for record in records:
            title = _clean_text(record["title"])
            onclick = record["onclick"]
            if title in seen:
                continue
            seen.add(title)
            report_id = _ready_to_use_report_id(onclick)
            parameter = _ready_to_use_parameter(onclick)
            reports.append(ReadyToUseReport(title=title, report_id=report_id, parameter=parameter))
        return reports

    async def _select_ready_to_use_category(self, page: Page, category: str) -> None:
        current = await page.locator("#ctl00_cphMain_ReadyToUseReports1_drpCategories_Input").input_value()
        if _clean_text(current) == _clean_text(category):
            return

        await page.locator("#ctl00_cphMain_ReadyToUseReports1_drpCategories_Arrow").click()
        await page.locator(
            "#ctl00_cphMain_ReadyToUseReports1_drpCategories_DropDown li.rcbItem",
            has_text=category,
        ).click()
        await self._wait_for_ready_to_use_reports(page)


def _ready_to_use_parameter(onclick: str) -> str:
    match = re.search(r"openReadyToUseReport\(\d+,'([^']+)'\)", onclick)
    if not match:
        raise WorldpanelError("Could not parse Data Explorer report parameter")
    return match.group(1)


async def _wait_for_page_frame(page: Page, url_fragment: str, timeout_ms: int):
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for frame in page.frames:
            if url_fragment.casefold() in frame.url.casefold():
                return frame
        await asyncio.sleep(0.1)
    raise WorldpanelError(f"Timed out waiting for frame URL containing {url_fragment}")


def _ready_to_use_report_id(onclick: str) -> str:
    match = re.search(r"openReadyToUseReport\((\d+),", onclick)
    if not match:
        raise WorldpanelError("Could not parse Ready-to-Use report id")
    return match.group(1)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _metric_from_text(text: str) -> str:
    for line in text.splitlines():
        cleaned = _clean_text(line)
        if cleaned:
            if "Spend" in cleaned and "RMB" in cleaned:
                return "Spend (RMB 000)"
            return cleaned
    return "Unknown KPI"


def _select_kpis(kpis: list[KpiOption], requested_kpis: list[str] | None) -> list[KpiOption]:
    if not requested_kpis:
        return kpis

    selected: list[KpiOption] = []
    for requested in requested_kpis:
        normalized = _normalize(requested)
        for kpi in kpis:
            kpi_normalized = _normalize(kpi.label)
            if normalized in kpi_normalized or kpi_normalized in normalized:
                selected.append(kpi)
                break
    seen: set[str] = set()
    unique = []
    for kpi in selected:
        if kpi.value not in seen:
            seen.add(kpi.value)
            unique.append(kpi)
    return unique or kpis


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", value.lower())


def _context_from_controls(
    report_set: str,
    report_name: str,
    report_parameter: str,
    controls: DataExplorerControls,
) -> DataExplorerContext:
    dimensions: dict[str, DataExplorerDimension] = {}
    for dimension in controls.dimensions:
        key = dimension.key
        if key in dimensions:
            key = f"{dimension.key}_{dimension.position}"
        dimensions[key] = dimension
    return DataExplorerContext(
        report_set=report_set,
        report_name=report_name,
        report_parameter=report_parameter,
        dimensions=dimensions,
        segments=controls.segments,
        pivot_slots=controls.pivot_slots,
        current_selections={key: dimension.current for key, dimension in dimensions.items()},
    )


def _dimension_by_key(controls: DataExplorerControls, key: str) -> DataExplorerDimension | None:
    for dimension in controls.dimensions:
        if dimension.key == key:
            return dimension
    return None


def _dimension_index(dimension: DataExplorerDimension) -> int:
    return dimension.position


def _match_option(requested: str, dimension: DataExplorerDimension) -> DataExplorerOption | None:
    requested_normalized = _normalize(requested)
    aliases = _option_aliases(requested_normalized)
    for option in dimension.options:
        option_normalized = _normalize(option.label)
        if (
            requested_normalized == option_normalized
            or requested_normalized in option_normalized
            or option_normalized in requested_normalized
            or any(alias in option_normalized for alias in aliases)
        ):
            return option
    return None


def _option_aliases(normalized: str) -> list[str]:
    if "spend" in normalized or "value" in normalized or "销额" in normalized or "销售额" in normalized:
        return ["spend", "value"]
    if "volume" in normalized or "销量" in normalized or "销售量" in normalized:
        return ["volume"]
    if "penetration" in normalized or "渗透" in normalized:
        return ["penetration"]
    return [normalized]


def _metric_label_for_request(requested: str, controls: DataExplorerControls) -> str:
    kpi = _dimension_by_key(controls, "kpi")
    if not kpi:
        return requested
    option = _match_option(requested, kpi)
    return option.label if option else requested


async def _report_id_value(frame) -> str:
    report_id = frame.locator("#ReportIDTemp")
    if await report_id.count():
        return await report_id.get_attribute("value") or ""
    return ""
