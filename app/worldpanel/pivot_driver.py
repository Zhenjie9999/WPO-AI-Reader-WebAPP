from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Frame, Locator, Page

from app.worldpanel.client import WorldpanelError
from app.worldpanel.pivot_models import (
    AppliedPivotState,
    DimensionTag,
    MemberNode,
    PivotLayout,
    ReportDropdown,
    normalize,
)
from app.worldpanel.pivot_parser import (
    classify_dropdown_role,
    parse_dimension_tags,
    parse_member_tree,
    parse_pivot_layout,
    selected_member_paths,
)


@dataclass(frozen=True)
class PivotFrames:
    report: Frame
    pivot: Frame | None = None
    selector: Frame | None = None


class PivotDriver:
    """Live DOM driver for the Telerik controls confirmed by the Phase 0 probe."""

    def __init__(self, page: Page, timeout_ms: int = 60_000):
        self.page = page
        self.timeout_ms = timeout_ms
        self.frames: PivotFrames | None = None
        self.active_dimension: str | None = None
        self.actions: list[str] = []
        self._selected_members: dict[str, set[tuple[str, ...]]] = {}
        # Dimensions temporarily moved onto an axis to open their member dialog,
        # keyed by normalized label -> (label, original_axis), so the layout can
        # be restored after a read-only enumeration.
        self._restore_axis: dict[str, tuple[str, str]] = {}

    async def attach(self) -> None:
        report = await self._wait_for_frame("DB_0001pp.aspx")
        self.frames = PivotFrames(report=report)

    async def open_pivot(self) -> None:
        if self.frames and self.frames.pivot and not self.frames.pivot.is_detached():
            if await self.frames.pivot.locator(".RadListBox").count():
                return
        report = self._require_frames().report
        pivot_button = report.locator(
            "input[id*='Pivot'], button[id*='Pivot'], a[id*='Pivot'], [title*='pivot screen' i]"
        ).first
        await pivot_button.wait_for(state="visible", timeout=self.timeout_ms)
        await self._legacy_click(pivot_button)
        pivot = await self._wait_for_frame("DB_Pivoting.aspx")
        await pivot.locator(".RadListBox").first.wait_for(state="visible", timeout=self.timeout_ms)
        self.frames = PivotFrames(report=report, pivot=pivot)
        self.actions.append("open_pivot")

    async def list_dimension_tags(self) -> list[DimensionTag]:
        pivot = self._require_pivot()
        return list(parse_dimension_tags(await self._html(pivot)))

    async def read_layout(self) -> PivotLayout:
        return parse_pivot_layout(await self._html(self._require_pivot()))

    async def set_axis(self, tag: DimensionTag, axis: str, position: int) -> None:
        pivot = self._require_pivot()
        result = await pivot.evaluate(
            """
            ({label, axis, position}) => {
              const suffix = axis === 'row' ? 'RowHierarchies' :
                axis === 'column' ? 'ColumnHierarchies' : 'PageHierarchies';
              const targetElement = [...document.querySelectorAll('.RadListBox')]
                .find(element => (element.id || '').endsWith(suffix));
              const sourceElement = [...document.querySelectorAll('.RadListBox')]
                .find(element => [...element.querySelectorAll('.rlbItem')].some(
                  item => (item.title || item.textContent || '').replace(/\\[\\d+\\]\\s*$/, '').trim() === label
                ));
              if (!targetElement || !sourceElement) return false;
              const source = $find(sourceElement.id);
              const target = $find(targetElement.id);
              let item = source.findItemByText(label);
              for (let index = 0; !item && index < source.get_items().get_count(); index++) {
                const candidate = source.getItem(index);
                if (candidate.get_text().replace(/\\[\\d+\\]\\s*$/, '').trim() === label) item = candidate;
              }
              if (!source || !target || !item) return false;
              if (source !== target && !source.transferItem(item, source, target)) return false;
              if (position >= 0 && position < target.get_items().get_count()) {
                target.reorderItem(item, position);
              }
              target._updateUI();
              return true;
            }
            """,
            {"label": tag.label, "axis": axis, "position": position},
        )
        if not result:
            raise WorldpanelError(f"Could not move dimension '{tag.label}' to {axis}")
        self.actions.append(f"set_axis:{tag.label}:{axis}:{position}")

    async def remove_dimension(self, tag: DimensionTag) -> None:
        await self.set_axis(tag, "available", 0)

    async def verify_layout(self, expected: PivotLayout) -> None:
        actual = await self.read_layout()
        for axis in ("row", "column", "filter"):
            expected_values = tuple(normalize(value) for value in expected.axis(axis))  # type: ignore[arg-type]
            actual_values = tuple(normalize(value) for value in actual.axis(axis))  # type: ignore[arg-type]
            if expected_values != actual_values:
                raise WorldpanelError(f"Pivot {axis} mismatch: expected {expected.axis(axis)}, got {actual.axis(axis)}")

    async def open_member_selector(self, tag: DimensionTag) -> None:
        pivot = self._require_pivot()
        # Page/filter dimensions render their member icon hidden, so the member
        # dialog can only be opened while the dimension sits on a Row/Column
        # axis. Move it onto Column first, then restore the layout afterwards.
        moved_from = None
        if not await self._member_icon_visible(tag):
            layout = await self.read_layout()
            current_axis = self._axis_of(layout, tag.label)
            if current_axis in (None, "filter", "available"):
                await self.set_axis(tag, "column", 0)
                moved_from = current_axis or "available"
                tag = self._refreshed_tag(await self.list_dimension_tags(), tag.label) or tag
        item = pivot.locator(".rlbItem", has_text=tag.label).first
        image = item.locator("img.rlbImage, img").first
        if not await image.count() or not await image.is_visible():
            raise WorldpanelError(f"Dimension has no selectable member dialog: {tag.label}")
        await self._legacy_click(image)
        selector = await self._wait_for_frame("SimpleSelector.aspx")
        await selector.locator(".RadTreeView").first.wait_for(state="visible", timeout=self.timeout_ms)
        self.frames = PivotFrames(report=self._require_frames().report, pivot=pivot, selector=selector)
        self.active_dimension = tag.label
        if moved_from:
            self._restore_axis[normalize(tag.label)] = (tag.label, moved_from)
        self.actions.append(f"open_members:{tag.label}")

    async def _member_icon_visible(self, tag: DimensionTag) -> bool:
        pivot = self._require_pivot()
        item = pivot.locator(".rlbItem", has_text=tag.label).first
        if not await item.count():
            return False
        image = item.locator("img.rlbImage, img").first
        return bool(await image.count() and await image.is_visible())

    @staticmethod
    def _axis_of(layout: PivotLayout, label: str) -> str | None:
        target = normalize(label)
        for axis in ("row", "column", "filter", "available"):
            if any(normalize(value) == target for value in layout.axis(axis)):  # type: ignore[arg-type]
                return axis
        return None

    @staticmethod
    def _refreshed_tag(tags: list[DimensionTag], label: str) -> DimensionTag | None:
        target = normalize(label)
        return next((tag for tag in tags if normalize(tag.label) == target), None)

    async def list_all_members(self, tag: DimensionTag, max_passes: int = 60) -> list[MemberNode]:
        """Open the member dialog and recursively expand every collapsed `+`
        node, returning the complete member tree (all hidden members included)."""
        await self._ensure_selector(tag)
        selector = self._require_selector()
        for _ in range(max_passes):
            remaining = await selector.evaluate(
                """
                () => {
                  const plus = [...document.querySelectorAll('.RadTreeView .rtPlus')]
                    .filter(el => el.offsetParent !== null);
                  plus.forEach(el => el.click());
                  return plus.length;
                }
                """
            )
            if not remaining:
                break
            await asyncio.sleep(0.35)
        return list(parse_member_tree(await self._html(selector)))

    async def read_dropdowns(self) -> list[ReportDropdown]:
        """Read every page/filter dropdown on the report with its options and
        current selection. Dropdowns are matched to page-dimension labels by
        document order when the counts line up."""
        report = await self._wait_for_frame("DB_0001pp.aspx")
        raw = await report.evaluate(
            """
            () => [...document.querySelectorAll('select')].map((select, index) => ({
              index,
              selected: (select.selectedOptions[0]?.textContent || '').trim().replace(/\\s+/g, ' '),
              options: [...select.options].map(option => option.textContent.trim().replace(/\\s+/g, ' '))
            }))
            """
        )
        page_labels: list[str] = []
        if self.frames and self.frames.pivot and not self.frames.pivot.is_detached():
            layout = await self.read_layout()
            page_labels = list(layout.filters)
        dropdowns: list[ReportDropdown] = []
        for entry in raw:
            options = tuple(str(option) for option in entry["options"])
            index = int(entry["index"])
            dimension = page_labels[index] if index < len(page_labels) else ""
            dropdowns.append(
                ReportDropdown(
                    index=index,
                    role=classify_dropdown_role(options),
                    dimension=dimension,
                    selected=str(entry["selected"]),
                    options=options,
                )
            )
        return dropdowns

    async def select_dropdown(self, index: int, value: str) -> str:
        """Select a value (by label match) in the report dropdown at `index`,
        wait for the report to refresh, and return the applied label."""
        report = await self._wait_for_frame("DB_0001pp.aspx")
        select = report.locator("select").nth(index)
        await select.wait_for(state="attached", timeout=self.timeout_ms)
        options = await select.evaluate(
            """
            element => [...element.options].map(option => ({
              label: option.textContent.trim().replace(/\\s+/g, ' '),
              value: option.value,
              selected: option.selected
            }))
            """
        )
        match = _match_label_option(value, options)
        if match is None:
            available = ", ".join(str(option["label"]) for option in options)
            raise WorldpanelError(f"Option '{value}' not available in dropdown {index}. Options: {available}")
        if match.get("selected"):
            return str(match["label"])
        before_text = await report.locator("body").inner_text(timeout=self.timeout_ms)
        await select.select_option(value=str(match["value"]))
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            report = await self._wait_for_frame("DB_0001pp.aspx")
            current_text = await report.locator("body").inner_text(timeout=self.timeout_ms)
            if current_text != before_text:
                break
            await asyncio.sleep(0.2)
        self.frames = PivotFrames(report=report)
        self.actions.append(f"select_dropdown:{index}:{match['label']}")
        return str(match["label"])

    async def list_members(self, tag: DimensionTag, path: tuple[str, ...] = ()) -> list[MemberNode]:
        await self._ensure_selector(tag)
        nodes = list(parse_member_tree(await self._html(self._require_selector())))
        if not path:
            return [node for node in nodes if node.level == 0]
        return [node for node in nodes if node.path[:-1] == path]

    async def search_member(self, tag: DimensionTag, text: str) -> list[MemberNode]:
        await self._ensure_selector(tag)
        selector = self._require_selector()
        if text:
            # Prefer the selector dialog's own search box when one exists, so
            # virtualized/long trees materialize matching nodes in the DOM.
            search_input = selector.locator("input[id*='Search' i], input[id*='Filter' i]").first
            try:
                if await search_input.count() and await search_input.is_visible():
                    await search_input.fill(text)
                    await asyncio.sleep(0.3)
            except Exception:
                pass
        normalized = normalize(text)
        return [
            node
            for node in parse_member_tree(await self._html(selector))
            if normalized in normalize(node.label)
        ]

    async def expand_member(self, tag: DimensionTag, path: tuple[str, ...]) -> None:
        await self._ensure_selector(tag)
        node = await self._node_by_path(path)
        toggle = node.locator(".rtPlus").first
        if await toggle.count():
            await self._legacy_click(toggle)
            await self._wait_for_class(node, "rtMinus")
        elif not await node.locator(".rtMinus").count():
            raise WorldpanelError(f"Member is not expandable: {' > '.join(path)}")
        self.actions.append(f"expand:{tag.label}:{' > '.join(path)}")

    async def check_member(self, tag: DimensionTag, member: MemberNode, checked: bool = True) -> None:
        await self._ensure_selector(tag)
        selector = self._require_selector()
        changed = await selector.evaluate(
            """
            ({path, checked}) => {
              const tree = window.membersTree || $find('ctl00_cphMain_trvSimpleSelector');
              const nodePath = node => {
                const parts = [];
                for (let current = node; current; current = current.get_parent()) {
                  if (typeof current.get_text === 'function') parts.unshift(current.get_text().trim());
                }
                return parts;
              };
              const node = tree?.get_allNodes().find(
                item => JSON.stringify(nodePath(item)) === JSON.stringify(path)
              );
              if (!tree || !node) return false;
              const selected = () => tree.get_selectedNodes().some(item => item === node);
              if (checked && !selected()) node.select();
              if (!checked && selected()) node.unselect();
              return selected() === checked;
            }
            """,
            {"path": list(member.path), "checked": checked},
        )
        if not changed:
            raise WorldpanelError(f"Could not set member selection: {' > '.join(member.path)}")
        self.actions.append(f"check:{tag.label}:{' > '.join(member.path)}:{checked}")
        selected = self._selected_members.setdefault(normalize(tag.label), set())
        if checked:
            selected.add(member.path)
        else:
            selected.discard(member.path)

    async def clear_member_selection(self, tag: DimensionTag) -> None:
        await self._ensure_selector(tag)
        selector = self._require_selector()
        cleared = await selector.evaluate(
            """
            () => {
              const tree = window.membersTree || $find('ctl00_cphMain_trvSimpleSelector');
              if (!tree) return false;
              tree.unselectAllNodes();
              return tree.get_selectedNodes().length === 0;
            }
            """
        )
        if not cleared:
            raise WorldpanelError(f"Could not clear member selection: {tag.label}")
        self._selected_members[normalize(tag.label)] = set()
        self.actions.append(f"clear_members:{tag.label}")

    async def apply_member_selection(self) -> None:
        selector = self._require_selector()
        try:
            await selector.evaluate(
                """
                () => {
                  if (typeof saveSelectedMemOrdinals !== 'function') return false;
                  saveSelectedMemOrdinals();
                  return true;
                }
                """
            )
        except Exception:
            # The site's save function closes and detaches its own frame.
            pass
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline and not selector.is_detached():
            await asyncio.sleep(0.1)
        if not selector.is_detached():
            raise WorldpanelError("Member selection did not close after submission")
        frames = self._require_frames()
        pivot = await self._wait_for_frame("DB_Pivoting.aspx")
        self.frames = PivotFrames(report=frames.report, pivot=pivot)
        self.actions.append("apply_member_selection")

    async def cancel_member_selection(self) -> None:
        if not self.frames or not self.frames.selector or self.frames.selector.is_detached():
            self.active_dimension = None
            return
        selector = self._require_selector()
        button = selector.locator("#cphMain_btnCancel, input[id$='btnCancel']").first
        await button.wait_for(state="visible", timeout=self.timeout_ms)
        await self._legacy_click(button)
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline and not selector.is_detached():
            await asyncio.sleep(0.1)
        frames = self._require_frames()
        self.frames = PivotFrames(report=frames.report, pivot=frames.pivot)
        self.active_dimension = None
        self.actions.append("cancel_member_selection")

    async def apply(self) -> None:
        pivot = self._require_pivot()
        before_report = self._require_frames().report
        before_text = await before_report.locator("body").inner_text(timeout=self.timeout_ms)
        button = pivot.locator(
            "#cphMain_btnDBLayoutSave, input[id$='btnDBLayoutSave'], a[id$='btnDBLayoutSave']"
        ).first
        await button.wait_for(state="visible", timeout=self.timeout_ms)
        await self._legacy_click(button)
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        changed = False
        while asyncio.get_running_loop().time() < deadline:
            if pivot.is_detached():
                break
            report = await self._wait_for_frame("DB_0001pp.aspx")
            current_text = await report.locator("body").inner_text(timeout=self.timeout_ms)
            if current_text != before_text:
                changed = True
                break
            await asyncio.sleep(0.1)
        report = await self._wait_for_frame("DB_0001pp.aspx")
        while not changed and asyncio.get_running_loop().time() < deadline:
            report = await self._wait_for_frame("DB_0001pp.aspx")
            await report.locator("body").wait_for(state="visible", timeout=self.timeout_ms)
            current_text = await report.locator("body").inner_text(timeout=self.timeout_ms)
            if current_text != before_text:
                changed = True
                break
            await asyncio.sleep(0.2)
        self.frames = PivotFrames(report=report)
        self.actions.append("apply_pivot" if changed else "apply_pivot:no_text_delta")

    async def read_applied_state(
        self,
        *,
        dimensions: tuple[DimensionTag, ...] = (),
        kpis: tuple[str, ...] = (),
        period: str | None = None,
        table_refreshed: bool = False,
    ) -> AppliedPivotState:
        """Read the layout and member selections back from the live UI.

        `dimensions` lists the tags whose member selectors are reopened so the
        actually-checked paths come from the page, not from driver memory.
        """
        if not self.frames or not self.frames.pivot or self.frames.pivot.is_detached():
            await self.open_pivot()
        layout = await self.read_layout()
        selected: dict[str, tuple[tuple[str, ...], ...]] = {}
        for tag in dimensions:
            selected[tag.label] = await self.read_selected_member_paths(tag)
        return AppliedPivotState(
            layout=layout,
            selected_members=selected,
            kpis=kpis,
            period=period,
            table_refreshed=table_refreshed,
        )

    async def read_selected_member_paths(self, tag: DimensionTag) -> tuple[tuple[str, ...], ...]:
        """Open the dimension's member dialog and read which paths the page
        itself reports as checked/selected, then close the dialog."""
        await self.open_member_selector(tag)
        paths = selected_member_paths(await self._html(self._require_selector()))
        await self.cancel_member_selection()
        return paths

    async def select_report_kpi(self, requested: str) -> str:
        """Switch the report's KPI dropdown to the requested KPI and return the
        actual option label that ended up applied."""
        report = await self._wait_for_frame("DB_0001pp.aspx")
        select = report.locator("select").first
        await select.wait_for(state="visible", timeout=self.timeout_ms)
        options = await select.evaluate(
            """
            element => [...element.options].map(option => ({
              label: option.textContent.trim().replace(/\\s+/g, ' '),
              value: option.value,
              selected: option.selected
            }))
            """
        )
        match = _match_kpi_option(requested, options)
        if match is None:
            available = ", ".join(str(option["label"]) for option in options)
            raise WorldpanelError(f"KPI '{requested}' is not available. Options: {available}")
        if match.get("selected"):
            return str(match["label"])
        before_text = await report.locator("body").inner_text(timeout=self.timeout_ms)
        await select.select_option(value=str(match["value"]))
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        refreshed = False
        while asyncio.get_running_loop().time() < deadline:
            report = await self._wait_for_frame("DB_0001pp.aspx")
            current_text = await report.locator("body").inner_text(timeout=self.timeout_ms)
            if current_text != before_text:
                refreshed = True
                break
            await asyncio.sleep(0.2)
        if not refreshed:
            raise WorldpanelError("KPI selection did not refresh the report")
        self.frames = PivotFrames(report=report)
        self.actions.append(f"select_kpi:{match['label']}")
        return await self.read_report_kpi()

    async def read_report_kpi(self) -> str:
        """Read the KPI label the report page currently shows as selected."""
        report = await self._wait_for_frame("DB_0001pp.aspx")
        select = report.locator("select").first
        if not await select.count():
            return ""
        return str(
            await select.evaluate(
                "element => (element.selectedOptions[0]?.textContent || '').trim().replace(/\\s+/g, ' ')"
            )
        )

    async def read_report_text(self) -> str:
        """Read the rendered report table text from the report frame."""
        report = await self._wait_for_frame("DB_0001pp.aspx")
        body = report.locator("body")
        await body.wait_for(state="visible", timeout=self.timeout_ms)
        return await body.inner_text(timeout=self.timeout_ms)

    async def read_report_grid(self) -> dict[str, Any]:
        """Read the rendered data grid straight from the DOM table.

        Returns {"columns": [...], "rows": [[label, [cell|null, ...]], ...]}.
        This is reliable across pivot orientations and KPI/calculation changes
        because it never has to disentangle the table from the surrounding
        dropdown option text. Empty cells (rendered as '.') become null.
        """
        report = await self._wait_for_frame("DB_0001pp.aspx")
        grid = report.locator("table.infoset, table[id$='_DB_0001_01']").first
        await grid.wait_for(state="attached", timeout=self.timeout_ms)
        data = await grid.evaluate(
            """
            table => {
              const rows = [...table.rows].map(row =>
                [...row.cells].map(cell => {
                  const text = (cell.innerText || '').trim().replace(/\\s+/g, ' ');
                  return (text === '' || text === '.') ? null : text;
                })
              );
              return rows;
            }
            """
        )
        if not data:
            raise WorldpanelError("Report grid table was empty")
        header = data[0]
        columns = [str(cell) for cell in header[1:] if cell]
        rows: list[list[Any]] = []
        for raw in data[1:]:
            if not raw or not raw[0]:
                continue
            rows.append([str(raw[0]), list(raw[1:])])
        return {"columns": columns, "rows": rows}

    async def _report_id(self, report: Frame) -> str:
        marker = report.locator("#ReportIDTemp")
        if await marker.count():
            return await marker.get_attribute("value") or ""
        return ""

    async def _ensure_selector(self, tag: DimensionTag) -> None:
        if not self.frames or not self.frames.selector or self.active_dimension != tag.label:
            await self.open_member_selector(tag)

    async def _node_by_path(self, path: tuple[str, ...]) -> Locator:
        selector = self._require_selector()
        nodes = selector.locator(".RadTreeView .rtLI")
        paths = await nodes.evaluate_all(
            """
            elements => elements.map(element => {
              const labels = [];
              let node = element;
              while (node && node.matches('.rtLI')) {
                const label = node.querySelector(':scope > div > .rtIn');
                labels.unshift((label?.title || label?.textContent || '').trim());
                node = node.parentElement?.closest('.rtLI');
              }
              return labels;
            })
            """
        )
        expected = tuple(normalize(part) for part in path)
        for index, candidate_path in enumerate(paths):
            if tuple(normalize(str(part)) for part in candidate_path) == expected:
                return nodes.nth(index)
        raise WorldpanelError(f"Could not find member path: {' > '.join(path)}")

    async def _wait_for_class(self, root: Locator, class_name: str) -> None:
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            if await root.locator(f".{class_name}").count():
                return
            await asyncio.sleep(0.1)
        raise WorldpanelError(f"Timed out waiting for {class_name}")

    async def _wait_for_frame(self, fragment: str) -> Frame:
        deadline = asyncio.get_running_loop().time() + self.timeout_ms / 1000
        while asyncio.get_running_loop().time() < deadline:
            for frame in reversed(self.page.frames):
                if fragment.casefold() in frame.url.casefold():
                    return frame
            await asyncio.sleep(0.1)
        raise WorldpanelError(f"Timed out waiting for frame URL containing {fragment}")

    async def _legacy_click(self, locator: Locator) -> None:
        await locator.evaluate("element => setTimeout(() => element.click(), 0)")

    async def _html(self, frame: Frame) -> str:
        return await frame.locator("html").evaluate("element => element.outerHTML")

    def _require_frames(self) -> PivotFrames:
        if not self.frames:
            raise WorldpanelError("PivotDriver is not attached")
        return self.frames

    def _require_pivot(self) -> Frame:
        frames = self._require_frames()
        if not frames.pivot:
            raise WorldpanelError("Pivot Screen is not open")
        return frames.pivot

    def _require_selector(self) -> Frame:
        frames = self._require_frames()
        if not frames.selector:
            raise WorldpanelError("Member selector is not open")
        return frames.selector


_KPI_KEYWORDS = (
    ("spend", ("spend", "value", "销额", "销售额")),
    ("volume", ("volume", "销量", "销售量")),
    ("penetration", ("penetration", "渗透")),
)


def _match_kpi_option(requested: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    direct = _match_label_option(requested, options)
    if direct is not None:
        return direct
    requested_normalized = normalize(requested)
    for keyword, aliases in _KPI_KEYWORDS:
        if any(normalize(alias) in requested_normalized for alias in aliases):
            for option in options:
                if keyword in normalize(str(option["label"])):
                    return option
    return None


def _match_label_option(requested: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    requested_normalized = normalize(requested)
    for option in options:
        if normalize(str(option["label"])) == requested_normalized:
            return option
    for option in options:
        label_normalized = normalize(str(option["label"]))
        if requested_normalized and (requested_normalized in label_normalized or label_normalized in requested_normalized):
            return option
    return None
