from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_returns_configured_backends():
    client = TestClient(create_app())
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "separator" in payload["adapters"]
