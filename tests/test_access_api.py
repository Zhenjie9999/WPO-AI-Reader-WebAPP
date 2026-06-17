from fastapi.testclient import TestClient

from app.main import _progress, _reset_progress, _sessions, app
from app.worldpanel.client import Credentials
from app.worldpanel.parser import parse_key_measures_text


def test_invite_code_returns_access_token():
    response = TestClient(app).post("/api/access", json={"invite_code": "WPO2026ZHEN"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert isinstance(payload["access_token"], str)
    assert len(payload["access_token"]) > 20


def test_wrong_invite_code_is_rejected():
    response = TestClient(app).post("/api/access", json={"invite_code": "wrong"})

    assert response.status_code == 403


def test_login_requires_invite_access_token():
    response = TestClient(app).post(
        "/api/login",
        json={"email": "person@example.com", "password": "secret"},
    )

    assert response.status_code == 403


def test_public_env_login_is_disabled_by_default():
    response = TestClient(app).post("/api/login-env")

    assert response.status_code == 403


def test_export_csv_uses_session_scoped_cache():
    _sessions["csv-session"] = {
        "credentials": Credentials("one@example.com", "password"),
        "cached_report": {"report_set": "Set A", "report_name": "Data Explorer"},
        "cached_table": parse_key_measures_text(
            """
Key Measures Data Table
Coke - Update
Spend (RMB 000)
Coke TM

26-Dec-25
4,000
"""
        ),
    }

    response = TestClient(app).get("/api/export.csv?session_id=csv-session")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "product,date,metric,value" in response.text
    assert "Coke TM,26-Dec-25,Spend (RMB 000),4000" in response.text


def test_session_progress_api_returns_current_operation_events():
    _sessions["progress-session"] = {"credentials": Credentials("one@example.com", "password")}
    _reset_progress(_sessions["progress-session"], "Prepare Data Explorer")
    _progress(_sessions["progress-session"], "running", "Reading current KPI table")
    _progress(_sessions["progress-session"], "done", "Prepared table", active=False)

    response = TestClient(app).get("/api/sessions/progress-session/progress")

    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is False
    assert payload["current"] == "Prepared table"
    assert [event["message"] for event in payload["events"]] == [
        "Prepare Data Explorer",
        "Reading current KPI table",
        "Prepared table",
    ]
