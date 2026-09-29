from fastapi.testclient import TestClient

from app.main import create_app


def test_health(db_url: str) -> None:
    with TestClient(create_app(db_url)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
