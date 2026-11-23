"""Snapshots of the call window during a recording, with their text, as notes."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from zachcap import capture, export, pipeline, snapshot
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.summarize import notes_text

AUTH = {"Authorization": "Bearer test-token"}


class FakeRecorder:
    def __init__(self, folder: Path, **devices) -> None:
        self.folder = folder
        self.elapsed = 42.0
        self.tracks: dict = {}
        self.paused = False
        self.started_at = None

    def start(self) -> None:
        self.folder.mkdir(parents=True)

    def levels(self) -> dict:
        return {}

    def errors(self) -> list:
        return []

    def stop(self) -> dict:
        return {"duration": 42.0}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    return TestClient(create_app("test-token"))


def fake_take(text: str):
    def take(folder: Path, note_id: int) -> dict:
        path = snapshot.image_path(folder, note_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (40, 20), "white").save(path)
        return {"path": path, "text": text, "window": "Microsoft Teams", "size": (40, 20)}

    return take


def test_a_snapshot_is_a_note_with_its_picture_and_text(client: TestClient, monkeypatch):
    assert client.post("/api/record/snapshot", headers=AUTH).status_code == 409
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    monkeypatch.setattr(snapshot, "take", fake_take("Error 0x80070005\nAccess is denied."))

    shot = client.post("/api/record/snapshot", headers=AUTH).json()

    assert (shot["kind"], shot["at"], shot["lines"], shot["window"]) == (
        "snapshot",
        42.0,
        2,
        "Microsoft Teams",
    )
    picture = client.get(f"/api/notes/{shot['id']}/image", headers=AUTH)
    assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
    assert client.get("/api/record", headers=AUTH).json()["notes"] == 1

    folder = Path(Store().meeting(meeting_id)["folder"])
    client.delete(f"/api/notes/{shot['id']}", headers=AUTH)
    assert not snapshot.image_path(folder, shot["id"]).exists()  # the picture goes too
    assert client.get(f"/api/notes/{shot['id']}/image", headers=AUTH).status_code == 404


def test_a_failed_snapshot_leaves_no_note(client: TestClient, monkeypatch):
    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]

    def broken(folder, note_id):
        raise RuntimeError("There's no window to capture")

    monkeypatch.setattr(snapshot, "take", broken)
    failed = client.post("/api/record/snapshot", headers=AUTH)
    assert failed.status_code == 400 and "no window" in failed.json()["detail"]
    client.post("/api/record/stop", headers=AUTH)
    assert client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["notes"] == []


def test_summaries_and_exports_get_the_screen_text(tmp_path: Path):
    store = Store(tmp_path / "test.db")
    meeting_id = store.create_meeting("Support call", "2026-11-23T09:00:00", tmp_path / "m")
    store.add_note(meeting_id, 65.0, "snapshot", "Error 0x80070005\nAccess is denied.")
    store.add_note(meeting_id, 70.0, "snapshot", "")
    store.add_note(meeting_id, 75.0, "snapshot", "x" * 5000)

    prompt = notes_text(store, meeting_id)
    assert (
        "[01:05] took a snapshot of the screen, which read: Error 0x80070005 / Access is denied."
        in prompt
    )
    assert "[01:10] took a snapshot of the screen (no text on it)" in prompt
    assert "x" * 1500 + "..." in prompt and "x" * 1501 not in prompt

    markdown = export.markdown(store, meeting_id)
    assert "- [01:05] Snapshot: Error 0x80070005 / Access is denied." in markdown
    assert "- [01:10] Snapshot (no text)" in markdown
    store.close()


def test_the_picture_lives_in_the_meeting_folder(tmp_path: Path):
    assert snapshot.image_path(tmp_path, 7) == tmp_path / "snapshots" / "note-7.png"
