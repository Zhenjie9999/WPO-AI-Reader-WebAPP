import pytest

from app.assistant import AISettings, AssistantClient, build_ai_status
from app.config import get_settings


def test_build_ai_status_uses_one_shared_configuration():
    settings = AISettings(
        provider="custom",
        model="gpt-5.4",
        api_key="secret",
        base_url="http://example.test/jdgpt/v1/chat/completions",
    )

    status = build_ai_status(settings)

    assert status == {
        "provider": "custom",
        "model": "gpt-5.4",
        "enabled": True,
        "base_url": "http://example.test/jdgpt/v1/chat/completions",
    }


def test_build_ai_status_marks_missing_key_as_local_mode():
    settings = AISettings(
        provider="custom",
        model="gpt-5.4",
        api_key=None,
        base_url="http://example.test/jdgpt/v1/chat/completions",
    )

    status = build_ai_status(settings)

    assert status["enabled"] is False
    assert status["provider"] == "local-rules"


def test_environment_api_key_does_not_enable_runtime_ai(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used-either")
    monkeypatch.setenv("AI_BASE_URL", "https://example.com/v1/chat/completions")
    monkeypatch.setenv("AI_MODEL", "must-not-be-used")

    settings = get_settings()

    assert settings.ai.enabled is False
    assert settings.ai.api_key is None
    assert settings.ai.base_url == ""
    assert settings.ai.model == ""


@pytest.mark.asyncio
async def test_assistant_client_posts_openai_compatible_payload():
    captured = {}

    async def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "choices": [
                        {"message": {"content": "检查结果正常。"}}
                    ]
                }

        return Response()

    settings = AISettings(
        provider="custom",
        model="gpt-5.4",
        api_key="secret",
        base_url="http://example.test/jdgpt/v1/chat/completions",
    )

    result = await AssistantClient(settings, post=fake_post).chat("请检查数据")

    assert result == "检查结果正常。"
    assert captured["url"] == "http://example.test/jdgpt/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "gpt-5.4"
    assert captured["json"]["messages"] == [{"role": "user", "content": "请检查数据"}]
