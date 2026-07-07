from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Axis = Literal["row", "column", "filter", "available"]
OutputShape = Literal["single_value", "table", "comparison", "trend", "ranking"]


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
class FilterSelection:
    """A page/filter dropdown choice, e.g. Outlet=Hypermarket or Duration=4 w/e."""

    role: str
    value: str


@dataclass(frozen=True)
class RankingSpec:
    """Superlative intent ("哪个品牌渗透率最高" / "top 3 brands"): order the
    rendered members by value at the resolved period and answer the extremes.

    `dimension` is the natural-language dimension the user ranked over (品牌 /
    渠道 / 品类, ...); the service resolves it to a live dimension or falls
    back to ranking the children of the selected member."""

    dimension: str = ""
    direction: Literal["max", "min"] = "max"
    top_n: int = 1


@dataclass(frozen=True)
class QueryPlan:
    report_set: str
    report: str
    axis_placements: tuple[AxisPlacement, ...] = ()
    member_selections: tuple[MemberSelection, ...] = ()
    kpis: tuple[str, ...] = ("Spend (RMB 000)",)
    expected_period: str | None = None
    output_shape: OutputShape = "single_value"
    # Performance/calculation dropdown, e.g. "Yr on Yr % Change" for growth rate.
    calculation: str | None = None
    # Other page/filter dropdown choices (channel, duration, geography, ...).
    filters: tuple[FilterSelection, ...] = ()
    # Present when the question asks for a superlative/ranking answer.
    ranking: RankingSpec | None = None


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
class ReportDropdown:
    """A page/filter dropdown rendered on the Data Explorer report itself.

    These controls have no stable id/name in the DOM, so they are addressed by
    their position (document order) and classified into a role from their
    option signature. `dimension` is the matching Pivot page-dimension label
    when one is known.
    """

    index: int
    role: str
    dimension: str
    selected: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class DimensionMembers:
    """Fully-enumerated members of one Pivot dimension (every `+` expanded)."""

    dimension: str
    axis: Axis
    members: tuple[MemberNode, ...]
    truncated: bool = False


@dataclass(frozen=True)
class PivotDiscovery:
    """Everything the Pivot Screen exposes: dimension tags, fully expanded
    member trees, and the report's page/filter dropdowns."""

    dimensions: tuple[DimensionTag, ...]
    members: tuple[DimensionMembers, ...]
    dropdowns: tuple[ReportDropdown, ...]


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
        normalize(plan.calculation or ""),
        tuple((normalize(item.role), normalize(item.value)) for item in plan.filters),
        (plan.ranking.direction, plan.ranking.top_n) if plan.ranking else None,
    )


def normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())
