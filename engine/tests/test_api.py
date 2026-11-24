import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import capture, pipeline
from zachcap.api import create_app
from zachcap.store import Store
from zachcap.voiceprint import Match

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path))
    return TestClient(create_app("test-token"), base_url=LOCAL)


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
    assert client.get("/api/record?token=test-token").json() == {"recording": False, "event": None}


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
        assert client.get(path).status_code == 401  # the gate: no token, no answer
        assert client.get(path, headers=AUTH).status_code == 404


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
    assert client.get("/api/record", headers=AUTH).json() == {"recording": False, "event": None}


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

    client = TestClient(create_app("test-token"), base_url=LOCAL)
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


def a_support_call(folder: Path, status: str = "ready") -> tuple[int, list[int]]:
    """A meeting with recorded audio (6 s, both tracks) and three lines, the middle one a
    password read aloud."""
    import json

    import numpy as np
    import soundfile as sf

    folder.mkdir(parents=True, exist_ok=True)
    tone = (0.3 * np.sin(2 * np.pi * 220 * np.arange(6 * 16000) / 16000)).astype(np.float32)
    for name in ("mic", "system"):
        sf.write(folder / f"{name}-000.flac", tone, 16000, subtype="PCM_16")
    info = {"tracks": {"mic": {"offset": 0.0}, "system": {"offset": 0.0}}, "complete": True}
    (folder / "recording.json").write_text(json.dumps(info), encoding="utf-8")
    sf.write(folder / "playback.flac", tone, 16000)  # the player's cached mix
    store = Store()
    meeting_id = store.create_meeting(
        "Support call", "2026-11-22T09:00:00", folder, duration=6.0, status=status
    )
    dana = store.add_speaker(
        meeting_id,
        "system",
        "Dana",
        None,
        6.0,
        (2.0, 4.0),
        Match(store.person_id("Dana"), 1.0, "confirmed"),
    )
    store.add_segments(
        meeting_id,
        [
            (dana, "system", 0.0, 2.0, "We moved to cooper netties last week", 0.9),
            (dana, "system", 2.0, 4.0, "The admin password is zebra99", 0.9),
            (dana, "system", 4.0, 6.0, "Thanks for the help", 0.9),
        ],
    )
    lines = [row["id"] for row in store.transcript(meeting_id)]
    store.close()
    return meeting_id, lines


def test_fixing_a_line_offers_words_to_know_and_reaches_the_export(client: TestClient, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(vault)})
    meeting_id, lines = a_support_call(tmp_path / "call")

    fixed = client.patch(
        f"/api/segments/{lines[0]}",
        headers=AUTH,
        json={"text": "We moved to  Kubernetes\nlast week"},
    ).json()

    assert fixed["text"] == "We moved to Kubernetes last week"
    assert fixed["suggestion"] == {"term": "Kubernetes", "misheard": "cooper netties"}
    first = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["segments"][0]
    assert (first["text"], first["edited"]) == ("We moved to Kubernetes last week", 1)
    assert "Kubernetes" in next(vault.glob("*.md")).read_text(encoding="utf-8")
    empty = client.patch(f"/api/segments/{lines[0]}", headers=AUTH, json={"text": " \n "})
    assert empty.status_code == 400
    assert client.patch("/api/segments/999", headers=AUTH, json={"text": "x"}).status_code == 404

    added = client.post("/api/vocabulary/correction", headers=AUTH, json=fixed["suggestion"])
    assert added.json()["vocabulary"] == "Kubernetes: cooper netties"
    assert (
        client.get("/api/settings", headers=AUTH).json()["vocabulary"]
        == "Kubernetes: cooper netties"
    )
    bad = client.post("/api/vocabulary/correction", headers=AUTH, json={"term": "C:Drive"})
    assert bad.status_code == 400


def test_redacting_silences_the_recording_and_removes_the_words(client: TestClient, tmp_path):
    import numpy as np

    from zachcap import audio

    folder = tmp_path / "call"
    meeting_id, _ = a_support_call(folder)
    assert client.get("/api/search?q=zebra99", headers=AUTH).json()

    redacted = client.post(
        f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 1.9, "end": 4.1}
    ).json()

    assert (redacted["audio_files"], redacted["lines_removed"], redacted["lines_redacted"]) == (
        2,
        1,
        2,  # the lines on each side reach 0.1 s into it
    )
    assert redacted["has_summary"] is False and "zebra99" not in str(redacted)
    assert not (folder / "playback.flac").exists()  # rebuilt from the silenced tracks
    meeting = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert [s["text"] for s in meeting["segments"]] == ["[redacted]", "[redacted]"]
    assert meeting["speakers"][0]["sample_start"] is None  # its sample was the password
    assert client.get("/api/search?q=zebra99", headers=AUTH).json() == []
    for name in ("mic", "system"):
        track = audio.load_track(folder, name)
        assert not np.any(track[int(1.91 * 16000) : int(4.09 * 16000)])
        assert np.any(track[: int(1.8 * 16000)]) and np.any(track[int(4.2 * 16000) :])
    mix = client.get(f"/api/meetings/{meeting_id}/audio?token=test-token")
    assert mix.status_code == 200

    for bad in ({"start": 3, "end": 3}, {"start": 0, "end": 1000}, {"start": 7, "end": 8}):
        refused = client.post(f"/api/meetings/{meeting_id}/redact", headers=AUTH, json=bad)
        assert refused.status_code == 400, bad


def test_without_its_recording_a_meeting_is_redacted_in_text_only(client: TestClient, tmp_path):
    folder = tmp_path / "old"
    meeting_id, _ = a_support_call(folder)
    store = Store()
    store.update_meeting(meeting_id, audio_removed_at="2026-11-01T00:00:00")
    store.close()

    redacted = client.post(
        f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 2.0, "end": 4.0}
    ).json()

    assert redacted["audio_removed"] is True and redacted["audio_files"] == 0
    assert redacted["lines_removed"] == 1
    assert (folder / "playback.flac").exists()  # retention's business, not touched here


def test_a_meeting_being_recorded_or_processed_cant_be_changed(
    client: TestClient, tmp_path, monkeypatch
):
    import threading

    gate = threading.Event()
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: gate.wait(10))
    meeting_id, lines = a_support_call(tmp_path / "call", status="recording")
    edit = {"text": "changed words"}
    moment = {"start": 2.0, "end": 4.0}

    assert client.patch(f"/api/segments/{lines[0]}", headers=AUTH, json=edit).status_code == 409
    recording = client.post(f"/api/meetings/{meeting_id}/redact", headers=AUTH, json=moment)
    assert recording.status_code == 409 and "recorded" in recording.json()["detail"]

    store = Store()
    store.update_meeting(meeting_id, status="recorded")
    store.close()
    client.post(f"/api/meetings/{meeting_id}/process", headers=AUTH)
    try:
        busy = client.patch(f"/api/segments/{lines[0]}", headers=AUTH, json=edit)
        assert busy.status_code == 409 and "being processed" in busy.json()["detail"]
        busy = client.post(f"/api/meetings/{meeting_id}/redact", headers=AUTH, json=moment)
        assert busy.status_code == 409
    finally:
        gate.set()
    for _ in range(50):  # the worker lets go of the meeting
        if (
            client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["meeting"]["progress"]
            is None
        ):
            break
        time.sleep(0.1)
    assert client.patch(f"/api/segments/{lines[0]}", headers=AUTH, json=edit).status_code == 200


def test_the_work_log_through_the_api(client: TestClient, tmp_path: Path):
    store = Store()
    store.create_meeting("Sync", "2026-11-23T09:00:00-05:00", tmp_path / "a", duration=1200.0)
    store.close()

    week = client.get("/api/worklog?period=week&day=2026-11-25", headers=AUTH).json()
    assert week["totals"] == {"meetings": 1, "seconds": 1200.0, "hours": 0.5}
    assert week["markdown"].startswith("# Work log: week of November 23, 2026")
    assert client.get("/api/worklog?period=month", headers=AUTH).status_code == 400
    assert client.get("/api/worklog?day=yesterday", headers=AUTH).status_code == 400

    unsaved = client.post("/api/worklog/save", headers=AUTH, json={"day": "2026-11-23"})
    assert unsaved.status_code == 400 and "export folder" in unsaved.json()["detail"]
    folder = tmp_path / "vault"
    folder.mkdir()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(folder)})
    saved = client.post("/api/worklog/save", headers=AUTH, json={"day": "2026-11-23"}).json()
    assert Path(saved["path"]).name == "Work log 2026-11-23.md"
    on = client.put("/api/settings", headers=AUTH, json={"export_work_log": True}).json()
    assert on["export_work_log"] is True


def test_where_your_name_was_said(client: TestClient, tmp_path: Path):
    store = Store()
    store.rename_person(store.me()["id"], "Alex Rivera")
    meeting_id = store.create_meeting("Sync", "2026-11-20T09:00:00", tmp_path / "audio")
    dana = store.add_speaker(
        meeting_id, "system", "Dana", None, 5.0, None, Match(store.person_id("Dana"), 0.9, "auto")
    )
    store.add_segments(
        meeting_id,
        [
            (dana, "system", 1.0, 4.0, "Alex, can you take this one?", 0.9),
            (dana, "system", 5.0, 8.0, "Nobody in particular.", 0.9),
        ],
    )
    store.update_meeting(meeting_id, status="ready")
    store.close()

    detail = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert [(m["speaker"], m["at"], m["names"]) for m in detail["mentions"]] == [
        ("Dana", 1.0, ["Alex"])
    ]
    assert client.get("/api/meetings", headers=AUTH).json()[0]["mentions"] == 1

    saved = client.put("/api/settings", headers=AUTH, json={"also_answer_to": " AR ,, you, A "})
    assert saved.json()["also_answer_to"] == "AR"
    too_long = client.put("/api/settings", headers=AUTH, json={"also_answer_to": "x" * 401})
    assert too_long.status_code == 400


def test_the_watch_folder_settings(client: TestClient, tmp_path: Path):
    import numpy as np
    import soundfile as sf

    folder = tmp_path / "exports"
    folder.mkdir()
    sf.write(folder / "old.wav", np.zeros(1600, np.float32), 16000)
    (folder / "broken.mp3").write_bytes(b"nope")
    assert client.get("/api/settings", headers=AUTH).json()["watch_folder"] == ""
    missing = client.put("/api/settings", headers=AUTH, json={"watch_folder": str(tmp_path / "x")})
    assert missing.status_code == 400
    files = client.get("/api/watch-folder/files", headers=AUTH, params={"path": str(folder)})
    assert files.json() == {"count": 2}

    # On, for new recordings only: what's there now is never imported.
    on = client.put("/api/settings", headers=AUTH, json={"watch_folder": str(folder)}).json()
    assert on["watch_folder"] == str(folder) and on["watch_folder_failed"] == []
    store = Store()
    watch = client.app.state.folder_watch  # the engine's own looks (its thread isn't started)
    watch._clock = lambda: time.time() + 120
    watch._submit = lambda _: None
    watch.check(store)
    assert watch.check(store) == []

    # Again, with "import the ones there too": the broken one is reported, once.
    client.put(
        "/api/settings", headers=AUTH, json={"watch_folder": str(folder), "watch_existing": True}
    )
    assert len(watch.check(store)) == 1
    store.close()
    failed = client.get("/api/settings", headers=AUTH).json()["watch_folder_failed"]
    assert [f["name"] for f in failed] == ["broken.mp3"] and failed[0]["error"]
    assert not failed[0]["retrying"]
    retried = client.post("/api/watch-folder/retry", headers=AUTH).json()
    assert retried == {"retrying": 1}
    assert client.get("/api/settings", headers=AUTH).json()["watch_folder_failed"] == []
    off = client.put("/api/settings", headers=AUTH, json={"watch_folder": ""}).json()
    assert off["watch_folder"] == "" and off["watch_folder_failed"] == []


def test_a_sound_check_reports_both_sides_and_keeps_nothing(client: TestClient, monkeypatch):
    from zachcap import soundcheck

    calls = []

    def fake_run(mic_name=None, output_name=None):
        calls.append((mic_name, output_name))
        heard = {"device": "Speakers", "peak_dbfs": -30.0, "heard": True}
        return {"mic": {**heard, "device": "Mic"}, "system": heard, "played": True, "errors": []}

    monkeypatch.setattr(soundcheck, "run", fake_run)
    assert client.get("/api/settings", headers=AUTH).json()["soundcheck_passed"] is None

    result = client.post("/api/soundcheck", headers=AUTH).json()
    assert result["mic"]["heard"] and result["system"]["device"] == "Speakers"
    assert calls == [(None, None)]  # every speaker and headset, the default mic
    assert client.get("/api/settings", headers=AUTH).json()["soundcheck_passed"]

    monkeypatch.setattr(capture, "Recorder", FakeRecorder)
    monkeypatch.setattr(pipeline, "process", lambda *args, **kwargs: "ready")
    client.post("/api/record/start", headers=AUTH, json={})
    assert client.post("/api/soundcheck", headers=AUTH).status_code == 409  # not mid-meeting
    client.post("/api/record/stop", headers=AUTH)


def raw_library_bytes(home: Path) -> bytes:
    """The database files as they sit on disk (the main file and its write-ahead log)."""
    return b"".join(p.read_bytes() for p in home.glob("zachcap.db*") if p.is_file())


def test_redacting_also_removes_what_was_made_from_the_words(client: TestClient, tmp_path):
    from PIL import Image

    from zachcap import snapshot

    vault = tmp_path / "vault"
    vault.mkdir()
    client.put("/api/settings", headers=AUTH, json={"export_folder": str(vault)})
    folder = tmp_path / "call"
    meeting_id, _ = a_support_call(folder)
    store = Store()
    content = {"title": "Support", "overview": "Dana read out zebra99 for the admin account."}
    store.save_summary(meeting_id, "claude-opus-5-5", content, 1, 1, 0.0)
    store.replace_action_items(meeting_id, [{"owner": "Dana", "task": "Rotate zebra99 tonight"}])
    store.add_note(meeting_id, 3.0, "note", "admin password zebra99")
    shot = store.add_note(meeting_id, 3.5, "snapshot", "Password: zebra99")
    kept = store.add_note(meeting_id, 5.5, "note", "Thanked Dana")
    store.close()
    picture = snapshot.image_path(folder, shot)
    picture.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(picture)

    redacted = client.post(
        f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 1.9, "end": 4.1}
    ).json()

    assert redacted["summary_removed"] and redacted["items_removed"] == 1
    assert redacted["notes_removed"] == 2 and redacted["scrubbed"]
    meeting = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()
    assert meeting["summary"] is None and meeting["action_items"] == []
    assert [n["id"] for n in meeting["notes"]] == [kept]
    assert not picture.exists()
    assert "zebra99" not in next(vault.glob("*.md")).read_text(encoding="utf-8")
    assert b"zebra99" not in raw_library_bytes(tmp_path)  # not even in freed pages


def test_an_empty_chunk_left_by_a_crash_doesnt_stop_a_redaction(client: TestClient, tmp_path):
    folder = tmp_path / "call"
    meeting_id, _ = a_support_call(folder)
    (folder / "mic-001.flac").write_bytes(b"")  # started just before the engine died

    redacted = client.post(
        f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 2.0, "end": 4.0}
    )

    assert redacted.status_code == 200 and redacted.json()["audio_error"] is None
    assert client.get("/api/search?q=zebra99", headers=AUTH).json() == []


def test_a_summary_being_written_and_a_redaction_dont_overlap(
    client: TestClient, tmp_path, monkeypatch
):
    import threading

    from zachcap import summarize

    meeting_id, _ = a_support_call(tmp_path / "call")
    writing, release = threading.Event(), threading.Event()

    def slow_summary(store, meeting, template=None):
        writing.set()
        release.wait(10)

    monkeypatch.setattr(summarize, "summarize", slow_summary)
    summary = threading.Thread(
        target=lambda: client.post(f"/api/meetings/{meeting_id}/summary", headers=AUTH, json={})
    )
    summary.start()
    assert writing.wait(5)
    refused = client.post(
        f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 2.0, "end": 4.0}
    )
    assert refused.status_code == 409 and "summary" in refused.json()["detail"]
    assert client.post(f"/api/meetings/{meeting_id}/process", headers=AUTH).status_code == 409
    release.set()
    summary.join(10)
    done = client.post(
        f"/api/meetings/{meeting_id}/redact", headers=AUTH, json={"start": 2.0, "end": 4.0}
    )
    assert done.status_code == 200


def test_recordings_and_samples_arent_kept_in_the_apps_cache(client: TestClient, tmp_path):
    meeting_id, _ = a_support_call(tmp_path / "call")
    speaker = client.get(f"/api/meetings/{meeting_id}", headers=AUTH).json()["speakers"][0]
    audio_url = f"/api/meetings/{meeting_id}/audio?token=test-token"
    sample_url = f"/api/meetings/{meeting_id}/speakers/{speaker['id']}/sample?token=test-token"
    for url in (audio_url, sample_url):
        assert client.get(url).headers["cache-control"] == "no-store"


def test_a_deleted_meetings_words_leave_the_database_files(client: TestClient, tmp_path):
    meeting_id, _ = a_support_call(tmp_path / "call")
    assert b"zebra99" in raw_library_bytes(tmp_path)

    client.delete(f"/api/meetings/{meeting_id}", headers=AUTH)

    assert b"zebra99" not in raw_library_bytes(tmp_path)


def test_settings_never_read_the_watch_folder_itself(client: TestClient, tmp_path, monkeypatch):
    # On a network share that is offline, listing the folder took 42 s, and Settings was
    # loaded by every page; now only the watch's own looks read it.
    from zachcap import watchfolder

    folder = tmp_path / "exports"
    folder.mkdir()
    client.put("/api/settings", headers=AUTH, json={"watch_folder": str(folder)})

    def unreachable(path):
        raise AssertionError("Settings read the folder")

    monkeypatch.setattr(watchfolder, "media_files", unreachable)
    assert client.get("/api/settings", headers=AUTH).json()["watch_folder_failed"] == []
