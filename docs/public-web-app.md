# WPO AI Reader Web App

This app exposes Worldpanel AI Reader as an invite-gated public trial.

## Access

- Public invite code: `WPO2026ZHEN`
- There is no account registration in the first version.
- Users enter their own AI API endpoint, model, and API key in the browser.
- Users enter their own Worldpanel Online account and password.

## Security Model

- Worldpanel credentials are kept in the in-memory server session only.
- AI API keys are stored in the user's browser local storage and the current in-memory server session after login.
- `.env` Worldpanel login is disabled by default in public deployment.
- Report data cache is scoped to each Worldpanel session.

## Deployment

The recommended first deployment is a Render free Web Service using Docker.

Render can run the FastAPI backend and Playwright browser in one long-lived container. This is more suitable than a serverless-only setup because Data Explorer and Pivot Screen automation need a persistent browser session.

Required environment variables:

- `WPO_INVITE_CODE=WPO2026ZHEN`
- `WPO_ENABLE_ENV_LOGIN=false`
- `WORLDPANEL_HEADLESS=true`

Server-side AI planner (LLM-first). The defaults target **Doubao (Volcano Ark)**,
so you only need to set the API key for the model to be the default recognizer;
without it, the app uses built-in rule-based recognition. Set the key as a Render
secret (never commit it):

- `WPO_AI_API_KEY` — **required** to enable the LLM; your Ark key (`ark-...`)
- `WPO_AI_BASE_URL` — optional override, default `https://ark.cn-beijing.volces.com/api/v3/chat/completions`
- `WPO_AI_MODEL` — optional override, default `ep-20260611143619-l7n26` (Doubao endpoint id; Ark requires the endpoint id, not the model name)
- `WPO_AI_PROVIDER` — optional label, default `doubao`

Notes:
- `WPO_AI_BASE_URL` must be the **chat-completions** URL (OpenAI-compatible
  `{model, messages}` body, `Authorization: Bearer <key>`), not just the host. A
  wrong key/endpoint surfaces as an AI error and the app falls back to rules — so
  verify via `/api/health` (`ai.enabled: true`) and the in-app "测试并保存" button.
- Per-user AI configured in the browser overrides the server default for that
  session, so users can bring their own key.

The service runs:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## User Flow

1. Enter invite code.
2. Configure AI provider, endpoint, model, and API key.
3. Log in to Worldpanel Online.
4. Select Report Set.
5. Select Ready-to-Use category and Data Explorer report.
6. Ask natural-language questions.
7. Copy the answer, download CSV, or run the data checker.

## Known First-Version Limitation

Render free services can sleep after inactivity. The first request after sleep may take longer while the container starts.
