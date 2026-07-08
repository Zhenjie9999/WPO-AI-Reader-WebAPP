"""Tests for the Pivot capabilities added to fix '+' members, dropdowns, and
growth-rate queries. Fixtures mirror the real Zespri Data Explorer DOM."""
import pytest

from app.worldpanel.parser import KeyMeasuresTable  # noqa: F401  (sanity import)
from app.worldpanel.pivot_parser import classify_dropdown_role
from app.worldpanel.pivot_result import (
    PivotResultError,
    answer_from_pivot_tables,
    resolve_period,
    table_from_grid,
)
from app.worldpanel.planner import (
    calculation_clarification_for_question,
    _detect_calculation,
    _detect_duration,
    _detect_filters,
    _detect_period,
)
from app.worldpanel.pivot_service import _member_match_length, _question_may_require_members


# Real option signatures captured from the live report dropdowns.
KPI_OPTIONS = ("Spend (RMB 000)", "Volume (000 kg)", "Penetration %", "Buyers (000)")
CALC_OPTIONS = ("Actual Yr on Yr", "Yr on Yr % Change", "Yr on Yr Difference", "Period on Period % Change~")
OUTLET_OPTIONS = ("Total Outlets", "Hypermarket", "Supermarket", "CVS", "Ecommerce")
DURATION_OPTIONS = ("STD", "52 w/e", "12 w/e", "4 w/e", "YTD")
GEO_OPTIONS = ("National", "Tier 1", "Tier 2", "Shanghai", "Beijing")


def test_classify_every_real_dropdown_role():
    assert classify_dropdown_role(KPI_OPTIONS) == "kpi"
    assert classify_dropdown_role(CALC_OPTIONS) == "calculation"
    assert classify_dropdown_role(OUTLET_OPTIONS) == "channel"
    assert classify_dropdown_role(DURATION_OPTIONS) == "duration"
    assert classify_dropdown_role(GEO_OPTIONS) == "geography"


def test_grid_with_null_yoy_cells_does_not_invent_zeros():
    # Early periods have no prior-year baseline -> '.' (null), later ones do.
    grid_columns = ["Fruit", "Apple", "Kiwifruit", "Gold kiwifruit", "Green kiwifruit"]
    grid_rows = [
        ["17-May-24", [None, None, None, None, None]],
        ["16-May-25", ["1.2", "0.5", "2.0", "1.1", "3.0"]],
        ["15-May-26", ["3.4", "-14.1", "3.1", "-5.1", "7.9"]],
    ]
    table = table_from_grid(grid_columns, grid_rows, metric="Spend (RMB 000) - Yr on Yr % Change")

    assert table.column_labels == tuple(grid_columns)
    assert table.row_labels == ("17-May-24", "16-May-25", "15-May-26")
    # Null row keeps no cells (not zeros).
    assert table.cells["17-May-24"] == {}
    assert table.cells["15-May-26"]["Green kiwifruit"] == 7.9
    # Asking for a null cell is an explicit empty-data error, never 0.
    with pytest.raises(PivotResultError, match="empty"):
        table.value("17-May-24", "Fruit")


def test_grid_value_lookup_resolves_the_may_2026_growth_rate():
    table = table_from_grid(
        ["Fruit", "Apple"],
        [["15-May-26", ["3.4", "-14.1"]]],
        metric="Spend (RMB 000) - Yr on Yr % Change",
    )
    value, row, column = table.value("15-May-26", "Fruit")
    assert (value, row, column) == (3.4, "15-May-26", "Fruit")


def test_resolve_period_handles_english_month_for_may_2026_and_last_year():
    dates = ["16-May-25", "13-Jun-25", "17-Apr-26", "15-May-26"]
    table = table_from_grid(dates, [["Fruit", ["1", "2", "3", "4"]]], metric="Spend")
    # Columns are the date axis here.
    assert resolve_period("2026 May", table) == "15-May-26"
    assert resolve_period("May 2026", table) == "15-May-26"
    assert resolve_period("2025 May", table) == "16-May-25"
    with pytest.raises(PivotResultError, match="No date"):
        resolve_period("2030 May", table)


def test_planner_detects_growth_period_and_filters_from_natural_language():
    question = "2026 May Spend growth rate vs last year in Hypermarket, 52 w/e"
    assert _detect_calculation(question) == "Yr on Yr % Change"
    assert _detect_period(question) == "2026 May"
    filters = _detect_filters(question)
    roles = {f["role"]: f["value"] for f in filters}
    assert roles == {"channel": "Hypermarket", "duration": "52 w/e"}


def test_ambiguous_growth_rate_requests_calculation_clarification():
    clarification = calculation_clarification_for_question("Show 2026 May spend growth rate")

    assert clarification is not None
    assert clarification.dimension == "calculation"
    assert ("Yr on Yr % Change",) in clarification.candidates
    assert ("Period on Period % Change~",) in clarification.candidates
    assert _detect_calculation("Show 2026 May spend growth rate") is None


def test_explicit_growth_basis_does_not_request_calculation_clarification():
    assert calculation_clarification_for_question("Show 2026 May spend growth rate vs last year") is None
    assert calculation_clarification_for_question("Show 2026 May spend period on period growth") is None
    assert _detect_calculation("Show 2026 May spend growth rate vs last year") == "Yr on Yr % Change"
    assert _detect_calculation("Show 2026 May spend period on period growth") == "Period on Period % Change~"


def test_duration_std_and_ytd_are_distinct_and_never_conflated():
    assert _detect_duration("Spend YTD") == "YTD"
    assert _detect_duration("Spend STD") == "STD"
    assert _detect_duration("销额 年初至今") == "YTD"
    assert _detect_duration("销额 单期") == "STD"
    assert _detect_duration("Spend 52 w/e") == "52 w/e"
    assert _detect_duration("Spend 12 w/e") == "12 w/e"
    assert _detect_duration("Spend 4 w/e") == "4 w/e"
    # A plain question with no duration keyword resolves to nothing (default STD
    # is applied by the report, not invented here).
    assert _detect_duration("2026 May spend") is None
    # 'std'/'ytd' must not match as substrings of unrelated words.
    assert _detect_duration("understand the trend") is None


def test_filters_carry_the_exact_duration_choice():
    ytd = {f["role"]: f["value"] for f in _detect_filters("CVS YTD spend")}
    assert ytd == {"channel": "CVS", "duration": "YTD"}
    std = {f["role"]: f["value"] for f in _detect_filters("STD spend in Hypermarket")}
    assert std == {"channel": "Hypermarket", "duration": "STD"}


def test_planner_detects_chinese_growth_and_period():
    assert _detect_calculation("2026年5月 销额 同比增长率") == "Yr on Yr % Change"
    assert _detect_period("2026年5月 销额 同比增长率") == "2026年5月"
    assert _detect_calculation("销额环比增长") == "Period on Period % Change~"


def test_answer_formats_percent_for_a_yoy_table_at_the_requested_period():
    table = table_from_grid(
        ["Fruit", "Green kiwifruit"],
        [["15-May-26", ["3.4", "7.9"]]],
        metric="Spend (RMB 000) - Yr on Yr % Change",
    )
    answer = answer_from_pivot_tables({"Spend (RMB 000) - Yr on Yr % Change": table}, [], "15-May-26")
    assert "+3.4%" in answer
    assert "Green kiwifruit +7.9%" in answer


def test_chinese_product_names_map_to_english_members():
    # 榴莲 (2 chars) must be recognized as member intent and match "Durian".
    assert _question_may_require_members("榴莲 2026 May 销额") is True
    assert _member_match_length("Durian", "榴莲2026may销额") > 0
    assert _member_match_length("Gold kiwifruit", "金果销额") > 0
    # An unrelated label does not match the durian question.
    assert _member_match_length("Apple", "榴莲销额") == 0


def test_llm_kpi_terms_canonicalize_to_real_report_labels():
    from app.worldpanel.planner import canonical_calculation, canonical_kpi

    assert canonical_kpi("Sales Amount") == "Spend (RMB 000)"
    assert canonical_kpi("销额") == "Spend (RMB 000)"
    assert canonical_kpi("销售金额") == "Spend (RMB 000)"
    assert canonical_kpi("Volume") == "Volume (000 kg)"
    assert canonical_kpi("Penetration") == "Penetration %"
    # An already-correct or unknown label is left unchanged.
    assert canonical_kpi("Spend (RMB 000)") == "Spend (RMB 000)"
    assert canonical_calculation("Year on Year % Change") == "Yr on Yr % Change"
    assert canonical_calculation("同比增长率") == "Yr on Yr % Change"


def test_exact_member_match_outranks_substring_so_no_false_ambiguity():
    # 金果 / "Gold kiwifruit" must pick the exact node, not tie with the longer
    # "Non-imported Gold Kiwifruit" / "Other Brands Gold Kiwifruit".
    exact = _member_match_length("Gold kiwifruit", "", ("Gold kiwifruit",))
    longer = _member_match_length("Non-imported Gold Kiwifruit", "", ("Gold kiwifruit",))
    other = _member_match_length("Other Brands Gold Kiwifruit", "", ("Gold kiwifruit",))
    assert exact > longer and exact > other
    # Chinese alias term resolves the exact member, not the variants.
    exact_cn = _member_match_length("Gold kiwifruit", "", ("金果",))
    variant_cn = _member_match_length("Non-imported Gold Kiwifruit", "", ("金果",))
    assert exact_cn > variant_cn


def test_specific_multiword_member_beats_generic_root_substring():
    # "4 Premium Fruits Type" must resolve to "4 Premium Fruit Types", not the
    # bare "Fruit" root (which previously won via substring -> wrong/ambiguous).
    term = ("4 Premium Fruits Type",)
    target = _member_match_length("4 Premium Fruit Types", "", term)
    generic = _member_match_length("Fruit", "", term)
    assert target > 0
    assert generic == 0  # generic short root must NOT match a longer specific term
    assert target > generic
    # Plural/word-order differences still match exactly.
    assert _member_match_length("4 Premium Fruit Types", "4premiumfruitstype在2026", ()) >= 0


def test_generic_fruit_root_not_stolen_by_premium_question_when_target_exists():
    # Simulate the discovery decision over the real product roots.
    roots = ["Fruit", "Fruit Brand", "Fruit", "4 Premium Fruit Types"]
    term = ("4 Premium Fruits Type",)
    scores = {r: _member_match_length(r, "4premiumfruitstype在2026年的渗透率", term) for r in roots}
    best = max(scores.values())
    winners = [r for r, s in scores.items() if s == best]
    assert winners == ["4 Premium Fruit Types"]


def test_english_member_still_matches_without_alias():
    assert _question_may_require_members("Durian spend 2026 May") is True
    assert _member_match_length("Durian", "durianspend2026may") > 0


def test_pure_calculation_question_needs_no_member():
    # "2026 May spend growth rate vs last year" has no product -> no member intent.
    assert _question_may_require_members("2026 May spend growth rate vs last year") is False


def test_format_number_preserves_decimals_and_trims_whole_numbers():
    from app.worldpanel.pivot_result import format_number

    assert format_number(7.3) == "7.3"
    assert format_number(46.27) == "46.27"
    assert format_number(61.0) == "61"
    assert format_number(2931643.0) == "2,931,643"
    assert format_number(1234.5) == "1,234.5"


def test_decimal_kpis_keep_their_decimals_in_the_answer():
    pen = table_from_grid(["Gold kiwifruit"], [["15-May-26", ["7.3"]]], metric="Penetration %")
    answer = answer_from_pivot_tables({"Penetration %": pen}, ["Gold kiwifruit"], "15-May-26")
    assert "7.3%" in answer  # not "7" and not "+7.3%"

    price = table_from_grid(["Gold kiwifruit"], [["15-May-26", ["46.27"]]], metric="Average Price (RMB)/(kg)")
    answer = answer_from_pivot_tables({"Average Price (RMB)/(kg)": price}, ["Gold kiwifruit"], "15-May-26")
    assert "46.27" in answer  # decimals kept


def test_to_key_measures_keeps_float_values():
    table = table_from_grid(["Gold kiwifruit"], [["15-May-26", ["7.3"]]], metric="Penetration %")
    km = table.to_key_measures()
    assert km.value_for("Gold kiwifruit", "15-May-26") == 7.3  # not rounded to 7


def _make_node(label, path):
    from app.worldpanel.pivot_models import MemberNode

    return MemberNode(
        label=label, value=label, path=tuple(path), level=len(path) - 1,
        has_children=False, expanded=False, checked=False, selected=False,
    )


_PRODUCT_TAG = None
_PRODUCT_NODES = None


def _product_fixture():
    global _PRODUCT_TAG, _PRODUCT_NODES
    from app.worldpanel.pivot_models import DimensionTag

    _PRODUCT_TAG = DimensionTag(label="Product", dimension_id="[Dim1]", axis="column", position=0)
    _PRODUCT_NODES = [
        _make_node("Fruit", ["Fruit"]),
        _make_node("Apple", ["Fruit", "Apple"]),
        _make_node("Cherry", ["Fruit", "Cherry"]),
        _make_node("Durian", ["Fruit", "Durian"]),
        _make_node("Fruit Brand", ["Fruit Brand"]),
        _make_node("Fruit", ["Fruit"]),  # duplicate-label root
        _make_node("4 Premium Fruit Types", ["4 Premium Fruit Types"]),
    ]
    return _PRODUCT_TAG, tuple(_PRODUCT_NODES)


async def _discover(question, extra_terms):
    import app.worldpanel.pivot_service as svc
    tag, nodes = _product_fixture()

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    return await svc._discover_members_from_question(
        question, "R", (tag,), _Schema(), _Driver(), extra_terms=extra_terms
    )


@pytest.mark.asyncio
async def test_crest_resolves_to_total_crest_when_tm_variant_is_present():
    import app.worldpanel.pivot_service as svc
    from app.worldpanel.pivot_models import DimensionTag

    tag = DimensionTag(label="Product", dimension_id="[Dim1]", axis="column", position=0)
    nodes = (
        _make_node("Total CREST", ["Total CREST"]),
        _make_node("CREST TM", ["CREST TM"]),
    )

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    result = await svc._discover_members_from_question(
        "CREST 2026 sales value",
        "R",
        (tag,),
        _Schema(),
        _Driver(),
        extra_terms=("CREST",),
    )

    assert result == [{"dimension": "Product", "member_path": ["Total CREST"], "checked": True}]


def test_term_match_prefers_specific_node_over_generic_root():
    from app.worldpanel.pivot_service import _term_match

    assert _term_match("4 Premium Fruit Types", "4 Premium Fruits", "") > _term_match("Fruit", "4 Premium Fruits", "")
    assert _term_match("Fruit", "4 Premium Fruits", "") == 0


def test_total_brand_member_beats_tm_variant_for_base_brand_term():
    from app.worldpanel.pivot_service import _term_match

    assert _term_match("Total CREST", "CREST", "") > _term_match("CREST TM", "CREST", "")


@pytest.mark.asyncio
async def test_4_premium_fruits_resolves_to_specific_member_not_fruit():
    result = await _discover("2026年5月4 Premium Fruits的销额", ("4 Premium Fruits",))
    paths = [tuple(s["member_path"]) for s in result]
    assert ("4 Premium Fruit Types",) in paths
    assert ("Fruit",) not in paths


@pytest.mark.asyncio
async def test_total_fruit_and_premium_select_both_members():
    result = await _discover("整体水果和4 Premium Fruits的表现", ("Fruit", "4 Premium Fruits"))
    paths = {tuple(s["member_path"]) for s in result}
    assert ("Fruit",) in paths
    assert ("4 Premium Fruit Types",) in paths


def test_asks_all_members_detection():
    from app.worldpanel.pivot_service import _asks_all_members

    assert _asks_all_members("给我所有product列表下的产品在2026年的销售额")
    assert _asks_all_members("all products sales 2026")
    assert _asks_all_members("全部品类的销额")
    assert not _asks_all_members("金果2026年5月销额")
    assert not _asks_all_members("2026年5月整体水果的销额")


@pytest.mark.asyncio
async def test_all_products_returns_select_all_sentinel():
    import app.worldpanel.pivot_service as svc
    tag, nodes = _product_fixture()

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    res = await svc._discover_members_from_question(
        "给我所有产品2026年的销售额", "R", (tag,), _Schema(), _Driver()
    )
    assert res == [{"dimension": "Product", "member_path": ["*"], "checked": True}]


@pytest.mark.asyncio
async def test_llm_fuzzy_resolves_member_against_live_list():
    import json
    import app.worldpanel.pivot_service as svc
    tag, nodes = _product_fixture()  # index 3 == Durian (Fruit>Durian)

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    class _AI:
        async def chat(self, prompt):
            assert "index: Dimension > path" in prompt  # live list was sent
            return "result: " + json.dumps({"indices": [3]})

    res = await svc._discover_members_from_question(
        "帮我看下那个带刺的水果的销额", "R", (tag,), _Schema(), _Driver(), assistant=_AI()
    )
    assert [tuple(s["member_path"]) for s in res] == [("Fruit", "Durian")]


def test_accumulate_export_rows_merges_across_queries():
    from app.worldpanel.parser import KeyMeasuresTable
    from app.main import _accumulate_export_rows

    session: dict = {}
    rep = {"report_set": "S", "report_name": "R"}
    t1 = KeyMeasuresTable(title="t", metric="Spend (RMB 000)", products=["Cherry"], dates=["15-May-26"], rows={"15-May-26": {"Cherry": 100.0}})
    t2 = KeyMeasuresTable(title="t", metric="Spend (RMB 000)", products=["Durian"], dates=["15-May-26"], rows={"15-May-26": {"Durian": 200.0}})
    _accumulate_export_rows(session, {"Spend (RMB 000)": t1}, rep)
    _accumulate_export_rows(session, {"Spend (RMB 000)": t2}, rep)
    rows = session["export_rows"]
    assert ("S", "R", "Spend (RMB 000)", "Cherry", "15-May-26") in rows
    assert ("S", "R", "Spend (RMB 000)", "Durian", "15-May-26") in rows


def test_restore_pivot_report_reattaches_after_session_loss():
    from app.main import _restore_pivot_report

    class _PS:
        current_report = None

    ps = _PS()
    http = {"current_report": {"report_set": "S", "report_parameter": "P", "report_name": "R"}}
    _restore_pivot_report(http, ps)
    assert ps.current_report["report_parameter"] == "P"


def test_answer_uses_absolute_format_for_value_metric():
    table = table_from_grid(
        ["Fruit"],
        [["15-May-26", ["42960150"]]],
        metric="Spend (RMB 000)",
    )
    answer = answer_from_pivot_tables({"Spend (RMB 000)": table}, [], "15-May-26")
    assert "42,960,150" in answer


def test_local_planner_detects_channel_breakdown_as_axis_not_filter():
    from app.worldpanel.planner import _local_tentative_plan

    plan = _local_tentative_plan("Blueberry在2026年5月，分渠道的销售额是什么样的？")
    # The breakdown becomes a column axis (so every channel shows), NOT a filter.
    assert {"dimension": "Channel", "axis": "column", "position": 0} in plan["axis_placements"]
    assert plan["output_shape"] == "table"
    assert all(f.get("role") != "channel" for f in plan["filters"])
    # English phrasing works too.
    plan_en = _local_tentative_plan("Show Blueberry sales by channel for May 2026")
    assert any(a["dimension"] == "Channel" for a in plan_en["axis_placements"])


def test_resolve_dimension_name_maps_channel_synonyms_to_live_outlet():
    from app.worldpanel.pivot_models import DimensionTag
    from app.worldpanel.pivot_service import _resolve_dimension_name

    live = (
        DimensionTag("Product", "d1", "row", 0),
        DimensionTag("Outlet", "d2", "filter", 0),
        DimensionTag("Period", "d3", "row", 1),
    )
    # English, Chinese and near-synonyms all resolve to the live "Outlet" dim.
    assert _resolve_dimension_name("Channel", live) == "Outlet"
    assert _resolve_dimension_name("channels", live) == "Outlet"
    assert _resolve_dimension_name("渠道", live) == "Outlet"
    assert _resolve_dimension_name("零售商", live) == "Outlet"
    # Exact live label still wins.
    assert _resolve_dimension_name("Product", live) == "Product"
    # Unknown dimension stays unresolved (caller drops it).
    assert _resolve_dimension_name("无关维度xyz", live) is None


def test_channel_breakdown_answer_lists_every_channel_for_the_period():
    # Period on rows, channels on columns: "Blueberry by channel in May 2026".
    table = table_from_grid(
        ["Hypermarket", "Supermarket", "CVS", "Ecommerce"],
        [
            ["16-May-25", ["10", "20", "30", "40"]],
            ["15-May-26", ["11.5", "22.1", "33.0", "44.4"]],
        ],
        metric="Spend (RMB 000)",
    )
    # member_leaves carries the product (Blueberry) and the "*" select-all
    # sentinel; neither is an axis label, so the answer falls to the
    # "every column at this period" branch.
    answer = answer_from_pivot_tables(
        {"Spend (RMB 000)": table}, ["Blueberry", "*"], "15-May-26"
    )
    assert "Hypermarket 11.5" in answer
    assert "Supermarket 22.1" in answer
    assert "CVS 33" in answer
    assert "Ecommerce 44.4" in answer
    assert "15-May-26" in answer


def test_local_planner_detects_ranking_intent_for_superlatives():
    from app.worldpanel.planner import _local_tentative_plan

    plan = _local_tentative_plan("牙膏里渗透率最高的品牌是哪个？")
    assert plan["ranking"] == {"dimension": "Brand", "direction": "max", "top_n": 1}
    assert plan["output_shape"] == "ranking"
    # The ranked dimension is spread across a column axis like a breakdown.
    assert {"dimension": "Brand", "axis": "column", "position": 0} in plan["axis_placements"]
    assert "Penetration %" in plan["kpis"]

    low = _local_tentative_plan("销售额最低的前3个渠道是哪些？")
    assert low["ranking"]["direction"] == "min"
    assert low["ranking"]["top_n"] == 3
    assert low["ranking"]["dimension"] == "Channel"

    top_en = _local_tentative_plan("Top 5 brands by spend in May 2026")
    assert top_en["ranking"]["top_n"] == 5

    none = _local_tentative_plan("Blueberry在2026年5月的销售额是多少？")
    assert none["ranking"] is None


def test_ranking_answer_orders_members_and_excludes_scope_parent():
    # Dates on rows, brands on columns; the scope parent 牙膏 also rendered.
    table = table_from_grid(
        ["牙膏", "BrandA", "BrandB", "BrandC", "BrandD"],
        [
            ["16-May-25", ["90", "10", "20", "30", "5"]],
            ["15-May-26", ["95.5", "12.3", "45.6", "33.3", "7.7"]],
        ],
        metric="Penetration %",
    )
    answer = answer_from_pivot_tables(
        {"Penetration %": table},
        ["牙膏"],
        "15-May-26",
        ranking={"direction": "max", "top_n": 1},
    )
    # The parent 牙膏 (95.5, highest raw value) must NOT win the ranking.
    assert "最高的是 BrandB" in answer
    assert "45.6%" in answer
    assert "15-May-26" in answer
    # Full ordering listed for transparency.
    assert answer.index("BrandB") < answer.index("BrandC") < answer.index("BrandA")

    lowest = answer_from_pivot_tables(
        {"Penetration %": table},
        ["牙膏"],
        "15-May-26",
        ranking={"direction": "min", "top_n": 2},
    )
    assert "最低的是 BrandD" in lowest


def test_query_plan_payload_roundtrip_preserves_ranking():
    from dataclasses import asdict
    from app.main import _query_plan_from_payload
    from app.worldpanel.pivot_models import QueryPlan, RankingSpec

    plan = QueryPlan(
        report_set="S",
        report="R",
        output_shape="ranking",
        ranking=RankingSpec(dimension="Brand", direction="max", top_n=3),
    )
    rebuilt = _query_plan_from_payload(asdict(plan))
    assert rebuilt.ranking == RankingSpec(dimension="Brand", direction="max", top_n=3)
    assert rebuilt.output_shape == "ranking"

    no_ranking = _query_plan_from_payload({"report_set": "S", "report": "R"})
    assert no_ranking.ranking is None


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_deterministic_matching():
    """Regression: pivot_service used an undefined `logger` in the LLM-failure
    path, so any Doubao timeout became a 500 instead of a graceful fallback."""
    import app.worldpanel.pivot_service as svc

    tag, nodes = _product_fixture()

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    class _BrokenAI:
        async def chat(self, prompt):
            raise TimeoutError("ark endpoint timed out")

    res = await svc._discover_members_from_question(
        "榴莲的销售额", "R", (tag,), _Schema(), _Driver(),
        extra_terms=("Durian",), assistant=_BrokenAI(),
    )
    assert [tuple(s["member_path"]) for s in res] == [("Fruit", "Durian")]


def test_idle_sessions_are_swept_and_active_ones_refreshed():
    import time as _time
    import app.main as main

    main._sessions.clear()
    main._sessions["old"] = {"credentials": object(), "last_used": _time.monotonic() - main._SESSION_IDLE_SECONDS - 10}
    main._sessions["fresh"] = {"credentials": object(), "last_used": _time.monotonic()}
    try:
        session = main._session("fresh")
        assert "old" not in main._sessions  # credentials wiped
        assert session["last_used"] >= _time.monotonic() - 5  # refreshed
        with pytest.raises(Exception):
            main._session("old")
    finally:
        main._sessions.clear()


@pytest.mark.asyncio
async def test_logout_wipes_session_and_credentials():
    import app.main as main

    main._sessions["s-logout"] = {"credentials": object(), "last_used": 0.0}
    result = await main.logout(main.LogoutRequest(session_id="s-logout"))
    assert result == {"ok": True}
    assert "s-logout" not in main._sessions
    # Logging out twice (or with an unknown id) is harmless.
    again = await main.logout(main.LogoutRequest(session_id="s-logout"))
    assert again == {"ok": True}


def test_datastore_records_overwrites_and_isolates_accounts(tmp_path):
    from app.worldpanel.datastore import DataStore

    store = DataStore(str(tmp_path / "facts.sqlite3"))
    store.record("a@x.com", "S", "R", "Spend (RMB 000)", [("Cherry", "15-May-26", 100.5)])
    store.record("b@x.com", "S", "R", "Spend (RMB 000)", [("Cherry", "15-May-26", 999.0)])
    # Re-pulling the same cell keeps the freshest value, no duplicates.
    store.record("a@x.com", "S", "R", "Spend (RMB 000)", [("Cherry", "15-May-26", 101.0)])

    rows_a = store.export_rows("a@x.com")
    assert rows_a == [("S", "R", "Spend (RMB 000)", "Cherry", "15-May-26", 101.0)]
    assert store.export_rows("b@x.com") == [("S", "R", "Spend (RMB 000)", "Cherry", "15-May-26", 999.0)]
    store.close()

    # Reopen: data survived the process "restart".
    reopened = DataStore(str(tmp_path / "facts.sqlite3"))
    assert reopened.export_rows("a@x.com")[0][-1] == 101.0
    reopened.close()


@pytest.mark.asyncio
async def test_export_csv_merges_datastore_with_in_memory_rows(tmp_path, monkeypatch):
    import app.main as main
    from app.worldpanel.client import Credentials
    from app.worldpanel.datastore import DataStore

    store = DataStore(str(tmp_path / "facts.sqlite3"))
    # A row pulled in an earlier (pre-restart) conversation, only on disk.
    store.record("a@x.com", "S", "R", "Spend (RMB 000)", [("Durian", "15-May-26", 42.5)])
    monkeypatch.setattr(main, "_datastore", store)

    main._sessions["s-export"] = {
        "credentials": Credentials(email="a@x.com", password="pw"),
        "last_used": __import__("time").monotonic(),
        "export_rows": {("S", "R", "Spend (RMB 000)", "Cherry", "15-May-26"): 100.5},
    }
    try:
        response = await main.export_current_csv("s-export")
        body = response.body.decode("utf-8")
        assert "Durian" in body  # survived "restart" via the datastore
        assert "Cherry" in body  # current conversation row
        assert "42.5" in body and "100.5" in body
    finally:
        main._sessions.clear()
        store.close()


def test_datastore_catalog_and_fetch_cells(tmp_path):
    from app.worldpanel.datastore import DataStore

    store = DataStore(str(tmp_path / "facts.sqlite3"))
    store.record("a@x.com", "S", "R", "Penetration %", [
        ("BrandA", "15-May-26", 12.3),
        ("BrandB", "15-May-26", 45.6),
        ("BrandA", "17-Apr-26", 11.0),
    ])
    catalog = store.catalog("a@x.com")
    assert catalog["metrics"] == ["Penetration %"]
    assert set(catalog["members"]) == {"BrandA", "BrandB"}
    assert set(catalog["dates"]) == {"15-May-26", "17-Apr-26"}
    assert catalog["members_truncated"] is False
    assert catalog["updated_at"]

    cells = store.fetch_cells("a@x.com", "Penetration %", ["BrandA", "BrandB"], ["15-May-26"])
    assert cells == {("BrandA", "15-May-26"): 12.3, ("BrandB", "15-May-26"): 45.6}
    # Conflicting values for the same cell under two reports -> ambiguous -> None.
    store.record("a@x.com", "S", "R2", "Penetration %", [("BrandA", "15-May-26", 99.0)])
    assert store.fetch_cells("a@x.com", "Penetration %", ["BrandA"], ["15-May-26"]) is None
    store.close()


class _LocalAI:
    def __init__(self, payload):
        self.payload = payload

    async def chat(self, prompt):
        import json as _json
        return _json.dumps(self.payload)


@pytest.mark.asyncio
async def test_local_answer_answers_ranking_from_store(tmp_path):
    from app.worldpanel.datastore import DataStore
    from app.worldpanel.local_answer import try_local_answer

    store = DataStore(str(tmp_path / "facts.sqlite3"))
    store.record("a@x.com", "S", "R", "Penetration %", [
        ("BrandA", "15-May-26", 12.3),
        ("BrandB", "15-May-26", 45.6),
        ("BrandC", "15-May-26", 33.3),
    ])
    ai = _LocalAI({
        "answerable": True,
        "metric": "Penetration %",
        "members": ["BrandA", "BrandB", "BrandC"],
        "dates": ["15-May-26"],
        "ranking": {"direction": "max", "top_n": 1},
    })
    result = await try_local_answer("刚才那些品牌里渗透率最高的是哪个？", "a@x.com", store, ai)
    assert result is not None
    assert "最高的是 BrandB" in result["answer"]
    assert result["source"]["kind"] == "local-store"
    store.close()


@pytest.mark.asyncio
async def test_local_answer_guards_fall_back_to_live_pull(tmp_path):
    from app.worldpanel.datastore import DataStore
    from app.worldpanel.local_answer import try_local_answer

    store = DataStore(str(tmp_path / "facts.sqlite3"))
    store.record("a@x.com", "S", "R", "Spend (RMB 000)", [("BrandA", "15-May-26", 10.0)])

    good_spec = {
        "answerable": True,
        "metric": "Spend (RMB 000)",
        "members": ["BrandA"],
        "dates": ["15-May-26"],
        "ranking": None,
    }
    # 1) Explicit fresh-pull wording always bypasses the local store.
    assert await try_local_answer("重新拉取 BrandA 5月销售额", "a@x.com", store, _LocalAI(good_spec)) is None
    # 2) LLM says not answerable.
    assert await try_local_answer("q", "a@x.com", store, _LocalAI({"answerable": False})) is None
    # 3) Invented member not in the catalog.
    bad_member = dict(good_spec, members=["BrandZ"])
    assert await try_local_answer("q", "a@x.com", store, _LocalAI(bad_member)) is None
    # 4) Missing cell (known member, but a date it was never pulled for).
    store.record("a@x.com", "S", "R", "Spend (RMB 000)", [("BrandB", "17-Apr-26", 5.0)])
    partial = dict(good_spec, members=["BrandA", "BrandB"], dates=["15-May-26"])
    assert await try_local_answer("q", "a@x.com", store, _LocalAI(partial)) is None
    # 5) LLM itself failing is contained.
    class _Boom:
        async def chat(self, prompt):
            raise RuntimeError("ark down")
    assert await try_local_answer("q", "a@x.com", store, _Boom()) is None
    # 6) No assistant configured.
    assert await try_local_answer("q", "a@x.com", store, None) is None
    # Control: the good spec on complete data does answer.
    ok = await try_local_answer("BrandA 5月销售额", "a@x.com", store, _LocalAI(good_spec))
    assert ok is not None and "BrandA" in ok["answer"]
    store.close()


@pytest.mark.asyncio
async def test_local_answer_orders_dates_chronologically(tmp_path):
    from app.worldpanel.datastore import DataStore
    from app.worldpanel.local_answer import try_local_answer

    store = DataStore(str(tmp_path / "facts.sqlite3"))
    store.record("a@x.com", "S", "R", "Spend (RMB 000)", [
        ("BrandA", "15-May-26", 20.0),
        ("BrandA", "17-Apr-26", 10.0),
    ])
    ai = _LocalAI({
        "answerable": True,
        "metric": "Spend (RMB 000)",
        # Deliberately reversed: the store must sort chronologically.
        "members": ["BrandA"],
        "dates": ["15-May-26", "17-Apr-26"],
        "ranking": None,
    })
    result = await try_local_answer("BrandA 4月和5月的销售额", "a@x.com", store, ai)
    assert result is not None
    assert result["source"]["dates"] == ["17-Apr-26", "15-May-26"]
    store.close()


class _JsonAI:
    """Fake assistant answering each prompt with a fixed JSON payload."""

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []

    async def chat(self, prompt):
        import json as _json
        self.prompts.append(prompt)
        return _json.dumps(self.payload)


@pytest.mark.asyncio
async def test_semantic_pick_option_maps_shorthand_to_real_label():
    from app.worldpanel.semantic_match import pick_option

    options = ["Spend (RMB 000)", "Volume (000 kg)", "Penetration %"]
    picked = await pick_option(
        _JsonAI({"index": 2}), question="牙膏的pen%是多少", term="pen%",
        options=options, purpose="KPI",
    )
    assert picked == "Penetration %"
    # null / out-of-range / broken responses all resolve to None, never invent.
    assert await pick_option(_JsonAI({"index": None}), question="q", term="t", options=options, purpose="KPI") is None
    assert await pick_option(_JsonAI({"index": 99}), question="q", term="t", options=options, purpose="KPI") is None

    class _Garbage:
        async def chat(self, prompt):
            return "not json at all"

    assert await pick_option(_Garbage(), question="q", term="t", options=options, purpose="KPI") is None
    assert await pick_option(None, question="q", term="t", options=options, purpose="KPI") is None


@pytest.mark.asyncio
async def test_semantic_related_indices_caps_dedupes_and_bounds():
    from app.worldpanel.semantic_match import related_indices

    items = [f"item{i}" for i in range(20)]
    result = await related_indices(
        _JsonAI({"indices": [3, 3, "5", 99, -1, 1, 2, 4, 6, 7, 8, 9]}),
        question="q", items=items, cap=5,
    )
    assert result == [3, 5, 1, 2, 4]
    assert await related_indices(None, question="q", items=items) == []


@pytest.mark.asyncio
async def test_dropdown_terms_resolve_via_alias_llm_or_clarify():
    from app.worldpanel.pivot_models import ReportDropdown
    from app.worldpanel.pivot_service import _resolve_dropdown_terms
    from app.worldpanel.planner import PlanClarification

    kpi_dd = ReportDropdown(index=0, role="kpi", dimension="Measures", selected="",
                            options=("Spend (RMB 000)", "Volume (000 kg)", "Average Price"))
    channel_dd = ReportDropdown(index=1, role="channel", dimension="Outlet", selected="",
                                options=("Total Outlets", "Hypermarket", "CVS"))

    # 1) Alias table hits deterministically — no LLM call needed.
    tentative = {"kpis": ["销额"], "filters": []}
    ai = _JsonAI({"index": 0})
    assert await _resolve_dropdown_terms(tentative, (kpi_dd,), ai, "q") is None
    assert tentative["kpis"] == ["Spend (RMB 000)"]
    assert ai.prompts == []

    # 2) Unfamiliar shorthand goes through the LLM pick over REAL options.
    tentative = {"kpis": ["客单价"], "filters": []}
    ai = _JsonAI({"index": 2})
    assert await _resolve_dropdown_terms(tentative, (kpi_dd,), ai, "牙膏的客单价") is None
    assert tentative["kpis"] == ["Average Price"]
    assert len(ai.prompts) == 1

    # 3) Nothing matches -> proactive clarification listing every real option.
    tentative = {"kpis": ["神秘指标"], "filters": []}
    result = await _resolve_dropdown_terms(tentative, (kpi_dd,), _JsonAI({"index": None}), "q")
    assert isinstance(result, PlanClarification)
    assert result.dimension == "kpi"
    assert ("Average Price",) in result.candidates and len(result.candidates) == 3

    # 4) Filter values resolve the same way ("HM" -> Hypermarket via LLM).
    tentative = {"kpis": [], "filters": [{"role": "channel", "value": "HM"}]}
    ai = _JsonAI({"index": 1})
    assert await _resolve_dropdown_terms(tentative, (kpi_dd, channel_dd), ai, "HM渠道的销额") is None
    assert tentative["filters"][0]["value"] == "Hypermarket"


def test_apply_clarification_handles_kpi_and_filter_answers():
    from app.worldpanel.pivot_service import _apply_clarification

    tentative = {"kpis": ["神秘指标"], "filters": [{"role": "channel", "value": "?"}]}
    _apply_clarification(tentative, {"dimension": "kpi", "member_path": ["Volume (000 kg)"]})
    assert tentative["kpis"] == ["Volume (000 kg)"]
    _apply_clarification(tentative, {"dimension": "channel", "member_path": ["CVS"]})
    assert tentative["filters"] == [{"role": "channel", "value": "CVS"}]


@pytest.mark.asyncio
async def test_member_no_match_offers_related_candidates_to_click():
    import json as _json
    import app.worldpanel.pivot_service as svc
    from app.worldpanel.planner import PlanClarification

    tag, nodes = _product_fixture()

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    class _AI:
        async def chat(self, prompt):
            if "RELATED" in prompt:
                # Related lookup: Apple (index 1) and Cherry (index 2).
                return _json.dumps({"indices": [1, 2]})
            return _json.dumps({"indices": []})  # exact resolution finds nothing

    result = await svc._discover_members_from_question(
        "帮我查一下 mystery fruit 的销额", "R", (tag,), _Schema(), _Driver(),
        extra_terms=(), assistant=_AI(),
    )
    assert isinstance(result, PlanClarification)
    assert result.dimension == "Product"
    assert ("Fruit", "Apple") in result.candidates
    assert ("Fruit", "Cherry") in result.candidates
    assert "你可能想查的是" in result.question


@pytest.mark.asyncio
async def test_resolve_dimension_falls_back_to_llm_pick():
    from app.worldpanel.pivot_models import DimensionTag
    from app.worldpanel.pivot_service import _resolve_dimension

    live = (
        DimensionTag("Product", "d1", "row", 0),
        DimensionTag("Banner", "d2", "filter", 0),
    )
    # "卖场banner" is in no synonym table; the LLM picks the live label.
    picked = await _resolve_dimension("bnr", live, _JsonAI({"index": 1}), "分bnr看销额")
    assert picked == "Banner"
    # Deterministic path still wins without the LLM.
    assert await _resolve_dimension("Product", live, None, "q") == "Product"
    assert await _resolve_dimension("bnr", live, None, "q") is None


@pytest.mark.asyncio
async def test_assistant_retries_transient_failures_once():
    import httpx
    from app.assistant import AISettings, AssistantClient

    settings = AISettings(provider="doubao", model="m", api_key="k", base_url="http://x", timeout_seconds=5)

    class _OkResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    calls = {"n": 0}

    async def flaky_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("cross-border blip")
        return _OkResponse()

    result = await AssistantClient(settings, post=flaky_post).chat("hi")
    assert result == "ok" and calls["n"] == 2

    class _ErrorResponse:
        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "503", request=httpx.Request("POST", "http://x"), response=httpx.Response(503)
            )

    calls["n"] = 0

    async def flaky_500(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _ErrorResponse()
        return _OkResponse()

    assert await AssistantClient(settings, post=flaky_500).chat("hi") == "ok"
    assert calls["n"] == 2

    # Timeouts are NOT retried — a second 60s wait would double user latency.
    async def timeout_post(url, headers=None, json=None, timeout=None):
        calls["t"] = calls.get("t", 0) + 1
        raise httpx.ReadTimeout("slow model")

    with pytest.raises(httpx.ReadTimeout):
        await AssistantClient(settings, post=timeout_post).chat("hi")
    assert calls["t"] == 1


@pytest.mark.asyncio
async def test_duplicate_label_members_become_clickable_candidates_without_ai():
    """Regression for the TOTAL CREST dead end: duplicate labels made the
    deterministic matcher refuse (ambiguous), and with the LLM down the user
    got a dead-end message. Now the tied candidates are offered to click."""
    import app.worldpanel.pivot_service as svc
    from app.worldpanel.planner import PlanClarification
    from app.worldpanel.pivot_models import DimensionTag

    tag = DimensionTag(label="Product", dimension_id="[Dim1]", axis="column", position=0)
    nodes = (
        _make_node("Total CREST", ["Oral Care", "Total CREST"]),
        _make_node("Total CREST", ["Total CREST"]),  # duplicate root — real WPO shape
        _make_node("CREST TM", ["CREST TM"]),
    )

    class _Schema:
        async def all_members(self, report, t):
            return nodes

    class _Driver:
        async def cancel_member_selection(self):
            pass

    result = await svc._discover_members_from_question(
        "TOTAL CREST在12-Jun-26的渗透率是多少?", "R", (tag,), _Schema(), _Driver(),
        extra_terms=(), assistant=None,
    )
    assert isinstance(result, PlanClarification)
    assert result.dimension == "Product"
    assert ("Oral Care", "Total CREST") in result.candidates
    assert ("Total CREST",) in result.candidates
    assert "请点选" in result.question
