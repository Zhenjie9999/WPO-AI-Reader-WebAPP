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


def test_term_match_prefers_specific_node_over_generic_root():
    from app.worldpanel.pivot_service import _term_match

    assert _term_match("4 Premium Fruit Types", "4 Premium Fruits", "") > _term_match("Fruit", "4 Premium Fruits", "")
    assert _term_match("Fruit", "4 Premium Fruits", "") == 0


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
