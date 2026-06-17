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

Optional server-side AI planner (LLM-first). When set, the deployed app maps
natural-language questions to the live schema with the model and only falls back
to built-in rule-based recognition if the call fails. Set these as Render
secrets (do not commit them):

- `WPO_AI_BASE_URL` — full chat-completions endpoint, e.g. `https://api.openai.com/v1/chat/completions`
- `WPO_AI_MODEL` — e.g. `gpt-4o-mini` or your model id
- `WPO_AI_API_KEY` — your provider key
- `WPO_AI_PROVIDER` — label only, e.g. `openai`

Notes:
- `WPO_AI_BASE_URL` must be the **chat-completions** URL (OpenAI-compatible
  `{model, messages}` body, `Authorization: Bearer <key>`), not just the host. A
  wrong path or key surfaces as an AI error and the app silently uses rules — so
  verify via `/api/health` (`ai.enabled: true`) and the in-app "测试并保存" button.
- Per-user AI configured in the browser overrides the server default for that
  session, so users can bring their own key without any server key set.

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
