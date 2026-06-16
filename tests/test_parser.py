from app.worldpanel.parser import parse_key_measures_text


SAMPLE_TEXT = """
Key Measures Data Table
Fresh Food - Update
Spend (RMB 000)
Product % of Category Value
Retailer % of Value
Fruit
Apple
Kiwifruit
Gold kiwifruit
Green kiwifruit

19-Apr-24
571,510,800
74,905,820
31,082,840
16,519,160
8,337,998

17-Apr-26
529,745,400
71,702,500
27,629,070
15,208,550
7,368,896
"""


def test_parse_key_measures_text_extracts_product_values_by_date():
    table = parse_key_measures_text(SAMPLE_TEXT)

    value = table.value_for(
        product="Kiwifruit",
        date_label="17-Apr-26",
        metric="Spend (RMB 000)",
    )

    assert value == 27_629_070


def test_parse_key_measures_text_keeps_available_products_and_dates():
    table = parse_key_measures_text(SAMPLE_TEXT)

    assert table.products == ["Fruit", "Apple", "Kiwifruit", "Gold kiwifruit", "Green kiwifruit"]
    assert table.dates == ["19-Apr-24", "17-Apr-26"]


def test_parse_key_measures_text_extracts_dynamic_product_headers_after_duration_block():
    text = """
Key Measures Data Table
Coke - Update
Spend (RMB 000)
Product % of Category Value
Total Market
National
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

    table = parse_key_measures_text(text)

    assert table.products == ["Coke", "Coke Regular", "Coke Zero"]
    assert table.value_for("Coke", "20-Mar-26") == 100_000
