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
            # Defaults target Doubao (Volcano Ark, OpenAI-compatible). Only the
            # API key is secret and must come from the environment — never commit
            # it (this repo is public). With WPO_AI_API_KEY set, the app is
            # LLM-first by default; users can still override per session.
            provider=os.getenv("WPO_AI_PROVIDER", "doubao"),
            model=os.getenv("WPO_AI_MODEL", "ep-20260611143619-l7n26"),
            api_key=os.getenv("WPO_AI_API_KEY") or None,
            base_url=os.getenv(
                "WPO_AI_BASE_URL",
                "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            ),
            timeout_seconds=float(os.getenv("WPO_AI_TIMEOUT_SECONDS", "60")),
        ),
    )
