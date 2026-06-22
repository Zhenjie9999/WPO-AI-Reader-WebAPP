from __future__ import annotations

from contextlib import asynccontextmanager
import csv
from dataclasses import asdict
from datetime import datetime, timezone
from io import StringIO
from typing import AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.assistant import AISettings, AssistantClient, build_ai_status, summarize_check_with_shared_ai
from app.config import get_settings
from app.worldpanel.checker import check_table
from app.worldpanel.client import Credentials, WorldpanelClient, WorldpanelError
from app.worldpanel.data_explorer import (
    Clarification,
    DataExplorerCache,
    DataExplorerContext,
    QuerySpec,
    apply_clarification,
    parse_query_spec,
    plan_query,
)
from app.worldpanel.multitable import MultiKpiTable
from app.worldpanel.parser import KeyMeasuresTable, parse_key_measures_text
from app.worldpanel.pivot_models import AxisPlacement, FilterSelection, MemberSelection, QueryPlan
from app.worldpanel.pivot_cache import VerifiedResult, VerifiedResultCache
from app.worldpanel.pivot_result import PivotResultError, answer_from_pivot_tables, format_plain
from app.worldpanel.pivot_service import PivotQueryService
from app.worldpanel.planner import PlanClarification
from app.worldpanel.query import answer_question
from app.worldpanel.session import DataExplorerSessionManager


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    # Close every live browser session so Chromium processes never outlive
    # the service.
    await _pivot_sessions.close_all()


app = FastAPI(title="Worldpanel AI Reader", lifespan=_lifespan)
_cors_settings = get_settings()
if _cors_settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_cors_settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.mount("/static", StaticFiles(directory="app/static"), name="static")

_cached_table: KeyMeasuresTable | MultiKpiTable | None = None
_cached_report: dict[str, object] | None = None
_sessions: dict[str, dict[str, object]] = {}
_access_tokens: set[str] = set()
_query_cache = DataExplorerCache("runtime/query-cache.json")
_pivot_sessions = DataExplorerSessionManager()
_pivot_result_cache = VerifiedResultCache()
_MAX_PROGRESS_EVENTS = 30


class LoginRequest(BaseModel):
    email: str
    password: str
    access_token: str | None = None


class AccessRequest(BaseModel):
    invite_code: str


class ReadyToUseRequest(BaseModel):
    session_id: str
    report_set: str
    category: str | None = None


class RefreshRequest(BaseModel):
    session_id: str | None = None
    report_set: str | None = None
    report_parameter: str | None = None
    report_name: str | None = None
    all_kpis: bool = False
    requested_kpis: list[str] | None = None


class ClarificationRequest(BaseModel):
    dimension_key: str
    value: str


class AskRequest(BaseModel):
    question: str
    session_id: str | None = None
    clarification: ClarificationRequest | None = None


class PivotClarificationSelection(BaseModel):
    dimension: str
    member_path: list[str]


class PivotPlanRequest(BaseModel):
    session_id: str
    question: str
    clarification: PivotClarificationSelection | None = None


class PivotExecuteRequest(BaseModel):
    session_id: str
    plan: dict[str, object]
    question: str | None = None


class AIConfigurationRequest(BaseModel):
    base_url: str
    model: str
    api_key: str
    provider: str = "custom"
    endpoint_id: str | None = None
    access_token: str | None = None


class CheckRequest(BaseModel):
    session_id: str | None = None


@app.post("/api/access")
async def grant_access(request: AccessRequest) -> dict[str, object]:
    if request.invite_code.strip() != get_settings().invite_code:
        raise HTTPException(status_code=403, detail="邀请码不正确")
    token = str(uuid4())
    _access_tokens.add(token)
    return {"ok": True, "access_token": token}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("app/static/index.html")


@app.get("/api/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    expired_sessions = await _pivot_sessions.discard_expired()
    has_session_cache = any("cached_table" in session for session in _sessions.values())
    return {
        "ok": True,
        "has_env_credentials": settings.has_credentials,
        "report_set": settings.report_set,
        "ai": build_ai_status(settings.ai),
        "ai_defaults": {
            "base_url": settings.ai.base_url,
            "model": settings.ai.model,
            "provider": settings.ai.provider,
            "endpoint_id": settings.ai.endpoint_id,
        },
        "has_cached_data": has_session_cache or _cached_table is not None,
        "cached_report": _cached_report,
        "query_cache": {
            "entries": _query_cache.size,
            "path": str(_query_cache.path) if _query_cache.path else None,
        },
        "pivot_sessions": _pivot_sessions.size,
        "pivot_result_cache": _pivot_result_cache.size,
        "expired_pivot_sessions_removed": expired_sessions,
    }


@app.post("/api/ai/test")
async def test_ai_configuration(request: AIConfigurationRequest) -> dict[str, object]:
    _require_access_token(request.access_token)
    ai_settings = _ai_settings(request)
    try:
        await AssistantClient(ai_settings).chat("Reply with OK only.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_redacted_ai_error(exc)) from exc
    return {"ok": True, "ai": build_ai_status(ai_settings)}


@app.put("/api/sessions/{session_id}/ai")
async def bind_ai_configuration(session_id: str, request: AIConfigurationRequest) -> dict[str, object]:
    session = _session(session_id)
    ai_settings = _ai_settings(request)
    try:
        await AssistantClient(ai_settings).chat("Reply with OK only.")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_redacted_ai_error(exc)) from exc
    session["ai_settings"] = ai_settings
    return {"ok": True, "ai": build_ai_status(ai_settings)}


@app.delete("/api/sessions/{session_id}/ai")
async def clear_ai_configuration(session_id: str) -> dict[str, object]:
    session = _session(session_id)
    session.pop("ai_settings", None)
    return {"ok": True, "ai": build_ai_status(get_settings().ai)}


@app.get("/api/sessions/{session_id}/progress")
async def session_progress(session_id: str) -> dict[str, object]:
    session = _session(session_id)
    return {
        "ok": True,
        "active": bool(session.get("progress_active")),
        "current": session.get("progress_current"),
        "events": list(session.get("progress_events", [])),
    }


@app.post("/api/login")
async def login(request: LoginRequest) -> dict[str, object]:
    _require_access_token(request.access_token)
    return await _login_with_credentials(Credentials(email=request.email, password=request.password))


@app.post("/api/login-env")
async def login_from_env() -> dict[str, object]:
    settings = get_settings()
    if not settings.public_env_login_enabled:
        raise HTTPException(status_code=403, detail="公开版本已关闭本地 .env 登录，请手动输入 Worldpanel 账号密码。")
    if not settings.has_credentials:
        raise HTTPException(status_code=400, detail="本地 .env 还没有配置 Worldpanel 账号和密码。")
    return await _login_with_credentials(Credentials(email=settings.email or "", password=settings.password or ""))


async def _login_with_credentials(credentials: Credentials) -> dict[str, object]:
    settings = get_settings()
    client = WorldpanelClient(settings)
    try:
        report_sets = await client.list_report_sets(credentials)
    except WorldpanelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Login failed: {exc}") from exc

    session_id = str(uuid4())
    _sessions[session_id] = {"credentials": credentials}
    _reset_progress(_sessions[session_id], "Worldpanel login")
    _progress(_sessions[session_id], "done", "Worldpanel login completed")
    _pivot_sessions.get_or_create(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "current": report_sets.current,
        "report_sets": report_sets.options,
    }


@app.post("/api/ready-to-use")
async def ready_to_use(request: ReadyToUseRequest) -> dict[str, object]:
    settings = get_settings()
    session = _session(request.session_id)
    _reset_progress(session, "Ready-to-Use reports")
    _progress(session, "running", f"Opening report set: {request.report_set}")
    credentials = session["credentials"]
    client = WorldpanelClient(settings)
    try:
        catalog = await client.list_ready_to_use(
            credentials=credentials,  # type: ignore[arg-type]
            report_set=request.report_set,
            category=request.category,
        )
    except WorldpanelError as exc:
        _progress(session, "error", str(exc), active=False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _progress(session, "error", f"Reading Ready-to-Use reports failed: {exc}", active=False)
        raise HTTPException(status_code=500, detail=f"Reading Ready-to-Use reports failed: {exc}") from exc

    session["report_set"] = request.report_set
    session["ready_category"] = request.category or catalog.current_category
    _progress(
        session,
        "done",
        f"Loaded {len(catalog.reports)} Ready-to-Use reports",
        active=False,
    )
    return {
        "ok": True,
        "report_set": catalog.report_set,
        "current_category": catalog.current_category,
        "categories": catalog.categories,
        "reports": [asdict(report) for report in catalog.reports],
    }


@app.post("/api/refresh")
async def refresh(request: RefreshRequest | None = None) -> dict[str, object]:
    global _cached_report, _cached_table
    settings = get_settings()
    client = WorldpanelClient(settings)
    credentials: Credentials | None = None
    report_set = settings.report_set
    session: dict[str, object] | None = None

    if request and request.session_id:
        session = _session(request.session_id)
        _reset_progress(session, "Prepare Data Explorer")
        _progress(session, "running", "Selecting report and opening Data Explorer")
        credentials = session["credentials"]  # type: ignore[assignment]
        report_set = request.report_set or str(session.get("report_set") or settings.report_set)
        if request.report_parameter:
            session["current_report"] = {
                "report_set": report_set,
                "report_parameter": request.report_parameter,
                "report_name": request.report_name or "Data Explorer",
            }
            existing_pivot_session = _pivot_sessions.get(request.session_id)
            next_report = {
                "report_set": report_set,
                "report_parameter": request.report_parameter,
                "report_name": request.report_name or "Data Explorer",
            }
            if (
                existing_pivot_session
                and existing_pivot_session.current_report
                and existing_pivot_session.current_report != next_report
            ):
                await _pivot_sessions.discard(request.session_id)
            pivot_session = _pivot_sessions.get_or_create(request.session_id)
            pivot_session.current_report = next_report
    elif request and request.report_set:
        report_set = request.report_set

    table: KeyMeasuresTable | MultiKpiTable | None = None
    report_info: dict[str, object] = {
        "report_set": report_set,
        "report_name": (request.report_name if request else None) or "Data Explorer",
        "metrics": [],
    }
    try:
        if request and request.session_id and request.report_parameter:
            session = _session(request.session_id)
            _progress(session, "running", "Discovering Data Explorer controls")
            try:
                context = await _ensure_data_explorer_context(session, client)
                session["data_explorer_context"] = context
            except Exception:
                # Legacy control discovery is Zespri-tuned; the Pivot Q&A path
                # does not need it, so a failure here must not block preparation.
                session.pop("data_explorer_context", None)

        if request and request.all_kpis and request.report_parameter:
            if session is not None:
                _progress(session, "running", "Reading all requested KPI tables")
            credentials = credentials or client._settings_credentials()
            multi = await client.extract_all_kpis(
                credentials=credentials,
                report_set=report_set,
                report_parameter=request.report_parameter,
                report_name=request.report_name or "Data Explorer",
                requested_kpis=request.requested_kpis,
            )
            tables = {
                report.metric: parse_key_measures_text(report.text, metric_override=report.metric)
                for report in multi.reports
            }
            table = MultiKpiTable(tables=tables)
            report_info = {
                "report_set": multi.report_set,
                "report_name": multi.report_name,
                "iframe_url": multi.iframe_url,
                "metrics": list(tables.keys()),
            }
        else:
            if session is not None:
                _progress(session, "running", "Reading current KPI table")
            report = await client.extract_key_measures_text(
                credentials=credentials,
                report_set=report_set,
                report_parameter=request.report_parameter if request else None,
                report_name=request.report_name if request else None,
            )
            try:
                table = parse_key_measures_text(report.text, metric_override=report.metric)
                report_info = {
                    "report_set": report.report_set,
                    "report_name": report.report_name,
                    "iframe_url": report.iframe_url,
                    "metrics": [table.metric],
                }
            except Exception:
                # The report opened but the legacy text parser (Zespri "Spend"
                # layout) could not read it. Pivot Q&A reads the live DOM grid
                # instead, so still report the prepare as successful.
                table = None
                report_info = {
                    "report_set": report.report_set,
                    "report_name": report.report_name,
                    "iframe_url": report.iframe_url,
                    "metrics": [],
                }
    except WorldpanelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"刷新报表数据失败：{exc}") from exc

    if session is not None:
        if table is not None:
            _set_session_cache(session, table, report_info)
        _progress(session, "done", "Report prepared", active=False)
    elif table is not None:
        _cached_table = table
        _cached_report = report_info
    metrics = (
        (table.metrics if isinstance(table, MultiKpiTable) else [table.metric])
        if table is not None
        else []
    )
    return {
        "ok": True,
        "report": report_info,
        "products": table.products if table is not None else [],
        "dates": table.dates if table is not None else [],
        "metrics": metrics,
        "metric": metrics[0] if metrics else None,
        "context": _context_payload(session.get("data_explorer_context")) if request and request.session_id else None,
    }


@app.post("/api/ask")
async def ask(request: AskRequest) -> dict[str, object]:
    if request.session_id:
        return await _ask_with_data_explorer(request)

    if _cached_table is None:
        raise HTTPException(status_code=400, detail="还没有缓存数据，请先刷新报表。")
    return _answer_from_table(request.question, _cached_table)


@app.post("/api/check")
async def check_data(request: CheckRequest | None = None) -> dict[str, object]:
    if request and request.session_id:
        table, _report = _get_session_cache(_session(request.session_id))
    else:
        if _cached_table is None:
            raise HTTPException(status_code=400, detail="还没有缓存数据，请先刷新报表。")
        table = _cached_table

    ai_settings = _session_ai_settings(request.session_id if request else None)
    first_table = _first_cached_table(table)
    result = check_table(first_table)
    summary = summarize_check_with_shared_ai(result.summary, ai_settings)
    if ai_settings.enabled:
        prompt = _build_check_prompt(result.summary, [issue.message for issue in result.issues])
        try:
            summary = await AssistantClient(ai_settings).chat(prompt)
        except Exception as exc:
            summary = f"{summary}\n\nAI 接口调用失败，已保留本地检查结果：{exc}"

    return {
        "ok": True,
        "status": result.status,
        "summary": summary,
        "issues": [asdict(issue) for issue in result.issues],
        "ai": build_ai_status(ai_settings),
    }


@app.get("/api/export.csv")
async def export_current_csv(session_id: str) -> Response:
    session = _session(session_id)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["report_set", "report_name", "product", "date", "metric", "value"])
    export_rows = session.get("export_rows")
    if isinstance(export_rows, dict) and export_rows:
        # Whole-conversation export: every (report, metric, product, date) ever
        # pulled, not just the latest question.
        for (report_set, report_name, metric, product, date_label), value in export_rows.items():
            writer.writerow([report_set, report_name, product, date_label, metric, format_plain(value)])
    else:
        # Fallback: the legacy single cached table.
        table, report = _get_session_cache(session)
        for metric, metric_table in _iter_metric_tables(table):
            for date_label in metric_table.dates:
                values = metric_table.rows.get(date_label, {})
                for product in metric_table.products:
                    writer.writerow(
                        [
                            (report or {}).get("report_set", ""),
                            (report or {}).get("report_name", ""),
                            product,
                            date_label,
                            metric,
                            format_plain(values[product]) if product in values else "",
                        ]
                    )
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="wpo-current-data.csv"'},
    )


@app.post("/api/pivot/plan")
async def pivot_plan(request: PivotPlanRequest) -> dict[str, object]:
    session = _session(request.session_id)
    _reset_progress(session, "Plan Pivot query")
    _progress(session, "running", "Opening Pivot Screen and discovering dimensions")
    credentials = session.get("credentials")
    if not isinstance(credentials, Credentials):
        raise HTTPException(status_code=400, detail="Session credentials are unavailable")
    pivot_session = _pivot_sessions.get_or_create(request.session_id)
    _restore_pivot_report(session, pivot_session)
    clarification = request.clarification.model_dump() if request.clarification else None
    try:
        result = await pivot_session.serialized(
            lambda: PivotQueryService(get_settings(), _session_ai_settings(request.session_id)).plan(
                pivot_session, credentials, request.question, clarification
            )
        )
    except WorldpanelError as exc:
        _progress(session, "error", str(exc), active=False)
        await _pivot_sessions.discard(request.session_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _progress(session, "error", f"Pivot planning failed: {exc}", active=False)
        await _pivot_sessions.discard(request.session_id)
        raise HTTPException(status_code=500, detail=f"Pivot planning failed: {exc}") from exc
    if isinstance(result, PlanClarification):
        _progress(session, "waiting", "Waiting for user clarification", active=False)
        return {
            "ok": True,
            "needs_clarification": True,
            "clarification": {
                "dimension_key": result.dimension,
                "question": result.question,
                "options": [
                    {"label": " > ".join(path), "value": list(path)}
                    for path in result.candidates
                ],
            },
        }
    _progress(session, "done", "Pivot query plan is ready", active=False)
    return {"ok": True, "needs_clarification": False, "plan": asdict(result)}


@app.post("/api/pivot/discover")
async def pivot_discover(request: PivotPlanRequest) -> dict[str, object]:
    """Fully enumerate the Pivot Screen for the current report: every dimension,
    the complete member tree behind each Row/Column '+', and all report
    dropdowns with their options."""
    session = _session(request.session_id)
    credentials = session.get("credentials")
    if not isinstance(credentials, Credentials):
        raise HTTPException(status_code=400, detail="Session credentials are unavailable")
    pivot_session = _pivot_sessions.get_or_create(request.session_id)
    _restore_pivot_report(session, pivot_session)
    if not pivot_session.current_report:
        raise HTTPException(status_code=400, detail="请先在左侧选择 Data Explorer 报表并点击“读取所选报表”。")
    try:
        discovery = await pivot_session.serialized(
            lambda: PivotQueryService(get_settings()).discover(pivot_session, credentials)
        )
    except WorldpanelError as exc:
        await _pivot_sessions.discard(request.session_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        await _pivot_sessions.discard(request.session_id)
        raise HTTPException(status_code=500, detail=f"Pivot discovery failed: {exc}") from exc
    return {
        "ok": True,
        "dimensions": [
            {"label": tag.label, "axis": tag.axis, "member_count": tag.member_count}
            for tag in discovery.dimensions
        ],
        "members": [
            {
                "dimension": dm.dimension,
                "axis": dm.axis,
                "count": len(dm.members),
                "members": [
                    {"path": list(node.path), "level": node.level, "has_children": node.has_children}
                    for node in dm.members
                ],
            }
            for dm in discovery.members
        ],
        "dropdowns": [
            {
                "index": dd.index,
                "role": dd.role,
                "dimension": dd.dimension,
                "selected": dd.selected,
                "options": list(dd.options),
            }
            for dd in discovery.dropdowns
        ],
    }


@app.post("/api/pivot/execute")
async def pivot_execute(request: PivotExecuteRequest) -> dict[str, object]:
    session = _session(request.session_id)
    _reset_progress(session, "Execute Pivot query")
    _progress(session, "running", "Applying Pivot layout, member selections, and KPI")
    credentials = session.get("credentials")
    if not isinstance(credentials, Credentials):
        raise HTTPException(status_code=400, detail="Session credentials are unavailable")
    pivot_session = _pivot_sessions.get_or_create(request.session_id)
    _restore_pivot_report(session, pivot_session)
    current_report = pivot_session.current_report
    if not current_report:
        raise HTTPException(status_code=400, detail="请先在左侧选择 Data Explorer 报表并点击“读取所选报表”。")
    plan = _query_plan_from_payload(request.plan)
    if plan.report and plan.report != current_report.get("report_name"):
        raise HTTPException(
            status_code=400,
            detail=f"计划针对的报表（{plan.report}）与当前会话报表（{current_report.get('report_name')}）不一致，请重新提问。",
        )
    cache_scope = f"{credentials.email}|{current_report.get('report_parameter', '')}"
    cached = _pivot_result_cache.get(plan, cache_scope)
    if cached is not None:
        _progress(session, "done", "Returned verified result from cache", active=False)
        if cached.tables:
            _activate_pivot_tables(cached.tables, current_report, session)
        response: dict[str, object] = {"ok": True, "receipt": asdict(cached.receipt)}
        if cached.answer is not None:
            response["answer"] = cached.answer
        if cached.data is not None:
            response["data"] = cached.data
        return response
    try:
        result = await pivot_session.serialized(
            lambda: PivotQueryService(get_settings(), _session_ai_settings(request.session_id)).execute(
                pivot_session, credentials, plan
            )
        )
    except WorldpanelError as exc:
        _progress(session, "error", str(exc), active=False)
        await _pivot_sessions.discard(request.session_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        _progress(session, "error", f"Pivot execution failed: {exc}", active=False)
        await _pivot_sessions.discard(request.session_id)
        raise HTTPException(status_code=500, detail=f"Pivot execution failed: {exc}") from exc

    response = {"ok": True, "receipt": asdict(result.receipt)}
    _progress(session, "running", "Parsing rendered table and preparing answer")

    # Make the freshly verified pivot result the table that /api/ask and
    # /api/check operate on, so the data checker inspects the current query.
    converted = _activate_pivot_tables(result.tables, current_report, session)
    # Accumulate across the whole conversation so CSV export covers every query.
    _accumulate_export_rows(session, converted, current_report)

    if request.question:
        # Always answer from the float-valued pivot result so decimals survive
        # for every KPI (Penetration %, Average Price, Frequency, growth %, ...).
        # The legacy KeyMeasures answerer rounds to int and cannot format
        # percentages, so it is not used for pivot answers.
        member_leaves = [
            selection.member_path[-1]
            for selection in plan.member_selections
            if selection.checked and selection.member_path
        ]
        try:
            response["answer"] = answer_from_pivot_tables(
                result.tables, member_leaves, result.receipt.period
            )
        except PivotResultError as exc:
            response["answer_error"] = str(exc)

    _pivot_result_cache.set(
        plan,
        cache_scope,
        VerifiedResult(
            receipt=result.receipt,
            answer=str(response["answer"]) if response.get("answer") else None,
            data=response.get("data") if isinstance(response.get("data"), dict) else None,
            tables=result.tables,
        ),
    )
    _progress(session, "done", "Pivot data pull completed", active=False)
    return response


async def _ask_with_data_explorer(request: AskRequest) -> dict[str, object]:
    settings = get_settings()
    session = _session(request.session_id or "")
    client = WorldpanelClient(settings)
    context = await _ensure_data_explorer_context(session, client)

    if request.clarification:
        pending = session.get("pending_query_spec")
        if not isinstance(pending, QuerySpec):
            raise HTTPException(status_code=400, detail="没有等待补充的筛选条件，请重新输入问题。")
        spec = apply_clarification(pending, request.clarification.dimension_key, request.clarification.value)
        question = str(session.get("pending_question") or request.question)
        session.pop("pending_query_spec", None)
        session.pop("pending_question", None)
    else:
        question = request.question
        spec = parse_query_spec(question)

    planned = plan_query(spec, context)
    if isinstance(planned, Clarification):
        session["pending_query_spec"] = planned.spec
        session["pending_question"] = question
        return {
            "ok": True,
            "needs_clarification": True,
            "clarification": {
                "dimension_key": planned.dimension_key,
                "question": planned.question,
                "options": [asdict(option) for option in planned.options],
            },
        }

    cached = _query_cache.get(context.report_set, context.report_name, planned)
    if cached is not None:
        _set_session_cache(session, cached, _report_payload_from_context(context, cached, cache_hit=True))
        result = _answer_from_table(question, cached)
        result["cache_hit"] = True
        result["filters"] = _filters_payload(planned)
        return result

    credentials = session["credentials"]
    try:
        multi = await client.extract_query_spec(
            credentials=credentials,  # type: ignore[arg-type]
            report_set=context.report_set,
            report_parameter=context.report_parameter,
            report_name=context.report_name,
            spec=planned,
        )
        tables = {
            report.metric: parse_key_measures_text(report.text, metric_override=report.metric)
            for report in multi.reports
        }
        table: KeyMeasuresTable | MultiKpiTable
        table = MultiKpiTable(tables=tables) if len(tables) > 1 else next(iter(tables.values()))
    except WorldpanelError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"按问题调整 Data Explorer 失败：{exc}") from exc

    _query_cache.set(context.report_set, context.report_name, planned, table)
    _set_session_cache(session, table, _report_payload_from_context(context, table, cache_hit=False))
    result = _answer_from_table(question, table)
    result["cache_hit"] = False
    result["filters"] = _filters_payload(planned)
    return result


async def _ensure_data_explorer_context(
    session: dict[str, object],
    client: WorldpanelClient,
) -> DataExplorerContext:
    context = session.get("data_explorer_context")
    if isinstance(context, DataExplorerContext):
        return context
    current_report = session.get("current_report")
    credentials = session.get("credentials")
    if not isinstance(current_report, dict) or not isinstance(credentials, Credentials):
        raise HTTPException(status_code=400, detail="请先在左侧选择 Data Explorer 报表并点击“读取所选报表”。")
    context = await client.discover_data_explorer_context(
        credentials=credentials,
        report_set=str(current_report["report_set"]),
        report_parameter=str(current_report["report_parameter"]),
        report_name=str(current_report["report_name"]),
    )
    session["data_explorer_context"] = context
    return context


def _answer_from_table(question: str, table: KeyMeasuresTable | MultiKpiTable) -> dict[str, object]:
    try:
        answer = answer_question(question, table)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "needs_clarification": False,
        "answer": answer.text,
        "data": asdict(answer),
    }


def _session(session_id: str) -> dict[str, object]:
    session = _sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=400, detail="登录会话不存在，请重新登录。")
    return session


def _require_access_token(access_token: str | None) -> None:
    if access_token not in _access_tokens:
        raise HTTPException(status_code=403, detail="请先输入邀请码进入应用。")


def _reset_progress(session: dict[str, object], title: str) -> None:
    session["progress_events"] = []
    session["progress_active"] = True
    session["progress_current"] = title
    _progress(session, "running", title)


def _progress(
    session: dict[str, object],
    status: str,
    message: str,
    *,
    active: bool | None = None,
) -> None:
    events = session.setdefault("progress_events", [])
    if not isinstance(events, list):
        events = []
        session["progress_events"] = events
    event = {
        "status": status,
        "message": message,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    events.append(event)
    del events[:-_MAX_PROGRESS_EVENTS]
    session["progress_current"] = message
    if active is not None:
        session["progress_active"] = active


def _restore_pivot_report(http_session: dict[str, object], pivot_session) -> None:
    """Re-attach the prepared report to a pivot session that was recreated after
    an error or TTL expiry, so the next question auto-reprepares the browser
    transparently — the user never has to click 'prepare report' again."""
    if getattr(pivot_session, "current_report", None):
        return
    report = http_session.get("current_report")
    if isinstance(report, dict) and report.get("report_parameter"):
        pivot_session.current_report = {
            "report_set": str(report.get("report_set", "")),
            "report_parameter": str(report.get("report_parameter", "")),
            "report_name": str(report.get("report_name", "")),
        }


def _accumulate_export_rows(
    session: dict[str, object],
    converted: dict[str, KeyMeasuresTable],
    report: dict[str, object],
) -> None:
    """Accumulate every value seen across the whole conversation, keyed by
    (report, metric, product, date), so CSV export covers all processed data —
    not just the latest question."""
    rows = session.get("export_rows")
    if not isinstance(rows, dict):
        rows = {}
        session["export_rows"] = rows
    report_set = str(report.get("report_set", ""))
    report_name = str(report.get("report_name", ""))
    for metric, table in converted.items():
        for date_label, products in table.rows.items():
            for product, value in products.items():
                rows[(report_set, report_name, metric, product, date_label)] = value


def _set_session_cache(
    session: dict[str, object],
    table: KeyMeasuresTable | MultiKpiTable,
    report: dict[str, object],
) -> None:
    session["cached_table"] = table
    session["cached_report"] = report


def _get_session_cache(
    session: dict[str, object],
) -> tuple[KeyMeasuresTable | MultiKpiTable, dict[str, object] | None]:
    table = session.get("cached_table")
    if not isinstance(table, (KeyMeasuresTable, MultiKpiTable)):
        raise HTTPException(status_code=400, detail="还没有缓存数据，请先读取报表。")
    report = session.get("cached_report")
    return table, report if isinstance(report, dict) else None


def _ai_settings(request: AIConfigurationRequest) -> AISettings:
    base_url = request.base_url.strip()
    model = request.model.strip()
    api_key = request.api_key.strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="AI API address must start with http:// or https://")
    if not model:
        raise HTTPException(status_code=400, detail="AI model is required")
    if not api_key:
        raise HTTPException(status_code=400, detail="AI API key is required")
    return AISettings(
        provider=request.provider.strip() or "custom",
        model=model,
        api_key=api_key,
        base_url=base_url,
        endpoint_id=request.endpoint_id.strip() if request.endpoint_id else None,
        timeout_seconds=get_settings().ai.timeout_seconds,
    )


def _session_ai_settings(session_id: str | None) -> AISettings:
    if session_id:
        session = _sessions.get(session_id)
        if session and isinstance(session.get("ai_settings"), AISettings):
            return session["ai_settings"]  # type: ignore[return-value]
    return get_settings().ai


def _redacted_ai_error(exc: Exception) -> str:
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code:
        return f"AI service test failed with HTTP {status_code}"
    return f"AI service test failed: {type(exc).__name__}"


def _first_cached_table(table: KeyMeasuresTable | MultiKpiTable) -> KeyMeasuresTable:
    if isinstance(table, KeyMeasuresTable):
        return table
    for metric_table in table.tables.values():
        return metric_table
    raise HTTPException(status_code=400, detail="当前没有缓存数据。")


def _iter_metric_tables(
    table: KeyMeasuresTable | MultiKpiTable,
) -> list[tuple[str, KeyMeasuresTable]]:
    if isinstance(table, KeyMeasuresTable):
        return [(table.metric, table)]
    return list(table.tables.items())


def _activate_pivot_tables(
    tables: dict[str, object],
    current_report: dict[str, str],
    session: dict[str, object] | None = None,
) -> dict[str, KeyMeasuresTable]:
    global _cached_report, _cached_table
    converted: dict[str, KeyMeasuresTable] = {}
    for metric, table in tables.items():
        try:
            converted[metric] = table.to_key_measures()  # type: ignore[attr-defined]
        except PivotResultError:
            return {}
    if converted:
        active_table: KeyMeasuresTable | MultiKpiTable = (
            MultiKpiTable(tables=converted)
            if len(converted) > 1
            else next(iter(converted.values()))
        )
        active_report = {
            "report_set": str(current_report.get("report_set", "")),
            "report_name": str(current_report.get("report_name", "")),
            "metrics": list(converted.keys()),
            "source": "pivot",
        }
        if session is not None:
            _set_session_cache(session, active_table, active_report)
        else:
            _cached_table = active_table
            _cached_report = active_report
    return converted


def _report_payload_from_context(
    context: DataExplorerContext,
    table: KeyMeasuresTable | MultiKpiTable,
    cache_hit: bool,
) -> dict[str, object]:
    metrics = table.metrics if isinstance(table, MultiKpiTable) else [table.metric]
    return {
        "report_set": context.report_set,
        "report_name": context.report_name,
        "metrics": metrics,
        "cache_hit": cache_hit,
    }


def _context_payload(context: object) -> dict[str, object] | None:
    if not isinstance(context, DataExplorerContext):
        return None
    return {
        "report_set": context.report_set,
        "report_name": context.report_name,
        "dimensions": {
            key: {
                "label": dimension.label,
                "current": dimension.current,
                "options": [asdict(option) for option in dimension.options],
            }
            for key, dimension in context.dimensions.items()
        },
        "segments": [asdict(segment) for segment in context.segments],
        "pivot_slots": {key: list(values) for key, values in context.pivot_slots.items()},
    }


def _filters_payload(spec: QuerySpec) -> dict[str, object]:
    return {
        "products": list(spec.products),
        "metrics": list(spec.metrics),
        "year": spec.year,
        "month": spec.month,
        "full_year": spec.full_year,
        "dimensions": spec.dimensions,
        "segments": list(spec.segments),
    }


def _build_check_prompt(summary: str, issue_messages: list[str]) -> str:
    issues = "\n".join(f"- {message}" for message in issue_messages[:20]) or "- 未发现明显问题"
    return (
        "你是一个数据质量检查助手。请用中文简洁总结以下 Worldpanel 报表检查结果，"
        "指出最需要业务用户关注的问题，并避免编造未提供的数据。\n\n"
        f"本地检查摘要：{summary}\n\n"
        f"问题列表：\n{issues}"
    )


def _query_plan_from_payload(payload: dict[str, object]) -> QueryPlan:
    return QueryPlan(
        report_set=str(payload.get("report_set") or ""),
        report=str(payload.get("report") or ""),
        axis_placements=tuple(
            AxisPlacement(
                dimension=str(item["dimension"]),
                axis=str(item["axis"]),  # type: ignore[arg-type]
                position=int(item.get("position", 0)),
            )
            for item in payload.get("axis_placements", [])  # type: ignore[union-attr]
            if isinstance(item, dict)
        ),
        member_selections=tuple(
            MemberSelection(
                dimension=str(item["dimension"]),
                member_path=tuple(str(part) for part in item.get("member_path", [])),
                checked=bool(item.get("checked", True)),
            )
            for item in payload.get("member_selections", [])  # type: ignore[union-attr]
            if isinstance(item, dict)
        ),
        kpis=tuple(str(value) for value in payload.get("kpis", ["Spend (RMB 000)"])),  # type: ignore[arg-type]
        expected_period=str(payload["expected_period"]) if payload.get("expected_period") else None,
        output_shape=str(payload.get("output_shape") or "single_value"),  # type: ignore[arg-type]
        calculation=str(payload["calculation"]) if payload.get("calculation") else None,
        filters=tuple(
            FilterSelection(role=str(item["role"]), value=str(item["value"]))
            for item in payload.get("filters", [])  # type: ignore[union-attr]
            if isinstance(item, dict) and item.get("role") and item.get("value")
        ),
    )


