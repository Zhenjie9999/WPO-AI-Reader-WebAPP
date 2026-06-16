from app.worldpanel.checker import check_table
from app.worldpanel.parser import parse_key_measures_text


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

15-May-26
1,500,000,000
70,000,000
-10
15,000,000
7,000,000
"""


def test_check_table_reports_quality_summary_and_issues():
    table = parse_key_measures_text(SAMPLE_TEXT)

    result = check_table(table)

    assert result.status == "warning"
    assert any(issue.kind == "negative_value" for issue in result.issues)
    assert any(issue.kind == "large_change" for issue in result.issues)
    assert "发现" in result.summary
