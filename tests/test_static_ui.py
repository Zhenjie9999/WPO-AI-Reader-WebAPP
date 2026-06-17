from pathlib import Path


APP_JS = Path("app/static/app.js")
INDEX_HTML = Path("app/static/index.html")


def test_public_frontend_contains_invite_ai_and_export_controls():
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "WPO AI Reader" in html
    assert "inviteCodeInput" in html
    assert "apiPresetSelect" in html
    assert "Doubao" in html
    assert "copyAnswerButton" in html
    assert "downloadCsvButton" in html
    assert "progressBoard" in html
    assert "progressList" in html
    assert "envLoginButton" not in html


def test_frontend_persists_and_restores_user_ai_configuration():
    script = APP_JS.read_text(encoding="utf-8")
    html = INDEX_HTML.read_text(encoding="utf-8")

    assert "wpo-ai-configuration" in script
    assert "localStorage.setItem" in script
    assert "localStorage.getItem" in script
    assert 'postJson("/api/ai/test"' in script
    assert "/api/sessions/${sessionId}/ai" in script
    assert 'id="aiBaseUrlInput"' in html
    assert 'id="aiModelInput"' in html
    assert 'id="aiApiKeyInput" type="password"' in html
    assert 'id="saveAiButton"' in html
    assert 'id="clearAiButton"' in html


def test_frontend_submits_typed_clarification_instead_of_starting_new_question():
    script = APP_JS.read_text(encoding="utf-8")

    assert "pendingClarification" in script
    assert "submitClarification(pendingClarification.dimensionKey, question" in script
    assert "pendingQuestion = question;" in script


def test_frontend_does_not_silently_ignore_clarification_when_question_state_is_missing():
    script = APP_JS.read_text(encoding="utf-8")

    assert "请选择后重新输入完整问题。" in script
    assert "if (!pendingQuestion) return;" not in script


def test_frontend_uses_live_pivot_plan_execute_and_displays_verified_receipt():
    script = APP_JS.read_text(encoding="utf-8")

    assert 'postJson("/api/pivot/plan"' in script
    assert 'postJson("/api/pivot/execute"' in script


def test_frontend_polls_session_progress_for_long_running_data_pull():
    script = APP_JS.read_text(encoding="utf-8")

    assert "/api/sessions/${sessionId}/progress" in script
    assert "startProgress(" in script
    assert "Data pull completed" in script
    assert "执行凭证" in script
    assert "receipt.verified" in script
