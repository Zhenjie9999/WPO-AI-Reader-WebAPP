from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from app.assistant import AssistantClient
from app.worldpanel.pivot_models import (
    AxisPlacement,
    FilterSelection,
    MemberNode,
    MemberSelection,
    QueryPlan,
    normalize,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanClarification:
    dimension: str
    question: str
    candidates: tuple[tuple[str, ...], ...]


_YOY_PERCENT = "Yr on Yr % Change"
_POP_PERCENT = "Period on Period % Change~"


def calculation_clarification_for_question(question: str) -> PlanClarification | None:
    if not _has_ambiguous_growth_intent(question):
        return None
    return PlanClarification(
        dimension="calculation",
        question=(
            "你提到增长率，但还需要确认比较口径：请选择同比（与去年同期相比）"
            "还是环比（与上一周期相比）。\n"
            "Which growth-rate basis should I use: year-on-year or period-on-period?"
        ),
        candidates=((_YOY_PERCENT,), (_POP_PERCENT,)),
    )


def _has_ambiguous_growth_intent(question: str) -> bool:
    if _detect_calculation(question):
        return False
    lowered = question.casefold()
    ambiguous_tokens = (
        "growth rate",
        "change rate",
        "% change",
        "rate of growth",
        "增长率",
        "增长",
        "增幅",
        "变化率",
    )
    return any(token in lowered for token in ambiguous_tokens)


def compile_query_plan(
    payload: dict[str, Any],
    *,
    report_set: str,
    report: str,
    discovered: dict[str, tuple[MemberNode, ...]],
) -> QueryPlan | PlanClarification:
    selections: list[MemberSelection] = []
    for request in payload.get("member_selections", []):
        dimension = str(request.get("dimension") or "")
        requested_path = tuple(str(part) for part in request.get("member_path", []))
        candidates = discovered.get(dimension, ())
        exact_matches = [
            node
            for node in candidates
            if tuple(normalize(part) for part in node.path) == tuple(normalize(part) for part in requested_path)
        ]
        matches = exact_matches or [
            node for node in candidates if normalize(node.label) == normalize(requested_path[-1] if requested_path else "")
        ]
        if len(matches) != 1:
            return PlanClarification(
                dimension=dimension,
                question=f"Please choose an exact {dimension} member.",
                candidates=tuple(node.path for node in matches or candidates[:12]),
            )
        selections.append(
            MemberSelection(
                dimension=dimension,
                member_path=matches[0].path,
                checked=bool(request.get("checked", True)),
            )
        )

    axes = tuple(
        AxisPlacement(
            dimension=str(item["dimension"]),
            axis=str(item["axis"]),  # type: ignore[arg-type]
            position=int(item.get("position", 0)),
        )
        for item in payload.get("axis_placements", [])
        if item.get("dimension") and item.get("axis") in {"row", "column", "filter", "available"}
    )
    filters = tuple(
        FilterSelection(role=str(item["role"]), value=str(item["value"]))
        for item in payload.get("filters", [])
        if isinstance(item, dict) and item.get("role") and item.get("value")
    )
    return QueryPlan(
        report_set=report_set,
        report=report,
        axis_placements=axes,
        member_selections=tuple(selections),
        kpis=tuple(str(value) for value in payload.get("kpis", ["Spend (RMB 000)"])),
        expected_period=str(payload["expected_period"]) if payload.get("expected_period") else None,
        output_shape=payload.get("output_shape", "single_value"),
        calculation=str(payload["calculation"]) if payload.get("calculation") else None,
        filters=filters,
    )


class StructuredPlanner:
    def __init__(self, assistant: AssistantClient | None = None):
        self.assistant = assistant

    async def tentative_plan(self, question: str) -> dict[str, Any]:
        if self.assistant is None:
            return _local_tentative_plan(question)
        try:
            response = await self.assistant.chat(_planner_prompt(question))
            payload = _extract_json(response)
            payload.setdefault("planner_mode", "ai")
            return payload
        except Exception as exc:
            logger.warning(
                "Configured AI planner failed (%s: %s); falling back to local rule-based planning",
                type(exc).__name__,
                exc,
            )
            fallback = _local_tentative_plan(question)
            fallback["planner_mode"] = "fallback"
            fallback["ai_error"] = f"{type(exc).__name__}: {exc}"
            return fallback


def _local_tentative_plan(question: str) -> dict[str, Any]:
    axes = []
    for dimension, axis in re.findall(
        r"(Product|Period|KPI|Measures|Outlet|Channel)\s+(?:on|in|放在)\s+(Row|Column|Filter)",
        question,
        flags=re.IGNORECASE,
    ):
        axes.append({"dimension": dimension.title(), "axis": axis.casefold(), "position": 0})
    # Breakdown intent ("分渠道" / "by channel"): place that dimension on a
    # column so every member is shown, mirroring the LLM planner's behaviour.
    axes += _detect_breakdown_axes(question)
    # Superlative intent ("渗透率最高的品牌"): rank members of a dimension, so
    # that dimension must also be spread across an axis like a breakdown.
    ranking = _detect_ranking(question)
    if ranking and ranking["dimension"] and not any(
        a["dimension"] == ranking["dimension"] for a in axes
    ):
        axes.append({"dimension": ranking["dimension"], "axis": "column", "position": 0})
    kpis = []
    for name, aliases in (
        ("Spend (RMB 000)", ("spend", "sales value", "销售额")),
        ("Volume (000 kg)", ("volume", "销量")),
        ("Penetration %", ("penetration", "渗透率")),
    ):
        if any(alias.casefold() in question.casefold() for alias in aliases):
            kpis.append(name)
    period = _detect_period(question)
    calculation = _detect_calculation(question)
    filters = _detect_filters(question)
    return {
        "axis_placements": axes,
        "member_selections": [],
        "products": [],
        "kpis": kpis or ["Spend (RMB 000)"],
        "expected_period": period,
        "calculation": calculation,
        "filters": filters,
        "ranking": ranking,
        "output_shape": (
            "ranking" if ranking
            else "table" if axes
            else "trend" if "trend" in question.casefold() or "趋势" in question
            else "single_value"
        ),
        "planner_fallback": True,
        "planner_mode": "fallback",
    }


# Superlative phrases. The dimension words reuse the same natural names the
# breakdown patterns emit (Channel/Geography/Product); "品牌/brand" maps to
# Brand so the service can prefer a live Brand/Manufacturer dimension and fall
# back to Product children when the report has none.
_RANKING_MAX = ("最高", "最大", "最多", "哪个最", "谁最", "第一名", "highest", "largest", "biggest", "top")
_RANKING_MIN = ("最低", "最小", "最少", "lowest", "smallest", "bottom")
_RANKING_LIST = ("排名", "排行", "排序", "ranking", "rank")
_RANKING_DIMENSION_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Brand", ("品牌", "brand")),
    ("Channel", ("渠道", "业态", "channel", "outlet")),
    ("Geography", ("地区", "区域", "市场", "region", "city", "省份", "城市")),
    ("Product", ("品类", "产品", "category", "product", "sku")),
)


def _detect_ranking(question: str) -> dict[str, Any] | None:
    lowered = question.casefold()
    is_min = any(word in lowered for word in _RANKING_MIN)
    is_max = any(word in lowered for word in _RANKING_MAX)
    is_list = any(word in lowered for word in _RANKING_LIST)
    if not (is_min or is_max or is_list):
        return None
    match = re.search(r"前\s*(\d{1,2})|top\s*(\d{1,2})|前十|top\s*ten", lowered)
    if match:
        digits = match.group(1) or match.group(2)
        top_n = int(digits) if digits else 10
    else:
        top_n = 5 if is_list and not (is_min or is_max) else 1
    dimension = ""
    for name, words in _RANKING_DIMENSION_WORDS:
        if any(word in lowered for word in words):
            dimension = name
            break
    return {"dimension": dimension, "direction": "min" if is_min and not is_max else "max", "top_n": top_n}


# Phrases that mean "show this value distributed across every member of a
# dimension" (a breakdown), mapped to the natural dimension name the resolver
# then maps onto the live dimension (Channel -> Outlet, etc.).
_BREAKDOWN_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Channel", ("分渠道", "各渠道", "按渠道", "分业态", "各业态", "by channel", "per channel", "across channels", "by outlet")),
    ("Geography", ("分地区", "各地区", "按地区", "分区域", "各区域", "分市场", "各市场", "by region", "by geography", "across regions", "by market")),
    ("Product", ("分品类", "各品类", "按品类", "分品牌", "各品牌", "分产品", "各产品", "by category", "by product", "by brand", "across categories")),
)


def _detect_breakdown_axes(question: str) -> list[dict[str, Any]]:
    lowered = question.casefold()
    axes: list[dict[str, Any]] = []
    for dimension, phrases in _BREAKDOWN_PATTERNS:
        if any(phrase.casefold() in lowered for phrase in phrases):
            axes.append({"dimension": dimension, "axis": "column", "position": 0})
    return axes


_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"


def _detect_period(question: str) -> str | None:
    lowered = question
    match = re.search(r"20\d{2}\s*[Pp]\s*\d{1,2}", lowered)
    if match:
        return match.group(0)
    match = re.search(r"20\d{2}\s*年\s*\d{1,2}\s*月", lowered)
    if match:
        return match.group(0)
    match = re.search(rf"({_MONTHS})[a-z]*\.?\s*20\d{{2}}", lowered, flags=re.IGNORECASE)
    if match:
        return match.group(0)
    match = re.search(rf"20\d{{2}}\s*({_MONTHS})[a-z]*", lowered, flags=re.IGNORECASE)
    if match:
        return match.group(0)
    match = re.search(r"20\d{2}\s*全年", lowered)
    if match:
        return match.group(0)
    return None


# Canonical WPO option labels. The LLM phrases KPIs/calculations freely
# (e.g. "Sales Amount", "Year on Year % Change"), so map them to the exact
# labels the report dropdowns expose before the executor selects them.
_KPI_CANON: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Spend (RMB 000)", ("spend", "sales", "amount", "value", "销额", "销售额", "销售金额", "金额")),
    ("Volume (000 kg)", ("volume", "销量", "销售量")),
    ("Penetration %", ("penetration", "渗透")),
    ("Buyers (000)", ("buyers", "buyer", "购买人数", "购买者", "买家")),
    ("Frequency", ("frequency", "频次", "购买频次")),
)


def canonical_kpi(term: str) -> str:
    n = normalize(term)
    if not n:
        return term
    for canon, aliases in _KPI_CANON:
        if any(normalize(alias) in n for alias in aliases):
            return canon
    return term


def canonical_calculation(term: str | None) -> str | None:
    if not term:
        return None
    detected = _detect_calculation(term)
    if detected:
        return detected
    n = normalize(term)
    if "actual" in n or "实际" in n:
        return "Actual Yr on Yr"
    return term


def _detect_calculation(question: str) -> str | None:
    lowered = question.casefold()
    if any(token in lowered for token in ("yr on yr difference", "\u540c\u6bd4\u5dee\u5f02", "yoy difference")):
        return "Yr on Yr Difference"
    if any(
        token in lowered
        for token in (
            "period on period %",
            "period on period growth",
            "period-on-period",
            "pop growth",
            "month on month",
            "mom growth",
            "vs previous period",
            "\u73af\u6bd4\u589e\u957f",
            "\u73af\u6bd4",
        )
    ):
        return "Period on Period % Change~"
    if any(
        token in lowered
        for token in (
            "yr on yr %",
            "yoy %",
            "year on year growth",
            "yr on yr growth",
            "yoy growth",
            "year-on-year",
            "year on year",
            "yr on yr",
            "yoy",
            "vs last year",
            "vs ly",
            "\u540c\u6bd4\u589e\u957f",
            "\u540c\u6bd4",
            "\u53bb\u5e74\u540c\u671f",
        )
    ):
        return "Yr on Yr % Change"
    return None


def _detect_filters(question: str) -> list[dict[str, str]]:
    lowered = question.casefold()
    filters: list[dict[str, str]] = []
    for value, aliases in (
        ("Hypermarket", ("hypermarket", "hyper", "大卖场")),
        ("CVS", ("cvs", "便利店")),
        ("Ecommerce", ("ecommerce", "电商", "线上")),
        ("Supermarket", ("supermarket", "超市")),
    ):
        if any(alias in lowered for alias in aliases):
            filters.append({"role": "channel", "value": value})
            break
    duration = _detect_duration(question)
    if duration:
        filters.append({"role": "duration", "value": duration})
    return filters


# Duration options are mutually exclusive and easy to confuse (STD vs YTD are
# different things). Match each precisely with word boundaries and never let one
# alias bleed into another.
_DURATION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("YTD", (r"\bytd\b", "年初至今", "年累计", "今年以来", "年初至本期")),
    ("52 w/e", (r"\b52\s*w/?e\b", r"\bmat\b", "rolling year", "滚动年", "52周")),
    ("12 w/e", (r"\b12\s*w/?e\b", "12周", "近12周")),
    ("4 w/e", (r"\b4\s*w/?e\b", "4周", "近4周", "最新一期")),
    ("STD", (r"\bstd\b", "single period", "单期", "标准期", "当期")),
)


def _detect_duration(question: str) -> str | None:
    lowered = question.casefold()
    for value, patterns in _DURATION_PATTERNS:
        for pattern in patterns:
            is_regex = pattern.startswith(r"\b") or "\\s" in pattern
            if (re.search(pattern, lowered) if is_regex else pattern in lowered):
                return value
    return None


def _planner_prompt(question: str) -> str:
    return (
        "Return JSON only. Extract the user's Worldpanel data intent. "
        "Schema: {\"products\": [string], \"kpis\": [string], \"expected_period\": string|null, "
        "\"calculation\": string|null, \"filters\": [{\"role\": string, \"value\": string}], "
        "\"axis_placements\": [{\"dimension\": string, \"axis\": string, \"position\": int}], "
        "\"ranking\": {\"dimension\": string, \"direction\": \"max\"|\"min\", \"top_n\": int}|null, "
        "\"output_shape\": string}. "
        "Put product/category/brand names the user mentions into \"products\" as plain natural-language "
        "terms (translate Chinese to the English product name when you can, e.g. 榴莲->Durian, "
        "车厘子->Cherry, 金果->Gold kiwifruit); do NOT guess exact hierarchy paths — the system resolves "
        "them against the live member tree. "
        "Use \"calculation\": \"Yr on Yr % Change\" only when the user explicitly asks for "
        "同比 / year-on-year / YoY / vs last year / 去年同期. "
        "Use \"calculation\": \"Period on Period % Change~\" only when the user explicitly asks for "
        "环比 / period-on-period / month-on-month / vs previous period. "
        "If the user only says growth rate / 增长率 / % change without the comparison basis, "
        "leave \"calculation\" null so the app can ask a clarification question. "
        "Use \"filters\": [{\"role\": \"channel|duration|geography\", \"value\": \"...\"}] only to pin "
        "ONE specific value the user named (e.g. 'in CVS' -> channel=CVS, '华东地区' -> geography=华东). "
        "The duration value must be exactly one of STD, 52 w/e, 12 w/e, 4 w/e, or YTD — these are "
        "distinct (STD is the standard/single period, YTD is year-to-date); never substitute one for another. "
        "IMPORTANT — breakdown vs filter: when the user wants a value DISTRIBUTED ACROSS every member "
        "of a dimension rather than one value (phrases like 分渠道/各渠道/按渠道/分品类/各品类/分地区/各地区/"
        "分品牌, or English 'by channel', 'by category', 'across regions', 'breakdown by', 'distribution by', "
        "'split by', 'per channel', 'respectively'), do NOT use filters. Instead emit an axis_placement "
        "{\"dimension\": <that dimension>, \"axis\": \"column\", \"position\": 0} so all members of that "
        "dimension are shown side by side, and set \"output_shape\": \"table\". Map the dimension to its "
        "natural name (渠道/channel -> Channel, 地区/region -> Geography, 品类/category/品牌/brand -> Product); "
        "the system resolves it to the live dimension. "
        "IMPORTANT — superlative/ranking questions: when the user asks WHICH member is highest/lowest "
        "or for a top-N ranking (最高/最低/最大/最小/最多/最少/排名/排行/前N名/哪个最/谁最, or English "
        "'highest', 'lowest', 'top 5', 'best', 'which brand has the most'), additionally emit "
        "\"ranking\": {\"dimension\": <dimension being ranked, e.g. Brand/Product/Channel>, "
        "\"direction\": \"max\"|\"min\", \"top_n\": int} and set \"output_shape\": \"ranking\". "
        "Also emit the axis_placement for that dimension as above, and put the scoping category "
        "(e.g. 牙膏 in '牙膏渗透率最高的品牌') into \"products\" so the system restricts the ranking "
        "to that category's members. "
        "Do not invent member paths.\nQuestion: "
        + question
    )


def _extract_json(value: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", value, flags=re.DOTALL)
    if not match:
        raise ValueError("Planner response did not contain JSON")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Planner response must be a JSON object")
    return payload
