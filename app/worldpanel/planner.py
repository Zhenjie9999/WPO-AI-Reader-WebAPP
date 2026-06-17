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
            return _extract_json(response)
        except Exception as exc:
            logger.warning(
                "Configured AI planner failed (%s: %s); falling back to local rule-based planning",
                type(exc).__name__,
                exc,
            )
            return _local_tentative_plan(question)


def _local_tentative_plan(question: str) -> dict[str, Any]:
    axes = []
    for dimension, axis in re.findall(
        r"(Product|Period|KPI|Measures|Outlet|Channel)\s+(?:on|in|放在)\s+(Row|Column|Filter)",
        question,
        flags=re.IGNORECASE,
    ):
        axes.append({"dimension": dimension.title(), "axis": axis.casefold(), "position": 0})
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
        "kpis": kpis or ["Spend (RMB 000)"],
        "expected_period": period,
        "calculation": calculation,
        "filters": filters,
        "output_shape": "trend" if "trend" in question.casefold() or "趋势" in question else "single_value",
        "planner_fallback": True,
    }


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


def _detect_calculation(question: str) -> str | None:
    lowered = question.casefold()
    if any(token in lowered for token in ("yr on yr %", "yoy %", "同比增长", "增长率", "growth rate", "% change", "yoy growth")):
        return "Yr on Yr % Change"
    if any(token in lowered for token in ("yr on yr difference", "同比差异", "yoy difference")):
        return "Yr on Yr Difference"
    if any(token in lowered for token in ("period on period %", "环比增长", "环比")):
        return "Period on Period % Change~"
    if any(token in lowered for token in ("同比", "year on year", "yr on yr", "yoy", "vs last year", "vs ly", "去年同期")):
        # Default same-period-last-year intent to the percentage growth view.
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
        "Return JSON only. Extract tentative Worldpanel dimensions, member labels, KPI, period, "
        "axis placement, output shape, calculation, and filters. "
        "Use \"calculation\": \"Yr on Yr % Change\" for growth-rate / 同比增长 / vs last year questions. "
        "Use \"filters\": [{\"role\": \"channel|duration|geography\", \"value\": \"...\"}] for "
        "channel, duration, or region filters. The duration value must be exactly one of "
        "STD, 52 w/e, 12 w/e, 4 w/e, or YTD — these are distinct (STD is the standard/single "
        "period, YTD is year-to-date); never substitute one for another. "
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
