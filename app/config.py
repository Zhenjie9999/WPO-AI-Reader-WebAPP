from __future__ import annotations

from dataclasses import dataclass
import os

from dotenv import load_dotenv

from app.assistant import AISettings


load_dotenv()


@dataclass(frozen=True)
class Settings:
    email: str | None
    password: str | None
    report_set: str
    headless: bool
    timeout_ms: int
    ai: AISettings
    invite_code: str
    public_env_login_enabled: bool
    allowed_origins: tuple[str, ...]
    login_url: str = (
        "https://eu.worldpanelonline.com/Commissioning/SPages/login.aspx"
        "?ReturnUrl=%2fCommissioning%2fPages%2fHome.aspx"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)


def get_settings() -> Settings:
    ai_endpoint_id = _env("WPO_DEFAULT_AI_ENDPOINT_ID") or _env("ARK_ENDPOINT_ID")
    ai_api_key = _env("WPO_DEFAULT_AI_API_KEY") or _env("ARK_API_KEY") or None
    ai_model = _env("WPO_DEFAULT_AI_MODEL") or ("doubao-seed-2.0-lite" if ai_endpoint_id else "")
    ai_base_url = _env("WPO_DEFAULT_AI_BASE_URL") or (
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        if ai_endpoint_id or ai_api_key
        else ""
    )
    ai_provider = _env("WPO_DEFAULT_AI_PROVIDER") or ("doubao" if ai_endpoint_id else "custom")

    return Settings(
        email=os.getenv("WORLDPANEL_EMAIL"),
        password=os.getenv("WORLDPANEL_PASSWORD"),
        report_set=os.getenv("WORLDPANEL_REPORT_SET", "CN - Zespri - CS"),
        headless=os.getenv("WORLDPANEL_HEADLESS", "true").lower() != "false",
        timeout_ms=int(os.getenv("WORLDPANEL_TIMEOUT_MS", "60000")),
        invite_code=os.getenv("WPO_INVITE_CODE", "WPO2026ZHEN"),
        public_env_login_enabled=os.getenv("WPO_ENABLE_ENV_LOGIN", "false").lower() == "true",
        allowed_origins=tuple(
            origin.strip()
            for origin in os.getenv("WPO_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        ),
        ai=AISettings(
            provider=ai_provider,
            model=ai_model,
            api_key=ai_api_key,
            base_url=ai_base_url,
            endpoint_id=ai_endpoint_id or None,
            timeout_seconds=60,
        ),
    )


def _env(name: str) -> str:
    return os.getenv(name, "").strip()
