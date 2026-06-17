# WPO AI Reader Web App

Invite-gated web app for reading authorized Worldpanel Online Data Explorer reports with natural-language questions.

## What It Does

- Users enter invite code `WPO2026ZHEN`.
- Users can enter their own AI API endpoint, model, and API key, or use the server default AI configured by the deployer.
- Users enter their own Worldpanel Online account and password.
- The backend uses Playwright to operate Worldpanel Online, including Data Explorer and Pivot Screen.
- Answers include the result plus an execution receipt.
- Users can copy answers, download the current data as CSV, and run a data check.

## Run Locally

```bash
pip install -r requirements.txt
python -m playwright install chromium
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

## Test

```bash
python -m pytest -q
```

## Deploy

The first recommended deployment target is Render free Web Service with Docker.

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://dashboard.render.com/blueprint/new?repo=https://github.com/Zhenjie9999/WPO-AI-Reader-WebAPP)

Required environment variables:

- `WPO_INVITE_CODE=WPO2026ZHEN`
- `WPO_ENABLE_ENV_LOGIN=false`
- `WORLDPANEL_HEADLESS=true`
- `WORLDPANEL_TIMEOUT_MS=90000`
- `WPO_DEFAULT_AI_PROVIDER=doubao`
- `WPO_DEFAULT_AI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3/chat/completions`
- `WPO_DEFAULT_AI_MODEL=doubao-seed-2.0-lite`
- `WPO_DEFAULT_AI_ENDPOINT_ID=ep-20260611143619-l7n26`
- `WPO_DEFAULT_AI_API_KEY=<set as a Render secret>`

The Docker start command binds to Render's `$PORT`, and the Blueprint uses `/api/health` as the HTTP health check.

Render may sleep after inactivity on the free plan, so the first login after sleep can be slow.

## Security Notes

- Do not commit `.env`, browser profiles, runtime traces, or exported confidential data.
- Worldpanel credentials stay in the in-memory server session.
- User-supplied AI keys are saved in the user's browser local storage and current in-memory server session only.
- The server default AI key must be configured as a hosting secret, not committed to Git.
- `.env` login is disabled by default in the public app.
