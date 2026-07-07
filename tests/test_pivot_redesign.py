import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from app.worldpanel.pivot_models import (
    AppliedPivotState,
    AxisPlacement,
    ExecutionReceipt,
    MemberNode,
    MemberSelection,
    PivotLayout,
    QueryPlan,
    plan_cache_key,
)
from app.worldpanel.pivot_cache import VerifiedResult, VerifiedResultCache
from app.worldpanel.pivot_result import parse_pivot_text, table_from_grid
from app.worldpanel.executor import QueryExecutor
from app.worldpanel.pivot_parser import parse_dimension_tags, parse_member_tree, parse_pivot_layout
from app.worldpanel.planner import PlanClarification, compile_query_plan
from app.worldpanel.planner import StructuredPlanner
from app.worldpanel.schema import SchemaService, TTLCache
from app.worldpanel.session import DataExplorerSession, DataExplorerSessionManager
from app.worldpanel.pivot_service import (
    _apply_clarification,
    _discover_members_from_question,
    _question_may_require_members,
)


PIVOT_HTML = """
<div id="cphMain_AvailableHierarchies" class="RadListBox"><ul class="rlbList">
  <li class="rlbItem" title="Outlet"><img dimid="[Dim2]"><span class="rlbText">Outlet</span></li>
</ul></div>
<div id="cphMain_RowHierarchies" class="RadListBox"><ul class="rlbList">
  <li class="rlbItem" title="Product"><img dimid="[Dim1]"><span class="rlbText">Product[5]</span></li>
</ul></div>
<div id="cphMain_ColumnHierarchies" class="RadListBox"><ul class="rlbList">
  <li class="rlbItem" title="Period"><img dimid="[Period]"><span class="rlbText">Period[27]</span></li>
  <li class="rlbItem" title="Measures"><img dimid="[Measures]"><span class="rlbText">Measures[20]</span></li>
</ul></div>
"""

TREE_HTML = """
<div class="RadTreeView"><ul class="rtUL">
  <li class="rtLI"><div class="rtTop rtSelected"><span class="rtMinus"></span><span title="Fruit" class="rtIn">Fruit</span></div>
    <ul class="rtUL">
      <li class="rtLI"><div class="rtTop"><span title="Apple" class="rtIn LeafNode">Apple</span></div></li>
      <li class="rtLI"><div class="rtMid"><span class="rtMinus"></span><span title="Kiwifruit" class="rtIn">Kiwifruit</span></div>
        <ul class="rtUL"><li class="rtLI"><div><span title="Gold" class="rtIn LeafNode">Gold</span></div></li></ul>
      </li>
    </ul>
  </li>
  <li class="rtLI"><div class="rtBot"><span class="rtPlus"></span><span title="Brand" class="rtIn">Brand</span></div>
    <ul class="rtUL"><li class="rtLI"><div><span title="Gold" class="rtIn LeafNode">Gold</span></div></li></ul>
  </li>
</ul></div>
"""


def test_parse_pivot_layout_and_tags_from_real_telerik_shape():
    layout = parse_pivot_layout(PIVOT_HTML)
    tags = parse_dimension_tags(PIVOT_HTML)

    assert layout.rows == ("Product",)
    assert layout.columns == ("Period", "Measures")
    assert layout.available == ("Outlet",)
    assert [(tag.label, tag.axis, tag.position, tag.member_count) for tag in tags] == [
        ("Product", "row", 0, 5),
        ("Period", "column", 0, 27),
        ("Measures", "column", 1, 20),
        ("Outlet", "available", 0, None),
    ]


def test_member_tree_preserves_duplicate_labels_under_distinct_parent_paths():
    nodes = parse_member_tree(TREE_HTML)

    gold_paths = [node.path for node in nodes if node.label == "Gold"]
    assert gold_paths == [("Fruit", "Kiwifruit", "Gold"), ("Brand", "Gold")]
    assert next(node for node in nodes if node.label == "Fruit").expanded is True
    assert next(node for node in nodes if node.label == "Brand").has_children is True


def test_compile_plan_requires_live_unique_member_path():
    discovered = {"Product": parse_member_tree(TREE_HTML)}
    ambiguous = compile_query_plan(
        {"member_selections": [{"dimension": "Product", "member_path": ["Gold"]}]},
        report_set="Set",
        report="Report",
        discovered=discovered,
    )
    exact = compile_query_plan(
        {"member_selections": [{"dimension": "Product", "member_path": ["Fruit", "Kiwifruit", "Gold"]}]},
        report_set="Set",
        report="Report",
        discovered=discovered,
    )

    assert isinstance(ambiguous, PlanClarification)
    assert ambiguous.candidates == (("Fruit", "Kiwifruit", "Gold"), ("Brand", "Gold"))
    assert isinstance(exact, QueryPlan)
    assert exact.member_selections[0].member_path == ("Fruit", "Kiwifruit", "Gold")


def test_plan_cache_key_changes_for_axis_order_member_path_kpi_and_period():
    base = QueryPlan(
        report_set="Set",
        report="Report",
        axis_placements=(AxisPlacement("Product", "row", 0), AxisPlacement("Period", "column", 0)),
        member_selections=(MemberSelection("Product", ("Fruit", "Gold")),),
        kpis=("Spend",),
        expected_period="2025 P1",
    )
    variants = [
        QueryPlan(**{**base.__dict__, "axis_placements": tuple(reversed(base.axis_placements))}),
        QueryPlan(**{**base.__dict__, "member_selections": (MemberSelection("Product", ("Brand", "Gold")),)}),
        QueryPlan(**{**base.__dict__, "kpis": ("Volume",)}),
        QueryPlan(**{**base.__dict__, "expected_period": "2025 P2"}),
    ]

    assert all(plan_cache_key(base) != plan_cache_key(variant) for variant in variants)


def test_receipt_is_visible_and_verified_only_from_applied_state():
    state = AppliedPivotState(
        layout=PivotLayout(rows=("Product",), columns=("Period", "KPI")),
        selected_members={"Product": (("Fruit", "Gold"),)},
        kpis=("Spend",),
        period="2025 P1",
        table_refreshed=True,
    )
    receipt = ExecutionReceipt.from_state(state, verified=True, actions=("apply_pivot",))

    assert receipt.verified is True
    assert receipt.table_refreshed is True
    assert receipt.selected_members["Product"] == (("Fruit", "Gold"),)


def test_ttl_cache_separates_keys_and_expires():
    cache: TTLCache[str] = TTLCache(ttl_seconds=60)
    cache.set(("report", "product", "gold"), "gold-result")
    cache.set(("report", "product", "green"), "green-result")

    assert cache.get(("report", "product", "gold")) == "gold-result"
    assert cache.get(("report", "product", "green")) == "green-result"
    cache._entries[("report", "product", "gold")].expires_at -= timedelta(seconds=61)
    assert cache.get(("report", "product", "gold")) is None


@pytest.mark.asyncio
async def test_session_serializes_queries_and_manager_discards_expired():
    session = DataExplorerSession("s1")
    order: list[str] = []

    async def first():
        order.append("first-start")
        await asyncio.sleep(0.02)
        order.append("first-end")

    async def second():
        order.append("second")

    await asyncio.gather(session.serialized(first), session.serialized(second))
    assert order == ["first-start", "first-end", "second"]

    manager = DataExplorerSessionManager(ttl_seconds=0)
    manager._sessions["s1"] = session
    assert await manager.discard_expired() == 1
    assert manager.size == 0


REPORT_GRID = {
    "columns": ["Gold", "Apple"],
    "rows": [
        ["15-Jan-25", ["1,234", "2,000"]],
        ["15-Feb-25", ["1,300", "2,100"]],
    ],
}


class _HonestDriver:
    """Mock driver that simulates the real page: KPI must be applied and the
    applied state is read back from what the page reports, not from memory."""

    def __init__(self, gold, product, report_grid=None, page_selected=None):
        self.gold = gold
        self.product = product
        self.report_grid = report_grid if report_grid is not None else REPORT_GRID
        self.page_selected = page_selected if page_selected is not None else {"Product": (gold.path,)}
        self.actions = []
        self.applied_kpi = None

    async def open_pivot(self):
        self.actions.append("open")

    async def list_dimension_tags(self):
        return [self.product]

    async def set_axis(self, tag, axis, position):
        self.actions.append(f"axis:{tag.label}:{axis}:{position}")

    async def verify_layout(self, expected):
        assert expected.rows == ("Product",)

    async def read_layout(self):
        return PivotLayout(rows=("Product",))

    async def check_member(self, tag, member, checked):
        assert member.path == self.gold.path
        self.actions.append("check")

    async def clear_member_selection(self, tag):
        self.actions.append(f"clear:{tag.label}")

    async def apply_member_selection(self):
        self.actions.append("member-apply")

    async def apply(self):
        self.actions.append("pivot-apply")

    async def select_report_kpi(self, requested):
        self.applied_kpi = "Spend (RMB 000)"
        self.actions.append(f"select_kpi:{requested}")
        return self.applied_kpi

    async def read_report_kpi(self):
        return self.applied_kpi or ""

    async def read_report_grid(self):
        return self.report_grid

    async def read_dropdowns(self):
        return []

    async def read_applied_state(self, *, dimensions=(), kpis=(), period=None, table_refreshed=False):
        return AppliedPivotState(
            layout=PivotLayout(rows=("Product",)),
            selected_members={tag.label: self.page_selected.get(tag.label, ()) for tag in dimensions},
            kpis=kpis,
            period=period,
            table_refreshed=table_refreshed,
        )


@pytest.mark.asyncio
async def test_executor_applies_kpi_resolves_period_and_returns_receipt_from_page_state():
    product = next(tag for tag in parse_dimension_tags(PIVOT_HTML) if tag.label == "Product")
    gold = next(node for node in parse_member_tree(TREE_HTML) if node.path == ("Fruit", "Kiwifruit", "Gold"))
    driver = _HonestDriver(gold, product)

    class Schema:
        async def search(self, report, tag, text):
            return (gold,)

        async def all_members(self, report, tag):
            return (gold,)

    plan = QueryPlan(
        report_set="Set",
        report="Report",
        axis_placements=(AxisPlacement("Product", "row", 0),),
        member_selections=(MemberSelection("Product", gold.path),),
        kpis=("销额",),
        expected_period="2025 P1",
    )
    result = await QueryExecutor(driver, Schema()).execute(plan)  # type: ignore[arg-type]

    assert result.receipt.verified is True
    assert result.receipt.table_refreshed is True
    # KPI in the receipt is the label the page applied, not the request alias.
    assert result.receipt.kpis == ("Spend (RMB 000)",)
    assert "select_kpi:销额" in result.receipt.actions
    # Period is resolved to a real date label present in the parsed table.
    assert result.receipt.period == "15-Jan-25"
    assert result.receipt.selected_members["Product"] == (gold.path,)
    assert "clear:Product" in result.receipt.actions
    # The parsed table is returned for answering/checking.
    assert result.tables["Spend (RMB 000)"].cells["15-Jan-25"]["Gold"] == 1234


@pytest.mark.asyncio
async def test_executor_rejects_member_mismatch_reported_by_the_page():
    product = next(tag for tag in parse_dimension_tags(PIVOT_HTML) if tag.label == "Product")
    gold = next(node for node in parse_member_tree(TREE_HTML) if node.path == ("Fruit", "Kiwifruit", "Gold"))
    driver = _HonestDriver(gold, product, page_selected={"Product": ()})

    class Schema:
        async def search(self, report, tag, text):
            return (gold,)

        async def all_members(self, report, tag):
            return (gold,)

    plan = QueryPlan(
        report_set="Set",
        report="Report",
        member_selections=(MemberSelection("Product", gold.path),),
    )

    with pytest.raises(Exception, match="Applied member mismatch"):
        await QueryExecutor(driver, Schema()).execute(plan)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_executor_rejects_unverifiable_period():
    product = next(tag for tag in parse_dimension_tags(PIVOT_HTML) if tag.label == "Product")
    gold = next(node for node in parse_member_tree(TREE_HTML) if node.path == ("Fruit", "Kiwifruit", "Gold"))
    driver = _HonestDriver(gold, product)

    class Schema:
        async def search(self, report, tag, text):
            return (gold,)

        async def all_members(self, report, tag):
            return (gold,)

    plan = QueryPlan(
        report_set="Set",
        report="Report",
        member_selections=(MemberSelection("Product", gold.path),),
        expected_period="2030 P1",
    )

    with pytest.raises(Exception, match="No date in the table matches"):
        await QueryExecutor(driver, Schema()).execute(plan)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_schema_service_discovers_on_demand_and_caches_search_separately():
    product = next(tag for tag in parse_dimension_tags(PIVOT_HTML) if tag.label == "Product")

    class Driver:
        member_calls = 0
        search_calls = 0

        async def list_dimension_tags(self):
            return [product]

        async def list_members(self, tag, path):
            self.member_calls += 1
            return list(parse_member_tree(TREE_HTML))

        async def search_member(self, tag, text):
            self.search_calls += 1
            return [node for node in parse_member_tree(TREE_HTML) if text.casefold() in node.label.casefold()]

    driver = Driver()
    schema = SchemaService(driver)  # type: ignore[arg-type]
    await schema.members("Report", product)
    await schema.members("Report", product)
    await schema.search("Report", product, "Gold")
    await schema.search("Report", product, "Gold")

    assert driver.member_calls == 1
    assert driver.search_calls == 1
    assert schema.schema_cache.size == 1
    assert schema.search_cache.size == 1


@pytest.mark.asyncio
async def test_structured_planner_is_provider_agnostic_and_parses_json_response():
    class Assistant:
        async def chat(self, prompt):
            assert "Return JSON only" in prompt
            return 'Result: {"axis_placements":[],"member_selections":[],"kpis":["Spend"],"output_shape":"table"}'

    payload = await StructuredPlanner(Assistant()).tentative_plan("show spend")  # type: ignore[arg-type]

    assert payload["kpis"] == ["Spend"]
    assert payload["output_shape"] == "table"


@pytest.mark.asyncio
async def test_structured_planner_falls_back_when_configured_provider_is_unavailable():
    class Assistant:
        async def chat(self, prompt):
            raise RuntimeError("provider unavailable")

    payload = await StructuredPlanner(Assistant()).tentative_plan("Product on Row, show Spend")  # type: ignore[arg-type]

    assert payload["axis_placements"][0] == {"dimension": "Product", "axis": "row", "position": 0}
    assert payload["kpis"] == ["Spend (RMB 000)"]
    assert payload["planner_fallback"] is True


def test_user_clarification_replaces_ambiguous_selection_and_skips_full_rescan():
    tentative = {
        "member_selections": [{"dimension": "Product", "member_path": ["Gold"]}],
        "planner_fallback": True,
    }

    _apply_clarification(
        tentative,
        {"dimension": "Product", "member_path": ["Fruit", "Kiwifruit", "Gold"]},
    )

    assert tentative["member_selections"] == [
        {"dimension": "Product", "member_path": ["Fruit", "Kiwifruit", "Gold"], "checked": True}
    ]
    assert "planner_fallback" not in tentative


def test_calculation_clarification_sets_growth_basis_not_member_selection():
    tentative = {
        "member_selections": [],
        "calculation": None,
        "planner_fallback": True,
    }

    _apply_clarification(
        tentative,
        {"dimension": "calculation", "member_path": ["Period on Period % Change~"]},
    )

    assert tentative["calculation"] == "Period on Period % Change~"
    assert tentative["member_selections"] == []
    assert "planner_fallback" not in tentative


@pytest.mark.asyncio
async def test_pivot_plan_requests_growth_basis_before_member_resolution(monkeypatch):
    from app.config import get_settings
    from app.worldpanel.client import Credentials
    from app.worldpanel.pivot_service import PivotQueryService

    opened = {"pivot": False}

    class Driver:
        async def open_pivot(self):
            opened["pivot"] = True

    async def fake_open_persistent_data_explorer(*args, **kwargs):
        return Driver()

    monkeypatch.setattr(
        "app.worldpanel.pivot_service.open_persistent_data_explorer",
        fake_open_persistent_data_explorer,
    )

    session = DataExplorerSession("clarify-growth")
    session.current_report = {
        "report_set": "Set",
        "report_parameter": "Parameter",
        "report_name": "Data Explorer",
    }

    result = await PivotQueryService(get_settings()).plan(
        session,
        Credentials("user@example.com", "password"),
        "Show 2026 May spend growth rate",
    )

    assert opened["pivot"] is True
    assert isinstance(result, PlanClarification)
    assert result.dimension == "calculation"


def test_fallback_planner_never_silently_defaults_a_member_request():
    assert _question_may_require_members("Product on Row, show Gold kiwifruit Spend") is True
    assert _question_may_require_members("Product on Row, Period on Column, show Spend") is False


def test_live_driver_submits_telerik_member_selection_instead_of_only_clicking_text():
    source = Path("app/worldpanel/pivot_driver.py").read_text(encoding="utf-8")

    assert "unselectAllNodes()" in source
    assert "saveSelectedMemOrdinals()" in source


@pytest.mark.asyncio
async def test_fallback_planner_maps_question_against_live_members_without_fixed_dictionary():
    product = next(tag for tag in parse_dimension_tags(PIVOT_HTML) if tag.label == "Product")
    nodes = parse_member_tree(TREE_HTML)

    class Schema:
        async def search(self, report, tag, text):
            return nodes

        async def all_members(self, report, tag):
            return nodes

    class Driver:
        cancel_count = 0

        async def cancel_member_selection(self):
            self.cancel_count += 1

    driver = Driver()
    result = await _discover_members_from_question(
        "show Gold spend",
        "Report",
        (product,),
        Schema(),  # type: ignore[arg-type]
        driver,
    )

    # Ambiguous 'Gold' with no LLM available: the deterministic resolver does
    # not guess, so a clarification is returned (the LLM path would disambiguate).
    assert isinstance(result, PlanClarification)
    assert driver.cancel_count == 1

    unique = await _discover_members_from_question(
        "show Apple spend",
        "Report",
        (product,),
        Schema(),  # type: ignore[arg-type]
        driver,
    )
    assert unique == [{"dimension": "Product", "member_path": ["Fruit", "Apple"], "checked": True}]


@pytest.mark.asyncio
async def test_executor_rejects_unavailable_dimension_before_apply():
    class Driver:
        actions = []

        async def open_pivot(self):
            self.actions.append("open")

        async def list_dimension_tags(self):
            return []

    driver = Driver()
    plan = QueryPlan(
        report_set="Set",
        report="Report",
        axis_placements=(AxisPlacement("Missing", "row", 0),),
    )

    with pytest.raises(Exception, match="Dimension is unavailable"):
        await QueryExecutor(driver, object()).execute(plan)  # type: ignore[arg-type]
    assert driver.actions == ["open"]


def test_verified_result_cache_is_scoped_per_account_and_report_parameter():
    plan = QueryPlan(report_set="Set", report="Report")
    verified = ExecutionReceipt(
        row_dimensions=("Product",),
        column_dimensions=("Period",),
        selected_members={},
        kpis=("Spend",),
        period="2025 P1",
        table_refreshed=True,
        verified=True,
    )
    cache = VerifiedResultCache()
    scope = "user@example.com|param-a"
    table = table_from_grid(REPORT_GRID["columns"], REPORT_GRID["rows"], metric="Spend")
    cache.set(plan, scope, VerifiedResult(receipt=verified, answer="42", tables={"Spend": table}))

    restored = cache.get(plan, scope)
    assert restored is not None
    assert restored.answer == "42"
    assert restored.tables["Spend"].cells["15-Jan-25"]["Gold"] == 1234
    assert restored.receipt.cache_hit is True

    # The same plan never leaks across accounts or report parameters.
    assert cache.get(plan, "other@example.com|param-a") is None
    assert cache.get(plan, "user@example.com|param-b") is None

    unverified = ExecutionReceipt(
        row_dimensions=(),
        column_dimensions=(),
        selected_members={},
        kpis=(),
        period=None,
        table_refreshed=False,
        verified=False,
    )
    with pytest.raises(ValueError, match="Only verified"):
        cache.set(plan, scope, VerifiedResult(receipt=unverified))


def test_live_driver_rejects_kpi_switch_when_report_never_refreshes():
    source = Path("app/worldpanel/pivot_driver.py").read_text(encoding="utf-8")

    assert "KPI selection did not refresh the report" in source


def test_live_driver_does_not_reject_pivot_apply_only_because_text_delta_is_absent():
    source = Path("app/worldpanel/pivot_driver.py").read_text(encoding="utf-8")

    assert "Pivot apply did not refresh the report" not in source
    assert "apply_pivot:no_text_delta" in source


@pytest.mark.asyncio
async def test_executor_rejects_unparseable_refreshed_table_and_never_returns_receipt():
    product = next(tag for tag in parse_dimension_tags(PIVOT_HTML) if tag.label == "Product")

    class Driver:
        actions = []

        async def open_pivot(self):
            pass

        async def list_dimension_tags(self):
            return [product]

        async def set_axis(self, tag, axis, position):
            pass

        async def verify_layout(self, expected):
            pass

        async def read_layout(self):
            return PivotLayout(rows=("Product",))

        async def apply(self):
            pass

        async def select_report_kpi(self, requested):
            return "Spend (RMB 000)"

        async def read_report_kpi(self):
            return "Spend (RMB 000)"

        async def read_report_grid(self):
            # A rendered error page: header row only, no data rows.
            return {"columns": [], "rows": []}

    plan = QueryPlan(
        report_set="Set",
        report="Report",
        axis_placements=(AxisPlacement("Product", "row", 0),),
    )

    with pytest.raises(Exception, match="could not be parsed"):
        await QueryExecutor(Driver(), object()).execute(plan)  # type: ignore[arg-type]
