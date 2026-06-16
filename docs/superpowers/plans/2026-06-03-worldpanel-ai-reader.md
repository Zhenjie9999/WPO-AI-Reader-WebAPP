# Worldpanel AI Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local MVP that can log into WorldpanelOnline, extract the Zespri Key Measures table, and answer common natural-language data questions.

**Architecture:** FastAPI serves a simple chat UI and JSON endpoints. Playwright handles login and navigation. Parser and query modules keep extraction and question interpretation testable without live website access.

**Tech Stack:** Python 3.11+, FastAPI, Playwright, pytest, vanilla HTML/CSS/JavaScript.

---

### Task 1: Project Skeleton

**Files:**
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `README.md`

- [ ] Add package metadata and dependencies.
- [ ] Add an environment template that keeps credentials out of source.
- [ ] Document setup, browser install, test, and run commands.

### Task 2: Parser And Query Tests

**Files:**
- Create: `tests/test_parser.py`
- Create: `tests/test_query.py`

- [ ] Write tests for converting representative Key Measures text into date/product/value rows.
- [ ] Write tests for mapping Chinese questions to product/date/metric parameters.
- [ ] Run tests and confirm they fail before implementation.

### Task 3: Parser And Query Implementation

**Files:**
- Create: `app/worldpanel/parser.py`
- Create: `app/worldpanel/query.py`
- Create: `app/worldpanel/__init__.py`

- [ ] Implement minimal parsing for the current Key Measures shape.
- [ ] Implement date, product, and metric recognition.
- [ ] Run unit tests and confirm they pass.

### Task 4: Website Automation Client

**Files:**
- Create: `app/config.py`
- Create: `app/worldpanel/client.py`

- [ ] Read settings from environment variables.
- [ ] Implement browser login and report navigation using stable selectors observed in the feasibility check.
- [ ] Extract report iframe text from `#NavigationReportPanel`.

### Task 5: API And Frontend

**Files:**
- Create: `app/main.py`
- Create: `app/static/index.html`
- Create: `app/static/styles.css`
- Create: `app/static/app.js`

- [ ] Add `/api/health`, `/api/refresh`, and `/api/ask`.
- [ ] Serve a compact local chat UI.
- [ ] Keep UI controls working without exposing credentials.

### Task 6: Verification

**Files:**
- Modify: `README.md`

- [ ] Run unit tests.
- [ ] Start the local server.
- [ ] Verify the UI loads and health endpoint responds.
- [ ] Document any live-site setup that requires environment variables or Playwright browsers.
