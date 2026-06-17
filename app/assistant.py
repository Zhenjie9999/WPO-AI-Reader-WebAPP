from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx


@dataclass(frozen=True)
class AISettings:
    provider: str
    model: str
    api_key: str | None
    base_url: str
    endpoint_id: str | None = None
    timeout_seconds: float = 60.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url)


def build_ai_status(settings: AISettings) -> dict[str, object]:
    if not settings.enabled:
        return {
            "provider": "local-rules",
            "model": "built-in",
            "enabled": False,
        }
    status = {
        "provider": settings.provider,
        "model": settings.model,
        "enabled": True,
        "base_url": settings.base_url,
    }
    if settings.endpoint_id:
        status["endpoint_id"] = settings.endpoint_id
    return status


def summarize_check_with_shared_ai(local_summary: str, ai_settings: AISettings) -> str:
    if not ai_settings.enabled:
        return f"{local_summary}\n\n当前未配置外部 AI，使用本地规则完成检查。"
    return f"{local_summary}\n\n当前 AI 配置：{ai_settings.provider} / {ai_settings.model}。"


PostCallable = Callable[..., Awaitable[Any]]


class AssistantClient:
    def __init__(self, settings: AISettings, post: PostCallable | None = None):
        self.settings = settings
        self._post = post

    async def chat(self, prompt: str) -> str:
        if not self.settings.enabled:
            raise RuntimeError("AI API is not configured")

        payload = {
            "model": self.settings.endpoint_id or self.settings.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }

        if self._post:
            response = await self._post(
                self.settings.base_url,
                headers=headers,
                json=payload,
                timeout=self.settings.timeout_seconds,
            )
        else:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self.settings.base_url,
                    headers=headers,
                    json=payload,
                    timeout=self.settings.timeout_seconds,
                )

        response.raise_for_status()
        return _extract_message_content(response.json())


def _extract_message_content(payload: dict[str, Any]) -> str:
    try:
        return str(payload["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("AI API response did not include choices[0].message.content") from exc
