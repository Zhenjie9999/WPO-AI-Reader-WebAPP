from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Axis = Literal["row", "column", "filter", "available"]
OutputShape = Literal["single_value", "table", "comparison", "trend"]


@dataclass(frozen=True)
class AxisPlacement:
    dimension: str
    axis: Axis
    position: int


@dataclass(frozen=True)
class MemberSelection:
    dimension: str
    member_path: tuple[str, ...]
    checked: bool = True


@dataclass(frozen=True)
class QueryPlan:
    report_set: str
    report: str
    axis_placements: tuple[AxisPlacement, ...] = ()
    member_selections: tuple[MemberSelection, ...] = ()
    kpis: tuple[str, ...] = ("Spend (RMB 000)",)
    expected_period: str | None = None
    output_shape: OutputShape = "single_value"


@dataclass(frozen=True)
class DimensionTag:
    label: str
    dimension_id: str
    axis: Axis
    position: int
    member_count: int | None = None


@dataclass(frozen=True)
class MemberNode:
    label: str
    value: str
    path: tuple[str, ...]
    level: int
    has_children: bool
    expanded: bool
    checked: bool
    selected: bool


@dataclass(frozen=True)
class PivotLayout:
    rows: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    available: tuple[str, ...] = ()

    def axis(self, name: Axis) -> tuple[str, ...]:
        if name == "row":
            return self.rows
        if name == "column":
            return self.columns
        if name == "filter":
            return self.filters
        return self.available


@dataclass(frozen=True)
class AppliedPivotState:
    layout: PivotLayout
    selected_members: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)
    kpis: tuple[str, ...] = ()
    period: str | None = None
    table_refreshed: bool = False


@dataclass(frozen=True)
class ExecutionReceipt:
    row_dimensions: tuple[str, ...]
    column_dimensions: tuple[str, ...]
    selected_members: dict[str, tuple[tuple[str, ...], ...]]
    kpis: tuple[str, ...]
    period: str | None
    table_refreshed: bool
    verified: bool
    cache_hit: bool = False
    actions: tuple[str, ...] = ()

    @classmethod
    def from_state(
        cls,
        state: AppliedPivotState,
        *,
        verified: bool,
        cache_hit: bool = False,
        actions: tuple[str, ...] = (),
    ) -> "ExecutionReceipt":
        return cls(
            row_dimensions=state.layout.rows,
            column_dimensions=state.layout.columns,
            selected_members=state.selected_members,
            kpis=state.kpis,
            period=state.period,
            table_refreshed=state.table_refreshed,
            verified=verified,
            cache_hit=cache_hit,
            actions=actions,
        )


def plan_cache_key(plan: QueryPlan) -> tuple[object, ...]:
    return (
        normalize(plan.report_set),
        normalize(plan.report),
        tuple((normalize(item.dimension), item.axis, item.position) for item in plan.axis_placements),
        tuple(
            (normalize(item.dimension), tuple(normalize(part) for part in item.member_path), item.checked)
            for item in plan.member_selections
        ),
        tuple(normalize(kpi) for kpi in plan.kpis),
        normalize(plan.expected_period or ""),
        plan.output_shape,
    )


def normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
