from fastapi.testclient import TestClient

from app.main import _query_plan_from_payload, _sessions, app
from app.worldpanel.client import Credentials


def test_query_plan_payload_round_trip_preserves_nested_member_path_and_axis_order():
    plan = _query_plan_from_payload(
        {
            "report_set": "Set",
            "report": "Data Explorer",
            "axis_placements": [
                {"dimension": "Product", "axis": "row", "position": 0},
                {"dimension": "Period", "axis": "column", "position": 0},
            ],
            "member_selections": [
                {"dimension": "Product", "member_path": ["Fruit", "Kiwifruit", "Gold"], "checked": True}
            ],
            "kpis": ["Spend"],
            "expected_period": "2025 P1",
            "output_shape": "table",
        }
    )

    assert plan.axis_placements[0].dimension == "Product"
    assert plan.axis_placements[1].dimension == "Period"
    assert plan.member_selections[0].member_path == ("Fruit", "Kiwifruit", "Gold")


def test_health_exposes_persistent_pivot_session_observability():
    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert "pivot_sessions" in payload
    assert "expired_pivot_sessions_removed" in payload


def test_api_exposes_separate_pivot_plan_and_execute_routes():
    routes = {route.path for route in app.routes}

    assert "/api/pivot/plan" in routes
    assert "/api/pivot/execute" in routes


def test_api_exposes_session_scoped_ai_configuration_routes():
    methods_by_path = {}
    for route in app.routes:
        methods_by_path.setdefault(route.path, set()).update(getattr(route, "methods", set()))

    assert "POST" in methods_by_path["/api/ai/test"]
    assert "PUT" in methods_by_path["/api/sessions/{session_id}/ai"]
    assert "DELETE" in methods_by_path["/api/sessions/{session_id}/ai"]


def test_ai_configuration_is_bound_to_one_session_and_never_returned(monkeypatch):
    async def fake_chat(self, prompt):
        return "OK"

    monkeypatch.setattr("app.main.AssistantClient.chat", fake_chat)
    _sessions["ai-one"] = {"credentials": Credentials("one@example.com", "password")}
    _sessions["ai-two"] = {"credentials": Credentials("two@example.com", "password")}
    payload = {
        "base_url": "https://example.com/v1/chat/completions",
        "model": "model-one",
        "api_key": "top-secret",
    }

    response = TestClient(app).put("/api/sessions/ai-one/ai", json=payload)

    assert response.status_code == 200
    assert "top-secret" not in response.text
    assert _sessions["ai-one"]["ai_settings"].api_key == "top-secret"
    assert "ai_settings" not in _sessions["ai-two"]

    cleared = TestClient(app).delete("/api/sessions/ai-one/ai")
    assert cleared.status_code == 200
    assert "ai_settings" not in _sessions["ai-one"]
