import pytest

from app.worldpanel.pivot_result import (
    PivotResultError,
    answer_from_pivot_tables,
    parse_pivot_text,
    resolve_period,
)


DATES_AS_ROWS = """
Key Measures Data Table
Spend (RMB 000)
Gold kiwifruit
Green kiwifruit
15-Jan-25
1,234
900
15-Feb-25
1,300
950
"""

PRODUCTS_AS_ROWS = """
Key Measures Data Table
Spend (RMB 000)
15-Jan-25
15-Feb-25
Gold kiwifruit
1,234
1,300
Green kiwifruit
900
950
"""

NO_DATE_AXIS = """
Key Measures Data Table
Spend (RMB 000)
Hyper
CVS
Gold kiwifruit
1,234
55
Green kiwifruit
900
44
"""


def test_parse_dates_as_rows_keeps_orientation_and_values():
    table = parse_pivot_text(DATES_AS_ROWS)

    assert table.metric == "Spend (RMB 000)"
    assert table.row_labels == ("15-Jan-25", "15-Feb-25")
    assert table.column_labels == ("Gold kiwifruit", "Green kiwifruit")
    assert table.date_axis == "row"
    assert table.cells["15-Feb-25"]["Green kiwifruit"] == 950


def test_parse_products_as_rows_handles_the_transposed_cross_tab():
    table = parse_pivot_text(PRODUCTS_AS_ROWS)

    assert table.row_labels == ("Gold kiwifruit", "Green kiwifruit")
    assert table.column_labels == ("15-Jan-25", "15-Feb-25")
    assert table.date_axis == "column"
    assert table.cells["Gold kiwifruit"]["15-Feb-25"] == 1300


def test_value_lookup_works_in_either_orientation():
    for text in (DATES_AS_ROWS, PRODUCTS_AS_ROWS):
        table = parse_pivot_text(text)
        value, _, _ = table.value("Gold kiwifruit", "15-Jan-25")
        assert value == 1234
        value, _, _ = table.value("15-Jan-25", "Gold kiwifruit")
        assert value == 1234


def test_both_orientations_convert_to_the_same_key_measures_table():
    by_rows = parse_pivot_text(DATES_AS_ROWS).to_key_measures()
    by_columns = parse_pivot_text(PRODUCTS_AS_ROWS).to_key_measures()

    assert by_rows.dates == by_columns.dates == ["15-Jan-25", "15-Feb-25"]
    assert by_rows.products == by_columns.products
    assert by_rows.rows == by_columns.rows
    assert by_rows.value_for("Gold kiwifruit", "15-Feb-25") == 1300


def test_resolve_period_matches_year_period_and_full_year():
    table = parse_pivot_text(DATES_AS_ROWS)

    assert resolve_period("2025 P1", table) == "15-Jan-25"
    assert resolve_period("2025 P2", table) == "15-Feb-25"
    assert resolve_period("2025 全年", table) == "15-Feb-25"
    with pytest.raises(PivotResultError, match="No date in the table matches"):
        resolve_period("2030 P1", table)


def test_resolve_period_accepts_an_exact_rendered_date_label():
    table = parse_pivot_text(DATES_AS_ROWS)

    assert resolve_period("15-Jan-25", table) == "15-Jan-25"


def test_resolve_period_refuses_tables_without_a_date_axis():
    table = parse_pivot_text(NO_DATE_AXIS)

    assert table.date_axis is None
    with pytest.raises(PivotResultError, match="no date axis"):
        resolve_period("2025 P1", table)


def test_no_date_axis_table_cannot_silently_become_key_measures():
    with pytest.raises(PivotResultError, match="no date axis"):
        parse_pivot_text(NO_DATE_AXIS).to_key_measures()


def test_answer_from_pivot_tables_uses_two_member_terms_when_no_period_exists():
    tables = {"Spend (RMB 000)": parse_pivot_text(NO_DATE_AXIS)}

    answer = answer_from_pivot_tables(tables, ["Gold kiwifruit", "Hyper"], None)

    assert "1,234" in answer
    assert "Gold kiwifruit" in answer


def test_answer_from_pivot_tables_uses_resolved_period():
    tables = {"Spend (RMB 000)": parse_pivot_text(PRODUCTS_AS_ROWS)}

    answer = answer_from_pivot_tables(tables, ["Gold kiwifruit"], "15-Feb-25")

    assert "1,300" in answer
