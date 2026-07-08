from __future__ import annotations

import logging
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
    RankingSpec,
)
from app.worldpanel.planner import (
    PlanClarification,
    StructuredPlanner,
    _extract_json,
    calculation_clarification_for_question,
    canonical_calculation,
    canonical_kpi,
)
from app.worldpanel.schema import SchemaService
from app.worldpanel.semantic_match import pick_option, related_indices
from app.worldpanel.session import DataExplorerSession, open_persistent_data_explorer


logger = logging.getLogger(__name__)


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
        else:
            calculation_clarification = calculation_clarification_for_question(question)
            if calculation_clarification:
                return calculation_clarification
        dimensions = await schema.dimensions()

        # Resolve axis-placement dimension names to live dimensions (the LLM may
        # say "Channel" while the live dimension is "Outlet"). A breakdown
        # dimension is placed on a Column so Period stays a clean date row axis,
        # which keeps the answer readable ("by channel" -> one value per channel).
        resolved_axes: list[dict[str, Any]] = []
        for item in tentative.get("axis_placements", []):
            if not isinstance(item, dict) or not item.get("dimension"):
                continue
            live = await _resolve_dimension(
                str(item["dimension"]), dimensions, assistant, question
            )
            if not live:
                continue
            axis = str(item.get("axis") or "column")
            if axis not in ("row", "column"):
                axis = "column"
            resolved_axes.append({"dimension": live, "axis": "column", "position": 0})

        # Ranking ("渗透率最高的品牌") needs the ranked dimension spread on an
        # axis too. Resolve it against the live dims; when the report has no
        # such dimension (e.g. no Brand dim — brands live under Product), leave
        # it unresolved and rank the children of the selected member instead.
        ranking = _parse_ranking(tentative.get("ranking"))
        ranking_dim_live: str | None = None
        if ranking:
            ranking_dim_live = await _resolve_dimension(
                ranking["dimension"], dimensions, assistant, question
            )
            if ranking_dim_live and not any(
                _normalize(axis["dimension"]) == _normalize(ranking_dim_live)
                for axis in resolved_axes
            ):
                resolved_axes.append(
                    {"dimension": ranking_dim_live, "axis": "column", "position": 0}
                )
        tentative["axis_placements"] = resolved_axes

        # A breakdown axis and a single-value filter on the SAME dimension
        # contradict each other (e.g. "by channel" + channel=CVS would collapse
        # the breakdown to one channel). The breakdown wins; drop such filters.
        axis_dims = {_normalize(axis["dimension"]) for axis in resolved_axes}
        if axis_dims:
            kept_filters = []
            for flt in tentative.get("filters", []):
                if not isinstance(flt, dict) or not flt.get("role"):
                    continue
                flt_dim = _resolve_dimension_name(str(flt["role"]), dimensions)
                if flt_dim and _normalize(flt_dim) in axis_dims:
                    continue
                kept_filters.append(flt)
            tentative["filters"] = kept_filters

        # Resolve KPI terms and filter values against the report's REAL
        # dropdown options (LLM association for unfamiliar shorthand); if a
        # term matches nothing, proactively ask the user with the real options.
        try:
            dropdowns = tuple(await driver.read_dropdowns())
        except Exception:
            dropdowns = ()
        dropdown_clarification = await _resolve_dropdown_terms(
            tentative, dropdowns, assistant, question
        )
        if dropdown_clarification is not None:
            return dropdown_clarification

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
        # Discovery runs unless the clarification itself pinned members: a
        # calculation/KPI/filter clarification must NOT skip member discovery,
        # or the product asked about in the original question would be lost.
        if not clarified_selections and (extra_terms or _question_may_require_members(question)):
            resolved = await _discover_members_from_question(
                question,
                report["report_name"],
                dimensions,
                schema,
                driver,
                extra_terms=tuple(extra_terms),
                assistant=assistant,
            )
            if isinstance(resolved, PlanClarification):
                return resolved
            resolved_selections = resolved

        member_selections = clarified_selections or resolved_selections

        # Ranking over a dimension the report doesn't have (e.g. "品牌" but no
        # Brand dim): rank the children of the selected member instead — the
        # brands of 牙膏 are its child nodes in the Product tree. Spread that
        # member's own dimension on a column so the children render side by side.
        if ranking and not ranking_dim_live and member_selections:
            host_dim = str(member_selections[0].get("dimension") or "")
            if host_dim and not any(
                _normalize(axis["dimension"]) == _normalize(host_dim)
                for axis in resolved_axes
            ):
                resolved_axes.append({"dimension": host_dim, "axis": "column", "position": 0})
                tentative["axis_placements"] = resolved_axes

        # When an axis-spread dimension also has a specific member selected
        # (牙膏 on Product while ranking/breaking down Product), the user wants
        # that member's CHILDREN across the axis, not the single parent total.
        # The trailing "*" turns the path into a scoped select-all-children.
        axis_dim_names = {_normalize(axis["dimension"]) for axis in resolved_axes}
        if ranking or resolved_axes:
            for sel in member_selections:
                path = list(sel.get("member_path") or ())
                if (
                    _normalize(str(sel.get("dimension") or "")) in axis_dim_names
                    and path
                    and path != ["*"]
                    and path[-1] != "*"
                ):
                    sel["member_path"] = path + ["*"]

        # For a breakdown dimension placed on an axis (e.g. "by channel"), the
        # user wants every member of that dimension shown. If the planner did not
        # pin specific members for it, inject a select-all ("*") so the axis
        # renders the full distribution instead of whatever was checked before.
        selected_dims = {
            _normalize(sel.get("dimension", ""))
            for sel in member_selections
            if sel.get("dimension")
        }
        for axis in resolved_axes:
            dim = axis["dimension"]
            if _normalize(dim) in selected_dims:
                continue
            member_selections.append(
                {"dimension": dim, "member_path": ["*"], "checked": True}
            )
            selected_dims.add(_normalize(dim))

        return _build_plan(
            tentative,
            member_selections,
            report_set=report["report_set"],
            report=report["report_name"],
            ranking=ranking,
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


async def _resolve_dimension(
    name: str,
    live_tags: tuple[DimensionTag, ...],
    assistant: Any,
    question: str,
) -> str | None:
    """Deterministic synonym resolution first; unfamiliar shorthand falls to
    an LLM pick over the LIVE dimension labels (never an invented name)."""
    live = _resolve_dimension_name(name, live_tags)
    if live or assistant is None:
        return live
    labels = [tag.label for tag in live_tags]
    picked = await pick_option(
        assistant, question=question, term=name, options=labels, purpose="dimension"
    )
    return picked if picked in labels else None


def _match_option(term: str, options: tuple[str, ...]) -> str | None:
    """Deterministic option match: exact-normalized first, then containment."""
    n = _normalize(term)
    if not n:
        return None
    for option in options:
        if _normalize(option) == n:
            return option
    for option in options:
        normalized_option = _normalize(option)
        if n in normalized_option or normalized_option in n:
            return option
    return None


async def _resolve_dropdown_terms(
    tentative: dict[str, Any],
    dropdowns: tuple[Any, ...],
    assistant: Any,
    question: str,
) -> PlanClarification | None:
    """Resolve KPI terms and filter values against the report's REAL dropdown
    options. Unfamiliar shorthand goes through an LLM pick over those options;
    a term matching nothing becomes a clarification listing every real option,
    so the user is proactively asked instead of hitting a dead end."""
    kpi_dropdown = next((d for d in dropdowns if getattr(d, "role", "") == "kpi"), None)
    if kpi_dropdown is not None and kpi_dropdown.options:
        resolved_kpis: list[str] = []
        for raw_term in tentative.get("kpis", []):
            term = str(raw_term).strip()
            if not term:
                continue
            target = _match_option(canonical_kpi(term), kpi_dropdown.options) or _match_option(
                term, kpi_dropdown.options
            )
            if target is None and assistant is not None:
                target = await pick_option(
                    assistant,
                    question=question,
                    term=term,
                    options=list(kpi_dropdown.options),
                    purpose="KPI",
                )
            if target is None:
                return PlanClarification(
                    dimension="kpi",
                    question=f"没有找到与“{term}”对应的指标。当前报表提供以下指标，请点选：",
                    candidates=tuple((option,) for option in kpi_dropdown.options),
                )
            if target not in resolved_kpis:
                resolved_kpis.append(target)
        if resolved_kpis:
            tentative["kpis"] = resolved_kpis
    for flt in tentative.get("filters", []):
        if not isinstance(flt, dict) or not flt.get("role") or not flt.get("value"):
            continue
        role = str(flt["role"])
        value = str(flt["value"])
        dropdown = next(
            (
                d
                for d in dropdowns
                if _normalize(getattr(d, "role", "")) == _normalize(role)
                or _normalize(getattr(d, "dimension", "")) == _normalize(role)
            ),
            None,
        )
        if dropdown is None or not dropdown.options:
            continue
        target = _match_option(value, dropdown.options)
        if target is None and assistant is not None:
            target = await pick_option(
                assistant,
                question=question,
                term=value,
                options=list(dropdown.options),
                purpose=f"{role} filter",
            )
        if target is None:
            return PlanClarification(
                dimension=role,
                question=f"没有找到与“{value}”对应的 {role} 选项。当前报表提供以下选项，请点选：",
                candidates=tuple((option,) for option in dropdown.options),
            )
        flt["value"] = target
    return None


def _parse_ranking(raw: Any) -> dict[str, Any] | None:
    """Validate the planner's ranking intent into {dimension, direction, top_n}."""
    if not isinstance(raw, dict):
        return None
    direction = "min" if str(raw.get("direction") or "max").strip().lower() in ("min", "lowest", "asc", "bottom") else "max"
    try:
        top_n = max(1, min(50, int(raw.get("top_n") or 1)))
    except (TypeError, ValueError):
        top_n = 1
    return {
        "dimension": str(raw.get("dimension") or "").strip(),
        "direction": direction,
        "top_n": top_n,
    }


def _build_plan(
    tentative: dict[str, Any],
    member_selections: list[dict[str, Any]],
    *,
    report_set: str,
    report: str,
    ranking: dict[str, Any] | None = None,
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
    # Canonicalize KPI terms to the exact report labels (the LLM may say
    # "Sales Amount" / "销额" for "Spend (RMB 000)").
    kpis = tuple(
        canonical_kpi(str(value))
        for value in tentative.get("kpis", [])
        if str(value).strip()
    ) or ("Spend (RMB 000)",)
    shape = str(tentative.get("output_shape") or "single_value").strip().lower().replace(" ", "_")
    if shape not in {"single_value", "table", "comparison", "trend", "ranking"}:
        shape = "single_value"
    if ranking:
        shape = "ranking"
    return QueryPlan(
        report_set=report_set,
        report=report,
        axis_placements=axes,
        member_selections=members,
        kpis=kpis,
        expected_period=str(tentative["expected_period"]) if tentative.get("expected_period") else None,
        output_shape=shape,  # type: ignore[arg-type]
        calculation=canonical_calculation(str(tentative["calculation"])) if tentative.get("calculation") else None,
        filters=filters,
        ranking=RankingSpec(
            dimension=ranking["dimension"],
            direction=ranking["direction"],  # type: ignore[arg-type]
            top_n=ranking["top_n"],
        )
        if ranking
        else None,
    )


def _apply_clarification(tentative: dict[str, Any], clarification: dict[str, Any]) -> None:
    dimension = str(clarification.get("dimension") or "")
    member_path = [str(part) for part in clarification.get("member_path", [])]
    if not dimension or not member_path:
        return
    if dimension.casefold() == "calculation":
        tentative["calculation"] = member_path[-1]
        tentative.pop("planner_fallback", None)
        return
    if dimension.casefold() == "kpi":
        tentative["kpis"] = [member_path[-1]]
        tentative.pop("planner_fallback", None)
        return
    if dimension.casefold() in ("channel", "duration", "geography"):
        filters = [
            flt
            for flt in tentative.get("filters", [])
            if str(flt.get("role") or "").casefold() != dimension.casefold()
        ]
        filters.append({"role": dimension.casefold(), "value": member_path[-1]})
        tentative["filters"] = filters
        tentative.pop("planner_fallback", None)
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


def _depluralize(token: str) -> str:
    return token[:-1] if len(token) > 3 and token.endswith("s") else token


def _tokens(value: str) -> set[str]:
    """Plural-insensitive token set; Latin words and individual CJK characters."""
    return {_depluralize(token) for token in re.findall(r"[a-z0-9]+|[一-鿿]", value.casefold())}


_EXACT = 10000
_STRONG = 1000


def _term_match(label: str, term: str, normalized_question: str) -> int:
    """Score one member label against one product term (+ the question, for
    aliases). Token-aware so 'Fruit'/总水果 -> the bare 'Fruit' root, while
    '4 Premium Fruits' -> the more specific '4 Premium Fruit Types'."""
    normalized_label = _normalize(label)
    if not normalized_label:
        return 0
    label_tokens = _tokens(label)
    aliases = tuple(_normalize(a) for a in _MEMBER_ALIASES.get(normalized_label, ()))
    normalized_term = _normalize(term)

    if not normalized_term:
        # Question-level signals only — used when no specific term is given.
        # These are intentionally NOT applied per product term, otherwise a
        # generic alias anywhere in the question (e.g. 水果 in 整体水果) would make
        # 'Fruit' win for every term, including '4 Premium Fruits'.
        best = 0
        for alias in aliases:
            if alias and alias in normalized_question:
                best = max(best, _EXACT + len(alias))
        if len(normalized_label) >= 3 and normalized_label in normalized_question:
            best = max(best, len(normalized_label))
        return best

    # Term-level matching: score this label only against this product term.
    term_tokens = _tokens(term)
    if normalized_term == normalized_label or term_tokens == label_tokens or any(a == normalized_term for a in aliases):
        return _EXACT + len(normalized_label)  # exact (plural/space-insensitive) or alias-equals-term
    if _is_total_member_for_term(label_tokens, term_tokens):
        return _STRONG * 2 + len(term_tokens)
    if term_tokens and term_tokens <= label_tokens:
        return _STRONG + len(term_tokens)  # every term word in a more specific label
    if label_tokens <= term_tokens and len(label_tokens) >= 2:
        return _STRONG // 2 + len(label_tokens)  # multi-word label inside a longer term
    if normalized_term in normalized_label:
        return len(normalized_term)
    if any(a and a in normalized_term for a in aliases):
        return len(normalized_term)
    return 0


def _is_total_member_for_term(label_tokens: set[str], term_tokens: set[str]) -> bool:
    return bool(term_tokens) and "total" in label_tokens and label_tokens - {"total"} == term_tokens


def _alias_seed_terms(normalized_question: str) -> list[str]:
    return [
        label
        for label, aliases in _MEMBER_ALIASES.items()
        if any(_normalize(a) in normalized_question for a in aliases)
    ]


def _member_match_length(
    label: str,
    normalized_question: str,
    extra_terms: tuple[str, ...] = (),
) -> int:
    """Best score of a label across the question and any provided product terms."""
    best = _term_match(label, "", normalized_question)
    for term in extra_terms:
        best = max(best, _term_match(label, term, normalized_question))
    return best


_DATE_RE = re.compile(r"\d{1,2}-[A-Za-z]{3}-\d{2}")


def _is_time_dimension(tag: DimensionTag, nodes: list) -> bool:
    """The time/period dimension is selected via the period, not as a product
    member, so it must be kept out of member matching (otherwise the LLM/term
    matcher can pick a date like '15-May-26' as a 'member')."""
    label = _normalize(tag.label)
    if any(token in label for token in ("period", "time", "date", "week", "duration", "时间", "期间", "日期")):
        return True
    leaves = [n for n in nodes if getattr(n, "label", "")]
    return bool(leaves) and all(_DATE_RE.search(n.label) for n in leaves)


# Synonym groups so an LLM axis-dimension name ("Channel") resolves to the
# live dimension label ("Outlet"), across report sets and languages.
_DIM_SYNONYMS: tuple[set[str], ...] = (
    {"channel", "channels", "outlet", "outlets", "retailer", "渠道", "零售商", "卖场", "业态"},
    {"geography", "region", "market", "geo", "地区", "区域", "市场", "省份", "城市"},
    {"product", "products", "category", "品类", "产品", "类别"},
    {"period", "time", "date", "时间", "期间", "日期", "月份"},
    {"measure", "measures", "kpi", "指标"},
    {"calculation", "performance", "计算", "表现"},
    {"duration", "时长", "周期"},
    {"manufacturer", "brand", "厂商", "品牌"},
)


def _resolve_dimension_name(name: str, live_tags: tuple[DimensionTag, ...]) -> str | None:
    """Map an LLM/free-text dimension name to an actual live dimension label."""
    n = _normalize(name)
    if not n:
        return None
    for tag in live_tags:
        if _normalize(tag.label) == n:
            return tag.label
    for group in _DIM_SYNONYMS:
        normalized_group = {_normalize(x) for x in group}
        if any(g == n or g in n or n in g for g in normalized_group):
            for tag in live_tags:
                if _normalize(tag.label) in normalized_group:
                    return tag.label
    for tag in live_tags:
        tl = _normalize(tag.label)
        if tl and (tl in n or n in tl):
            return tag.label
    return None


def _asks_all_members(question: str) -> bool:
    lowered = question.casefold()
    all_word = any(w in lowered for w in ("所有", "全部", "每个", "各个", "all ", "every ", "list of", "列表", "都有哪些", "有哪些"))
    member_word = any(w in lowered for w in ("product", "产品", "品类", "品牌", "brand", "sku", "item", "member", "成员", "category"))
    return all_word and member_word


def _primary_member_dimension(by_dim: dict) -> DimensionTag:
    for tag in by_dim:
        if "product" in _normalize(tag.label) or "产品" in tag.label:
            return tag
    return max(by_dim, key=lambda tag: len(by_dim[tag]))


async def _llm_resolve_members(question: str, members: list, assistant) -> list[dict[str, Any]]:
    """Fuzzy-map the question to the report's actual members using the LLM.

    The model only chooses from the live member list (by index), so it can match
    loosely (case/spacing/abbreviation/language) without inventing paths and
    without any per-client alias dictionary."""
    if assistant is None or not members:
        return []
    capped = members[:800]
    lines = "\n".join(
        f"{index}: {tag.label} > {' > '.join(node.path)}" for index, (tag, node) in enumerate(capped)
    )
    prompt = (
        "Map the user's Worldpanel question to the report's ACTUAL members listed below. "
        "Match loosely and fuzzily by meaning — ignore case, spacing, punctuation, abbreviations, "
        "and language (Chinese/English). Pick EVERY member the question is about. If the user asks "
        "for all/every product (所有/全部/每个产品), return all indices whose dimension is the product "
        "dimension. Return JSON only: {\"indices\": [int, ...]}; if nothing matches, {\"indices\": []}.\n"
        f"Question: {question}\n"
        "Members (index: Dimension > path):\n" + lines
    )
    try:
        response = await assistant.chat(prompt)
        payload = _extract_json(response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM member resolution failed (%s: %s)", type(exc).__name__, exc)
        return []
    indices = payload.get("indices", [])
    selections: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for raw in indices:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if not (0 <= index < len(capped)):
            continue
        tag, node = capped[index]
        key = (tag.label, node.path)
        if key not in seen:
            seen.add(key)
            selections.append({"dimension": tag.label, "member_path": list(node.path), "checked": True})
    return selections


def _resolve_terms_deterministic(
    normalized_question: str,
    members: list,
    terms: list[str],
) -> list[dict[str, Any]]:
    """Fast exact/alias/token resolution; best-effort, returns whatever it can."""
    selections: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for term in terms:
        scored = [(tag, node, _term_match(node.label, term, normalized_question)) for tag, node in members]
        best = max((score for _, _, score in scored), default=0)
        if best <= 0:
            continue
        top = [(tag, node) for tag, node, score in scored if score == best]
        unique = list(dict.fromkeys((tag.label, node.path) for tag, node in top))
        if len(unique) != 1:
            continue  # ambiguous — leave it for the LLM / clarification
        tag, node = top[0]
        key = (tag.label, node.path)
        if key not in seen:
            seen.add(key)
            selections.append({"dimension": tag.label, "member_path": list(node.path), "checked": True})
    return selections


async def _discover_members_from_question(
    question: str,
    report: str,
    dimensions: tuple[DimensionTag, ...],
    schema: SchemaService,
    driver,
    extra_terms: tuple[str, ...] = (),
    assistant=None,
) -> list[dict[str, Any]] | PlanClarification:
    normalized_question = _normalize(question)
    by_dim: dict[DimensionTag, list] = {}
    for tag in dimensions:
        # Only Row/Column dimensions have a selectable member tree; page/filter
        # dimensions are driven by report dropdowns.
        if tag.axis not in ("row", "column"):
            continue
        try:
            nodes = await schema.all_members(report, tag)
            await driver.cancel_member_selection()
        except WorldpanelError:
            continue
        nodes = list(nodes)
        # The time/period dimension is targeted by the period, not as a member.
        if _is_time_dimension(tag, nodes):
            continue
        by_dim[tag] = nodes
    if not by_dim:
        return PlanClarification(
            dimension="member",
            question="No selectable member tree is available.",
            candidates=(),
        )

    # "All products / 所有产品" -> select every member of the product dimension.
    if _asks_all_members(question):
        tag = _primary_member_dimension(by_dim)
        return [{"dimension": tag.label, "member_path": ["*"], "checked": True}]

    members = [(tag, node) for tag, nodes in by_dim.items() for node in nodes]

    # LLM-first fuzzy match against the live member labels (handles arbitrary
    # client product names without any hard-coded dictionary).
    llm_selections = await _llm_resolve_members(question, members, assistant)
    if llm_selections:
        return llm_selections

    # Deterministic fallback: exact / alias / token matching (also used when the
    # LLM is unavailable or returns nothing).
    terms = [t for t in extra_terms if t and t.strip()]
    terms += _candidate_tokens(question)
    terms += _alias_seed_terms(normalized_question)
    terms = list(dict.fromkeys(t.strip() for t in terms if t.strip()))
    selections = _resolve_terms_deterministic(normalized_question, members, terms)
    if selections:
        return selections

    # Nothing matched: instead of a dead end, proactively offer every RELATED
    # member as a clickable candidate so the user just picks one.
    capped = members[:800]
    related = await related_indices(
        assistant,
        question=question,
        items=[f"{tag.label} > {' > '.join(node.path)}" for tag, node in capped],
    )
    if related:
        # Clarification answers carry a single dimension, so keep the
        # candidates from the dimension the top pick belongs to.
        top_dimension = capped[related[0]][0].label
        candidate_paths = tuple(
            tuple(capped[index][1].path)
            for index in related
            if capped[index][0].label == top_dimension
        )
        if candidate_paths:
            return PlanClarification(
                dimension=top_dimension,
                question="没有找到完全匹配的成员。你可能想查的是以下之一，请点选：",
                candidates=candidate_paths,
            )
    return PlanClarification(
        dimension="member",
        question="未能在当前报表中匹配到你说的成员，请换个更接近报表里的名称，或指明维度与成员名。",
        candidates=tuple((tag.label,) for tag in by_dim),
    )


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]+", "", value.casefold())
