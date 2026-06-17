from __future__ import annotations

from dataclasses import dataclass, field

from app.worldpanel.client import WorldpanelError
from app.worldpanel.pivot_driver import PivotDriver
from app.worldpanel.pivot_models import (
    AppliedPivotState,
    ExecutionReceipt,
    PivotLayout,
    QueryPlan,
    normalize,
)
from app.worldpanel.pivot_result import (
    PivotResultError,
    PivotResultTable,
    resolve_period,
    table_from_grid,
)
from app.worldpanel.schema import SchemaService


@dataclass(frozen=True)
class ExecutionResult:
    receipt: ExecutionReceipt
    tables: dict[str, PivotResultTable] = field(default_factory=dict)


class QueryExecutor:
    def __init__(self, driver: PivotDriver, schema: SchemaService):
        self.driver = driver
        self.schema = schema

    async def execute(self, plan: QueryPlan) -> ExecutionResult:
        await self.driver.open_pivot()
        tags = await self.driver.list_dimension_tags()
        by_name = {normalize(tag.label): tag for tag in tags}

        for placement in plan.axis_placements:
            tag = by_name.get(normalize(placement.dimension))
            if not tag:
                raise WorldpanelError(f"Dimension is unavailable: {placement.dimension}")
            await self.driver.set_axis(tag, placement.axis, placement.position)

        expected_layout = _layout_from_plan(plan)
        if plan.axis_placements:
            await self.driver.verify_layout(expected_layout)

        selections_by_dimension: dict[str, list] = {}
        for selection in plan.member_selections:
            selections_by_dimension.setdefault(selection.dimension, []).append(selection)
        for dimension, selections in selections_by_dimension.items():
            tag = by_name.get(normalize(dimension))
            if not tag:
                raise WorldpanelError(f"Dimension is unavailable: {dimension}")
            await self.driver.clear_member_selection(tag)
            for selection in selections:
                candidates = await self.schema.search(plan.report, tag, selection.member_path[-1])
                matches = [node for node in candidates if node.path == selection.member_path]
                if len(matches) != 1:
                    raise WorldpanelError(f"Could not uniquely resolve member path: {selection.member_path}")
                await self.driver.check_member(tag, matches[0], selection.checked)
            await self.driver.apply_member_selection()

        await self.driver.apply()

        # Apply page/filter dropdowns (calculation, channel, duration, ...) so
        # the rendered table reflects, e.g., a Yr-on-Yr % growth view.
        applied_calculation = None
        if plan.calculation:
            applied_calculation = await self._apply_dropdown("calculation", plan.calculation)
        for selection in plan.filters:
            await self._apply_dropdown(selection.role, selection.value)

        # Apply each requested KPI on the real report and read the table the
        # page actually rendered, straight from the data grid. The receipt
        # carries what was applied, not what was planned.
        tables: dict[str, PivotResultTable] = {}
        applied_kpis: list[str] = []
        kpi_requests = plan.kpis or (await self.driver.read_report_kpi(),)
        for requested in kpi_requests:
            actual = await self.driver.select_report_kpi(requested) if requested else ""
            label = actual or await self.driver.read_report_kpi() or "KPI"
            if applied_calculation:
                label = f"{label} - {applied_calculation}"
            try:
                table = await self._read_table(label)
            except PivotResultError as exc:
                raise WorldpanelError(f"Applied pivot table could not be parsed: {exc}") from exc
            tables[label] = table
            applied_kpis.append(label)
        table_refreshed = bool(tables)

        period = None
        if plan.expected_period:
            try:
                period = resolve_period(plan.expected_period, next(iter(tables.values())))
            except PivotResultError as exc:
                raise WorldpanelError(str(exc)) from exc

        selected_tags = tuple(
            by_name[normalize(dimension)] for dimension in selections_by_dimension
        )
        state = await self.driver.read_applied_state(
            dimensions=selected_tags,
            kpis=tuple(applied_kpis),
            period=period,
            table_refreshed=table_refreshed,
        )
        _verify_applied_state(plan, state)
        _verify_rendered_members(plan, state, tables)
        receipt = ExecutionReceipt.from_state(
            state,
            verified=True,
            actions=tuple(self.driver.actions),
        )
        return ExecutionResult(receipt=receipt, tables=tables)

    async def _apply_dropdown(self, role: str, value: str) -> str:
        dropdowns = await self.driver.read_dropdowns()
        target = next(
            (d for d in dropdowns if normalize(d.role) == normalize(role)),
            None,
        )
        if target is None:
            target = next(
                (d for d in dropdowns if normalize(d.dimension) == normalize(role)),
                None,
            )
        if target is None:
            raise WorldpanelError(f"No report dropdown for '{role}'")
        return await self.driver.select_dropdown(target.index, value)

    async def _read_table(self, metric: str) -> PivotResultTable:
        grid = await self.driver.read_report_grid()
        rows = [(label, values) for label, values in grid["rows"]]
        return table_from_grid(grid["columns"], rows, metric=metric)


def _layout_from_plan(plan: QueryPlan) -> PivotLayout:
    axes: dict[str, list[tuple[int, str]]] = {"row": [], "column": [], "filter": [], "available": []}
    for item in plan.axis_placements:
        axes[item.axis].append((item.position, item.dimension))
    return PivotLayout(
        rows=tuple(value for _, value in sorted(axes["row"])),
        columns=tuple(value for _, value in sorted(axes["column"])),
        filters=tuple(value for _, value in sorted(axes["filter"])),
        available=tuple(value for _, value in sorted(axes["available"])),
    )


def _verify_applied_state(plan: QueryPlan, state: AppliedPivotState) -> None:
    if not state.table_refreshed:
        raise WorldpanelError("Applied table did not refresh")
    for placement in plan.axis_placements:
        values = state.layout.axis(placement.axis)
        if placement.position >= len(values) or normalize(values[placement.position]) != normalize(placement.dimension):
            raise WorldpanelError(
                f"Applied layout mismatch for {placement.dimension}: expected {placement.axis}[{placement.position}]"
            )
    actual_by_dimension = {
        normalize(dimension): {
            tuple(normalize(part) for part in path) for path in paths
        }
        for dimension, paths in state.selected_members.items()
    }
    for selection in plan.member_selections:
        actual = actual_by_dimension.get(normalize(selection.dimension), set())
        requested = tuple(normalize(part) for part in selection.member_path)
        present = requested in actual
        if present != selection.checked:
            raise WorldpanelError(f"Applied member mismatch: {selection.dimension} / {selection.member_path}")


def _verify_rendered_members(
    plan: QueryPlan,
    state: AppliedPivotState,
    tables: dict[str, PivotResultTable],
) -> None:
    rendered_dimensions = {
        normalize(dimension) for dimension in (*state.layout.rows, *state.layout.columns)
    }
    labels = {
        normalize(label)
        for table in tables.values()
        for label in (*table.row_labels, *table.column_labels)
    }
    for selection in plan.member_selections:
        if not selection.checked or normalize(selection.dimension) not in rendered_dimensions:
            continue
        leaf = normalize(selection.member_path[-1])
        if leaf not in labels:
            raise WorldpanelError(
                f"Rendered table does not contain requested member: {selection.dimension} / {selection.member_path}"
            )
