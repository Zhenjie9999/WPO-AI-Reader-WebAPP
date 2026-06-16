# WPO Public Web App Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Worldpanel AI Reader into a public invite-gated web app where each user enters their own AI API configuration and Worldpanel Online credentials, then asks natural-language questions against Data Explorer.

**Architecture:** Keep the existing FastAPI + Playwright backend because Data Explorer requires a long-lived browser session. Serve the first usable frontend from the FastAPI app on Render free Web Service; keep the frontend static and API-compatible so it can be moved to Vercel later. Store Worldpanel credentials, AI settings, and current cached data per session in memory, with browser-local restore only for the user's AI config.

**Tech Stack:** Python 3.11, FastAPI, Playwright Chromium, vanilla HTML/CSS/JavaScript, pytest, Render Web Service, optional Vercel static frontend later.

---

## File Structure

- Modify `app/config.py`: add invite-code, public-login, CORS, and runtime flags.
- Modify `app/main.py`: add invite access sessions, protect public API endpoints, move cached table/report into each Worldpanel session, add CSV export, and disable environment login by default.
- Modify `app/static/index.html`: replace the local-tool layout with an Apple-style public onboarding flow.
- Modify `app/static/app.js`: add invite verification, AI provider presets, local AI restore, copy/CSV actions, and remove public env-login usage.
- Modify `app/static/styles.css`: implement the polished responsive visual system.
- Create `tests/test_access_api.py`: verify invite gate and disabled env-login.
- Modify `tests/test_pivot_api.py`: verify session-scoped cache behavior.
- Modify `tests/test_static_ui.py`: verify public UI contains invite, API config, Worldpanel credentials, and no env-login button.
- Create `Dockerfile`: Render-compatible Playwright deployment image.
- Create `render.yaml`: default Render Web Service config.
- Create `docs/public-web-app.md`: short operator guide for deployment and user flow.

## Task 1: Invite Gate And Public Settings

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`
- Create: `tests/test_access_api.py`

- [ ] **Step 1: Write failing invite tests**

```python
from fastapi.testclient import TestClient

from app.main import app


def test_invite_code_returns_access_token():
    client = TestClient(app)
    response = client.post("/api/access", json={"invite_code": "WPO2026ZHEN"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["access_token"], str)
    assert len(payload["access_token"]) > 20


def test_wrong_invite_code_is_rejected():
    client = TestClient(app)
    response = client.post("/api/access", json={"invite_code": "wrong"})
    assert response.status_code == 403


def test_public_env_login_is_disabled_by_default():
    client = TestClient(app)
    response = client.post("/api/login-env")
    assert response.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_access_api.py -q`

Expected: FAIL because `/api/access` does not exist and `/api/login-env` is still callable when `.env` exists.

- [ ] **Step 3: Add settings**

Add these fields to `Settings` and `get_settings()` in `app/config.py`:

```python
invite_code: str
public_env_login_enabled: bool
allowed_origins: tuple[str, ...]
```

Use defaults:

```python
invite_code=os.getenv("WPO_INVITE_CODE", "WPO2026ZHEN")
public_env_login_enabled=os.getenv("WPO_ENABLE_ENV_LOGIN", "false").lower() == "true"
allowed_origins=tuple(
    origin.strip()
    for origin in os.getenv("WPO_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
)
```

- [ ] **Step 4: Add access endpoint**

In `app/main.py`, create:

```python
class AccessRequest(BaseModel):
    invite_code: str


_access_tokens: set[str] = set()


@app.post("/api/access")
async def grant_access(request: AccessRequest) -> dict[str, object]:
    if request.invite_code.strip() != get_settings().invite_code:
        raise HTTPException(status_code=403, detail="邀请码不正确")
    token = str(uuid4())
    _access_tokens.add(token)
    return {"ok": True, "access_token": token}
```

Change `/api/login-env` to return HTTP 403 unless `public_env_login_enabled` is true.

- [ ] **Step 5: Run invite tests**

Run: `python -m pytest tests/test_access_api.py -q`

Expected: PASS.

## Task 2: Session-Scoped Data Cache

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_pivot_api.py`

- [ ] **Step 1: Write failing cache isolation test**

Add a test that calls a helper to store cached data under two different session dictionaries and asserts that each session keeps its own `cached_table` and `cached_report`.

```python
def test_cached_data_is_session_scoped():
    assert "_cached_table" not in public_health_payload_for_sessions()
```

If direct helper coverage is easier, expose small private helpers `_set_session_cache(session, table, report)` and `_get_session_cache(session)`.

- [ ] **Step 2: Run the focused tests**

Run: `python -m pytest tests/test_pivot_api.py -q`

Expected: FAIL until helpers and endpoint changes exist.

- [ ] **Step 3: Implement cache helpers**

In `app/main.py`:

```python
def _set_session_cache(session: dict[str, object], table: KeyMeasuresTable | MultiKpiTable, report: dict[str, object]) -> None:
    session["cached_table"] = table
    session["cached_report"] = report


def _get_session_cache(session: dict[str, object]) -> tuple[KeyMeasuresTable | MultiKpiTable, dict[str, object] | None]:
    table = session.get("cached_table")
    if not isinstance(table, (KeyMeasuresTable, MultiKpiTable)):
        raise HTTPException(status_code=400, detail="还没有缓存数据，请先读取报表。")
    report = session.get("cached_report")
    return table, report if isinstance(report, dict) else None
```

- [ ] **Step 4: Replace global cache reads/writes**

Update `/api/refresh`, `/api/ask`, `/api/check`, `/api/pivot/execute`, `_ask_with_data_explorer`, and `_activate_pivot_tables` so all session requests read and write `session["cached_table"]` and `session["cached_report"]`. Keep the old global cache only for no-session local fallback.

- [ ] **Step 5: Run pivot and static tests**

Run: `python -m pytest tests/test_pivot_api.py tests/test_static_ui.py -q`

Expected: PASS.

## Task 3: Public Apple-Style Frontend

**Files:**
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/styles.css`
- Modify: `tests/test_static_ui.py`

- [ ] **Step 1: Write static UI tests**

Assert that the page includes:

```python
assert "WPO AI Reader" in html
assert "inviteCodeInput" in html
assert "apiPresetSelect" in html
assert "Doubao" in html
assert "copyAnswerButton" in html
assert "downloadCsvButton" in html
assert "envLoginButton" not in html
```

- [ ] **Step 2: Run static UI tests**

Run: `python -m pytest tests/test_static_ui.py -q`

Expected: FAIL because the current UI is still the local prototype.

- [ ] **Step 3: Implement the public UI**

Create a four-stage first screen:

1. Invite code: one input, one continue button.
2. AI setup: provider preset, endpoint, model, API key, test and save.
3. Worldpanel login: email/password only.
4. Workbench: Report Set, Ready-to-Use report, chat, copy answer, CSV download, data check.

Use browser `localStorage` for `wpo-access-token` and `wpo-ai-configuration`.

- [ ] **Step 4: Add model presets**

In `app/static/app.js`, define:

```javascript
const AI_PRESETS = {
  custom: { label: "Custom", baseUrl: "", model: "" },
  openai: { label: "OpenAI compatible", baseUrl: "https://api.openai.com/v1/chat/completions", model: "gpt-4.1" },
  deepseek: { label: "DeepSeek", baseUrl: "https://api.deepseek.com/chat/completions", model: "deepseek-chat" },
  doubao: { label: "Doubao", baseUrl: "https://ark.cn-beijing.volces.com/api/v3/chat/completions", model: "doubao-seed-1-6-250615" },
};
```

- [ ] **Step 5: Run frontend tests**

Run: `python -m pytest tests/test_static_ui.py -q`

Expected: PASS.

## Task 4: CSV Export And Copyable Answers

**Files:**
- Modify: `app/main.py`
- Modify: `app/static/app.js`
- Create or modify: `tests/test_access_api.py`

- [ ] **Step 1: Write failing export test**

Create a session with a small `KeyMeasuresTable`, call `/api/export.csv?session_id=...`, and assert:

```python
assert response.status_code == 200
assert response.headers["content-type"].startswith("text/csv")
assert "product,date,metric,value" in response.text
```

- [ ] **Step 2: Run export test**

Run: `python -m pytest tests/test_access_api.py -q`

Expected: FAIL because export endpoint does not exist.

- [ ] **Step 3: Implement export**

Add `/api/export.csv` in `app/main.py`. It reads the session-scoped cached table and returns product/date/metric/value rows. For `MultiKpiTable`, emit rows for every metric table.

- [ ] **Step 4: Wire buttons**

In `app/static/app.js`, store the latest answer text, use `navigator.clipboard.writeText(latestAnswerText)` for copy, and open `/api/export.csv?session_id=${encodeURIComponent(sessionId)}` for CSV.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_access_api.py tests/test_static_ui.py -q`

Expected: PASS.

## Task 5: Render Deployment

**Files:**
- Create: `Dockerfile`
- Create: `render.yaml`
- Create: `docs/public-web-app.md`

- [ ] **Step 1: Add Dockerfile**

Use a Playwright Python image:

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV WORLDPANEL_HEADLESS=true
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Add render.yaml**

```yaml
services:
  - type: web
    name: wpo-ai-reader
    env: docker
    plan: free
    autoDeploy: true
    envVars:
      - key: WPO_INVITE_CODE
        value: WPO2026ZHEN
      - key: WPO_ENABLE_ENV_LOGIN
        value: "false"
      - key: WORLDPANEL_HEADLESS
        value: "true"
```

- [ ] **Step 3: Add operator doc**

Document:

- Render full-stack first deployment.
- Invite code `WPO2026ZHEN`.
- User credentials and AI keys are typed by users and kept in session/browser storage.
- No registration, no shared server-side AI key.
- Free Render services may sleep after inactivity, so first login can be slow.

- [ ] **Step 4: Run full test suite**

Run: `python -m pytest -q`

Expected: PASS.

## Task 6: Figma Review Design

**Files:**
- Figma file, no repository file required unless exported screenshots are added later.

- [ ] **Step 1: Create a Figma file**

Create a file named `WPO AI Reader Public Web App`.

- [ ] **Step 2: Generate four frames**

Frames:

- Invite gate.
- AI setup.
- Worldpanel login and report selection.
- Workbench with chat answer, filter receipt, copy and CSV actions.

- [ ] **Step 3: Match implementation**

Use the implemented UI as the source of truth: neutral white/gray Apple-style surfaces, blue accent, 8px controls, clear progressive flow, no logo requirement.

- [ ] **Step 4: Share Figma link**

Include the Figma link in the final delivery message.

## Self-Review

- Spec coverage: Invite gate, no registration, user-provided API config, Doubao preset, user-provided Worldpanel credentials, natural language Data Explorer flow, copy/CSV export, Render deployment, and no public env-login are covered.
- Security: Session cache is per user; credentials remain in memory; AI key is browser-local plus current server session only; `.env` AI remains disabled.
- Deployment: Render is the first full-stack target because Playwright needs a long-running browser process. Vercel can host the static frontend later once the Render API URL is stable.
- Remaining limitation: Render free spin-down may make first use slow; this is acceptable for the first open trial.
