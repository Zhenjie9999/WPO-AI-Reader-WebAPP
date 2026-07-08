from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from app.worldpanel.datastore import DataStore
from app.worldpanel.pivot_result import answer_from_pivot_tables, format_plain, table_from_grid


logger = logging.getLogger(__name__)

# Users who explicitly want fresh numbers must always reach the live pull.
_FRESH_PULL_RE = re.compile(r"重新拉|重新查|重拉|再拉一次|刷新数据|refresh|re-?pull|latest data", re.IGNORECASE)


async def try_local_answer(
    question: str,
    account: str,
    datastore: DataStore,
    assistant: Any,
) -> dict[str, object] | None:
    """Answer from already-pulled local data when — and only when — every cell
    the question needs is on record. Anything uncertain returns None so the
    caller falls through to the live browser pull.

    This is the second step of the "data lands locally, answers query locally"
    architecture: repeat and derivative questions (rankings, comparisons over
    known members/periods) come back in seconds instead of minutes."""
    if assistant is None or _FRESH_PULL_RE.search(question):
        return None
    try:
        catalog = datastore.catalog(account)
    except Exception:
        logger.warning("Datastore catalog failed", exc_info=True)
        return None
    if not catalog["metrics"] or not catalog["members"] or not catalog["dates"]:
        return None
    if catalog.get("members_truncated"):
        # An incomplete member list would make "not listed -> not answerable"
        # judgements unsafe; let the live path handle it.
        return None

    prompt = (
        "You decide if a Worldpanel question can be answered ENTIRELY from the locally "
        "stored data described below. The store only has these exact metrics, members and "
        "date labels — nothing else. If the question needs ANY member, KPI, period, channel "
        "filter, growth calculation, or breakdown not present below, or you are unsure, "
        "answer {\"answerable\": false}. Copy labels EXACTLY as listed. Return JSON only: "
        "{\"answerable\": bool, \"metric\": string, \"members\": [string], \"dates\": [string], "
        "\"ranking\": {\"direction\": \"max\"|\"min\", \"top_n\": int}|null}. "
        "Use \"ranking\" for superlative questions (最高/最低/排名/top N) over the listed members.\n"
        f"Question: {question}\n"
        f"Metrics: {json.dumps(catalog['metrics'], ensure_ascii=False)}\n"
        f"Members: {json.dumps(catalog['members'], ensure_ascii=False)}\n"
        f"Dates: {json.dumps(catalog['dates'], ensure_ascii=False)}\n"
    )
    try:
        response = await assistant.chat(prompt)
        match = re.search(r"\{.*\}", response, flags=re.DOTALL)
        spec = json.loads(match.group(0)) if match else {}
    except Exception as exc:
        logger.warning("Local-answer LLM failed (%s: %s)", type(exc).__name__, exc)
        return None
    if not isinstance(spec, dict) or not spec.get("answerable"):
        return None

    metric = str(spec.get("metric") or "")
    members = [str(member) for member in spec.get("members", []) if str(member).strip()]
    dates = [str(date) for date in spec.get("dates", []) if str(date).strip()]
    # Hard guard: the LLM may only pick from the catalog, never invent.
    if metric not in catalog["metrics"]:
        return None
    known_members = set(catalog["members"])
    known_dates = set(catalog["dates"])
    if not members or not dates:
        return None
    if any(member not in known_members for member in members):
        return None
    if any(date not in known_dates for date in dates):
        return None

    cells = datastore.fetch_cells(account, metric, members, dates)
    if cells is None:
        return None
    # Completeness guard: every requested cell must exist, or the local answer
    # could silently misrepresent partial data.
    if any((member, date) not in cells for member in members for date in dates):
        return None

    dates = _chronological(dates)
    table = table_from_grid(
        members,
        [[date, [format_plain(cells[(member, date)]) for member in members]] for date in dates],
        metric=metric,
    )
    ranking = spec.get("ranking") if isinstance(spec.get("ranking"), dict) else None
    answer = answer_from_pivot_tables(
        {metric: table},
        [] if ranking else members,
        dates[-1] if len(dates) == 1 else None,
        ranking=ranking,
    )
    return {
        "answer": answer,
        "source": {
            "kind": "local-store",
            "metric": metric,
            "members": len(members),
            "dates": dates,
            "updated_at": catalog.get("updated_at"),
        },
    }


def _chronological(dates: list[str]) -> list[str]:
    """Sort dd-MMM-yy labels by real date; unknown formats keep their order."""
    try:
        return sorted(dates, key=lambda label: datetime.strptime(label, "%d-%b-%y"))
    except ValueError:
        return dates
