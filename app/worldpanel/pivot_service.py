from __future__ import annotations

import re
from typing import Any

from app.assistant import AISettings, AssistantClient
from app.config import Settings
from app.worldpanel.client import Credentials, WorldpanelError
from app.worldpanel.executor import ExecutionResult, QueryExecutor
from app.worldpanel.pivot_models import (
    AxisPlacement,
    DimensionMembers,
    DimensionTag,
    FilterSelection,
    MemberSelection,
    PivotDiscovery,
    QueryPlan,
)
from app.worldpanel.planner import PlanClarification, StructuredPlanner
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

        # Product terms come from the LLM (translated/natural names) and from any
        # member the user explicitly clarified. We always resolve them against
        # the live member tree ourselves, so the model only needs to understand
        # language — the exact hierarchy path is verified, never invented.
        clarified_selections = [
            dict(selection)
            for selection in tentative.get("member_selections", [])
            if selection.get("member_path")
        ]
        extra_terms = [str(term) for term in tentative.get("products", []) if str(term).strip()]
        extra_terms += [
            str(selection["member_path"][-1])
            for selection in clarified_selections
        ]

        resolved_selections: list[dict[str, Any]] = []
        if not clarification and (extra_terms or _question_may_require_members(question)):
            resolved = await _discover_members_from_question(
                question,
                report["report_name"],
                dimensions,
                schema,
                driver,
                extra_terms=tuple(extra_terms),
            )
            if isinstance(resolved, PlanClarification):
                return resolved
            resolved_selections = resolved

        member_selections = clarified_selections or resolved_selections
        return _build_plan(
            tentative,
            member_selections,
            report_set=report["report_set"],
            report=report["report_name"],
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


def _build_plan(
    tentative: dict[str, Any],
    member_selections: list[dict[str, Any]],
    *,
    report_set: str,
    report: str,
) -> QueryPlan:
    """Assemble a QueryPlan from the planner intent plus already-resolved
    (live, verified) member paths."""
    axes = tuple(
        AxisPlacement(
            dimension=str(item["dimension"]),
            axis=str(item["axis"]),  # type: ignore[arg-type]
            position=int(item.get("position", 0)),
        )
        for item in tentative.get("axis_placements", [])
        if isinstance(item, dict) and item.get("dimension") and item.get("axis") in {"row", "column", "filter", "available"}
    )
    members = tuple(
        MemberSelection(
            dimension=str(selection["dimension"]),
            member_path=tuple(str(part) for part in selection["member_path"]),
            checked=bool(selection.get("checked", True)),
        )
        for selection in member_selections
        if selection.get("member_path")
    )
    filters = tuple(
        FilterSelection(role=str(item["role"]), value=str(item["value"]))
        for item in tentative.get("filters", [])
        if isinstance(item, dict) and item.get("role") and item.get("value")
    )
    kpis = tuple(str(value) for value in tentative.get("kpis", []) if str(value).strip()) or ("Spend (RMB 000)",)
    return QueryPlan(
        report_set=report_set,
        report=report,
        axis_placements=axes,
        member_selections=members,
        kpis=kpis,
        expected_period=str(tentative["expected_period"]) if tentative.get("expected_period") else None,
        output_shape=tentative.get("output_shape", "single_value"),
        calculation=str(tentative["calculation"]) if tentative.get("calculation") else None,
        filters=filters,
    )


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
    normalized = _normalize(question)
    # Short Chinese product names (榴莲, 苹果, 西瓜...) are valid 2-char members,
    # so a known alias anywhere in the question signals member intent.
    if any(
        _normalize(alias) in normalized
        for aliases in _MEMBER_ALIASES.values()
        for alias in aliases
    ):
        return True
    stripped = _STOP_TOKEN_RE.sub(" ", question)
    stripped = re.sub(r"[^A-Za-z0-9一-鿿]+", "", stripped)
    return len(stripped) >= 3


# Page/filter dropdown values (durations, calculations) are resolved by the
# planner's _detect_* helpers, never as Pivot members. Keep them out of the
# member-candidate token list so e.g. 'STD'/'YTD' are never searched as members.
_RESERVED_FILTER_TOKENS = {
    "std", "ytd", "mat", "52we", "12we", "4we",
    "actual", "yoy", "yronyr",
}


def _candidate_tokens(question: str) -> list[str]:
    stripped = _STOP_TOKEN_RE.sub(" ", question)
    tokens = [
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9 ]*[A-Za-z0-9]|[一-鿿]{2,}", stripped)
        if len(_normalize(token)) >= 3 and _normalize(token) not in _RESERVED_FILTER_TOKENS
    ]
    return list(dict.fromkeys(token.strip() for token in tokens if token.strip()))


# Chinese (and common alternate) names for English member labels in the CN
# Zespri report. The Pivot member tree is labelled in English, so a Chinese
# question like "榴莲销额" would otherwise never match. Keys are normalized
# English labels; values are alternate terms to look for in the question.
_MEMBER_ALIASES: dict[str, tuple[str, ...]] = {
    "durian": ("榴莲",),
    "apple": ("苹果",),
    "banana": ("香蕉", "蕉"),
    "cherry": ("车厘子", "樱桃"),
    "grapes": ("葡萄", "提子"),
    "blueberry": ("蓝莓",),
    "strawberry": ("草莓",),
    "watermelon": ("西瓜",),
    "orangetangerine": ("橙", "柑橘", "橘子", "柑"),
    "kiwifruit": ("奇异果", "猕猴桃"),
    "goldkiwifruit": ("金果", "黄心奇异果", "黄心猕猴桃"),
    "greenkiwifruit": ("绿果", "绿心奇异果", "绿心猕猴桃"),
    "redkiwifruit": ("红果", "红心奇异果", "红心猕猴桃"),
    "zespri": ("佳沛",),
    "fruit": ("水果", "总水果"),
}


def _member_match_length(
    label: str,
    normalized_question: str,
    extra_terms: tuple[str, ...] = (),
) -> int:
    """Return how strongly a member label appears in the question or in an
    LLM-provided product term, by the length of the matched term (English label
    or a known alias). 0 means no match."""
    normalized_label = _normalize(label)
    if not normalized_label:
        return 0
    aliases = tuple(_normalize(a) for a in _MEMBER_ALIASES.get(normalized_label, ()))
    best = 0
    if len(normalized_label) >= 3 and normalized_label in normalized_question:
        best = len(normalized_label)
    for normalized_alias in aliases:
        if normalized_alias and normalized_alias in normalized_question:
            best = max(best, len(normalized_alias) + 2)  # prefer explicit alias hits
    # LLM-provided terms (already translated to the English product name) match
    # the label directly, even if the raw label never appeared in the question.
    for term in extra_terms:
        normalized_term = _normalize(term)
        if not normalized_term:
            continue
        if normalized_term == normalized_label or normalized_term in normalized_label or normalized_label in normalized_term:
            best = max(best, len(normalized_term) + 1)
        elif any(alias and (alias in normalized_term or normalized_term in alias) for alias in aliases):
            best = max(best, len(normalized_term) + 2)
    return best


async def _discover_members_from_question(
    question: str,
    report: str,
    dimensions: tuple[DimensionTag, ...],
    schema: SchemaService,
    driver,
    extra_terms: tuple[str, ...] = (),
) -> list[dict[str, Any]] | PlanClarification:
    normalized_question = _normalize(question)
    selections: list[dict[str, Any]] = []
    for tag in dimensions:
        # Only Row/Column dimensions have a selectable member tree. Page/filter
        # dimensions (Measures, Performance, Outlet, Duration, ...) are driven
        # by report dropdowns, so never search them for members here.
        if tag.axis not in ("row", "column"):
            continue
        try:
            # Match against the fully enumerated member tree rather than the
            # selector search box, which is unreliable; this is deterministic
            # for both English labels and Chinese aliases.
            nodes = await schema.all_members(report, tag)
            await driver.cancel_member_selection()
        except WorldpanelError:
            continue
        scored = [(node, _member_match_length(node.label, normalized_question, extra_terms)) for node in nodes]
        matches = [node for node, score in scored if score > 0]
        if not matches:
            continue
        longest = max(_member_match_length(node.label, normalized_question, extra_terms) for node in matches)
        matches = [node for node in matches if _member_match_length(node.label, normalized_question, extra_terms) == longest]
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
    if not selections:
        return PlanClarification(
            dimension="member",
            question="No exact live member label was found. Please specify the dimension and exact member label.",
            candidates=tuple((tag.label,) for tag in dimensions),
        )
    return selections


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", value.casefold())
