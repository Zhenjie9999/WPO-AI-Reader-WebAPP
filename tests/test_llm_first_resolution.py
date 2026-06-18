"""Regression tests for LLM-first member resolution.

When the AI planner is enabled it returns natural-language product *terms*
(not hierarchy paths); the service must resolve those against the live,
fully-enumerated member tree (the data behind each Pivot '+'), not trust the
model's guessed paths or the flaky selector search box.
"""
from app.worldpanel.pivot_service import _build_plan, _member_match_length


def test_llm_product_term_resolves_member_behind_plus():
    # The model returns the English term; it must match the live member label
    # even if the raw question text never literally contained it.
    assert _member_match_length("Cherry", "今年5月卖了多少", ("cherry",)) > 0
    assert _member_match_length("Durian", "2026 May sales", ("Durian",)) > 0
    # Chinese alias still works without any LLM term.
    assert _member_match_length("Durian", "榴莲销额", ()) > 0
    # Unrelated member is not matched by an unrelated term.
    assert _member_match_length("Apple", "2026 May", ("cherry",)) == 0


def test_build_plan_uses_resolved_live_member_paths():
    tentative = {
        "products": ["cherry"],
        "kpis": ["Spend (RMB 000)"],
        "expected_period": "2026 May",
        "calculation": None,
        "filters": [],
        "output_shape": "single_value",
    }
    # Member path is the live path resolved from the tree (what the executor checks).
    members = [{"dimension": "Product", "member_path": ["Fruit", "Cherry"], "checked": True}]
    plan = _build_plan(tentative, members, report_set="CN - Zespri - CS", report="Data Explorer")
    assert plan.member_selections[0].member_path == ("Fruit", "Cherry")
    assert plan.kpis == ("Spend (RMB 000)",)
    assert plan.expected_period == "2026 May"
