from app.worldpanel.multitable import MultiKpiTable
from app.worldpanel.parser import parse_key_measures_text
from app.worldpanel.query import answer_question, interpret_question


SAMPLE_TEXT = """
Key Measures Data Table
Fresh Food - Update
Spend (RMB 000)
Product % of Category Value
Fruit
Apple
Kiwifruit
Gold kiwifruit
Green kiwifruit

17-Apr-26
529,745,400
71,702,500
27,629,070
15,208,550
7,368,896
"""


def test_interpret_question_extracts_product_date_and_metric_from_chinese():
    parsed = interpret_question("查一下 2026 年 4 月 Kiwifruit 的 Spend 是多少？")

    assert parsed.product == "Kiwifruit"
    assert parsed.year == 2026
    assert parsed.month == 4
    assert parsed.metrics == ["Spend (RMB 000)"]


def test_interpret_question_extracts_multiple_chinese_metrics():
    parsed = interpret_question("可口可乐Coke TM在2025年全年的销量，销额，渗透率是多少？")

    assert parsed.metrics == ["Spend (RMB 000)", "Volume (000 kg)", "Penetration %"]


def test_interpret_question_extracts_unicode_escaped_chinese_metrics_and_full_year():
    parsed = interpret_question(
        "\u53ef\u53e3\u53ef\u4e50Coke TM\u57282025\u5e74\u5168\u5e74"
        "\u7684\u9500\u91cf\uff0c\u9500\u989d\uff0c\u6e17\u900f\u7387\u662f\u591a\u5c11\uff1f",
        ["Coke TM"],
    )

    assert parsed.product == "Coke TM"
    assert parsed.year == 2025
    assert parsed.full_year is True
    assert parsed.metrics == ["Spend (RMB 000)", "Volume (000 kg)", "Penetration %"]


def test_answer_question_returns_matching_value():
    table = parse_key_measures_text(SAMPLE_TEXT)

    answer = answer_question("Gold kiwifruit 在 17-Apr-26 的销售额是多少？", table)

    assert "Gold kiwifruit" in answer.text
    assert "17-Apr-26" in answer.text
    assert "15,208,550" in answer.text


def test_answer_question_matches_product_from_current_table_instead_of_defaulting():
    coke_text = """
Key Measures Data Table
Coke - Update
Spend (RMB 000)
STD
52 w/e
12 w/e
4 w/e
YTD
Coke
Coke Regular
Coke Zero

20-Mar-26
100,000
60,000
40,000
"""
    table = parse_key_measures_text(coke_text)

    answer = answer_question("Coke 2026P3的销售额是多少？", table)

    assert "Coke 在 20-Mar-26" in answer.text
    assert "100,000" in answer.text


def test_answer_question_matches_coke_short_name_to_current_coke_product():
    coke_text = """
Key Measures Data Table
Coke - Update
Spend (RMB 000)
STD
52 w/e
12 w/e
4 w/e
YTD
TTL SPKL
TCCC SPKL
Coke TM
Coke Regular
Coke Zero

20-Mar-26
500,000
300,000
100,000
60,000
40,000
"""
    table = parse_key_measures_text(coke_text)

    answer = answer_question("Coke 2026P3的销售额是多少？", table)

    assert "Coke TM 在 20-Mar-26" in answer.text
    assert "100,000" in answer.text


def test_answer_question_uses_year_end_for_full_year():
    coke_text = """
Key Measures Data Table
Coke - Update
Spend (RMB 000)
STD
52 w/e
12 w/e
4 w/e
YTD
Coke TM

21-Mar-25
1,000

26-Dec-25
4,000

20-Mar-26
5,021
"""
    table = parse_key_measures_text(coke_text)

    answer = answer_question("可口可乐Coke TM在2025年全年的销额是多少？", table)

    assert "26-Dec-25" in answer.text
    assert "4,000" in answer.text


def test_answer_question_reports_missing_metrics_instead_of_using_spend_for_everything():
    coke_text = """
Key Measures Data Table
Coke - Update
Spend (RMB 000)
STD
52 w/e
12 w/e
4 w/e
YTD
Coke TM

26-Dec-25
4,000
"""
    table = parse_key_measures_text(coke_text)

    answer = answer_question("可口可乐Coke TM在2025年全年的销量，销额，渗透率是多少？", table)

    assert "销额" in answer.text
    assert "4,000" in answer.text
    assert "销量" in answer.text
    assert "渗透率" in answer.text
    assert "当前读取的报表只包含 Spend (RMB 000)" in answer.text


def test_answer_question_reads_multiple_kpis_from_data_explorer_tables():
    spend = parse_key_measures_text(
        """
Key Measures Data Table
Coke - Update
Spend (000000 RMB)
Coke TM

26-Dec-25
4,000
""",
        metric_override="Spend (000000 RMB)",
    )
    volume = parse_key_measures_text(
        """
Key Measures Data Table
Coke - Update
Volume (000000 L)
Coke TM

26-Dec-25
12,000
""",
        metric_override="Volume (000000 L)",
    )
    penetration = parse_key_measures_text(
        """
Key Measures Data Table
Coke - Update
Penetration %
Coke TM

26-Dec-25
35
""",
        metric_override="Penetration %",
    )
    table = MultiKpiTable(
        {
            "Spend (000000 RMB)": spend,
            "Volume (000000 L)": volume,
            "Penetration %": penetration,
        }
    )

    answer = answer_question("可口可乐Coke TM在2025年全年的销量，销额，渗透率是多少？", table)

    assert "销额：4,000" in answer.text
    assert "销量：12,000" in answer.text
    assert "渗透率：35" in answer.text


def test_answer_question_tells_user_when_requested_product_is_not_in_current_table():
    category_text = """
Key Measures Data Table
Coke - Category
Spend (RMB 000)
STD
52 w/e
12 w/e
4 w/e
YTD
NARTD
SPKL
P.Water

26-Dec-25
10,000
4,000
2,000
"""
    table = parse_key_measures_text(category_text)

    try:
        answer_question("可口可乐Coke TM在2025年全年的销额是多少？", table)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected ValueError when Coke TM is not in the current table")

    assert "当前已读取报表中没有" in message
    assert "NARTD、SPKL、P.Water" in message
