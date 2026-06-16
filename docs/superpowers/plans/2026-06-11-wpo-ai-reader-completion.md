# WPO AI Reader Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the original Worldpanel AI Reader reliably submit Pivot member selections and accept per-browser AI service configuration, then publish the direct-reading Skill separately as public `WPO-AI-Reader`.

**Architecture:** Browser AI configuration is restored from `localStorage`, tested through a dedicated endpoint, and bound only to the current in-memory Worldpanel session. Pivot member selection uses Telerik's `membersTree` API and `saveSelectedMemOrdinals()` before applying the Pivot; rendered headers and selected members are reread as proof. The reusable direct-reading Skill lives in a separate public repository; the application updates remain in the original repository.

**Tech Stack:** FastAPI, Pydantic, Playwright, Telerik legacy controls, vanilla HTML/CSS/JavaScript, pytest, GitHub CLI.

---

### Task 1: Browser-managed AI configuration

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `app/worldpanel/pivot_service.py`
- Modify: `app/static/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/styles.css`
- Test: `tests/test_assistant.py`
- Test: `tests/test_pivot_api.py`
- Test: `tests/test_static_ui.py`

- [x] Write failing tests proving environment API keys do not enable AI, session AI configurations are isolated and redacted, and the frontend persists/restores/tests/clears configuration.
- [x] Run the focused tests and confirm they fail for the missing session configuration behavior.
- [x] Add test/bind/clear AI endpoints and pass session AI settings into Pivot planning and data checking.
- [x] Add the sidebar AI configuration UI backed by `localStorage`.
- [x] Run focused tests and confirm they pass.

### Task 2: Real Telerik Pivot member submission

**Files:**
- Modify: `app/worldpanel/pivot_driver.py`
- Modify: `app/worldpanel/executor.py`
- Modify: `app/worldpanel/pivot_result.py`
- Test: `tests/test_pivot_redesign.py`
- Test: `tests/test_pivot_result.py`

- [x] Write failing tests requiring `membersTree.unselectAllNodes()`, exact member selection, `saveSelectedMemOrdinals()`, and exact date-label resolution.
- [x] Run focused tests and confirm they fail.
- [x] Implement Telerik selection submission and wait for the selector dialog to close.
- [x] Reject execution when the rendered table does not contain a requested member.
- [x] Support exact date labels and year resolution.
- [x] Run focused tests and confirm they pass.

### Task 3: Skill and documentation

**Files:**
- Create: `skills/wpo-ai-reader/SKILL.md`
- Modify: `README.md`
- Modify: `.env.example`

- [x] Document browser-managed AI configuration and remove instructions that enable AI with `.env`.
- [x] Document the reliable Pivot member submission and verification rule.
- [x] Validate the Skill structure and install/copy it into the local Codex skills directory.

### Task 4: Verification and public release

**Files:**
- Verify: all source and test files
- Publish: GitHub repository `WPO-AI-Reader`

- [x] Run `python -m pytest -q` and require all tests to pass.
- [x] Run an authorized live Fanta TM query and prove the final table header is `Fanta TM`.
- [x] Run a historical reporting-period query and prove both 2023 and 2024 values.
- [x] Audit tracked files and history for secrets; ensure `.env` and `runtime/` are excluded.
- [x] Commit the completed implementation.
- [x] Create public GitHub repository `WPO-AI-Reader` containing only the Skill resource.
- [x] Push the completed application updates to the original `worldpanel-ai-reader` repository.
