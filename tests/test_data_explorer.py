from app.worldpanel.data_explorer import (
    Clarification,
    DataExplorerCache,
    DataExplorerContext,
    DataExplorerDimension,
    DataExplorerOption,
    DataExplorerSegment,
    QuerySpec,
    discover_controls_from_html,
    parse_query_spec,
    plan_query,
)
from app.worldpanel.parser import parse_key_measures_text


def test_parse_query_spec_extracts_cross_filters_from_natural_language():
    spec = parse_query_spec(
        "\u53ef\u53e3\u53ef\u4e50 Coke TM \u5728 2025 \u5168\u5e74\uff0c"
        "\u6e20\u9053 Hyper\uff0c\u54c1\u7c7b Sparkling \u7684\u9500\u91cf\u548c\u6e17\u900f\u7387\u662f\u591a\u5c11\uff1f"
    )

    assert spec.products == ["Coke TM"]
    assert spec.metrics == ["Volume (000 kg)", "Penetration %"]
    assert spec.year == 2025
    assert spec.full_year is True
    assert spec.dimensions["channel"] == "Hyper"
    assert spec.dimensions["category"] == "Sparkling"


def test_parse_query_spec_extracts_real_unicode_chinese_metrics_and_filters():
    spec = parse_query_spec(
        "\u53ef\u53e3\u53ef\u4e50Coke TM\u57282025\u5e74\u5168\u5e74"
        "Hyper\u6e20\u9053Sparkling\u54c1\u7c7b\u7684\u9500\u91cf\u3001"
        "\u9500\u989d\u3001\u6e17\u900f\u7387\u662f\u591a\u5c11\uff1f"
    )

    assert spec.products == ["Coke TM"]
    assert spec.metrics == ["Spend (RMB 000)", "Volume (000 kg)", "Penetration %"]
    assert spec.year == 2025
    assert spec.full_year is True
    assert spec.dimensions == {"channel": "Hyper", "category": "Sparkling"}


def test_plan_query_requests_clarification_for_missing_required_dimensions():
    context = DataExplorerContext(
        report_set="CN - Coca Cola - CS",
        report_name="Data Explorer",
        report_parameter="x=1",
        required_dimensions=("channel",),
        dimensions={
            "channel": DataExplorerDimension(
                key="channel",
                label="Channel",
                control_id="channel-select",
                current="All",
                options=(
                    DataExplorerOption(label="All", value="all"),
                    DataExplorerOption(label="Hyper", value="hyper"),
                    DataExplorerOption(label="CVS", value="cvs"),
                ),
            )
        },
    )

    planned = plan_query(parse_query_spec("Coke TM 2025 \u5168\u5e74\u9500\u989d"), context)

    assert isinstance(planned, Clarification)
    assert planned.dimension_key == "channel"
    assert planned.question == "\u8bf7\u9009\u62e9 Channel"
    assert [option.label for option in planned.options] == ["All", "Hyper", "CVS"]


def test_plan_query_requests_clarification_for_unavailable_dimension_value():
    context = DataExplorerContext(
        report_set="CN - Coca Cola - CS",
        report_name="Data Explorer",
        report_parameter="x=1",
        required_dimensions=("channel",),
        dimensions={
            "channel": DataExplorerDimension(
                key="channel",
                label="Channel",
                control_id="channel-select",
                current="All",
                options=(
                    DataExplorerOption(label="Total Outlet", value="total"),
                    DataExplorerOption(label="CVS", value="cvs"),
                ),
            )
        },
    )
    spec = QuerySpec(
        products=("Coke TM",),
        metrics=("Spend (RMB 000)",),
        year=2025,
        full_year=True,
        dimensions={"channel": "Hyper"},
    )

    planned = plan_query(spec, context)

    assert isinstance(planned, Clarification)
    assert planned.dimension_key == "channel"
    assert [option.label for option in planned.options] == ["Total Outlet", "CVS"]


def test_plan_query_requests_clarification_for_unavailable_product_value():
    context = DataExplorerContext(
        report_set="CN - Coca Cola - CS",
        report_name="Data Explorer",
        report_parameter="x=1",
        required_dimensions=(),
        dimensions={
            "product": DataExplorerDimension(
                key="product",
                label="Product",
                control_id="product-select",
                current="TTL SPKL",
                options=(
                    DataExplorerOption(label="TTL SPKL", value="ttl"),
                    DataExplorerOption(label="TCCC SPKL", value="tccc"),
                ),
            )
        },
    )
    spec = QuerySpec(
        products=("Coke TM",),
        metrics=("Spend (RMB 000)",),
        year=2025,
        full_year=True,
    )

    planned = plan_query(spec, context)

    assert isinstance(planned, Clarification)
    assert planned.dimension_key == "product"
    assert [option.label for option in planned.options] == ["TTL SPKL", "TCCC SPKL"]


def test_cache_uses_stable_query_key_for_same_cross_filter():
    table = parse_key_measures_text(
        """
Key Measures Data Table
Coke
Spend (000000 RMB)
Coke TM

26-Dec-25
4,965
""",
        metric_override="Spend (000000 RMB)",
    )
    cache = DataExplorerCache()
    first = QuerySpec(
        products=("Coke TM",),
        metrics=("Spend (RMB 000)",),
        year=2025,
        full_year=True,
        dimensions={"channel": "Hyper", "category": "Sparkling"},
    )
    second = QuerySpec(
        products=("Coke TM",),
        metrics=("Spend (RMB 000)",),
        year=2025,
        full_year=True,
        dimensions={"category": "Sparkling", "channel": "Hyper"},
    )

    cache.set("CN - Coca Cola - CS", "Data Explorer", first, table)

    assert cache.get("CN - Coca Cola - CS", "Data Explorer", second) is table


def test_cache_persists_query_results_across_process_restarts(tmp_path):
    table = parse_key_measures_text(
        """
Key Measures Data Table
Coke
Spend (000000 RMB)
Coke TM

26-Dec-25
4,965
""",
        metric_override="Spend (000000 RMB)",
    )
    spec = QuerySpec(
        products=("Coke TM",),
        metrics=("Spend (RMB 000)",),
        year=2025,
        full_year=True,
        dimensions={"channel": "Hyper"},
    )
    cache_path = tmp_path / "query-cache.json"

    cache = DataExplorerCache(cache_path)
    cache.set("CN - Coca Cola - CS", "Data Explorer", spec, table)

    restored = DataExplorerCache(cache_path)
    restored_table = restored.get("CN - Coca Cola - CS", "Data Explorer", spec)

    assert restored.size == 1
    assert restored_table is not None
    assert restored_table.value_for("Coke TM", "26-Dec-25") == 4_965


def test_discover_controls_from_html_reads_select_dimensions_and_pivot_segments():
    html = """
    <html><body>
      <select id="kpi"><option selected>Spend (000000 RMB)</option><option>Volume (000000 L)</option></select>
      <select id="product"><option>TTL SPKL</option><option selected>Coke TM</option></select>
      <select id="period"><option selected>52 w/e</option><option>YTD</option></select>
      <select id="category"><option selected>Sparkling</option><option>NARTD</option></select>
      <select id="channel"><option selected>All</option><option>Hyper</option></select>
      <select id="classification"><option selected>Manufacturer</option><option>Pack Type</option></select>
      <input id="PivotButton1" type="button" value="pivot screen" />
      <span id="seg-1" onclick="expandSegment('Coke Zero')">+ Coke Zero</span>
    </body></html>
    """

    controls = discover_controls_from_html(html)

    assert [dimension.key for dimension in controls.dimensions][:6] == [
        "kpi",
        "product",
        "period",
        "category",
        "channel",
        "classification",
    ]
    assert controls.pivot_button_id == "PivotButton1"
    assert controls.segments[0].label == "Coke Zero"


def test_discover_controls_from_html_reads_tree_style_pivot_segments():
    html = """
    <html><body>
      <a id="PivotButton1" title="Pivot Screen">Pivot Screen</a>
      <div class="rtPlus"></div><span data-node-id="n1">Coke TM</span>
      <a href="#" onclick="TreeView_Select('n2')">Coke Zero</a>
      <input type="button" value="+ Sprite" />
    </body></html>
    """

    controls = discover_controls_from_html(html)

    assert [segment.label for segment in controls.segments] == ["Coke TM", "Coke Zero", "Sprite"]


def test_discover_controls_from_html_ignores_encoded_hidden_segment_noise():
    html = """
    <html><body>
      <input type="button" value="+ tT41hbNEsIW+6rjg4E7BdTfLV2MuOJ00wcnNFZA6GZX/bEH5o0Hm8emIpzTnHlhddaaJDnRYpkdc9qUk6uj5fGRgzFJ8k00sJ8lBgiwFgRKku4cAcqsiIXUYcQuKD27TruHR07K0YBD/Ax8A/hqB1BZUHGooqizuvB8BRCGzvOKDF3gilX3WNpu6som7fo6d91BBCZL83XPChiylToBkBLJbzGcZTVPK7jvsfq2hS0P4+DxsjtNTKfl9WVtSuXntdWr7bWbv4g4fJkgaVElIFNeSNXAUnu9fUfQSkKBAcK0o3/dVV9Xvv3Q9hQPyr6CkiWgBhyANe5ugFFPeWo68e+ZrmP2qyGp5zzIdwWLRTt7T5388kp7Zw8SZg+QjSEgpn0dExakNa1Av6WIqqIWSqlyHXEJC2QZ7A6iU7dxLmXjTJcCXezYgqVtyq/ZSDWJL65/Q8OXMtnvtIXVueUeBkQBhO8IFl4Own2nFf4u5JPuxXJRUcZsdMwEZW4Ftsdwo+C9ryUi9+S7GaRTT5HZt5jOs1p8m8FNVz7bNJ35ZyzSneAe6qhxgMmCaCJpGJRjIsDGjiQofbwnlAXbXDIxEYH6oiH2WEpdGccdj6hCo7yC1xI4dSNsAXC68NcD6lKgIEMQOKNb8HqhlzLKwQLehiJxULlH+dNN2zzHaVChrX+XWe2/ZBZ4eDXNS1wyJfekDUdlmBFaztTavAGrOfqee7ddfW/dHxWjR4ldyv11ptchmvZXalxZ/wg/DOMEuCPFm6UWE4JanYfebO+sIhWfxg7Xguc8yXXfzpwmMW/cUKNjFdpkgzRLQJnlc2TMAyyo3I1q+hapgiqgOcl1sDurak70OTKlrzfHmhBb4bjy+AWIxgb2tbh7Ukc0ZTD+RVC9CJ9nEPtF4SuOB+b5BGlFqm+cCAG9UyEJLolvzd0VwmxHyYAq1uAuJxUDF7vgfo7BM39QhWzQCwkd9L8yBGw5r8t7lmO+YcMZrzZJwEL1esjl3fXMuLbH5K/DVzRT4Xx4tEIQJw4sxH7kFG9oSilk7ZVSFWmPwG1SwwzbSmnx1yciuJg7blqRCUDhRddEVbctZAKAB6X/4cpi3DvJ8dxsenRI0hbK313JYtfEj5B46sySkPyN7" />
      <span class="rtPlus"></span><span>Coke TM</span>
    </body></html>
    """

    controls = discover_controls_from_html(html)

    assert [segment.label for segment in controls.segments] == ["Coke TM"]


def test_plan_query_converts_product_to_segment_when_segment_exists():
    context = DataExplorerContext(
        report_set="CN - Coca Cola - CS",
        report_name="Data Explorer",
        report_parameter="x=1",
        required_dimensions=(),
        segments=(
            DataExplorerSegment(label="TTL SPKL", control_id="n1"),
        ),
    )

    planned = plan_query(
        QuerySpec(products=("TTL SPKL",), metrics=("Spend (RMB 000)",), year=2025, full_year=True),
        context,
    )

    assert isinstance(planned, QuerySpec)
    assert planned.products == ()
    assert planned.segments == ("TTL SPKL",)


def test_discover_controls_from_html_is_case_insensitive_for_legacy_markup():
    html = """
    <BODY>
      <SELECT ID="Measures"><OPTION SELECTED>Spend (000000 RMB)</OPTION><OPTION>Volume (000000 L)</OPTION></SELECT>
      <SELECT ID="Products"><OPTION SELECTED>Coke TM</OPTION></SELECT>
    </BODY>
    """

    controls = discover_controls_from_html(html)

    assert [dimension.key for dimension in controls.dimensions] == ["kpi", "product"]


def test_discover_controls_from_html_reads_pivot_row_column_slots_and_expandable_nodes():
    html = """
    <html><body>
      <a id="PivotButton1" title="Pivot Screen">Pivot Screen</a>
      <div id="PivotDialog">
        <div class="pivotColumn">
          <span>Column</span>
          <select id="columnDim"><option selected>Period</option><option>Product</option></select>
        </div>
        <div class="pivotRow">
          <span>Row</span>
          <select id="rowDim"><option selected>Product</option><option>Channel</option></select>
        </div>
        <span class="rtPlus" data-node-id="product-plus"></span><span>Product</span>
        <span class="rtPlus" data-node-id="period-plus"></span><span>Period</span>
      </div>
    </body></html>
    """

    controls = discover_controls_from_html(html)

    assert controls.pivot_slots["column"] == ("Period", "Product")
    assert controls.pivot_slots["row"] == ("Product", "Channel")
    assert [segment.label for segment in controls.segments] == ["Product", "Period"]
    assert [segment.control_id for segment in controls.segments] == ["product-plus", "period-plus"]
