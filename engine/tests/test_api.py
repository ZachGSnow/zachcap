import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import capture, pipeline
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match


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
    words = client.put("/api/settings", headers=AUTH, json={"vocabulary": " Contoso\nProxmox "})
    assert words.json()["vocabulary"] == "Contoso\nProxmox"
    too_long = client.put("/api/settings", headers=AUTH, json={"vocabulary": "x" * 4001})
    assert too_long.status_code == 400
    styled = client.put("/api/settings", headers=AUTH, json={"summary_template": "incident"})
    assert styled.json()["summary_template"] == "incident"
    assert {"id": "email", "label": "Follow-up email"} in styled.json()["recipes"]
    bad_style = client.put("/api/settings", headers=AUTH, json={"summary_template": "poem"})
    assert bad_style.status_code == 400


def test_import_rejects_missing_files_and_actions_validate(client: TestClient, tmp_path: Path):
    missing = client.post("/api/import", headers=AUTH, json={"path": str(tmp_path / "nope.mp4")})
    assert missing.status_code == 400
    assert client.get("/api/actions", headers=AUTH).json()["items"] == []
    bad = client.patch("/api/actions/1", headers=AUTH, json={"status": "maybe"})
    assert bad.status_code == 400
    assert client.get("/api/stats", headers=AUTH).json()["meetings"] == 0


def test_there_are_no_open_docs_pages(client: TestClient):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


class FakeRecorder:
    def __init__(self, folder: Path, **devices) -> None:
        self.folder = folder
        self.started_at = datetime.now().astimezone()
        self.elapsed = 12.5
        self.tracks: dict = {}
        self.paused = False

    def pause(self, paused: bool = True) -> None:
        self.paused = paused

    def start(self) -> None:
        self.folder.mkdir(parents=True)

    def levels(self) -> dict:
        return {}

    def errors(self) -> list:
        return []

    def stop(self) -> dict:
        return {"duration": 12.5}


def test_a_recording_is_a_meeting_from_its_first_second(client: TestClient, monkeypatch):
    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")

    started = client.post("/api/record/start", headers=AUTH, json={}).json()
    store = Store()
    assert store.meeting(started["meeting_id"])["status"] == "recording"
    store.close()
    assert client.get("/api/meetings", headers=AUTH).json() == []  # not listed while recording

    stopped = client.post("/api/record/stop", headers=AUTH).json()
    assert stopped["meeting_id"] == started["meeting_id"]
    store = Store()
    assert store.meeting(stopped["meeting_id"])["duration"] == 12.5
    store.close()
    assert client.post("/api/record/stop", headers=AUTH).status_code == 409


def test_a_start_the_database_refuses_leaves_nothing_recording(client: TestClient, monkeypatch):
    import sqlite3

    started: list[FakeRecorder] = []

    class Tracked(FakeRecorder):
        stopped = False

        def start(self) -> None:
            super().start()
            started.append(self)

        def stop(self) -> dict:
            self.stopped = True
            return super().stop()

    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(capture, "Recorder", Tracked)
    monkeypatch.setattr(Store, "create_meeting", busy)

    refused = client.post("/api/record/start", headers=AUTH, json={})

    assert refused.status_code == 500 and "database is locked" in refused.json()["detail"]
    assert started[0].stopped and not started[0].folder.exists()
    assert client.get("/api/record", headers=AUTH).json() == {"recording": False}


def test_startup_finishes_what_a_crash_left_behind(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    processed: list[int] = []
    monkeypatch.setattr(
        pipeline, "process", lambda store, meeting_id, log: processed.append(meeting_id)
    )
    meetings = tmp_path / "meetings"
    store = Store()
    cut_off = store.create_meeting(
        "Cut off", "2026-11-22T09:00:00", meetings / "2026-11-22_090000", status="recording"
    )
    busy = store.create_meeting("Busy", "2026-11-22T10:00:00", meetings / "b", status="processing")
    store.create_meeting("Done", "2026-11-22T11:00:00", meetings / "c", status="ready")
    store.close()
    # Recorded by an older version that only saved the meeting at Stop.
    orphan = meetings / "2026-11-21_100000"
    orphan.mkdir(parents=True)
    (orphan / "mic-000.flac").write_bytes(b"")
    # Finished, and its meeting deleted: stays gone.
    deleted = meetings / "2026-11-20_100000"
    deleted.mkdir()
    (deleted / "mic-000.flac").write_bytes(b"")
    (deleted / "recording.json").write_text('{"complete": true}', encoding="utf-8")

    client = TestClient(create_app("test-token"))
    deadline = time.monotonic() + 5
    while len(processed) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)

    listed = {m["title"]: m for m in client.get("/api/meetings", headers=AUTH).json()}
    recovered = listed["Recovered recording Nov 21 10:00"]
    assert set(listed) == {"Cut off", "Busy", "Done", "Recovered recording Nov 21 10:00"}
    assert sorted(processed) == sorted([cut_off, busy, recovered["id"]])


def test_the_player_never_gets_a_half_written_file(tmp_path: Path):
    import threading

    import numpy as np
    import soundfile as sf

    from zachcap.api import _playback_file

    sf.write(tmp_path / "system.flac", np.zeros(16000 * 5, np.float32), 16000)
    built: list[Path] = []
    threads = [
        threading.Thread(target=lambda: built.append(_playback_file(tmp_path))) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(built)) == 1 and sf.info(built[0]).frames == 16000 * 5
    assert not list(tmp_path.glob("*.partial.flac"))


def test_notes_bookmarks_and_a_private_pause_while_recording(client: TestClient, monkeypatch):
    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    assert client.post("/api/record/note", headers=AUTH, json={}).status_code == 409

    meeting_id = client.post("/api/record/start", headers=AUTH, json={}).json()["meeting_id"]
    noted = client.post("/api/record/note", headers=AUTH, json={"text": " Ask about budget "})
    marked = client.post("/api/record/note", headers=AUTH, json={"kind": "bookmark"})
    assert (noted.json()["at"], noted.json()["text"]) == (12.5, "Ask about budget")
    assert marked.json()["kind"] == "bookmark"
    assert client.post("/api/record/note", headers=AUTH, json={"text": " "}).status_code == 400
    assert client.post("/api/record/pause", headers=AUTH, json={"paused": True}).json() == {
        "paused": True
    }
    status = client.get("/api/record", headers=AUTH).json()
    assert status["paused"] and status["notes"] == 2
    # What the app needs to warn, and to offer to stop when the call ends.
    assert status["warnings"] == [] and set(status["call"]) == {"apps", "ended_for"}
    assert status["quiet_for"] >= 0
    client.post("/api/record/stop", headers=AUTH)

    notes = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["notes"]
    assert [n["kind"] for n in notes] == ["note", "bookmark"]
    edited = client.patch(f"/api/notes/{notes[1]['id']}", headers=AUTH, json={"text": "Key moment"})
    assert edited.json()["text"] == "Key moment"
    later = client.post(
        f"/api/meetings/{meeting_id}/notes", headers=AUTH, json={"text": "Follow up", "at": 3}
    )
    assert later.json()["at"] == 3
    client.delete(f"/api/notes/{notes[0]['id']}", headers=AUTH)
    left = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["notes"]
    assert [n["text"] for n in left] == ["Follow up", "Key moment"]


def test_exports_to_a_download_a_chosen_file_and_a_folder(client: TestClient, tmp_path: Path):
    store = Store()
    meeting_id = store.create_meeting("Weekly sync", "2026-11-20T09:00:00", tmp_path / "audio")
    store.close()

    download = client.get(f"/api/meetings/{meeting_id}/export?format=srt", headers=AUTH)
    assert download.status_code == 200
    assert "2026-11-20%200900%20Weekly%20sync.srt" in download.headers["content-disposition"]
    assert (
        client.get(f"/api/meetings/{meeting_id}/export?format=pdf", headers=AUTH).status_code == 400
    )

    chosen = tmp_path / "out" / "notes.md"
    chosen.parent.mkdir()
    saved = client.post(
        f"/api/meetings/{meeting_id}/export",
        headers=AUTH,
        json={"format": "md", "path": str(chosen)},
    )
    assert saved.status_code == 200 and chosen.read_text(encoding="utf-8").startswith(
        "# Weekly sync"
    )
    wrong = client.post(
        f"/api/meetings/{meeting_id}/export",
        headers=AUTH,
        json={"format": "md", "path": str(chosen.with_suffix(".txt"))},
    )
    assert wrong.status_code == 400
    taken = tmp_path / "out" / "taken.md"
    taken.mkdir()  # a folder with the name picked: Windows refuses to write it
    picked = {"format": "md", "path": str(taken)}
    blocked = client.post(f"/api/meetings/{meeting_id}/export", headers=AUTH, json=picked)
    assert blocked.status_code == 400 and "Couldn't write taken.md" in blocked.json()["detail"]

    vault = tmp_path / "vault"
    vault.mkdir()
    assert (
        client.put(
            "/api/settings", headers=AUTH, json={"export_folder": str(vault / "x")}
        ).status_code
        == 400
    )
    settings = client.put("/api/settings", headers=AUTH, json={"export_folder": str(vault)}).json()
    assert settings["export_folder"] == str(vault)
    backfilled = [p.name for p in vault.iterdir()]
    assert backfilled == ["2026-11-20 0900 Weekly sync (M1).md"]


def test_the_export_folder_follows_every_change(client: TestClient, tmp_path: Path):
    store = Store()
    meeting_id = store.create_meeting("Weekly sync", "2026-11-20T09:00:00", tmp_path / "audio")
    dana = store.person_id("Dana")
    named = store.add_speaker(
        meeting_id, "system", "Dana", None, 10.0, None, Match(dana, 1.0, "confirmed")
    )
    unnamed = store.add_speaker(
        meeting_id, "system", "Speaker 2", None, 5.0, None, Match(None, 0.0, "unknown")
    )
    store.add_segments(
        meeting_id,
        [
            (named, "system", 1.0, 4.0, "I'll send the notice.", 0.9),
            (named, "system", 5.0, 6.0, "Thanks, all.", 0.9),
        ],
    )
    summary = {"overview": "The notice goes out.", "decisions": [], "open_questions": []}
    store.save_summary(meeting_id, "claude-opus-5-5", summary, 1, 1, 0.0)
    store.replace_action_items(meeting_id, [{"owner": "Dana", "task": "Send the notice"}])
    [item] = store.action_items(meeting_id=meeting_id)
    line = store.transcript(meeting_id)[1]
    store.close()
    vault = tmp_path / "vault"
    vault.mkdir()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(vault)})
    copy = vault / "2026-11-20 0900 Weekly sync (M1).md"

    client.patch(f"/api/actions/{item['id']}", headers=AUTH, json={"status": "done"})
    assert "- [x] **Dana**: Send the notice" in copy.read_text(encoding="utf-8")

    moved = {"speaker_id": unnamed}
    client.post(f"/api/segments/{line['id']}/speaker", headers=AUTH, json=moved)
    assert "**[00:05] Speaker 2:** Thanks, all." in copy.read_text(encoding="utf-8")

    client.patch(f"/api/people/{dana}", headers=AUTH, json={"name": "Dana Lee"})
    deadline = time.monotonic() + 5  # renames export in the background
    while "Dana Lee:" not in copy.read_text(encoding="utf-8") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "**[00:01] Dana Lee:** I'll send the notice." in copy.read_text(encoding="utf-8")


def test_settings_show_how_to_add_zachcap_to_claude(client: TestClient, tmp_path: Path):
    config = client.get("/api/mcp", headers=AUTH).json()
    assert config["claude_code"].startswith('claude mcp add zachcap -e ZACHCAP_HOME="')
    assert config["claude_code"].endswith("-m zachcap.cli mcp")
    assert (
        '"zachcap"' in config["claude_desktop"]
        and str(tmp_path).replace("\\", "\\\\") in config["claude_desktop"]
    )


def test_privacy_settings_and_a_meeting_without_its_recording(client: TestClient, tmp_path: Path):
    kept = client.put("/api/settings", headers=AUTH, json={"keep_audio_days": 30}).json()
    assert kept["keep_audio_days"] == 30
    assert client.put("/api/settings", headers=AUTH, json={"keep_audio_days": 3}).status_code == 400
    masked = client.put("/api/settings", headers=AUTH, json={"mask_before_claude": True}).json()
    assert masked["mask_before_claude"] is True

    store = Store()
    meeting_id = store.create_meeting("Old", "2026-01-01T09:00:00", tmp_path / "gone")
    store.update_meeting(meeting_id, status="ready", audio_removed_at="2026-02-01T00:00:00")
    store.close()
    refused = client.post(f"/api/meetings/{meeting_id}/process", headers=AUTH)
    assert refused.status_code == 400 and "recording was deleted" in refused.json()["detail"]


def test_a_quiet_call_plays_back_as_loud_as_the_mic(tmp_path: Path):
    import numpy as np
    import soundfile as sf

    from zachcap.api import _playback_file

    t = np.arange(16000 * 4) / 16000
    voice = np.sin(2 * np.pi * 220 * t).astype(np.float32)
    mic, call = voice.copy(), voice.copy()
    mic[16000 * 2 :] = 0  # you talk for 2 s, then the call does, at low Windows volume
    call[: 16000 * 2] = 0
    sf.write(tmp_path / "mic.flac", 0.4 * mic, 16000)
    sf.write(tmp_path / "system.flac", 0.02 * call, 16000)

    played, _ = sf.read(_playback_file(tmp_path), dtype="float32")
    you, them = np.abs(played[: 16000 * 2]).max(), np.abs(played[16000 * 2 :]).max()
    assert them > 0.25 and 0.5 < them / you < 2  # it was 20 times quieter


def test_your_own_kinds_and_documents_through_the_api(client: TestClient):
    library = client.get("/api/prompts", headers=AUTH).json()
    assert [e["id"] for e in library["recipes"]] == ["email", "status", "ticket"]
    assert library["limits"] == {"label": 40, "instructions": 2000}

    made = client.post(
        "/api/prompts",
        headers=AUTH,
        json={"kind": "template", "label": "Vendor call", "instructions": "Quotes, lead times."},
    )
    assert made.status_code == 200
    mine = made.json()["id"]
    settings = client.put("/api/settings", headers=AUTH, json={"summary_template": mine}).json()
    assert settings["summary_template"] == mine
    assert {"id": mine, "label": "Vendor call"} in settings["templates"]

    renamed = client.patch(f"/api/prompts/{mine}", headers=AUTH, json={"label": "Supplier call"})
    assert renamed.json()["label"] == "Supplier call"
    duplicate = client.post(
        "/api/prompts",
        headers=AUTH,
        json={"kind": "template", "label": "supplier CALL", "instructions": "Again."},
    )
    assert duplicate.status_code == 400 and "already" in duplicate.json()["detail"]

    edited = client.patch(
        "/api/prompts/ticket", headers=AUTH, json={"instructions": "Short ticket style."}
    )
    assert edited.json()["edited"] is True
    reset = client.post("/api/prompts/ticket/reset", headers=AUTH)
    assert reset.json()["edited"] is False
    assert client.delete("/api/prompts/ticket", headers=AUTH).status_code == 400
    assert client.patch("/api/prompts/custom-gone", headers=AUTH, json={}).status_code == 404

    assert client.delete(f"/api/prompts/{mine}", headers=AUTH).json() == {"deleted": mine}
    after = client.get("/api/settings", headers=AUTH).json()
    assert after["summary_template"] == "general"
    assert mine not in [t["id"] for t in after["templates"]]


def test_stopping_when_the_call_ends_is_on_unless_turned_off(client: TestClient):
    assert client.get("/api/settings", headers=AUTH).json()["stop_when_call_ends"] is True
    off = client.put("/api/settings", headers=AUTH, json={"stop_when_call_ends": False}).json()
    assert off["stop_when_call_ends"] is False
