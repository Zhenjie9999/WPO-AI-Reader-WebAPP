from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence


logger = logging.getLogger(__name__)


async def pick_option(
    assistant: Any,
    *,
    question: str,
    term: str,
    options: Sequence[str],
    purpose: str,
) -> str | None:
    """Ask the LLM which REAL option the user's term refers to.

    The model can only answer with an index into `options`, so casing,
    abbreviations, shorthand and cross-language phrasing all resolve to an
    exact live label — or to None, never to an invented value."""
    if assistant is None or not options:
        return None
    lines = "\n".join(f"{index}: {option}" for index, option in enumerate(options))
    prompt = (
        f"The user's term '{term}' must map to ONE of the real {purpose} options below "
        "(from the live report). Match by meaning — ignore case, spacing, abbreviations, "
        "shorthand, and language (Chinese/English). If none clearly matches, use null. "
        "Return JSON only: {\"index\": int|null}.\n"
        f"Full question: {question}\n"
        f"Options:\n{lines}"
    )
    try:
        payload = _extract_json(await assistant.chat(prompt))
    except Exception as exc:
        logger.warning("Semantic option pick failed (%s: %s)", type(exc).__name__, exc)
        return None
    index = payload.get("index")
    try:
        index = int(index)
    except (TypeError, ValueError):
        return None
    if 0 <= index < len(options):
        return options[index]
    return None


async def related_indices(
    assistant: Any,
    *,
    question: str,
    items: Sequence[str],
    cap: int = 8,
) -> list[int]:
    """Nothing matched exactly — ask the LLM which items are RELATED to the
    question, so the user can be shown real, clickable candidates instead of
    a dead-end error."""
    if assistant is None or not items:
        return []
    lines = "\n".join(f"{index}: {item}" for index, item in enumerate(items))
    prompt = (
        "No item matched the user's question exactly. List the indices of up to "
        f"{cap} items RELATED to what the user is asking about (same category, similar "
        "name, likely abbreviation or translation), most likely first. Return JSON only: "
        "{\"indices\": [int, ...]}; use [] when nothing is even related.\n"
        f"Question: {question}\n"
        f"Items:\n{lines}"
    )
    try:
        payload = _extract_json(await assistant.chat(prompt))
    except Exception as exc:
        logger.warning("Semantic related-items lookup failed (%s: %s)", type(exc).__name__, exc)
        return []
    result: list[int] = []
    for raw in payload.get("indices", []):
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(items) and index not in result:
            result.append(index)
        if len(result) >= cap:
            break
    return result


def _extract_json(value: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", value, flags=re.DOTALL)
    if not match:
        raise ValueError("Response did not contain JSON")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise ValueError("Response must be a JSON object")
    return payload
