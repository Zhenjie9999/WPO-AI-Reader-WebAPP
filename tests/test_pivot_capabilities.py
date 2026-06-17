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
from app.worldpanel.pivot_service import (
    _build_plan,
    _member_match_length,
    _question_may_require_members,
)


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


def test_llm_product_term_resolves_member_even_without_alias_or_literal_mention():
    # The question never literally contains the product; the LLM supplies the
    # translated English term, which must still resolve the live member.
    assert _member_match_length("Dragonfruit", "给我2026年5月的销额", ("Dragonfruit",)) > 0
    assert _member_match_length("Cherry", "2026 May value", ("cherry",)) > 0
    # An unrelated label is not matched by an unrelated term.
    assert _member_match_length("Apple", "2026 May value", ("cherry",)) == 0


def test_build_plan_assembles_query_from_intent_and_resolved_members():
    tentative = {
        "axis_placements": [],
        "kpis": ["Spend (RMB 000)"],
        "expected_period": "2026 May",
        "calculation": "Yr on Yr % Change",
        "filters": [{"role": "duration", "value": "YTD"}],
        "output_shape": "single_value",
    }
    members = [{"dimension": "Product", "member_path": ["Fruit", "Cherry"], "checked": True}]
    plan = _build_plan(tentative, members, report_set="Set", report="Data Explorer")
    assert plan.member_selections[0].member_path == ("Fruit", "Cherry")
    assert plan.calculation == "Yr on Yr % Change"
    assert plan.filters[0].role == "duration" and plan.filters[0].value == "YTD"
    assert plan.expected_period == "2026 May"
    assert plan.kpis == ("Spend (RMB 000)",)


def test_english_member_still_matches_without_alias():
    assert _question_may_require_members("Durian spend 2026 May") is True
    assert _member_match_length("Durian", "durianspend2026may") > 0


def test_pure_calculation_question_needs_no_member():
    # "2026 May spend growth rate vs last year" has no product -> no member intent.
    assert _question_may_require_members("2026 May spend growth rate vs last year") is False


def test_answer_uses_absolute_format_for_value_metric():
    table = table_from_grid(
        ["Fruit"],
        [["15-May-26", ["42960150"]]],
        metric="Spend (RMB 000)",
    )
    answer = answer_from_pivot_tables({"Spend (RMB 000)": table}, [], "15-May-26")
    assert "42,960,150" in answer
