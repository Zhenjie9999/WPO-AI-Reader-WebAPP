from __future__ import annotations

import re
from typing import Any

from app.assistant import AISettings, AssistantClient
from app.config import Settings
from app.worldpanel.client import Credentials, WorldpanelError
from app.worldpanel.executor import ExecutionResult, QueryExecutor
from app.worldpanel.pivot_models import (
    DimensionMembers,
    DimensionTag,
    PivotDiscovery,
    QueryPlan,
)
from app.worldpanel.planner import PlanClarification, StructuredPlanner, compile_query_plan
from app.worldpanel.schema import SchemaService
from app.worldpanel.session import DataExplorerSession, open_persistent_data_explorer


class PivotQueryService:
    def __init__(self, settings: Settings, ai_settings: AISettings | None = None):
        self.settings = settings
        self.ai_settings = ai_settings or settings.ai

    async def plan(
        self,
        session: DataExplorerSession,
        credentials: Credentials,
        question: str,
        clarification: dict[str, Any] | None = None,
    ) -> QueryPlan | PlanClarification:
        report = self._report(session)
        driver = await open_persistent_data_explorer(
            session,
            settings=self.settings,
            credentials=credentials,
            report_set=report["report_set"],
            report_parameter=report["report_parameter"],
            report_name=report["report_name"],
        )
        await driver.open_pivot()
        schema = _schema_for(session, driver)
        assistant = AssistantClient(self.ai_settings) if self.ai_settings.enabled else None
        tentative = await StructuredPlanner(assistant).tentative_plan(question)
        if clarification:
            _apply_clarification(tentative, clarification)
        dimensions = await schema.dimensions()
        discovered: dict[str, tuple[Any, ...]] = {}
        if (
            not clarification
            and tentative.get("planner_fallback")
            and _question_may_require_members(question)
        ):
            fallback = await _discover_members_from_question(
                question,
                report["report_name"],
                dimensions,
                schema,
                driver,
            )
            if isinstance(fallback, PlanClarification):
                return fallback
            tentative["member_selections"] = fallback
        requested_by_dimension: dict[str, list[tuple[str, ...]]] = {}
        for selection in tentative.get("member_selections", []):
            dimension_name = str(selection.get("dimension") or "")
            requested_by_dimension.setdefault(dimension_name, []).append(
                tuple(str(part) for part in selection.get("member_path", []))
            )
        for dimension_name, requested_paths in requested_by_dimension.items():
            tag = _tag_by_label(dimensions, dimension_name)
            if not tag:
                return PlanClarification(
                    dimension=dimension_name,
                    question=f"Dimension is unavailable: {dimension_name}",
                    candidates=tuple((tag.label,) for tag in dimensions),
                )
            matches: dict[tuple[str, ...], Any] = {}
            for requested in requested_paths:
                for node in await schema.search(report["report_name"], tag, requested[-1] if requested else ""):
                    matches[node.path] = node
            discovered[dimension_name] = tuple(matches.values())
            await driver.cancel_member_selection()
        return compile_query_plan(
            tentative,
            report_set=report["report_set"],
            report=report["report_name"],
            discovered=discovered,
        )

    async def discover(
        self,
        session: DataExplorerSession,
        credentials: Credentials,
    ) -> PivotDiscovery:
        """Fully enumerate the Pivot Screen: every dimension tag, the complete
        member tree behind each Row/Column dimension's '+', and every report
        page/filter dropdown with its options."""
        report = self._report(session)
        driver = await open_persistent_data_explorer(
            session,
            settings=self.settings,
            credentials=credentials,
            report_set=report["report_set"],
            report_parameter=report["report_parameter"],
            report_name=report["report_name"],
        )
        await driver.open_pivot()
        dimensions = await driver.list_dimension_tags()
        members: list[DimensionMembers] = []
        for tag in dimensions:
            if tag.axis not in ("row", "column"):
                continue
            try:
                nodes = await driver.list_all_members(tag)
                await driver.cancel_member_selection()
            except WorldpanelError:
                continue
            members.append(
                DimensionMembers(dimension=tag.label, axis=tag.axis, members=tuple(nodes))
            )
        dropdowns = tuple(await driver.read_dropdowns())
        return PivotDiscovery(
            dimensions=tuple(dimensions),
            members=tuple(members),
            dropdowns=dropdowns,
        )

    async def execute(
        self,
        session: DataExplorerSession,
        credentials: Credentials,
        plan: QueryPlan,
    ) -> ExecutionResult:
        report = self._report(session)
        driver = await open_persistent_data_explorer(
            session,
            settings=self.settings,
            credentials=credentials,
            report_set=report["report_set"],
            report_parameter=report["report_parameter"],
            report_name=report["report_name"],
        )
        result = await QueryExecutor(driver, _schema_for(session, driver)).execute(plan)
        session.last_verified_state = result.receipt
        return result

    def _report(self, session: DataExplorerSession) -> dict[str, str]:
        if not session.current_report:
            raise WorldpanelError("No Data Explorer report is prepared for this session")
        return session.current_report


def _schema_for(session: DataExplorerSession, driver: Any) -> SchemaService:
    context = session.context if isinstance(session.context, dict) else None
    if context is None:
        return SchemaService(driver)
    schema = context.get("schema")
    if isinstance(schema, SchemaService) and schema.driver is driver:
        return schema
    schema = SchemaService(driver)
    context["schema"] = schema
    return schema


def _apply_clarification(tentative: dict[str, Any], clarification: dict[str, Any]) -> None:
    dimension = str(clarification.get("dimension") or "")
    member_path = [str(part) for part in clarification.get("member_path", [])]
    if not dimension or not member_path:
        return
    selections = [
        selection
        for selection in tentative.get("member_selections", [])
        if str(selection.get("dimension") or "").casefold() != dimension.casefold()
    ]
    selections.append({"dimension": dimension, "member_path": member_path, "checked": True})
    tentative["member_selections"] = selections
    tentative.pop("planner_fallback", None)


def _tag_by_label(tags: tuple[DimensionTag, ...], label: str) -> DimensionTag | None:
    requested = label.casefold()
    return next((tag for tag in tags if tag.label.casefold() == requested), None)


_STOP_WORDS_EN = (
    "product|period|kpi|measures|measure|outlet|channel|performance|duration|geography|"
    "optional1|optional2|row|column|filter|on|in|for|of|by|to|at|vs|versus|the|and|a|an|"
    "show|give|me|please|what|is|are|how|much|many|value|values|sales|spend|volume|"
    "penetration|buyers|frequency|trips|price|trend|table|comparison|growth|rate|change|"
    "difference|actual|year|yr|yoy|last|ly|previous|prior|same|period|vs|"
    "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|november|december"
)
_STOP_WORDS_ZH = (
    "销额|销售额|金额|销量|销售量|渗透率|渗透|增长率|增长|同比|环比|去年|同期|本期|"
    "月|年|的|和|与|对比|相比|是|多少|查询|查|看|读取|数据"
)
_STOP_TOKEN_RE = re.compile(
    rf"\b({_STOP_WORDS_EN})\b|({_STOP_WORDS_ZH})|20\d{{2}}|P\d{{1,2}}",
    flags=re.IGNORECASE,
)


def _question_may_require_members(question: str) -> bool:
    stripped = _STOP_TOKEN_RE.sub(" ", question)
    stripped = re.sub(r"[^A-Za-z0-9一-鿿]+", "", stripped)
    return len(stripped) >= 3


def _candidate_tokens(question: str) -> list[str]:
    stripped = _STOP_TOKEN_RE.sub(" ", question)
    tokens = [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9 ]*[A-Za-z0-9]|[一-鿿]{2,}", stripped)
        if len(_normalize(token)) >= 3
    ]
    return list(dict.fromkeys(token.strip() for token in tokens if token.strip()))


async def _discover_members_from_question(
    question: str,
    report: str,
    dimensions: tuple[DimensionTag, ...],
    schema: SchemaService,
    driver,
) -> list[dict[str, Any]] | PlanClarification:
    normalized_question = _normalize(question)
    tokens = _candidate_tokens(question)
    outstanding = set(_normalize(token) for token in tokens)
    selections: list[dict[str, Any]] = []
    for tag in dimensions:
        if tokens and not outstanding:
            break
        try:
            nodes: list[Any] = []
            seen: set[tuple[str, ...]] = set()
            for token in tokens or [""]:
                for node in await schema.search(report, tag, token):
                    if node.path not in seen:
                        seen.add(node.path)
                        nodes.append(node)
            await driver.cancel_member_selection()
        except WorldpanelError:
            continue
        matches = [
            node
            for node in nodes
            if len(_normalize(node.label)) >= 3 and _normalize(node.label) in normalized_question
        ]
        if not matches:
            continue
        longest = max(len(_normalize(node.label)) for node in matches)
        matches = [node for node in matches if len(_normalize(node.label)) == longest]
        unique_paths = tuple(dict.fromkeys(node.path for node in matches))
        if len(unique_paths) != 1:
            return PlanClarification(
                dimension=tag.label,
                question=f"Please choose the exact {tag.label} member path.",
                candidates=unique_paths,
            )
        selections.append(
            {"dimension": tag.label, "member_path": list(unique_paths[0]), "checked": True}
        )
        matched_label = _normalize(matches[0].label)
        outstanding = {token for token in outstanding if token not in matched_label and matched_label not in token}
    if not selections:
        return PlanClarification(
            dimension="member",
            question="No exact live member label was found. Please specify the dimension and exact member label.",
            candidates=tuple((tag.label,) for tag in dimensions),
        )
    return selections


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", value.casefold())
