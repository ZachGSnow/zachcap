from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap.api import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app("test-token"))


def test_every_call_needs_the_token(client: TestClient):
    assert client.get("/api/health").status_code == 401
    assert client.get("/api/health", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/meetings?token=nope").status_code == 401


def test_health_and_empty_lists_with_the_token(client: TestClient, tmp_path: Path):
    auth = {"Authorization": "Bearer test-token"}
    health = client.get("/api/health", headers=auth).json()
    assert health["ok"] and health["data"] == str(tmp_path)
    assert client.get("/api/meetings", headers=auth).json() == []
    assert [p["name"] for p in client.get("/api/people", headers=auth).json()] == ["You"]
    assert client.get("/api/record?token=test-token").json() == {"recording": False}


def test_only_the_app_origin_may_call_from_a_browser(client: TestClient):
    preflight = {"Access-Control-Request-Method": "GET"}
    allowed = client.options(
        "/api/health", headers={"Origin": "http://tauri.localhost", **preflight}
    )
    blocked = client.options("/api/health", headers={"Origin": "https://evil.example", **preflight})
    assert allowed.status_code == 200
    assert blocked.status_code == 400


AUTH = {"Authorization": "Bearer test-token"}


def test_settings_round_trip_and_validation(client: TestClient):
    settings = client.get("/api/settings", headers=AUTH).json()
    assert settings["summary_model"] == "claude-opus-5-5"
    assert settings["language"] == "en" and settings["me"]["name"] == "You"

    changed = client.put(
        "/api/settings",
        headers=AUTH,
        json={"summary_model": "claude-sonnet-5-5", "language": "auto"},
    ).json()
    assert (changed["summary_model"], changed["language"]) == ("claude-sonnet-5-5", "auto")
    assert (
        client.put("/api/settings", headers=AUTH, json={"summary_model": "gpt"}).status_code == 400
    )
    assert client.put("/api/settings", headers=AUTH, json={"language": "xx"}).status_code == 400


def test_import_rejects_missing_files_and_actions_validate(client: TestClient, tmp_path: Path):
    missing = client.post("/api/import", headers=AUTH, json={"path": str(tmp_path / "nope.mp4")})
    assert missing.status_code == 400
    assert client.get("/api/actions", headers=AUTH).json()["items"] == []
    bad = client.patch("/api/actions/1", headers=AUTH, json={"status": "maybe"})
    assert bad.status_code == 400
    assert client.get("/api/stats", headers=AUTH).json()["meetings"] == 0
