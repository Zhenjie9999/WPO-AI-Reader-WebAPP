from app.worldpanel.client import _exact_label_index


def test_exact_label_index_does_not_match_longer_category_prefix():
    labels = [
        "CN - PCC1 - TOTAL",
        "CN - PCC1 - TOTAL - KG",
    ]

    assert _exact_label_index(labels, "CN - PCC1 - TOTAL") == 0


def test_exact_label_index_cleans_whitespace_before_matching():
    labels = [
        "  CN - PCC1 - TOTAL  ",
        "CN - PCC1 - TOTAL - KG",
    ]

    assert _exact_label_index(labels, "CN - PCC1 - TOTAL") == 0


def test_exact_label_index_rejects_partial_match():
    labels = [
        "CN - PCC1 - TOTAL - KG",
    ]

    assert _exact_label_index(labels, "CN - PCC1 - TOTAL") is None
