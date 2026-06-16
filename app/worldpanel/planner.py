from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re
from typing import Any

from app.assistant import AssistantClient
from app.worldpanel.pivot_models import AxisPlacement, MemberNode, MemberSelection, QueryPlan, normalize


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
    return QueryPlan(
        report_set=report_set,
        report=report,
        axis_placements=axes,
        member_selections=tuple(selections),
        kpis=tuple(str(value) for value in payload.get("kpis", ["Spend (RMB 000)"])),
        expected_period=str(payload["expected_period"]) if payload.get("expected_period") else None,
        output_shape=payload.get("output_shape", "single_value"),
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
    period = re.search(r"20\d{2}\s*[Pp]\s*\d{1,2}|20\d{2}\s*全年", question)
    return {
        "axis_placements": axes,
        "member_selections": [],
        "kpis": kpis or ["Spend (RMB 000)"],
        "expected_period": period.group(0) if period else None,
        "output_shape": "trend" if "trend" in question.casefold() or "趋势" in question else "single_value",
        "planner_fallback": True,
    }


def _planner_prompt(question: str) -> str:
    return (
        "Return JSON only. Extract tentative Worldpanel dimensions, member labels, KPI, period, "
        "axis placement, and output shape. Do not invent member paths.\nQuestion: "
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
