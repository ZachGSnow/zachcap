import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from zachcap import __version__, about, live
from zachcap.api import create_app
from zachcap.store import Store

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path / "data"))
    return TestClient(create_app("test-token"), base_url="http://127.0.0.1")


def test_the_build_stamp_is_beside_the_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    assert about.build() is None  # from source

    stamp = tmp_path / "BUILD"
    stamp.write_text("0.5.0 480063a 2026-11-24T23:35:02Z\n", encoding="utf-8")
    assert about.build() == "0.5.0 480063a 2026-11-24T23:35:02Z"
    stamp.write_bytes(b"\xff\xfe")
    assert about.build() is None
    stamp.write_text(" \n", encoding="utf-8")
    assert about.build() is None


@pytest.mark.parametrize(
    ("build", "product", "name"),
    [
        (26200, 1, "Windows 11 (build 26200)"),
        (22000, 1, "Windows 11 (build 22000)"),
        (19045, 1, "Windows 10 (build 19045)"),
        (20348, 3, "Windows Server (build 20348)"),
    ],
)
def test_windows_by_its_name(monkeypatch: pytest.MonkeyPatch, build: int, product: int, name: str):
    found = SimpleNamespace(build=build, product_type=product)
    monkeypatch.setattr(sys, "getwindowsversion", lambda: found)
    assert about.windows() == name


def test_what_the_page_gets(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    assert client.get("/api/about").status_code == 401

    about_it = client.get("/api/about", headers=AUTH).json()
    assert about_it["version"] == __version__ and about_it["build"] is None
    assert about_it["data"] == str(tmp_path / "data")
    assert about_it["python"].startswith("3.") and about_it["windows"].startswith("Windows")
    assert about_it["gpu"] is None  # not looked for yet

    store = Store()
    store.set_setting(live.GPU_SETTING, "0")  # what the last start found
    store.close()
    assert client.get("/api/about", headers=AUTH).json()["gpu"] is False


def test_the_gpu_this_start_found_counts(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(live.live_child, "gpu_usable", lambda: True)
    client.app.state.look_for_gpu()
    assert client.get("/api/about", headers=AUTH).json()["gpu"] is True
